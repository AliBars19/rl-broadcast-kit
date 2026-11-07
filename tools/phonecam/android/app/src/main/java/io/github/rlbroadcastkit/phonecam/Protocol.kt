package io.github.rlbroadcastkit.phonecam

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Wire format of the live socket (phone to PC), all integers big-endian.
 *
 * Stream start: one Hello (24 bytes), then packets. Each packet is a 24 byte header
 * followed by `length` payload bytes. The PC may send 12 byte commands at any time.
 * See docs/PHONECAM.md for the field table.
 */
object Protocol {
    val MAGIC = byteArrayOf(0x50, 0x43, 0x4D, 0x31) // "PCM1"
    const val HELLO_SIZE = 24
    const val HEADER_SIZE = 24
    const val COMMAND_SIZE = 12
    const val MAX_PAYLOAD = 8 * 1024 * 1024

    const val TYPE_CONFIG = 1
    const val TYPE_FRAME = 2
    const val TYPE_PONG = 3
    const val TYPE_MARK = 4

    const val FLAG_KEY = 1

    const val CODEC_H264 = 1
    const val CODEC_HEVC = 2

    fun encodeHello(hello: Hello): ByteArray {
        val buf = ByteBuffer.allocate(HELLO_SIZE).order(ByteOrder.BIG_ENDIAN)
        buf.put(MAGIC)
        buf.put(hello.codec.toByte())
        buf.put(0)
        buf.putShort(hello.fps.toShort())
        buf.putShort(hello.width.toShort())
        buf.putShort(hello.height.toShort())
        buf.putInt(hello.bitrate)
        buf.putLong(hello.phoneClockUs)
        return buf.array()
    }

    fun decodeHello(bytes: ByteArray): Hello {
        require(bytes.size == HELLO_SIZE) { "hello must be $HELLO_SIZE bytes, got ${bytes.size}" }
        require(bytes.copyOfRange(0, 4).contentEquals(MAGIC)) { "bad hello magic" }
        val buf = ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
        buf.position(4)
        val codec = buf.get().toInt()
        buf.get()
        return Hello(
            codec = codec,
            fps = buf.short.toInt() and 0xFFFF,
            width = buf.short.toInt() and 0xFFFF,
            height = buf.short.toInt() and 0xFFFF,
            bitrate = buf.int,
            phoneClockUs = buf.long,
        )
    }

    fun encodePacket(type: Int, flags: Int, ptsUs: Long, sendUs: Long, payload: ByteArray): ByteArray {
        require(payload.size <= MAX_PAYLOAD) { "payload too large: ${payload.size}" }
        val buf = ByteBuffer.allocate(HEADER_SIZE + payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(type.toByte())
        buf.put(flags.toByte())
        buf.putShort(0)
        buf.putInt(payload.size)
        buf.putLong(ptsUs)
        buf.putLong(sendUs)
        buf.put(payload)
        return buf.array()
    }

    fun decodeHeader(bytes: ByteArray): PacketHeader {
        require(bytes.size == HEADER_SIZE) { "header must be $HEADER_SIZE bytes, got ${bytes.size}" }
        val buf = ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
        val type = buf.get().toInt() and 0xFF
        val flags = buf.get().toInt() and 0xFF
        buf.short
        val length = buf.int
        require(length in 0..MAX_PAYLOAD) { "payload length out of range: $length" }
        return PacketHeader(type, flags, length, buf.long, buf.long)
    }

    /** Commands are 4 ASCII letters followed by one big-endian long. */
    fun decodeCommand(bytes: ByteArray): Command {
        require(bytes.size == COMMAND_SIZE) { "command must be $COMMAND_SIZE bytes, got ${bytes.size}" }
        val name = String(bytes, 0, 4, Charsets.US_ASCII)
        val kind = Command.Kind.entries.firstOrNull { it.code == name }
            ?: throw IllegalArgumentException("unknown command '$name'")
        return Command(kind, ByteBuffer.wrap(bytes, 4, 8).order(ByteOrder.BIG_ENDIAN).long)
    }

    fun encodeCommand(command: Command): ByteArray {
        val buf = ByteBuffer.allocate(COMMAND_SIZE).order(ByteOrder.BIG_ENDIAN)
        buf.put(command.kind.code.toByteArray(Charsets.US_ASCII))
        buf.putLong(command.arg)
        return buf.array()
    }
}

data class Hello(
    val codec: Int,
    val fps: Int,
    val width: Int,
    val height: Int,
    val bitrate: Int,
    val phoneClockUs: Long,
)

data class PacketHeader(val type: Int, val flags: Int, val length: Int, val ptsUs: Long, val sendUs: Long) {
    val isKey: Boolean get() = flags and Protocol.FLAG_KEY != 0
}

/** PING echoes arg with the phone clock; KEYF asks for a keyframe; TORC sets the torch (arg 1 or 0). */
data class Command(val kind: Kind, val arg: Long) {
    enum class Kind(val code: String) { PING("PING"), KEYF("KEYF"), TORCH("TORC") }
}
