package io.github.rlbroadcastkit.phonecam

/** Settings for one service run, parsed from string extras of the start intent. */
data class CamConfig(
    val codec: Int = Protocol.CODEC_H264,
    val bitrate: Int = 20_000_000,
    val width: Int = 1920,
    val height: Int = 1080,
    val fps: Int = 60,
    val cameraId: String = "0",
    val chunkSeconds: Int = 300,
    val port: Int = 49401,
    val audioBitrate: Int = 128_000,
    val minFreeMb: Int = 2048,
    /** torch: no camera pipeline, only the live socket and the torch (used to measure other camera paths). */
    val torchOnly: Boolean = false,
    /** Low-latency encoder flags (latency hint, realtime priority, no B-frames, CBR). Off only for A/B tests. */
    val lowLatency: Boolean = true,
    val gopSeconds: Int = 1,
    /** KEY_OPERATING_RATE, 0 = leave unset. */
    val operatingRate: Int = 0,
    /** Camera tuning knobs, kept for A/B tests of the camera pipeline delay: record|preview. */
    val template: String = "record",
    /** Stream use case: video | preview | default. */
    val useCase: String = "video",
    /** Noise reduction: default | fast | off. */
    val noiseReduction: String = "default",
    /** Torch on from the start (lights the scene for motion based latency tests). */
    val torchAtStart: Boolean = false,
) {
    val mime: String get() = if (codec == Protocol.CODEC_HEVC) "video/hevc" else "video/avc"

    companion object {
        fun parse(get: (String) -> String?): CamConfig {
            val d = CamConfig()
            fun int(key: String, default: Int, min: Int, max: Int): Int {
                val v = get(key)?.toIntOrNull() ?: return default
                require(v in min..max) { "$key=$v out of range $min..$max" }
                return v
            }
            val codec = when (val c = get("codec")?.lowercase()) {
                null -> d.codec
                "hevc" -> Protocol.CODEC_HEVC
                "avc", "h264" -> Protocol.CODEC_H264
                else -> throw IllegalArgumentException("codec=$c, expected hevc or avc")
            }
            return CamConfig(
                codec = codec,
                bitrate = int("bitrate", d.bitrate, 1_000_000, 100_000_000),
                width = int("width", d.width, 160, 4096),
                height = int("height", d.height, 90, 4096),
                fps = int("fps", d.fps, 1, 120),
                cameraId = get("camera") ?: d.cameraId,
                chunkSeconds = int("chunk_s", d.chunkSeconds, 5, 3600),
                port = int("port", d.port, 1024, 65535),
                audioBitrate = int("audio_bitrate", d.audioBitrate, 16_000, 512_000),
                minFreeMb = int("min_free_mb", d.minFreeMb, 0, 100_000),
                torchOnly = get("mode") == "torch",
                lowLatency = get("lowlat") != "0",
                gopSeconds = int("gop_s", d.gopSeconds, 1, 30),
                operatingRate = int("oprate", d.operatingRate, 0, 1000),
                template = get("template") ?: d.template,
                useCase = get("usecase") ?: d.useCase,
                noiseReduction = get("nr") ?: d.noiseReduction,
                torchAtStart = get("torch") == "1",
            )
        }
    }
}
