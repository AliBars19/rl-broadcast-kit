package io.github.rlbroadcastkit.phonecam

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat

/** One encoded AAC frame of silence, made by the real encoder so it matches the chunks' audio track. */
object SilentAac {
    fun generate(sampleRate: Int, bitrate: Int): ByteArray? {
        var codec: MediaCodec? = null
        try {
            val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, 1).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            }
            codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            return firstFrame(codec)
        } catch (e: Exception) {
            return null
        } finally {
            runCatching { codec?.stop() }
            runCatching { codec?.release() }
        }
    }

    private fun firstFrame(codec: MediaCodec): ByteArray? {
        val zeros = ByteArray(2048)  // 1024 samples of 16-bit mono silence
        val info = MediaCodec.BufferInfo()
        var ptsUs = 0L
        repeat(60) {
            val input = codec.dequeueInputBuffer(20_000)
            if (input >= 0) {
                val buf = codec.getInputBuffer(input)!!
                buf.clear()
                buf.put(zeros, 0, minOf(zeros.size, buf.capacity()))
                codec.queueInputBuffer(input, 0, minOf(zeros.size, buf.capacity()), ptsUs, 0)
                ptsUs += 21_333
            }
            val output = codec.dequeueOutputBuffer(info, 20_000)
            if (output >= 0) {
                val frame = if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                    ByteArray(info.size).also { codec.getOutputBuffer(output)!!.apply { position(info.offset) }.get(it) }
                } else null
                codec.releaseOutputBuffer(output, false)
                if (frame != null) return frame
            }
        }
        return null
    }
}
