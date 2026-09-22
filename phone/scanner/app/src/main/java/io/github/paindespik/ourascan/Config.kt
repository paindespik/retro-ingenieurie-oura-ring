package io.github.paindespik.ourascan

/**
 * Constantes de déploiement.
 *
 * WEB_USER / WEB_PASS = Basic Auth nginx existant de oura.example.com
 * (identique à ce que le navigateur envoie) — à renseigner au déploiement.
 * PHONE_TOKEN = X-Oura-Token (identifié la source « téléphone » côté serveur).
 */
object Config {
    const val SERVER = "https://oura.example.com"
    const val INGEST_PATH = "/ingest/events"

    const val WEB_USER = "oura"
    const val WEB_PASS = "MOT_DE_PASSE_RETIRE"
    const val PHONE_TOKEN = "TOKEN_RETIRE"

    const val RING_SERIAL = "RING_SERIAL"

    /** Période WorkManager (min) — minimum Android = 15. */
    const val WORK_PERIOD_MIN = 15L
    const val WORK_NAME = "oura-sync-15min"

    /** Fichier (hex de la clé 16 o) posé via adb run-as dans le dossier privé. */
    const val KEY_FILE = "ring.key.hex"
    const val DB_FILE = "oura.db"
}
