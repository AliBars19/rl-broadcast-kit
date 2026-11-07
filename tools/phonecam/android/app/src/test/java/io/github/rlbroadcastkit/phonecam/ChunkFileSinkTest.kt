package io.github.rlbroadcastkit.phonecam

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicInteger

private class FakePort : MuxerPort {
    val writes = mutableListOf<Triple<Int, Long, Int>>()   // track, pts, size
    var failWriteOn: Int? = null
    var failStop = false
    var failRelease = false
    var released = false
    var stopped = false

    override fun write(track: Int, ptsUs: Long, key: Boolean, bytes: ByteArray) {
        if (failWriteOn == track) throw IllegalStateException("muxer write failed")
        writes.add(Triple(track, ptsUs, bytes.size))
    }

    override fun stop() {
        stopped = true
        if (failStop) throw IllegalStateException("Failed to stop the muxer")
    }

    override fun release() {
        released = true
        if (failRelease) throw IllegalStateException("release failed")
    }
}

class ChunkFileSinkTest {
    private lateinit var dir: File
    private lateinit var part: File
    private val logs = mutableListOf<String>()
    private val direct = Executor { it.run() }
    private val closing = AtomicInteger()

    @Before
    fun setUp() {
        dir = Files.createTempDirectory("chunks").toFile()
        part = File(dir, "cam-x-0001-1.mp4.part").also { it.writeText("data") }
    }

    @After
    fun tearDown() {
        dir.deleteRecursively()
    }

    private fun sink(port: FakePort, audioTrack: Int = 1, silence: ByteArray? = byteArrayOf(1, 2, 3)) =
        ChunkFileSink(
            port, 0, audioTrack, part, File(dir, "cam-x-0001-1.mp4"), File(dir, "cam-x-0001-1.mp4.broken"),
            { silence }, direct, closing, { logs.add(it) },
        )

    private fun video(t: Long) = Sample(t, true, ByteArray(10))
    private fun audioSample(t: Long) = Sample(t, false, ByteArray(4))
    private fun names() = dir.list()!!.sorted()

    @Test
    fun aCleanChunkBecomesTheFinalMp4() {
        val port = FakePort()
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
        assertTrue(port.released)
        assertEquals(2, port.writes.size)  // no silent filler: the audio track had a real sample
    }

    @Test
    fun anEmptyAudioTrackGetsOneSilentFrameSoStopSucceeds() {
        val port = FakePort()
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.finish()
        assertEquals(Triple(1, 0L, 3), port.writes.last())
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
    }

    @Test
    fun aVideoOnlyChunkNeedsNoSilence() {
        val port = FakePort()
        val s = sink(port, audioTrack = -1)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(1, port.writes.size)
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
    }

    @Test
    fun aMuxerStopFailureKeepsTheFileAsBrokenAndStillReleases() {
        val port = FakePort().apply { failStop = true }
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4.broken"), names())
        assertEquals("data", File(dir, "cam-x-0001-1.mp4.broken").readText())
        assertTrue(port.released)
    }

    @Test
    fun aFailedReleaseDoesNotLoseTheChunk() {
        val port = FakePort().apply { failRelease = true }
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
    }

    @Test
    fun aLostSampleDoesNotMakeAGoodChunkBroken() {
        val port = FakePort().apply { failWriteOn = 0 }
        val s = sink(port)
        s.writeVideo(0, video(0))                 // lost
        port.failWriteOn = null
        s.writeVideo(1000, video(1000))           // written
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
        assertEquals(2, port.writes.size)
        assertTrue(logs.any { it.contains("samples are being lost") })
        assertTrue(logs.any { it.contains("1 samples could not be written") })
    }

    @Test
    fun aWriteFailureFollowedByAFailedStopKeepsTheChunkAsBroken() {
        val port = FakePort().apply { failWriteOn = 0; failStop = true }
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4.broken"), names())
    }

    @Test
    fun theClosingCounterCoversTheTimeUntilTheChunkIsPublished() {
        val port = FakePort()
        val queued = mutableListOf<Runnable>()
        val delayed = ChunkFileSink(
            port, 0, 1, part, File(dir, "cam-x-0001-1.mp4"), File(dir, "cam-x-0001-1.mp4.broken"),
            { null }, Executor { queued.add(it) }, closing, { logs.add(it) },
        )
        delayed.writeVideo(0, video(0))
        delayed.writeAudio(0, audioSample(0))
        delayed.finish()
        assertEquals(1, closing.get())
        assertEquals(listOf("cam-x-0001-1.mp4.part"), names())
        queued.forEach { it.run() }
        assertEquals(0, closing.get())
        assertEquals(listOf("cam-x-0001-1.mp4"), names())
    }

    @Test
    fun theClosingCounterDropsEvenWhenTheExecutorRefuses() {
        val s = ChunkFileSink(
            FakePort(), 0, -1, part, File(dir, "a.mp4"), File(dir, "a.broken"), { null },
            Executor { throw java.util.concurrent.RejectedExecutionException("shut down") }, closing, { },
        )
        try {
            s.finish()
        } catch (expected: RuntimeException) {
        }
        assertEquals(0, closing.get())
    }

    @Test
    fun noSilentFrameAvailableStillNeverDeletes() {
        val port = FakePort().apply { failStop = true }
        val s = sink(port, silence = null)
        s.writeVideo(0, video(0))
        s.finish()
        assertEquals(listOf("cam-x-0001-1.mp4.broken"), names())
        assertTrue(logs.any { it.contains("no silent frame") })
    }

    @Test
    fun theActivePartNameIsReleasedWhateverHappensWhileClosing() {
        val registered = { ActiveParts.names.contains(part.name) }
        val rejecting = ChunkFileSink(
            FakePort(), 0, -1, part, File(dir, "a.mp4"), File(dir, "a.broken"), { null },
            Executor { throw java.util.concurrent.RejectedExecutionException("shut down") }, closing, { },
        )
        assertTrue(registered())
        try {
            rejecting.finish()
        } catch (expected: RuntimeException) {
        }
        assertFalse(registered())

        val exploding = ChunkFileSink(
            FakePort(), 0, 1, part, File(dir, "b.mp4"), File(dir, "b.broken"),
            { throw IllegalStateException("silent frame generator blew up") }, direct, closing, { },
        )
        assertTrue(registered())
        try {
            exploding.finish()
        } catch (expected: RuntimeException) {
        }
        assertFalse(registered())
    }

    @Test
    fun aFailedRenameLeavesThePartFileForTheNextStartToRescue() {
        val port = FakePort()
        val blocked = File(dir, "cam-x-0001-1.mp4").also { it.mkdir(); File(it, "x").writeText("x") }
        val s = sink(port)
        s.writeVideo(0, video(0))
        s.writeAudio(0, audioSample(0))
        s.finish()
        assertTrue(part.exists())
        assertTrue(blocked.isDirectory)
        assertFalse(logs.none { it.contains("cannot rename") })
    }
}
