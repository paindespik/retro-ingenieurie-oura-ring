package io.github.paindespik.ourascan

/**
 * Wrapper JNI de `liboura_phone_core.so` (noyau Rust : oura-protocol + oura-link + oura-store).
 *
 * Cycle d'utilisation (voir OuraWorker) :
 *   init(clé hex, chemin db) → startSync() → boucle [nextWrite() → write BLE] +
 *   [notification BLE → feed()] → status() → recentEvents() → drop()
 */
object Core {
    var loaded: Boolean = false
        private set
    var error: String = ""
        private set
    private var ptr: Long = 0L

    fun isReady() = loaded && ptr != 0L

    fun init(keyHex: String, dbPath: String): Boolean {
        if (loaded) return true
        return try {
            System.loadLibrary("oura_phone_core")
            val p = nativeCreate(keyHex.toByteArray(), dbPath.toByteArray())
            if (p == 0L) {
                error = "core_create null (voir logcat : oura_core_create)"
                false
            } else {
                ptr = p
                loaded = true
                true
            }
        } catch (t: Throwable) {
            error = t.toString()
            false
        }
    }

    fun startSync() = nativeStartSync(ptr)

    fun feed(data: ByteArray) = nativeFeed(ptr, data)

    fun nextWrite(): ByteArray? = nativeNextWrite(ptr)

    fun status(): String = nativeStatus(ptr)

    fun recentEvents(limit: Int): String = nativeRecentEvents(ptr, limit)

    fun drop() {
        if (ptr != 0L) {
            nativeDrop(ptr)
            ptr = 0L
            loaded = false
        }
    }

    private external fun nativeCreate(keyHex: ByteArray, dbPath: ByteArray): Long
    private external fun nativeStartSync(p: Long)
    private external fun nativeFeed(p: Long, data: ByteArray)
    private external fun nativeNextWrite(p: Long): ByteArray?
    private external fun nativeStatus(p: Long): String
    private external fun nativeRecentEvents(p: Long, limit: Int): String
    private external fun nativeDrop(p: Long)
}
