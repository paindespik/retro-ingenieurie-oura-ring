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
        .build()

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
                    if (r.isSuccessful) {
                        Log.i(TAG, "push OK: $body")
                        return@withContext "push OK (HTTP ${r.code})"
                    }
                    lastErr = "HTTP ${r.code} $body"
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
