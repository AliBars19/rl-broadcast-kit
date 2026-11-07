package io.github.rlbroadcastkit.phonecam

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Range
import android.view.Surface
import java.util.concurrent.atomic.AtomicLong

/**
 * Camera2 into ONE hardware encoder. The encoder output is handed to the listener, which
 * tees it to the live socket and to the chunk recorder. No B-frames, one keyframe per second.
 */
class VideoPipeline(
    private val context: Context,
    private val cfg: CamConfig,
    private val listener: Listener,
    private val log: (String) -> Unit,
) {
    interface Listener {
        fun onFormat(format: MediaFormat)
        fun onConfig(bytes: ByteArray)
        fun onFrame(sample: Sample)
        fun onFatal(reason: String)
    }

    val frames = AtomicLong()

    /** Moving average of (result arrival minus sensor timestamp) in ms: the camera's own pipeline delay. */
    @Volatile var cameraResultLagMs = 0.0
        private set

    private val thread = HandlerThread("camera").apply { start() }
    private val handler = Handler(thread.looper)
    private var encoder: MediaCodec? = null
    private var surface: Surface? = null
    private var camera: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var builder: CaptureRequest.Builder? = null
    @Volatile private var stopped = false

    fun start() = handler.post {
        try {
            startEncoder()
            openCamera()
        } catch (e: Exception) {
            fatal("start failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    fun stop() {
        stopped = true
        handler.post { release() }
        thread.quitSafely()
    }

    fun requestKeyFrame() {
        try {
            encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        } catch (e: IllegalStateException) {
            log("keyframe request ignored: ${e.message}")
        }
    }

    /** Switches the torch on the running stream and returns the phone clock (us) of the request. */
    fun setTorch(on: Boolean): Long {
        val b = builder
        val s = session
        if (b == null || s == null) return Clock.nowUs()
        b.set(CaptureRequest.FLASH_MODE, if (on) CaptureRequest.FLASH_MODE_TORCH else CaptureRequest.FLASH_MODE_OFF)
        return try {
            s.setRepeatingRequest(b.build(), lagProbe, handler)
            Clock.nowUs()
        } catch (e: Exception) {
            log("torch request failed: ${e.message}")
            Clock.nowUs()
        }
    }

    private val lagProbe = object : CameraCaptureSession.CaptureCallback() {
        override fun onCaptureCompleted(
            session: CameraCaptureSession, request: CaptureRequest, result: android.hardware.camera2.TotalCaptureResult,
        ) {
            val sensorUs = (result.get(android.hardware.camera2.CaptureResult.SENSOR_TIMESTAMP) ?: return) / 1000
            val lagMs = (Clock.nowUs() - sensorUs) / 1000.0
            cameraResultLagMs = if (cameraResultLagMs == 0.0) lagMs else cameraResultLagMs * 0.98 + lagMs * 0.02
        }
    }

    private fun startEncoder() {
        val format = MediaFormat.createVideoFormat(cfg.mime, cfg.width, cfg.height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, cfg.bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, cfg.fps)
            setInteger(MediaFormat.KEY_CAPTURE_RATE, cfg.fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, cfg.gopSeconds)
            if (cfg.operatingRate > 0) setInteger(MediaFormat.KEY_OPERATING_RATE, cfg.operatingRate)
            if (cfg.lowLatency) {
                setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0)
                setInteger(MediaFormat.KEY_LATENCY, 1)
                setInteger(MediaFormat.KEY_PRIORITY, 0)
            }
            setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1)
            setInteger(MediaFormat.KEY_COLOR_STANDARD, MediaFormat.COLOR_STANDARD_BT709)
            setInteger(MediaFormat.KEY_COLOR_RANGE, MediaFormat.COLOR_RANGE_LIMITED)
            setInteger(MediaFormat.KEY_COLOR_TRANSFER, MediaFormat.COLOR_TRANSFER_SDR_VIDEO)
        }
        val codec = MediaCodec.createEncoderByType(cfg.mime)
        codec.setCallback(EncoderCallback(), handler)
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        surface = codec.createInputSurface()
        codec.start()
        encoder = codec
        log("encoder ${codec.name} ${cfg.mime} ${cfg.width}x${cfg.height}@${cfg.fps} ${cfg.bitrate / 1000} kbps CBR")
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        manager.openCamera(cfg.cameraId, { handler.post(it) }, object : CameraDevice.StateCallback() {
            override fun onOpened(device: CameraDevice) {
                camera = device
                createSession(device, manager)
            }
            override fun onDisconnected(device: CameraDevice) = fatal("camera disconnected")
            override fun onError(device: CameraDevice, error: Int) = fatal("camera error $error")
        })
    }

    private fun createSession(device: CameraDevice, manager: CameraManager) {
        val chars = manager.getCameraCharacteristics(cfg.cameraId)
        val out = OutputConfiguration(surface!!).apply {
            timestampBase = OutputConfiguration.TIMESTAMP_BASE_REALTIME
            pickStreamUseCase(chars)?.let { streamUseCase = it }
        }
        val template = if (cfg.template == "preview") CameraDevice.TEMPLATE_PREVIEW else CameraDevice.TEMPLATE_RECORD
        val request = device.createCaptureRequest(template).apply {
            noiseReductionMode()?.let { set(CaptureRequest.NOISE_REDUCTION_MODE, it) }
            set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, pickFpsRange(chars))
            set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
            set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            if (cfg.torchAtStart) set(CaptureRequest.FLASH_MODE, CaptureRequest.FLASH_MODE_TORCH)
            addTarget(surface!!)
        }
        builder = request
        val config = SessionConfiguration(
            SessionConfiguration.SESSION_REGULAR, listOf(out), { handler.post(it) },
            object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) {
                    session = s
                    try {
                        s.setRepeatingRequest(request.build(), lagProbe, handler)
                        log("camera ${cfg.cameraId} streaming")
                    } catch (e: Exception) {
                        fatal("repeating request failed: ${e.message}")
                    }
                }
                override fun onConfigureFailed(s: CameraCaptureSession) = fatal("camera session configure failed")
            },
        )
        config.sessionParameters = request.build()
        device.createCaptureSession(config)
    }

    private fun pickStreamUseCase(chars: CameraCharacteristics): Long? {
        val wanted = when (cfg.useCase) {
            "video" -> android.hardware.camera2.CameraMetadata.SCALER_AVAILABLE_STREAM_USE_CASES_VIDEO_RECORD
            "preview" -> android.hardware.camera2.CameraMetadata.SCALER_AVAILABLE_STREAM_USE_CASES_PREVIEW
            else -> return null
        }.toLong()
        val supported = chars.get(CameraCharacteristics.SCALER_AVAILABLE_STREAM_USE_CASES) ?: return null
        return wanted.takeIf { it in supported }.also { if (it == null) log("stream use case ${cfg.useCase} not supported") }
    }

    private fun noiseReductionMode(): Int? = when (cfg.noiseReduction) {
        "fast" -> CaptureRequest.NOISE_REDUCTION_MODE_FAST
        "off" -> CaptureRequest.NOISE_REDUCTION_MODE_OFF
        else -> null
    }

    private fun pickFpsRange(chars: CameraCharacteristics): Range<Int> {
        val ranges = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES) ?: emptyArray()
        return ranges.firstOrNull { it.lower == cfg.fps && it.upper == cfg.fps }
            ?: ranges.filter { it.upper == cfg.fps }.maxByOrNull { it.lower }
            ?: Range(cfg.fps, cfg.fps).also { log("no AE range ending at ${cfg.fps}, asking for it anyway") }
    }

    private fun fatal(reason: String) {
        if (stopped) return
        log("video pipeline fatal: $reason")
        listener.onFatal(reason)
    }

    private fun release() {
        runCatching { session?.close() }
        runCatching { camera?.close() }
        runCatching { encoder?.stop() }
        runCatching { encoder?.release() }
        runCatching { surface?.release() }
        session = null; camera = null; encoder = null; surface = null; builder = null
    }

    private inner class EncoderCallback : MediaCodec.Callback() {
        override fun onInputBufferAvailable(codec: MediaCodec, index: Int) = Unit

        override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
            try {
                val buf = codec.getOutputBuffer(index)
                if (buf != null && info.size > 0) {
                    val bytes = ByteArray(info.size)
                    buf.position(info.offset)
                    buf.get(bytes)
                    deliver(bytes, info)
                }
            } finally {
                codec.releaseOutputBuffer(index, false)
            }
        }

        private fun deliver(bytes: ByteArray, info: MediaCodec.BufferInfo) {
            if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                listener.onConfig(bytes)
                return
            }
            frames.incrementAndGet()
            val key = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
            listener.onFrame(Sample(info.presentationTimeUs, key, bytes))
        }

        override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) = listener.onFormat(format)

        override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) =
            fatal("encoder error: ${e.diagnosticInfo}")
    }
}
