package io.github.rlbroadcastkit.phonecam

import org.json.JSONObject
import java.io.File
import java.io.IOException

/** Writes status.json atomically (temp file, then rename) so the PC never reads half a file. */
class StatusWriter(private val file: File, private val log: (String) -> Unit) {
    private val temp = File(file.path + ".tmp")

    fun write(fields: Map<String, Any?>) {
        try {
            temp.writeText(JSONObject(fields).toString())
            if (!temp.renameTo(file)) throw IOException("rename failed")
        } catch (e: IOException) {
            log("cannot write ${file.name}: ${e.message}")
        }
    }

    fun delete() {
        file.delete()
    }
}
