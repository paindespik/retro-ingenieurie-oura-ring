package io.github.paindespik.ourascan

import android.content.Context
import android.util.Base64
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Push du delta vers serv (POST /ingest/events).
 *
 * Le serveur dédup sur UNIQUE(serial, tag, ring_timestamp, body) : repousser
 * des événements déjà reçus est inoffensif (c'est la v1 du « pushed » tracking).
 */
object Pusher {
    private const val TAG = "OuraPush"
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        // Ne pas suivre les redirections : un portail d'authentification
        // (SSO) placé devant renvoie 302 vers une page de connexion qui
        // répond 200 en HTML — l'envoi paraîtrait réussi alors que rien n'est
        // ingéré. On veut un échec explicite.
        .followRedirects(false)
        .followSslRedirects(false)
        .build()

    /**
     * Envoie **tout** le retard, par lots, en mémorisant un repère de
     * progression (identifiant du dernier événement accepté par le serveur).
     *
     * L'ancienne approche « les N plus récents » laissait définitivement de
     * côté tout ce qui passait sous la limite dès qu'un cycle rattrapait
     * plusieurs heures de retard.
     */
    suspend fun pushPending(ctx: Context, cursor: Long): String {
        val prefs = ctx.getSharedPreferences("oura", Context.MODE_PRIVATE)
        var mark = prefs.getLong("push_mark", 0L)
        var total = 0
        var lots = 0
        while (lots < MAX_LOTS) {
            val batch = runCatching { Core.eventsSince(mark, BATCH) }.getOrDefault("[]")
            val arr = runCatching { JSONArray(batch) }.getOrDefault(JSONArray())
            if (arr.length() == 0) break
            val res = push(ctx, batch, cursor)
            if (!res.startsWith("push OK")) {
                return if (total > 0) "$total événements envoyés puis $res" else res
            }
            mark = arr.getJSONObject(arr.length() - 1).optLong("id", mark)
            prefs.edit().putLong("push_mark", mark).apply()
            total += arr.length()
            lots++
            if (arr.length() < BATCH) break
        }
        return if (total == 0) "rien à envoyer" else "push OK ($total événements, repère $mark)"
    }

    private const val BATCH = 2000
    private const val MAX_LOTS = 40   // 80 000 événements par cycle au maximum

    suspend fun push(ctx: Context, eventsJson: String, cursor: Long): String = withContext(Dispatchers.IO) {
        val payload = JSONObject()
            .put("source", "phone")
            .put("serial", Config.RING_SERIAL)
            .put("cursor", cursor)
            .put("pushed_unix", System.currentTimeMillis() / 1000)
            .put("events", JSONArray(eventsJson))
            .toString()
        val cred = Base64.encodeToString("${Config.WEB_USER}:${Config.WEB_PASS}".toByteArray(), Base64.NO_WRAP)
        val req = Request.Builder()
            .url(Config.SERVER + Config.INGEST_PATH)
            .header("Authorization", "Basic $cred")
            .header("X-Oura-Token", Config.PHONE_TOKEN)
            .post(payload.toRequestBody("application/json".toMediaType()))
            .build()
        var lastErr = ""
        for (attempt in 1..2) {
            try {
                client.newCall(req).execute().use { r ->
                    val body = r.body?.string()?.take(300) ?: ""
                    // Le serveur répond {"inserted":N,...} : l'exiger évite de
                    // prendre une page intermédiaire quelconque pour un succès.
                    if (r.isSuccessful && body.contains("\"inserted\"")) {
                        Log.i(TAG, "push OK: $body")
                        return@withContext "push OK (HTTP ${r.code})"
                    }
                    lastErr = if (r.isSuccessful) {
                        "HTTP ${r.code} mais réponse inattendue (portail d'authentification ?)"
                    } else {
                        "HTTP ${r.code} $body"
                    }
                    Log.w(TAG, "push KO (tentative $attempt): $lastErr")
                }
            } catch (e: Exception) {
                lastErr = e.toString()
                Log.w(TAG, "push exception (tentative $attempt): $lastErr")
            }
            Thread.sleep(5000)
        }
        "push KO ($lastErr)"
    }
}
