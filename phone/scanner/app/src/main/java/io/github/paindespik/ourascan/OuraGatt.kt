package io.github.paindespik.ourascan

import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log

/**
 * Connexion GATT à l'anneau + pompage du core Rust.
 *
 * Séquence : connect → discoverServices → requestMtu → CCC notify →
 * core.startSync() → boucle :
 *   core.nextWrite() → write BLE (WITH_RESPONSE)
 *   notification BLE → core.feed(bytes)
 * Le core (machine à états Rust) décide de la fin : status "done" / "error".
 *
 * Toutes les callbacks GATT arrivent sur [handler] (HandlerThread dédiée) ;
 * le pompage y est donc sérialisé.
 */
class OuraGatt(
    private val ctx: Context,
    private val onResult: (state: String, detail: String) -> Unit
) {
    private val thread = HandlerThread("oura-gatt").also { it.start() }
    private val handler = Handler(thread.looper)
    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null
    private var notifyChar: BluetoothGattCharacteristic? = null
    private var inFlight = false
    private var finished = false
    private var mtuDone = false
    private var cccWritten = false
    private var idleSince = 0L
    private val totalDeadline = SystemClock.elapsedRealtime() + TOTAL_TIMEOUT_MS

    fun connect(addr: String) {
        handler.post { doConnect(addr) }
    }

    private fun doConnect(addr: String) {
        val adapter = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        val dev = adapter?.getRemoteDevice(addr)
            ?: return finish("error", "BT adapter indisponible")
        Log.i(TAG, "connectGatt $addr")
        gatt = dev.connectGatt(ctx, false, cb, BluetoothDevice.TRANSPORT_LE)
        idleSince = SystemClock.elapsedRealtime()
    }

    private fun finish(state: String, detail: String) {
        if (finished) return
        finished = true
        Log.i(TAG, "FIN $state — $detail")
        onResult(state, detail)
    }

    private val cb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    Log.i(TAG, "GATT connecté → discoverServices")
                    g.discoverServices()
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    // si le core n'a pas déjà conclu, c'est une perte de lien
                    val st = runCatching { Core.status() }.getOrDefault("")
                    if (!st.contains("\"done\"") && !st.contains("\"error\"")) {
                        finish("error", "BLE déconnecté (status=$status)")
                    }
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                return finish("error", "service discovery status=$status")
            }
            val svc = g.getService(OURA_SVC)
                ?: return finish("error", "service Oura introuvable")
            val w = svc.getCharacteristic(OURA_WRITE)
                ?: return finish("error", "caractéristique write introuvable")
            val n = svc.getCharacteristic(OURA_NOTIFY)
                ?: return finish("error", "caractéristique notify introuvable")
            writeChar = w
            notifyChar = n
            Log.i(TAG, "services OK → MTU 512")
            g.requestMtu(512)
            // fallback : si onMtuChanged ne vient pas (certaines stacks), on avance quand même
            handler.postDelayed({
                if (!mtuDone && !finished) {
                    Log.w(TAG, "timeout MTU → on avance avec le MTU actuel")
                    enableNotifications()
                }
            }, 3000)
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            if (mtuDone) return
            mtuDone = true
            Log.i(TAG, "MTU=$mtu status=$status")
            enableNotifications()
        }

        override fun onDescriptorWrite(g: BluetoothGatt, desc: BluetoothGattDescriptor, status: Int) {
            if (cccWritten) return
            cccWritten = true
            if (status != BluetoothGatt.GATT_SUCCESS) {
                return finish("error", "write CCC status=$status")
            }
            Log.i(TAG, "notifications actives → core.startSync() + pump")
            idleSince = SystemClock.elapsedRealtime()
            Core.startSync()
            pump()
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, ch: BluetoothGattCharacteristic, status: Int) {
            inFlight = false
            idleSince = SystemClock.elapsedRealtime()
            if (status == BluetoothGatt.GATT_SUCCESS) pump()
            else finish("error", "write GATT status=$status")
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, ch: BluetoothGattCharacteristic) {
            val v = ch.value ?: return
            Core.feed(v)
            idleSince = SystemClock.elapsedRealtime()
            pump()
        }
    }

    private fun enableNotifications() {
        val g = gatt ?: return
        val n = notifyChar ?: return
        runCatching { g.setCharacteristicNotification(n, true) }
        val ccc = n.getDescriptor(CCC_UUID)
        if (ccc == null) return finish("error", "descripteur CCC introuvable")
        ccc.value = 0x0100.toByteArray() // ENABLE_NOTIFICATION
        g.writeDescriptor(ccc)
    }

    private fun pump() {
        if (finished || inFlight) return
        handler.post {
            if (finished || inFlight) return@post
            val pkt = Core.nextWrite()
            if (pkt != null) {
                inFlight = true
                writeChar!!.value = pkt
                gatt!!.writeCharacteristic(writeChar!!)
                return@post
            }
            // file vide : le core attend une notification (ou a conclu)
            val st = Core.status()
            when {
                st.contains("\"done\"") -> finish("done", detailOf(st))
                st.contains("\"error\"") -> finish("error", detailOf(st))
                SystemClock.elapsedRealtime() > totalDeadline ->
                    finish("error", "timeout global ${TOTAL_TIMEOUT_MS / 1000 / 60} min")
                SystemClock.elapsedRealtime() - idleSince > IDLE_TIMEOUT_MS ->
                    finish("error", "anneau silencieux ${IDLE_TIMEOUT_MS / 1000} s")
            }
        }
    }

    private fun detailOf(statusJson: String): String {
        val i = statusJson.indexOf("\"detail\":\"")
        if (i < 0) return statusJson
        val start = i + 10
        val j = statusJson.indexOf("\",", start)
        return statusJson.substring(start, if (j > 0) j else statusJson.length - 2)
    }

    fun close() {
        runCatching { gatt?.disconnect() }
        runCatching { gatt?.close() }
        thread.quitSafely()
    }

    private fun Int.toByteArray(): ByteArray = byteArrayOf((this and 0xFF).toByte(), ((this shr 8) and 0xFF).toByte())

    companion object {
        private const val TAG = "OuraSync"
        private const val TOTAL_TIMEOUT_MS = 15 * 60 * 1000L
        private const val IDLE_TIMEOUT_MS = 90 * 1000L
        private val OURA_SVC = java.util.UUID.fromString("98ed0001-a541-11e4-b6a0-0002a5d5c51b")
        private val OURA_WRITE = java.util.UUID.fromString("98ed0002-a541-11e4-b6a0-0002a5d5c51b")
        private val OURA_NOTIFY = java.util.UUID.fromString("98ed0003-a541-11e4-b6a0-0002a5d5c51b")
        private val CCC_UUID = java.util.UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }
}
