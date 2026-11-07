package io.github.rlbroadcastkit.phonecam

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder

/**
 * Phone microphone into an AAC encoder. Used only to line the camera footage up with the stream
 * audio later, so one mono 48 kHz track is enough. Sample times are on the same elapsed-realtime
 * clock as the camera frames: the first AudioRecord timestamp anchors the sample counter.
 */
class AudioPipeline(
    private val bitrate: Int,
    private val onFormat: (MediaFormat) -> Unit,
    private val onSample: (Sample) -> Unit,
    private val onDied: () -> Unit,
    private val log: (String) -> Unit,
) {
    @Volatile private var running = false
    @Volatile private var injectedFailure = false
    private var thread: Thread? = null

    fun start() {
        running = true
        thread = Thread({ run() }, "audio").also { it.start() }
    }

    /** Debug hook: makes the pipeline fail the way a dead AudioRecord would. */
    fun failForTest() {
        injectedFailure = true
    }

    fun stop() {
        running = false
        thread?.join(2000)
    }

    @SuppressLint("MissingPermission")
    private fun run() {
        var record: AudioRecord? = null
        var codec: MediaCodec? = null
        try {
            record = buildRecord()
            codec = buildCodec()
            record.startRecording()
            pump(record, codec)
        } catch (e: Exception) {
            log("audio pipeline stopped: ${e.javaClass.simpleName}: ${e.message}")
            if (running) onDied()
        } finally {
            runCatching { record?.stop() }
            runCatching { record?.release() }
            runCatching { codec?.stop() }
            runCatching { codec?.release() }
        }
    }

    @SuppressLint("MissingPermission")
    private fun buildRecord(): AudioRecord {
        val min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        check(min > 0) { "AudioRecord.getMinBufferSize returned $min" }
        val record = AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.MIC)
            .setAudioFormat(
                AudioFormat.Builder().setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build(),
            )
            .setBufferSizeInBytes(maxOf(min * 4, RATE / 5 * 2))
            .build()
        check(record.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord not initialised (microphone busy or no permission)" }
        return record
    }

    private fun buildCodec(): MediaCodec {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, RATE, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, CHUNK_BYTES * 4)
        }
        return MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).also {
            it.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            it.start()
        }
    }

    private fun pump(record: AudioRecord, codec: MediaCodec) {
        val pcm = ByteArray(CHUNK_BYTES)
        val info = MediaCodec.BufferInfo()
        val stamp = AudioTimestamp()
        var framesRead = 0L
        var badReads = 0
        var anchorNs = Long.MIN_VALUE
        while (running) {
            check(!injectedFailure) { "audio failure injected by test" }
            val n = record.read(pcm, 0, pcm.size)
            if (n <= 0) {
                check(n != AudioRecord.ERROR_DEAD_OBJECT) { "audio record died" }
                badReads++
                check(badReads < MAX_BAD_READS) { "audio record keeps returning errors (last $n)" }
                Thread.sleep(BAD_READ_BACKOFF_MS)  // never spin on a failing read
                continue
            }
            badReads = 0
            if (anchorNs == Long.MIN_VALUE &&
                record.getTimestamp(stamp, AudioTimestamp.TIMEBASE_BOOTTIME) == AudioRecord.SUCCESS
            ) {
                anchorNs = stamp.nanoTime - stamp.framePosition * 1_000_000_000L / RATE
                log("audio clock anchored after $framesRead frames")
            }
            if (anchorNs != Long.MIN_VALUE) {
                val ptsUs = (anchorNs + framesRead * 1_000_000_000L / RATE) / 1000
                feed(codec, pcm, n, ptsUs)
            }
            framesRead += n / 2
            drain(codec, info)
        }
    }

    private fun feed(codec: MediaCodec, pcm: ByteArray, n: Int, ptsUs: Long) {
        val index = codec.dequeueInputBuffer(10_000)
        if (index < 0) return
        val buf = codec.getInputBuffer(index)!!
        buf.clear()
        buf.put(pcm, 0, n)
        codec.queueInputBuffer(index, 0, n, ptsUs, 0)
    }

    private fun drain(codec: MediaCodec, info: MediaCodec.BufferInfo) {
        while (true) {
            val index = codec.dequeueOutputBuffer(info, 0)
            when {
                index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> onFormat(codec.outputFormat)
                index < 0 -> return
                else -> {
                    val buf = codec.getOutputBuffer(index)
                    if (buf != null && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        val bytes = ByteArray(info.size)
                        buf.position(info.offset)
                        buf.get(bytes)
                        onSample(Sample(info.presentationTimeUs, false, bytes))
                    }
                    codec.releaseOutputBuffer(index, false)
                }
            }
        }
    }

    private companion object {
        const val RATE = 48_000
        const val MAX_BAD_READS = 100
        const val BAD_READ_BACKOFF_MS = 20L
        const val CHUNK_BYTES = 1920 // 20 ms of mono 16-bit
    }
}
