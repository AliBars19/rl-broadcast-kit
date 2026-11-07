package io.github.rlbroadcastkit.phonecam

import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Logcat plus a small rotating text file (phonecam.log in the app files dir) the PC can read. */
class FileLog(private val file: File, private val maxBytes: Long = 512 * 1024) {
    private val format = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    @Synchronized
    fun log(message: String) {
        Log.i(TAG, message)
        try {
            if (file.length() > maxBytes) {
                val old = File(file.path + ".1")
                old.delete()
                file.renameTo(old)
            }
            file.appendText("${format.format(Date())} $message\n")
        } catch (e: java.io.IOException) {
            Log.e(TAG, "cannot write ${file.path}: ${e.message}")
        }
    }

    companion object {
        const val TAG = "phonecam"
    }
}
