package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class CamConfigTest {
    private fun parse(vararg pairs: Pair<String, String>) = CamConfig.parse { pairs.toMap()[it] }

    @Test
    fun defaultsAre1080p60Avc20MbpsFiveMinuteChunks() {
        val cfg = parse()
        assertEquals("video/avc", cfg.mime)
        assertEquals(20_000_000, cfg.bitrate)
        assertEquals(60, cfg.fps)
        assertEquals(300, cfg.chunkSeconds)
        assertEquals("0", cfg.cameraId)
    }

    @Test
    fun hevcAndOverridesAreParsed() {
        val cfg = parse("codec" to "hevc", "bitrate" to "8000000", "chunk_s" to "60")
        assertEquals("video/hevc", cfg.mime)
        assertEquals(8_000_000, cfg.bitrate)
        assertEquals(60, cfg.chunkSeconds)
    }

    @Test
    fun unknownCodecIsRejected() {
        try {
            parse("codec" to "vp9")
            fail("expected IllegalArgumentException")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("vp9"))
        }
    }

    @Test
    fun outOfRangeValueIsRejected() {
        try {
            parse("fps" to "500")
            fail("expected IllegalArgumentException")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("fps"))
        }
    }
}
