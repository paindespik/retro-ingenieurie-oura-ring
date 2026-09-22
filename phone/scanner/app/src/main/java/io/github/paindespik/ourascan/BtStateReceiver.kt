package io.github.paindespik.ourascan

import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager

/**
 * Reprise immédiate à l'allumage du Bluetooth.
 *
 * Sans ça, après une coupure BT il faudrait attendre le prochain créneau
 * périodique (jusqu'à 15 min). Rien n'est perdu entre-temps (l'anneau
 * bufferise plusieurs jours), c'est juste de la fraîcheur gagnée.
 */
class BtStateReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != BluetoothAdapter.ACTION_STATE_CHANGED) return
        if (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1) != BluetoothAdapter.STATE_ON) return
        Log.i("OuraWorker", "Bluetooth rallumé → sync immédiate")
        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "oura-sync-bt-on",
            ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<OuraWorker>().build()
        )
    }
}
