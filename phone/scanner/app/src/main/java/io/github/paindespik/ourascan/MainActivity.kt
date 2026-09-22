package io.github.paindespik.ourascan

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.app.NotificationCompat
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * UI minimale : état de la dernière sync + boutons.
 *
 * - « Sync maintenant » : enfile un work one-shot.
 * - « Periodic 15 min » : enregistre le PeriodicWorkRequest unique.
 * - La clé 16 o (hex) doit être posée dans filesDir/ring.key.hex
 *   (adb install puis : run-as io.github.paindespik.ourascan sh -c 'cat > files/ring.key.hex').
 */
class MainActivity : Activity() {

    private lateinit var statusView: TextView
    private lateinit var detailView: TextView
    private val fmt = SimpleDateFormat("dd/MM HH:mm:ss", Locale.FRANCE)
    private val scope = CoroutineScope(Dispatchers.Main)
    private val logBuf = StringBuilder()

    private fun appendLog(msg: String) {
        logBuf.appendLine(msg)
        refreshStatus()
    }

    @SuppressLint("MissingPermission")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        statusView = TextView(this)
        detailView = TextView(this).apply {
            setPadding(0, 16, 0, 16)
            setTextIsSelectable(true)
        }
        val btnNow = Button(this).apply {
            text = "Sync maintenant"
            setOnClickListener {
                WorkManager.getInstance(this@MainActivity)
                    .enqueueUniqueWork(
                        "oura-sync-now",
                        androidx.work.ExistingWorkPolicy.REPLACE,
                        OneTimeWorkRequestBuilder<OuraWorker>().build()
                    )
                refreshStatus()
            }
            setOnLongClickListener {
                // test du pipeline push (HTTPS + Basic + token) sans sync :
                // pousse un event factice (tag 97, nettoyable côté serveur)
                val now = System.currentTimeMillis() / 1000
                val ev = "[{\"tag\":97,\"name\":\"debug_data\",\"ring_timestamp\":999999,\"body_hex\":\"7b7d\",\"decoded_json\":{\"push_test\":true},\"captured_unix\":" + now + "}]"
                scope.launch {
                    val r = Pusher.push(applicationContext, ev, 999)
                    appendLog("push test : $r")
                    refreshStatus()
                }
                true
            }
        }
        val btnStop = Button(this).apply {
            text = "Stop (annuler tous les cycles)"
            setOnClickListener {
                WorkManager.getInstance(this@MainActivity).cancelAllWork()
                appendLog("tous les cycles annulés")
            }
        }
        val btnCeremony = Button(this).apply {
            text = "Bascule (factory-reset → clé → sync)"
            setOnClickListener {
                val req = OneTimeWorkRequestBuilder<OuraWorker>()
                    .setInputData(androidx.work.Data.Builder().putInt("ceremony", 1).build())
                    .build()
                WorkManager.getInstance(this@MainActivity)
                    .enqueueUniqueWork("oura-ceremony", ExistingWorkPolicy.REPLACE, req)
                refreshStatus()
            }
        }
        val btnPeriodic = Button(this).apply {
            text = "Enregistrer le periodic 15 min"
            setOnClickListener {
                val req = PeriodicWorkRequestBuilder<OuraWorker>(Config.WORK_PERIOD_MIN, TimeUnit.MINUTES)
                    .build()
                WorkManager.getInstance(this@MainActivity)
                    .enqueueUniquePeriodicWork(Config.WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, req)
                refreshStatus()
            }
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 32, 32, 32)
            addView(statusView)
            addView(detailView)
            addView(btnNow)
            addView(btnCeremony)
            addView(btnPeriodic)
            addView(btnStop)
        }
        setContentView(ScrollView(this).apply { addView(root) })

        ensurePermissions()
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun ensurePermissions() {
        val needed = mutableListOf<String>()
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED)
            needed.add(Manifest.permission.BLUETOOTH_SCAN)
        if (checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED)
            needed.add(Manifest.permission.BLUETOOTH_CONNECT)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty()) requestPermissions(needed.toTypedArray(), 1)
    }

    private fun refreshStatus() {
        val prefs = getSharedPreferences("oura", MODE_PRIVATE)
        val state = prefs.getString("last_state", "jamais") ?: "jamais"
        val detail = prefs.getString("last_detail", "") ?: ""
        val push = prefs.getString("last_push", "") ?: ""
        val unix = prefs.getLong("last_unix", 0)
        val whenTxt = if (unix > 0) " — ${fmt.format(Date(unix * 1000))}" else ""
        statusView.text = "Dernière sync : $state$whenTxt"
        val keyFile = java.io.File(filesDir, Config.KEY_FILE)
        val keyTxt = if (keyFile.exists()) "présente (${keyFile.length()} o)" else "MANQUANTE (à poser via adb)"
        val dbFile = java.io.File(filesDir, Config.DB_FILE)
        val dbTxt = if (dbFile.exists()) "${dbFile.length()} o" else "pas encore créée"
        detailView.text = buildString {
            if (logBuf.isNotEmpty()) {
                append(logBuf)
                appendLine()
            }
            appendLine("détail : $detail")
            if (push.isNotBlank()) appendLine("push   : $push")
            appendLine("clé    : $keyTxt")
            appendLine("db     : $dbTxt")
        }
    }
}
