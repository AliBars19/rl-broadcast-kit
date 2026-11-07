package io.github.rlbroadcastkit.phonecam

import android.media.MediaFormat
import java.io.File

/** Owns the chunk router for one service run; a session is one recording from start to stop. */
class Recorder(
    private val dir: File,
    private val cfg: CamConfig,
    private val videoFormat: () -> MediaFormat?,
    private val audioFormat: () -> MediaFormat?,
    private val silentFrame: () -> ByteArray?,
    private val log: (String) -> Unit,
) {
    @Volatile var session: String? = null
        private set
    @Volatile var lastError: String? = null
        private set
    @Volatile private var router: ChunkRouter? = null
    @Volatile private var factory: Mp4ChunkFactory? = null

    val isRecording: Boolean get() = router?.isRecording == true
    val closingChunks: Int get() = factory?.closing?.get() ?: 0
    val chunkIndex: Int get() = router?.chunkIndex ?: 0
    val chunkStartUs: Long get() = router?.currentStartUs ?: 0L

    @Synchronized
    fun start(name: String) {
        if (isRecording) {
            log("recording already running (session $session), ignoring start $name")
            return
        }
        lastError = null
        session = name
        val chunkFactory = Mp4ChunkFactory(dir, name, videoFormat, audioFormat, silentFrame, cfg.minFreeMb * 1_048_576L, log)
        factory = chunkFactory
        router = ChunkRouter(chunkFactory, cfg.chunkSeconds * 1_000_000L) { message ->
            lastError = message
            log("recording error: $message")
        }.also { it.start() }
        log("recording started, session $name, chunks of ${cfg.chunkSeconds} s")
    }

    @Synchronized
    fun stop() {
        router?.stop()
        if (router != null) log("recording stopped, session $session")
        router = null
    }

    /** After stop(): wait (bounded) until the last chunk's index is written. */
    fun awaitFinished(timeoutMs: Long) {
        factory?.awaitIdle(timeoutMs)
    }

    fun recordError(message: String) {
        lastError = message
        log("recording error: $message")
    }

    /** The camera restarted: close the open chunk and let the next keyframe begin a new one. */
    fun cut() {
        router?.cut()
    }

    fun onVideo(sample: Sample) {
        router?.onVideo(sample)
    }

    fun onAudio(sample: Sample) {
        router?.onAudio(sample)
    }
}
