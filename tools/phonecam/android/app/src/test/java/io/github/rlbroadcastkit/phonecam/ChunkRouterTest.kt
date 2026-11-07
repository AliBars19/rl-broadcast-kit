package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

private const val SEC = 1_000_000L

private class FakeSink(val index: Int, val startUs: Long) : ChunkSink {
    val video = mutableListOf<Long>()
    val audio = mutableListOf<Long>()
    val videoKeys = mutableListOf<Boolean>()
    var finished = false
    override fun writeVideo(relativePtsUs: Long, sample: Sample) {
        video.add(relativePtsUs)
        videoKeys.add(sample.key)
    }
    override fun writeAudio(relativePtsUs: Long, sample: Sample) { audio.add(relativePtsUs) }
    override fun finish() { finished = true }
}

private class FakeFactory : ChunkSinkFactory {
    val sinks = mutableListOf<FakeSink>()
    var failOn: Int? = null
    override fun open(index: Int, startPtsUs: Long): ChunkSink {
        if (failOn == index) throw IOException("disk full")
        return FakeSink(index, startPtsUs).also { sinks.add(it) }
    }
}

class ChunkRouterTest {
    private val factory = FakeFactory()
    private val errors = mutableListOf<String>()
    private val router = ChunkRouter(factory, 10 * SEC, 1 * SEC) { errors.add(it) }

    private fun video(second: Double, key: Boolean) =
        router.onVideo(Sample(Math.round(second * SEC), key, ByteArray(1)))

    private fun audio(second: Double) = router.onAudio(Sample(Math.round(second * SEC), false, ByteArray(1)))

    /** One frame per second, keyframes every 2 s, from `from` to `to` inclusive. */
    private fun feed(from: Int, to: Int) {
        for (s in from..to) video(s.toDouble(), s % 2 == 0)
    }

    @Test
    fun nothingIsWrittenBeforeStart() {
        feed(0, 5)
        assertTrue(factory.sinks.isEmpty())
    }

    @Test
    fun firstChunkWaitsForAKeyframe() {
        router.start()
        video(1.0, false)
        assertTrue(factory.sinks.isEmpty())
        video(2.0, true)
        assertEquals(1, factory.sinks.size)
        assertEquals(listOf(0L), factory.sinks[0].video)
    }

    @Test
    fun rotatesOnTheFirstKeyframeAtOrAfterTheTarget() {
        router.start()
        feed(2, 14)
        assertEquals(2, factory.sinks.size)
        assertEquals(12 * SEC, factory.sinks[1].startUs)
        assertTrue(factory.sinks[1].videoKeys.first())
        assertEquals(2, factory.sinks[1].index)
    }

    @Test
    fun relativeTimesRestartAtZeroInEachChunk() {
        router.start()
        feed(2, 14)
        assertEquals(0L, factory.sinks[1].video.first())
        assertEquals(2 * SEC, factory.sinks[1].video.last())
    }

    @Test
    fun noDroppedVideoAcrossTheCut() {
        router.start()
        feed(2, 14)
        val total = factory.sinks.sumOf { it.video.size }
        assertEquals(13, total)
    }

    @Test
    fun lateAudioBeforeTheCutGoesToThePreviousChunk() {
        router.start()
        feed(2, 12)
        audio(11.98)
        assertEquals(listOf(9_980_000L), factory.sinks[0].audio)
        assertTrue(factory.sinks[1].audio.isEmpty())
        assertFalse(factory.sinks[0].finished)
    }

    @Test
    fun audioPastTheCutClosesThePreviousChunk() {
        router.start()
        feed(2, 12)
        audio(12.02)
        assertTrue(factory.sinks[0].finished)
        assertEquals(listOf(20_000L), factory.sinks[1].audio)
    }

    @Test
    fun previousChunkClosesAfterGraceWithoutAudio() {
        router.start()
        feed(2, 14)
        assertTrue(factory.sinks[0].finished)
    }

    @Test
    fun audioBeforeTheFirstKeyframeIsDropped() {
        router.start()
        audio(1.0)
        video(2.0, true)
        audio(1.99)
        audio(2.01)
        assertEquals(listOf(10_000L), factory.sinks[0].audio)
    }

    @Test
    fun stopFinishesEveryOpenChunk() {
        router.start()
        feed(2, 12)
        router.stop()
        assertTrue(factory.sinks.all { it.finished })
        assertFalse(router.isRecording)
    }

    @Test
    fun startAfterStopNumbersChunksFromOneAgain() {
        router.start()
        feed(2, 4)
        router.stop()
        router.start()
        video(20.0, true)
        assertEquals(1, factory.sinks.last().index)
    }

    @Test
    fun openFailureStopsRecordingAndReports() {
        factory.failOn = 2
        router.start()
        feed(2, 14)
        assertFalse(router.isRecording)
        assertEquals(1, errors.size)
        assertTrue(errors[0].contains("disk full"))
        assertTrue(factory.sinks[0].finished)
    }

    @Test
    fun aRuntimeExceptionWhileOpeningStopsTheRecordingInsteadOfPropagating() {
        val throwing = object : ChunkSinkFactory {
            override fun open(index: Int, startPtsUs: Long): ChunkSink = throw IllegalStateException("addTrack failed")
        }
        val r = ChunkRouter(throwing, 10 * SEC, 1 * SEC) { errors.add(it) }
        r.start()
        r.onVideo(Sample(2 * SEC, true, ByteArray(1)))   // must not throw
        assertFalse(r.isRecording)
        assertTrue(errors[0].contains("addTrack failed"))
    }

    @Test
    fun deltaOnlyStreamNeverOpensAChunk() {
        router.start()
        for (s in 0..30) video(s.toDouble(), false)
        assertTrue(factory.sinks.isEmpty())
    }
}
