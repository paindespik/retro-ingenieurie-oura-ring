package io.github.paindespik.ourascan

import android.app.NotificationChannel
import android.app.NotificationManager
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters

import kotlinx.coroutines.suspendCancellableCoroutine
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * Un cycle de sync complet (WorkManager, ~15 min) :
 *   clé + core → scan anneau → GATT + sync (core Rust) → push delta → notification.
 *
 * En cas d'échec (BT off, anneau absent, lien perdu…) → Result.retry()
 * (WorkManager réessaie avec backoff — aucune perte : l'anneau bufferise).
 */
class OuraWorker(ctx: Context, private val params: WorkerParameters) : CoroutineWorker(ctx, params) {
    // Un seul cycle à la fois : le Core natif est un singleton (une seule
    // machine à états / une seule file d'écriture). Deux workers simultanés
    // (periodic + one-shot + cérémonie) corrompraient le flux.
    private object Lock {
        val running = java.util.concurrent.atomic.AtomicBoolean(false)
    }

    override suspend fun doWork(): Result {
        val appCtx = applicationContext
        val ceremony = params.inputData.getInt("ceremony", 0) == 1
        if (!Lock.running.compareAndSet(false, true)) {
            log("un cycle est déjà en cours → abandon de celui-ci")
            return Result.retry()
        }
        try {
            return runCycle(appCtx, ceremony)
        } finally {
            Lock.running.set(false)
        }
    }

    private suspend fun runCycle(appCtx: Context, ceremony: Boolean): Result {
        log("doWork — début${if (ceremony) " [CÉRÉMONIE]" else ""}")

        val adapter = (appCtx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            log("Bluetooth éteint → retry")
            return Result.retry()
        }

        // 1. clé + core
        val keyFile = File(appCtx.filesDir, Config.KEY_FILE)
        if (!keyFile.exists()) {
            log("clé $CONFIG_KEY introuvable (à poser via adb) → retry".replace(CONFIG_KEY, Config.KEY_FILE))
            return Result.retry()
        }
        val keyHex = keyFile.readText().trim()
        val dbPath = File(appCtx.filesDir, Config.DB_FILE).absolutePath
        if (!Core.init(keyHex, dbPath)) {
            log("core init échoué : ${Core.error} → retry")
            return Result.retry()
        }

        // 2. scan de l'anneau (≤ 25 s)
        val addr = scanForOura(adapter, 25_000)
        if (addr == null) {
            log("aucune annonce Oura en 25 s → retry")
            return Result.retry()
        }
        log("annonce Oura vue : $addr")
        notify(
            if (ceremony) "Oura : bascule en cours…" else "Oura : sync en cours…",
            "connexion à l'anneau ($addr)" + if (ceremony) " + bond" else ""
        )

        // 3. GATT + (ceremony | sync) (core Rust)
        var state = "error"
        var detail = ""
        val latch = CountDownLatch(1)
        val gatt = OuraGatt(appCtx, ceremony) { s, d ->
            state = s
            detail = d
            latch.countDown()
        }
        gatt.connect(addr)
        // trace de la machine à états pendant l'attente (diagnostic)
        val deadline = System.currentTimeMillis() + TOTAL_TIMEOUT_MIN * 60_000
        while (!latch.await(2, TimeUnit.SECONDS)) {
            log("… ${runCatching { Core.status() }.getOrDefault("<KO>")}")
            if (System.currentTimeMillis() > deadline) {
                detail = "timeout worker $TOTAL_TIMEOUT_MIN min"
                break
            }
        }
        gatt.close()
        log("sync terminé : $state — $detail")

        // 4. push delta (si OK)
        var pushDetail = ""
        if (state == "done") {
            val cursor = cursorFromStatus()
            val events = Core.recentEvents(PUSH_LIMIT)
            log("push de ${countEvents(events)} événement(s), curseur $cursor")
            pushDetail = Pusher.push(appCtx, events, cursor)
        }

        // 5. persistance + notification
        appCtx.getSharedPreferences("oura", Context.MODE_PRIVATE).edit()
            .putString("last_state", state)
            .putString("last_detail", detail)
            .putString("last_push", pushDetail)
            .putLong("last_unix", System.currentTimeMillis() / 1000)
            .apply()
        val emoji = if (state == "done") "✅" else "⚠️"
        val titre = if (ceremony) "Oura : $emoji bascule $state" else "Oura : $emoji $state"
        notify(titre, "$detail ${pushDetail}".trim())
        return if (state == "done") Result.success() else Result.retry()
    }

    private suspend fun scanForOura(adapter: BluetoothAdapter, timeoutMs: Long): String? {
        return kotlinx.coroutines.withTimeoutOrNull(timeoutMs) {
            suspendCancellableCoroutine { cont ->
                val scanner = adapter.bluetoothLeScanner
                var resumed = false
                val cb = object : ScanCallback() {
                    override fun onScanResult(callbackType: Int, result: ScanResult) {
                        val name = result.scanRecord?.deviceName ?: ""
                        val hasOuraSvc = result.scanRecord?.serviceUuids?.any {
                            it.uuid.toString().equals(OURA_UUID, ignoreCase = true)
                        } == true
                        if ((hasOuraSvc || name.contains("oura", ignoreCase = true)) && !resumed) {
                            resumed = true
                            runCatching { scanner.stopScan(this) }
                            cont.resume(result.device.address)
                        }
                    }
                }
                runCatching { scanner.startScan(cb) }
                cont.invokeOnCancellation { runCatching { scanner.stopScan(cb) } }
            }
        }
    }

    private fun cursorFromStatus(): Long {
        val s = runCatching { Core.status() }.getOrDefault("")
        val i = s.indexOf("\"cursor\":")
        if (i < 0) return 0
        val j = s.indexOf(",", i)
        return s.substring(i + 9, if (j > 0) j else s.length - 1).trim().toLongOrNull() ?: 0
    }

    private fun countEvents(eventsJson: String): Int {
        var n = 0
        var i = 0
        while (true) {
            i = eventsJson.indexOf("ring_timestamp", i)
            if (i < 0) break
            n++
            i += 1
        }
        return n
    }

    private fun notify(title: String, text: String) {
        val nm = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val ch = NotificationChannel(CH_ID, "Sync Oura", NotificationManager.IMPORTANCE_LOW)
        nm.createNotificationChannel(ch)
        val n = NotificationCompat.Builder(applicationContext, CH_ID)
            .setSmallIcon(android.R.drawable.ic_menu_upload)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .build()
        runCatching { nm.notify(NOTIF_ID, n) }
    }

    private fun log(msg: String) = Log.i(TAG, msg)

    companion object {
        private const val TAG = "OuraWorker"
        private const val CH_ID = "oura-sync"
        private const val NOTIF_ID = 42
        /** minutes : une première sync complète (drain du buffer) peut être longue. */
        private const val TOTAL_TIMEOUT_MIN = 15L
        /** Delta re-poussé à chaque cycle (le serveur dédup) — large marge
         *  pour rattraper un premier drain ou des pushes manqués. */
        private const val PUSH_LIMIT = 5000
        private const val OURA_UUID = "98ed0001-a541-11e4-b6a0-0002a5d5c51b"
        private const val CONFIG_KEY = "KEY"
    }
}
