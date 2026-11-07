package io.github.rlbroadcastkit.phonecam

import java.io.File
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicInteger

/**
 * Names of .part files this process is still writing or closing. A killed run leaves orphans behind, but a
 * *finishing* chunk of the previous service run in this same process (shutdown tail longer than the wait) must
 * not be mistaken for one, so [OrphanRescue] skips these.
 */
object ActiveParts {
    val names: MutableSet<String> = java.util.concurrent.ConcurrentHashMap.newKeySet()
}

/** The few muxer calls the chunk sink needs, so the failure handling can be tested without Android. */
interface MuxerPort {
    fun write(track: Int, ptsUs: Long, key: Boolean, bytes: ByteArray)
    fun stop()
    fun release()
}

/**
 * One chunk file. The muxer writes NAME.mp4.part; on a clean finish it becomes NAME.mp4, and if the muxer
 * could not be stopped cleanly it becomes NAME.mp4.broken. A sample that could not be written is lost but
 * does not make the chunk broken (the file is still a valid MP4 as long as stop() works; the loss is logged).
 * A chunk is never deleted here: whatever was recorded is evidence
 * the PC can still pull (and sometimes repair). Unfinished files of a killed run are handled at start-up
 * by [OrphanRescue].
 *
 * An audio track with no samples makes MediaMuxer.stop() fail, losing the whole file, so a silent AAC
 * frame is written to an empty audio track before stopping (the microphone may have died mid-stream).
 */
class ChunkFileSink(
    private val port: MuxerPort,
    private val videoTrack: Int,
    private val audioTrack: Int,
    private val part: File,
    private val target: File,
    private val broken: File,
    private val silentFrame: () -> ByteArray?,
    private val finisher: Executor,
    private val closing: AtomicInteger,
    private val log: (String) -> Unit,
) : ChunkSink {
    init {
        ActiveParts.names.add(part.name)
    }

    private var failed: String? = null
    private var lostSamples = 0
    private var audioSamples = 0

    override fun writeVideo(relativePtsUs: Long, sample: Sample) {
        write(videoTrack, relativePtsUs, sample)
    }

    override fun writeAudio(relativePtsUs: Long, sample: Sample) {
        if (audioTrack < 0) return
        if (write(audioTrack, relativePtsUs, sample)) audioSamples++
    }

    private fun write(track: Int, ptsUs: Long, sample: Sample): Boolean {
        return try {
            port.write(track, ptsUs, sample.key, sample.bytes)
            true
        } catch (e: RuntimeException) {
            if (lostSamples++ == 0) log("${part.name}: write failed, samples are being lost: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    override fun finish() {
        closing.incrementAndGet()
        try {
            finisher.execute {
                try {
                    close()
                } finally {
                    closing.decrementAndGet()
                }
            }
        } catch (e: RuntimeException) {
            closing.decrementAndGet()
            ActiveParts.names.remove(part.name)
            throw e
        }
    }

    private fun close() {
        try {
            fillEmptyAudioTrack()
            try {
                port.stop()
            } catch (e: RuntimeException) {
                failed = failed ?: "stop failed: ${e.javaClass.simpleName}: ${e.message}"
            }
            try {
                port.release()
            } catch (e: RuntimeException) {
                log("${part.name}: release failed: ${e.message}")
            }
            publish()
        } finally {
            ActiveParts.names.remove(part.name)  // however the close ended, the name must not stay registered
        }
    }

    private fun fillEmptyAudioTrack() {
        if (audioTrack < 0 || audioSamples > 0) return
        val silence = silentFrame()
        if (silence == null) {
            log("${part.name}: audio track is empty and no silent frame is available")
            return
        }
        try {
            port.write(audioTrack, 0, false, silence)
            log("${part.name}: no audio was recorded, wrote one silent frame so the file stays valid")
        } catch (e: RuntimeException) {
            log("${part.name}: silent frame failed: ${e.message}")
        }
    }

    private fun publish() {
        val problem = failed
        val destination = if (problem == null) target else broken
        if (part.renameTo(destination)) {
            val note = if (lostSamples > 0) " ($lostSamples samples could not be written)" else ""
            log(if (problem == null) "chunk done: ${destination.name} ${destination.length()} bytes$note"
                else "chunk kept as ${destination.name} ($problem)$note")
        } else {
            log("${part.name}: cannot rename to ${destination.name}; it stays as .part and is rescued at the next start")
        }
    }
}
