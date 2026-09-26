package io.github.paindespik.ourascan

/**
 * Constantes de déploiement.
 *
 * Les secrets ne sont PAS dans le code : ils viennent de
 * `phone/scanner/secrets.properties` (gitignoré) ou de l'environnement, et
 * sont injectés dans `BuildConfig` par Gradle. Voir `secrets.properties.example`.
 */
object Config {
    /** Vhost du portail (Basic Auth nginx + TLS Let's Encrypt). */
    val SERVER: String = BuildConfig.OURA_SERVER
    const val INGEST_PATH = "/ingest/events"

    /** Basic Auth du portail. */
    val WEB_USER: String = BuildConfig.WEB_USER
    val WEB_PASS: String = BuildConfig.WEB_PASS

    /** En-tête X-Oura-Token : identifie la source côté serveur. */
    val PHONE_TOKEN: String = BuildConfig.PHONE_TOKEN

    val RING_SERIAL: String = BuildConfig.RING_SERIAL

    /** `true` si le build a bien reçu les secrets (sinon : push impossible). */
    val configured: Boolean
        get() = WEB_PASS.isNotBlank() && PHONE_TOKEN.isNotBlank()

    /** Période WorkManager (min) — minimum Android = 15. */
    const val WORK_PERIOD_MIN = 15L
    const val WORK_NAME = "oura-sync-15min"

    /** Fichier (hex de la clé 16 o) posé via adb run-as dans le dossier privé. */
    const val KEY_FILE = "ring.key.hex"

    /** Journal de chaque paquet BLE (← notif / → write) : diagnostic uniquement,
     *  des milliers de lignes par cycle sinon. */
    const val TRACE_PACKETS = false
    const val DB_FILE = "oura.db"
}
