package io.github.paindespik.ourascan

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.util.Log
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager

/**
 * Battement de cœur qui survit au Doze.
 *
 * Pourquoi : en Doze profond (téléphone immobile, débranché, écran éteint),
 * Android diffère les jobs WorkManager vers des fenêtres de maintenance
 * espacées de 1 à 6 h. L'exemption d'optimisation de batterie couvre le réseau
 * et les wakelocks, pas ce report.
 *
 * `setAndAllowWhileIdle` est en revanche délivrée même en Doze (le système la
 * limite à ~1 déclenchement / 9 min par app, ce qui convient pour 15 min) et
 * ne demande aucune permission spéciale, contrairement aux alarmes exactes.
 *
 * Le travail est ensuite enfilé en *expedited* pour ne pas retomber dans le
 * report des jobs ordinaires.
 */
object Heartbeat {
    const val PERIOD_MS = 15 * 60 * 1000L
    private const val REQ = 4242

    fun schedule(ctx: Context, delayMs: Long = PERIOD_MS) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pi = PendingIntent.getBroadcast(
            ctx, REQ, Intent(ctx, HeartbeatReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        am.setAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + delayMs,
            pi
        )
        Log.i(TAG, "battement programmé dans ${delayMs / 1000} s")
    }

    const val TAG = "OuraWorker"
}

class HeartbeatReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        Log.i(Heartbeat.TAG, "battement → cycle + réarmement")
        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "oura-sync-heartbeat",
            ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<OuraWorker>()
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .build()
        )
        Heartbeat.schedule(ctx)
    }
}

/** Réarme le battement après un redémarrage (WorkManager se replanifie seul). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        Log.i(Heartbeat.TAG, "boot → réarmement du battement")
        Heartbeat.schedule(ctx, 60_000)
    }
}
