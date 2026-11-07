package io.github.rlbroadcastkit.phonecam

import java.io.File

/**
 * A .mp4.part file found at start-up belongs to a run that was killed (its MP4 index was never written).
 * It is renamed to NAME.mp4.orphan so the PC offloads it like any chunk; nothing is deleted on the phone
 * without a verified copy on the PC. Files named in [active] belong to a chunk this process is still closing and
 * are left alone.
 */
object OrphanRescue {
    fun rescue(dir: File, active: Set<String> = emptySet(), log: (String) -> Unit): Int {
        val parts = dir.listFiles { f -> f.name.endsWith(".mp4.part") } ?: return 0
        var renamed = 0
        for (part in parts) {
            if (part.name in active) {
                log("${part.name} is still being closed by this process, not an orphan")
                continue
            }
            val target = freeName(dir, part.name.removeSuffix(".part") + ".orphan")
            if (part.renameTo(target)) {
                renamed++
                log("unfinished chunk from an earlier run kept as ${target.name} (${target.length()} bytes)")
            } else {
                log("cannot rename ${part.name} to ${target.name}")
            }
        }
        return renamed
    }

    private fun freeName(dir: File, wanted: String): File {
        var candidate = File(dir, wanted)
        var n = 1
        while (candidate.exists()) candidate = File(dir, "$wanted.$n").also { n++ }
        return candidate
    }
}
