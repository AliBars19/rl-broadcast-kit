package io.github.rlbroadcastkit.phonecam

/** One encoded access unit. `ptsUs` is the capture time on the elapsed-realtime clock. */
class Sample(val ptsUs: Long, val key: Boolean, val bytes: ByteArray)

/** One chunk file being written. Sample times passed in are relative to the chunk start. */
interface ChunkSink {
    fun writeVideo(relativePtsUs: Long, sample: Sample)
    fun writeAudio(relativePtsUs: Long, sample: Sample)

    /** Finish and publish the file. May hand the slow part to another thread. */
    fun finish()
}

interface ChunkSinkFactory {
    /** Throws java.io.IOException when the chunk cannot be created (for example the disk is full). */
    fun open(index: Int, startPtsUs: Long): ChunkSink
}

/**
 * Cuts the encoded stream into chunk files on keyframes.
 *
 * A chunk starts on a keyframe and a new one starts at the first keyframe at least
 * `targetUs` after the chunk start. Audio arrives on another thread and a little late,
 * so after a cut the previous chunk stays open until audio passes the cut time (or video
 * runs `graceUs` past it); late audio before the cut still lands in the previous chunk.
 */
class ChunkRouter(
    private val factory: ChunkSinkFactory,
    private val targetUs: Long,
    private val graceUs: Long = 1_000_000L,
    private val onError: (String) -> Unit = {},
) {
    private class Open(val index: Int, val startUs: Long, val sink: ChunkSink)

    private var current: Open? = null
        set(value) {
            field = value
            chunkIndex = value?.index ?: 0
            currentStartUs = value?.startUs ?: 0L
        }
    private var previous: Open? = null
    private var previousCutUs = 0L
    private var nextIndex = 1
    private var recording = false
        set(value) {
            field = value
            isRecording = value
        }

    // Mirrors of the state below, readable without the lock: a stalled flash write must never block a status read.
    @Volatile var chunkIndex: Int = 0
        private set
    @Volatile var isRecording: Boolean = false
        private set
    @Volatile var currentStartUs: Long = 0L
        private set

    @Synchronized
    fun start() {
        if (recording) return
        recording = true
        nextIndex = 1
    }

    /** Close the open chunks but keep recording: the next keyframe opens the next numbered chunk. */
    @Synchronized
    fun cut() {
        finishPrevious()
        current?.let { finish(it) }
        current = null
    }

    @Synchronized
    fun stop() {
        // `recording` goes false last: a status read that sees "not recording" must also see the chunks closing.
        finishPrevious()
        current?.let { finish(it) }
        current = null
        recording = false
    }

    @Synchronized
    fun onVideo(sample: Sample) {
        if (!recording) return
        val open = current
        if (open == null) {
            if (sample.key) openFirst(sample)
        } else {
            if (sample.key && sample.ptsUs - open.startUs >= targetUs) rotate(sample)
            if (previous != null && sample.ptsUs - previousCutUs > graceUs) finishPrevious()
        }
        current?.let { it.sink.writeVideo(sample.ptsUs - it.startUs, sample) }
    }

    @Synchronized
    fun onAudio(sample: Sample) {
        val open = current ?: return
        val old = previous
        if (old != null && sample.ptsUs < open.startUs) {
            old.sink.writeAudio(maxOf(0L, sample.ptsUs - old.startUs), sample)
            return
        }
        if (old != null) finishPrevious()
        if (sample.ptsUs < open.startUs) return
        open.sink.writeAudio(sample.ptsUs - open.startUs, sample)
    }

    private fun openFirst(sample: Sample) {
        current = openChunk(sample.ptsUs) ?: return
    }

    private fun rotate(sample: Sample) {
        val fresh = openChunk(sample.ptsUs)
        if (fresh == null) {
            recording = false
            current?.let { finish(it) }
            current = null
            return
        }
        finishPrevious()
        previous = current
        previousCutUs = sample.ptsUs
        current = fresh
    }

    private fun openChunk(startUs: Long): Open? {
        val index = nextIndex
        return try {
            Open(index, startUs, factory.open(index, startUs)).also { nextIndex++ }
        } catch (e: java.io.IOException) {
            recording = false
            onError("cannot open chunk $index: ${e.message}")
            null
        } catch (e: RuntimeException) {
            // Any failure inside the recording path ends the recording, never the app (the live feed goes on).
            recording = false
            onError("cannot open chunk $index: ${e.javaClass.simpleName}: ${e.message}")
            null
        }
    }

    private fun finishPrevious() {
        previous?.let { finish(it) }
        previous = null
    }

    private fun finish(open: Open) {
        try {
            open.sink.finish()
        } catch (e: RuntimeException) {
            onError("cannot finish chunk ${open.index}: ${e.message}")
        }
    }
}
