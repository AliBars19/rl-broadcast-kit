package io.github.rlbroadcastkit.phonecam

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class ProtocolTest {
    private val hello = Hello(Protocol.CODEC_HEVC, 60, 1920, 1080, 20_000_000, 123_456_789_012L)

    @Test
    fun helloRoundTrips() {
        assertEquals(hello, Protocol.decodeHello(Protocol.encodeHello(hello)))
    }

    @Test
    fun helloLayoutIsBigEndian() {
        val bytes = Protocol.encodeHello(hello)
        assertEquals(Protocol.HELLO_SIZE, bytes.size)
        assertArrayEquals(Protocol.MAGIC, bytes.copyOfRange(0, 4))
        assertEquals(2, bytes[4].toInt())
        assertEquals(0, bytes[6].toInt())
        assertEquals(60, bytes[7].toInt())
        assertEquals(0x07, bytes[8].toInt())
        assertEquals(0x80, bytes[9].toInt() and 0xFF)
    }

    @Test
    fun helloRejectsBadMagic() {
        val bytes = Protocol.encodeHello(hello)
        bytes[0] = 0
        try {
            Protocol.decodeHello(bytes)
            fail("expected IllegalArgumentException")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message!!.contains("magic"))
        }
    }

    @Test
    fun packetRoundTrips() {
        val payload = byteArrayOf(1, 2, 3, 4, 5)
        val bytes = Protocol.encodePacket(Protocol.TYPE_FRAME, Protocol.FLAG_KEY, 42L, 99L, payload)
        val header = Protocol.decodeHeader(bytes.copyOfRange(0, Protocol.HEADER_SIZE))
        assertEquals(Protocol.TYPE_FRAME, header.type)
        assertTrue(header.isKey)
        assertEquals(5, header.length)
        assertEquals(42L, header.ptsUs)
        assertEquals(99L, header.sendUs)
        assertArrayEquals(payload, bytes.copyOfRange(Protocol.HEADER_SIZE, bytes.size))
    }

    @Test
    fun deltaFrameIsNotKey() {
        val bytes = Protocol.encodePacket(Protocol.TYPE_FRAME, 0, 1L, 2L, ByteArray(0))
        assertFalse(Protocol.decodeHeader(bytes).isKey)
    }

    @Test
    fun headerRejectsHugeLength() {
        val bytes = Protocol.encodePacket(Protocol.TYPE_FRAME, 0, 1L, 2L, ByteArray(0))
        bytes[4] = 0x7F
        try {
            Protocol.decodeHeader(bytes)
            fail("expected IllegalArgumentException")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message!!.contains("out of range"))
        }
    }

    @Test
    fun commandsRoundTrip() {
        for (kind in Command.Kind.entries) {
            val command = Command(kind, -7L)
            assertEquals(command, Protocol.decodeCommand(Protocol.encodeCommand(command)))
        }
    }

    @Test
    fun unknownCommandIsRejected() {
        val bytes = "NOPE".toByteArray() + ByteArray(8)
        try {
            Protocol.decodeCommand(bytes)
            fail("expected IllegalArgumentException")
        } catch (expected: IllegalArgumentException) {
            assertTrue(expected.message!!.contains("NOPE"))
        }
    }
}
