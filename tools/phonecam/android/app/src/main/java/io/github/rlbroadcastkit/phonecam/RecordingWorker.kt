package io.github.rlbroadcastkit.phonecam

import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * Runs every recording action (open chunk, write samples, rotate, stop) on its own thread, behind a
 * bounded queue. The camera thread only calls submit*, which never blocks: when flash stalls and the
 * queue is full the recording sample is dropped and counted, and the live feed carries on.
 * Control actions (start, stop, cut) use a separate unbounded queue and are never dropped.
 */
class RecordingWorker(
    capacity: Int,
    private val onVideo: (Sample) -> Unit,
    private val onAudio: (Sample) -> Unit,
    private val onError: (String) -> Unit,
) {
    private sealed class Item {
        class Video(val sample: Sample) : Item()
        class Audio(val sample: Sample) : Item()
    }

    val dropped = AtomicLong()
    private val samples = ArrayBlockingQueue<Item>(capacity)
    private val control = LinkedBlockingQueue<() -> Unit>()
    @Volatile private var running = true
    private companion object {
        const val MAX_SAMPLES_BEFORE_CONTROL = 50
    }
    private val thread = Thread({ loop() }, "recording")

    fun start() = thread.start()

    /** After a drop, P-frames are skipped until the next keyframe so the chunk stays decodable. */
    @Volatile private var waitingForKey = false

    fun submitVideo(sample: Sample): Boolean {
        if (waitingForKey) {
            if (!sample.key) {
                dropped.incrementAndGet()
                return false
            }
            waitingForKey = false
        }
        val accepted = offer(Item.Video(sample))
        if (!accepted) waitingForKey = true
        return accepted
    }

    fun submitAudio(sample: Sample): Boolean = offer(Item.Audio(sample))

    private fun offer(item: Item): Boolean {
        if (samples.offer(item)) return true
        dropped.incrementAndGet()
        return false
    }

    /**
     * Queue an action. It runs on the recording thread after the samples already queued, except that under
     * sustained overload at most [MAX_SAMPLES_BEFORE_CONTROL] samples are handled first, so a stop or a start is
     * never starved by a full queue.
     */
    fun post(action: () -> Unit) {
        control.add(action)
    }

    /**
     * Queue a stop-like action: every sample queued before it is handled first, however many (a full queue after
     * a flash stall must not lose its samples just because the chunk is closed). Use [post] for start and cut,
     * which are subject to the starvation cap.
     */
    fun postDraining(action: () -> Unit) {
        control.add {
            drainSamples()
            action()
        }
    }

    /** Runs what is queued, then ends the thread. Waits up to [timeoutMs]. */
    fun stop(timeoutMs: Long) {
        control.add {
            drainSamples()
            running = false
        }
        thread.join(timeoutMs)
    }

    private fun drainSamples() {
        while (true) {
            val item = samples.poll() ?: return
            handle(item)
        }
    }

    private fun handle(item: Item) = guarded {
        when (item) {
            is Item.Video -> onVideo(item.sample)
            is Item.Audio -> onAudio(item.sample)
        }
    }

    private fun loop() {
        var streak = 0
        while (running) {
            // Samples first (a stop queued after them must not discard them), but never more than
            // MAX_SAMPLES_BEFORE_CONTROL in a row while a control action is waiting.
            val mustServeControl = streak >= MAX_SAMPLES_BEFORE_CONTROL && control.isNotEmpty()
            val item = if (mustServeControl) null else samples.poll()
            if (item != null) {
                streak++
                handle(item)
                continue
            }
            streak = 0
            val action = control.poll(10, TimeUnit.MILLISECONDS) ?: continue
            guarded(action)
        }
    }

    private fun guarded(action: () -> Unit) {
        try {
            action()
        } catch (e: RuntimeException) {
            onError("recording thread: ${e.javaClass.simpleName}: ${e.message}")
        }
    }
}
