package io.github.rlbroadcastkit.phonecam

import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaMuxer
import android.os.StatFs
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Writes chunks as MP4 files named cam-SESSION-NNNN-EPOCHMS.mp4 (see [ChunkFileSink] for the .part,
 * .mp4 and .broken life cycle).
 */
class Mp4ChunkFactory(
    private val dir: File,
    private val session: String,
    private val videoFormat: () -> MediaFormat?,
    private val audioFormat: () -> MediaFormat?,
    private val silentFrame: () -> ByteArray?,
    private val minFreeBytes: Long,
    private val log: (String) -> Unit,
) : ChunkSinkFactory {
    private val finisher: ExecutorService = Executors.newSingleThreadExecutor { Thread(it, "chunk-finisher") }

    /** Chunks being closed right now (moov box and rename pending). Shown in the status so the PC does not
     *  announce "recording ready" while the last chunk is still being finalized. */
    val closing = AtomicInteger()

    override fun open(index: Int, startPtsUs: Long): ChunkSink {
        val video = videoFormat() ?: throw IOException("video format not ready")
        dir.mkdirs()
        val free = try {
            StatFs(dir.path).availableBytes
        } catch (e: RuntimeException) {
            throw IOException("cannot read free space of ${dir.path}: ${e.message}", e)
        }
        if (free < minFreeBytes) throw IOException("only ${free / 1_048_576} MB free, need ${minFreeBytes / 1_048_576}")
        val name = "cam-%s-%04d-%d".format(session, index, Clock.epochMsOf(startPtsUs))
        val part = File(dir, "$name.mp4.part")
        val muxer = try {
            MediaMuxer(part.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        } catch (e: Exception) {
            throw IOException("cannot create ${part.name}: ${e.message}", e)
        }
        try {
            val videoTrack = muxer.addTrack(video)
            val audio = audioFormat()
            val audioTrack = if (audio != null) muxer.addTrack(audio) else -1
            muxer.start()
            log("chunk $index open: $name")
            return ChunkFileSink(
                MediaMuxerPort(muxer), videoTrack, audioTrack, part, File(dir, "$name.mp4"),
                File(dir, "$name.mp4.broken"), silentFrame, finisher, closing, log,
            )
        } catch (e: RuntimeException) {
            runCatching { muxer.release() }
            part.delete()  // nothing was written yet, so nothing is lost
            throw IOException("cannot start ${part.name}: ${e.javaClass.simpleName}: ${e.message}", e)
        }
    }

    /** Waits (bounded) for chunks still being closed; used when the service shuts down. */
    fun awaitIdle(timeoutMs: Long) {
        finisher.shutdown()
        finisher.awaitTermination(timeoutMs, TimeUnit.MILLISECONDS)
    }
}

private class MediaMuxerPort(private val muxer: MediaMuxer) : MuxerPort {
    private val info = MediaCodec.BufferInfo()

    override fun write(track: Int, ptsUs: Long, key: Boolean, bytes: ByteArray) {
        info.set(0, bytes.size, ptsUs, if (key) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
        muxer.writeSampleData(track, ByteBuffer.wrap(bytes), info)
    }

    override fun stop() = muxer.stop()

    override fun release() = muxer.release()
}
