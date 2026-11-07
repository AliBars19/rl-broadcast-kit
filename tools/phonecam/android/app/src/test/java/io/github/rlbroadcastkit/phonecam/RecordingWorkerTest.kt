package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.CopyOnWriteArrayList

class RecordingWorkerTest {
    private fun sample(t: Long) = Sample(t, false, ByteArray(1))

    @Test
    fun aStalledWriterNeverBlocksTheCaller() {
        val gate = CountDownLatch(1)
        val worker = RecordingWorker(4, { gate.await(5, TimeUnit.SECONDS) }, {}, {})
        worker.start()
        val begin = System.nanoTime()
        val accepted = (1..50).count { worker.submitVideo(sample(it.toLong())) }
        val tookMs = (System.nanoTime() - begin) / 1_000_000
        assertTrue("submit blocked for $tookMs ms", tookMs < 500)
        assertTrue(accepted <= 6)                       // queue of 4, one in the stalled handler
        assertEquals((50 - accepted).toLong(), worker.dropped.get())
        gate.countDown()
        worker.stop(2000)
    }

    @Test
    fun aStopQueuedAfterSamplesStillHandlesEverySample() {
        val seen = CopyOnWriteArrayList<String>()
        val gate = CountDownLatch(1)
        val worker = RecordingWorker(100, { gate.await(2, TimeUnit.SECONDS); seen.add("v${it.ptsUs}") }, { seen.add("a${it.ptsUs}") }, {})
        worker.start()
        worker.submitVideo(Sample(1, true, ByteArray(1)))
        worker.submitAudio(sample(2))
        worker.submitVideo(Sample(3, true, ByteArray(1)))
        worker.post { seen.add("stop") }
        worker.stop(3000 * 2)
        gate.countDown()
        worker.stop(2000)
        assertEquals(listOf("v1", "a2", "v3", "stop"), seen.toList())
    }

    @Test
    fun afterADropVideoWaitsForTheNextKeyframe() {
        val gate = CountDownLatch(1)
        val handled = CopyOnWriteArrayList<Long>()
        val worker = RecordingWorker(2, { gate.await(5, TimeUnit.SECONDS); handled.add(it.ptsUs) }, {}, {})
        worker.start()
        for (t in 1L..6L) worker.submitVideo(Sample(t, t == 1L, ByteArray(1)))   // queue of 2 overflows
        gate.countDown()
        Thread.sleep(100)
        // Everything after the drop is refused until a keyframe arrives, even though room is free again.
        assertFalse("delta frame after a drop must be refused", worker.submitVideo(Sample(7, false, ByteArray(1))))
        assertTrue("keyframe must be accepted", worker.submitVideo(Sample(8, true, ByteArray(1))))
        assertTrue("delta after the keyframe must be accepted", worker.submitVideo(Sample(9, false, ByteArray(1))))
        worker.stop(2000)
        assertTrue(handled.contains(8L) && handled.contains(9L))
        assertFalse(handled.contains(7L))
    }

    @Test
    fun audioIsNeverHeldBackByTheKeyframeRule() {
        val gate = CountDownLatch(1)
        val audio = CopyOnWriteArrayList<Long>()
        val worker = RecordingWorker(2, { gate.await(5, TimeUnit.SECONDS) }, { audio.add(it.ptsUs) }, {})
        worker.start()
        for (t in 1L..6L) worker.submitVideo(Sample(t, t == 1L, ByteArray(1)))
        gate.countDown()
        Thread.sleep(100)
        assertTrue(worker.submitAudio(sample(50)))
        worker.stop(2000)
        assertEquals(listOf(50L), audio.toList())
    }

    @Test
    fun controlActionsRunEvenWhenTheSampleQueueIsFull() {
        val gate = CountDownLatch(1)
        val ran = CountDownLatch(1)
        val worker = RecordingWorker(2, { gate.await(5, TimeUnit.SECONDS) }, {}, {})
        worker.start()
        repeat(10) { worker.submitVideo(sample(it.toLong())) }
        worker.post { ran.countDown() }
        gate.countDown()
        assertTrue(ran.await(2, TimeUnit.SECONDS))
        worker.stop(2000)
    }

    @Test
    fun anExceptionInTheHandlerIsReportedAndTheWorkerSurvives() {
        val errors = CopyOnWriteArrayList<String>()
        val handled = CountDownLatch(1)
        val worker = RecordingWorker(10, {
            if (it.ptsUs == 1L) throw IllegalStateException("muxer exploded")
            handled.countDown()
        }, {}, { errors.add(it) })
        worker.start()
        worker.submitVideo(sample(1))
        worker.submitVideo(sample(2))
        assertTrue(handled.await(2, TimeUnit.SECONDS))
        assertEquals(1, errors.size)
        assertTrue(errors[0].contains("muxer exploded"))
        worker.stop(2000)
    }

    @Test
    fun aControlActionIsNotStarvedByAFullQueue() {
        val seen = CopyOnWriteArrayList<String>()
        val release = CountDownLatch(1)
        val worker = RecordingWorker(1000, { release.await(5, TimeUnit.SECONDS); seen.add("s") }, {}, {})
        worker.start()
        repeat(500) { worker.submitVideo(Sample(it.toLong(), true, ByteArray(1))) }
        worker.post { seen.add("CONTROL") }
        release.countDown()
        val deadline = System.currentTimeMillis() + 5000
        while (!seen.contains("CONTROL") && System.currentTimeMillis() < deadline) Thread.sleep(10)
        val at = seen.indexOf("CONTROL")
        worker.stop(3000)
        assertTrue("control never ran", at >= 0)
        assertTrue("control ran only after $at samples", at <= 60)
    }

    @Test
    fun aDrainingStopHandlesEveryQueuedSampleEvenWhenTheQueueIsFull() {
        val seen = CopyOnWriteArrayList<String>()
        val release = CountDownLatch(1)
        val worker = RecordingWorker(1000, { release.await(5, TimeUnit.SECONDS); seen.add("s") }, {}, {})
        worker.start()
        repeat(500) { worker.submitVideo(Sample(it.toLong(), true, ByteArray(1))) }
        worker.postDraining { seen.add("STOP") }
        release.countDown()
        worker.stop(10_000)
        assertEquals(501, seen.size)
        assertEquals("STOP", seen.last())             // all 500 samples came before the stop
        assertEquals(500, seen.count { it == "s" })
    }

    @Test
    fun anExceptionInAControlActionIsReportedToo() {
        val errors = CopyOnWriteArrayList<String>()
        val worker = RecordingWorker(10, {}, {}, { errors.add(it) })
        worker.start()
        worker.post { throw IllegalArgumentException("bad start") }
        worker.stop(2000)
        assertFalse(errors.isEmpty())
    }
}
