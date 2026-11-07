package io.github.rlbroadcastkit.phonecam

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.StatFs
import java.io.File

/**
 * Foreground service (camera + microphone). While it runs it keeps one camera session and one
 * encoder going, serves the live stream on a loopback socket and, between REC_START and
 * REC_STOP, writes chunked MP4 files to its own external files dir.
 *
 * Threads: main (commands only), "service-work" (status file, shutdown joins), the camera thread
 * (encoder callbacks, which publish live frames), and "recording" (everything that touches chunk files).
 * Nothing on the recording side can stall or crash the live feed.
 */
class CamService : Service() {
    private lateinit var log: FileLog
    private lateinit var filesDir2: File
    private lateinit var status: StatusWriter
    private val main = Handler(Looper.getMainLooper())
    private val workThread = HandlerThread("service-work").apply { start() }
    private val work = Handler(workThread.looper)
    private var cfg = CamConfig()
    private var video: VideoPipeline? = null
    private var audio: AudioPipeline? = null
    private var live: LiveServer? = null
    private var recorder: Recorder? = null
    private var worker: RecordingWorker? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var videoFormat: MediaFormat? = null
    @Volatile private var audioFormat: MediaFormat? = null
    @Volatile private var csd: ByteArray? = null
    @Volatile private var silentFrame: ByteArray? = null
    private var lastLoggedDrops = 0L
    @Volatile private var audioAlive = false
    @Volatile private var audioMuted = false   // debug: the microphone is alive but delivers nothing
    private var startedAtUs = 0L
    private var framesAtLastTick = 0L
    private var fps = 0.0
    @Volatile private var running = false
    /** True from the moment a shutdown begins until its slow tail (chunk finalizing) has finished. */
    @Volatile private var shuttingDown = false
    private var pendingStart: Intent? = null
    @Volatile private var thermalStatus = 0
    private val thermalListener = PowerManager.OnThermalStatusChangedListener { status ->
        thermalStatus = status
        log.log("thermal status is now $status (0 none, 1 light, 2 moderate, 3 severe, 4 critical, 5 emergency, 6 shutdown)")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        filesDir2 = getExternalFilesDir(null) ?: filesDir
        log = FileLog(File(filesDir2, "phonecam.log"))
        status = StatusWriter(File(filesDir2, "status.json")) { log.log(it) }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START
        log.log("command $action")
        when (action) {
            ACTION_START -> if (shuttingDown) {
                log.log("start requested while the previous run is still closing its chunks: waiting for it")
                pendingStart = intent
            } else {
                handleStart(intent)
            }
            ACTION_REC_START -> startRecording(intent?.getStringExtra("session") ?: defaultSession())
            ACTION_REC_STOP -> worker?.postDraining { recorder?.stop() }
            ACTION_STOP -> {
                pendingStart = null
                shutdown()
            }
            ACTION_DEBUG_KILL_AUDIO -> audio?.failForTest()
            ACTION_DEBUG_MUTE_AUDIO -> audioMuted = intent?.getStringExtra("on") != "0"
        }
        return START_NOT_STICKY
    }

    private fun handleStart(intent: Intent?) {
        val hasCamera = hasPermission(android.Manifest.permission.CAMERA)
        val hasMic = hasPermission(android.Manifest.permission.RECORD_AUDIO)
        // startForeground comes first (Android kills a service started with startForegroundService that never
        // calls it), but only with the types whose permissions we hold: Android 14+ throws otherwise.
        if (!enterForeground(ForegroundTypes.pick(hasCamera, hasMic))) {
            shutdown()
            return
        }
        if (running) {
            log.log("already running, start ignored")
            return
        }
        cfg = try {
            CamConfig.parse { intent?.getStringExtra(it) }
        } catch (e: IllegalArgumentException) {
            log.log("bad start options: ${e.message}")
            shutdown()
            return
        }
        if (!hasCamera) {
            log.log("CAMERA permission missing: run adb shell pm grant io.github.rlbroadcastkit.phonecam android.permission.CAMERA")
            shutdown()
            return
        }
        running = true
        startedAtUs = Clock.nowUs()
        lastLoggedDrops = 0L
        audioAlive = false
        framesAtLastTick = 0L
        val power = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "phonecam:service").also { it.acquire() }
        thermalStatus = power.currentThermalStatus
        power.addThermalStatusListener(thermalListener)
        val chunksDir = File(filesDir2, "chunks")
        OrphanRescue.rescue(chunksDir, ActiveParts.names) { log.log(it) }
        startRecordingSide(chunksDir)
        startLive()
        startVideo()
        if (hasMic) startAudio() else log.log("RECORD_AUDIO permission missing, chunks will have no audio track")
        work.post(statusTick)
        log.log("started: ${cfg.mime} ${cfg.width}x${cfg.height}@${cfg.fps} ${cfg.bitrate} bps, chunks ${cfg.chunkSeconds} s")
    }

    private fun startRecordingSide(chunksDir: File) {
        val rec = Recorder(chunksDir, cfg, { videoFormat }, { audioFormat }, { silentFrame }) { log.log(it) }
        recorder = rec
        worker = RecordingWorker(
            QUEUE_SAMPLES,
            { rec.onVideo(it) },
            { rec.onAudio(it) },
            { rec.recordError(it) },
        ).also { it.start() }
        // The silent frame is made once, off every hot path, so a dead microphone can never cost a chunk.
        Thread({ silentFrame = SilentAac.generate(AUDIO_RATE, cfg.audioBitrate) }, "silent-aac").start()
    }

    private fun startLive() {
        live = LiveServer(
            cfg.port,
            { Hello(cfg.codec, cfg.fps, cfg.width, cfg.height, cfg.bitrate, Clock.nowUs()) },
            { csd },
            { video?.requestKeyFrame() },
            { on -> video?.setTorch(on) ?: directTorch(on) },
            { log.log(it) },
        ).also {
            try { it.start() } catch (e: java.io.IOException) { log.log("live server failed: ${e.message}") }
        }
    }

    /** Torch without our own camera session (the camera may belong to another client). */
    private fun directTorch(on: Boolean): Long {
        val at = Clock.nowUs()
        try {
            (getSystemService(CAMERA_SERVICE) as android.hardware.camera2.CameraManager).setTorchMode(cfg.cameraId, on)
        } catch (e: android.hardware.camera2.CameraAccessException) {
            log.log("torch request failed: ${e.message}")
        }
        return at
    }

    private fun startVideo() {
        video = VideoPipeline(this, cfg, object : VideoPipeline.Listener {
            override fun onFormat(format: MediaFormat) { videoFormat = format }
            override fun onConfig(bytes: ByteArray) { csd = bytes; live?.publishConfig(bytes) }
            override fun onFrame(sample: Sample) {
                live?.publishFrame(sample)
                worker?.submitVideo(sample)   // never logs here: file I/O on the camera thread is what M1 removed
            }
            override fun onFatal(reason: String) { main.post { restartVideo(reason) } }
        }) { log.log(it) }.also { it.start() }
    }

    /** Logged from the status tick (work thread), never from the camera thread. */
    private fun logNewDrops() {
        val total = worker?.dropped?.get() ?: 0L
        if (total > lastLoggedDrops) {
            log.log("recording queue full (flash stall?): ${total - lastLoggedDrops} more samples dropped, $total in total, chunk ${recorder?.chunkIndex}")
            lastLoggedDrops = total
        }
    }

    private fun restartVideo(reason: String) {
        if (!running) return
        log.log("restarting camera after: $reason")
        video?.stop()
        video = null
        worker?.post { recorder?.cut() }
        main.postDelayed({ if (running && video == null) startVideo() }, RESTART_DELAY_MS)
    }

    private fun startAudio() {
        audio = AudioPipeline(
            cfg.audioBitrate,
            { audioFormat = it; audioAlive = true },
            { if (!audioMuted) worker?.submitAudio(it) },
            { onAudioDied() },
        ) { log.log(it) }.also { it.start() }
    }

    /**
     * The microphone stopped. The last known audio format is kept on purpose: chunks opened meanwhile still get
     * an audio track (they receive one silent frame when closed), so every chunk has the same streams.
     */
    private fun onAudioDied() {
        audioAlive = false
        log.log("audio pipeline died; chunks keep an audio track (silent) until it is back, retrying in ${RESTART_DELAY_MS / 1000} s")
        main.postDelayed({
            if (running) {
                audio?.stop()
                startAudio()
            }
        }, RESTART_DELAY_MS)
    }

    private fun startRecording(session: String) {
        if (!running) {
            log.log("REC_START ignored: service is not running")
            return
        }
        worker?.post { recorder?.start(session) }
    }

    private fun shutdown() {
        // A second STOP (or a failed start) while nothing is running must not touch the main queue: the first
        // shutdown's tail may already have posted afterShutdown() there, and removing it would leave
        // shuttingDown true forever, deferring every later START.
        if (!running) {
            if (!shuttingDown) finishForeground()
            return
        }
        running = false
        main.removeCallbacksAndMessages(null)
        work.removeCallbacks(statusTick)
        shuttingDown = true
        video?.stop()
        live?.stop()
        val rec = recorder
        val recWorker = worker
        val micPipeline = audio
        video = null; audio = null; live = null; recorder = null; worker = null
        videoFormat = null; audioFormat = null; csd = null
        work.post {
            micPipeline?.stop()
            recWorker?.postDraining { rec?.stop() }
            recWorker?.stop(STOP_WAIT_MS)
            rec?.awaitFinished(STOP_WAIT_MS)
            (getSystemService(POWER_SERVICE) as PowerManager).removeThermalStatusListener(thermalListener)
            wakeLock?.let { if (it.isHeld) it.release() }
            status.delete()
            log.log("stopped")
            main.post { afterShutdown() }
        }
    }

    /** Runs on the main thread when the old run is completely finished; only now may a waiting START begin. */
    private fun afterShutdown() {
        shuttingDown = false
        val next = pendingStart
        pendingStart = null
        if (next != null) handleStart(next) else finishForeground()
    }

    private fun finishForeground() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        if (running) shutdown()
        workThread.quitSafely()
        super.onDestroy()
    }

    private val statusTick = object : Runnable {
        override fun run() {
            if (!running) return
            writeStatus()
            work.postDelayed(this, 1000)
        }
    }

    private fun writeStatus() {
        logNewDrops()
        val rec = recorder
        val frames = video?.frames?.get() ?: 0L
        fps = (frames - framesAtLastTick).toDouble()
        framesAtLastTick = frames
        val chunksDir = File(filesDir2, "chunks")
        status.write(
            mapOf(
                "running" to running,
                "recording" to (rec?.isRecording == true),
                "session" to rec?.session,
                "chunk" to rec?.chunkIndex,
                "chunk_age_s" to if (rec?.isRecording == true && rec.chunkStartUs > 0) (Clock.nowUs() - rec.chunkStartUs) / 1_000_000 else 0,
                "fps" to fps,
                "frames" to frames,
                "camera_result_lag_ms" to Math.round((video?.cameraResultLagMs ?: 0.0) * 10) / 10.0,
                "live_clients" to (live?.clientCount ?: 0),
                "live_drops" to (live?.dropped?.get() ?: 0),
                "rec_dropped_samples" to (worker?.dropped?.get() ?: 0),
                "codec" to cfg.mime,
                "bitrate" to cfg.bitrate,
                "audio" to audioAlive,
                "closing_chunks" to (rec?.closingChunks ?: 0),
                "uptime_s" to (Clock.nowUs() - startedAtUs) / 1_000_000,
                "free_mb" to StatFs(filesDir2.path).availableBytes / 1_048_576,
                "pending_chunks" to (chunksDir.listFiles { f -> f.name.endsWith(".mp4") }?.size ?: 0),
                "broken_chunks" to (chunksDir.listFiles { f -> f.name.endsWith(".broken") || f.name.endsWith(".orphan") }?.size ?: 0),
                "thermal_status" to thermalStatus,
                "battery_temp_c" to batteryTempC(),
                "error" to rec?.lastError,
                "phone_clock_us" to Clock.nowUs(),
            ),
        )
    }

    private fun batteryTempC(): Double {
        val sticky = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        return (sticky?.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, -1) ?: -1) / 10.0
    }

    /** False when no foreground type can be declared (no camera and no microphone permission). */
    private fun enterForeground(type: Int): Boolean {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Phone camera", NotificationManager.IMPORTANCE_LOW),
        )
        val note = Notification.Builder(this, CHANNEL)
            .setContentTitle("Phone camera running")
            .setContentText("Live feed to the PC, recording on stream start")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .build()
        return try {
            startForeground(NOTIFICATION_ID, note, type)
            true
        } catch (e: RuntimeException) {
            log.log("startForeground refused (type $type): ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    private fun hasPermission(name: String) = checkSelfPermission(name) == PackageManager.PERMISSION_GRANTED

    private fun defaultSession() =
        java.text.SimpleDateFormat("yyyyMMdd-HHmmss", java.util.Locale.US).format(java.util.Date())

    companion object {
        const val ACTION_START = "io.github.rlbroadcastkit.phonecam.START"
        const val ACTION_REC_START = "io.github.rlbroadcastkit.phonecam.REC_START"
        const val ACTION_REC_STOP = "io.github.rlbroadcastkit.phonecam.REC_STOP"
        const val ACTION_STOP = "io.github.rlbroadcastkit.phonecam.STOP"
        /** Test hook: fails the audio pipeline like a dead microphone would. */
        const val ACTION_DEBUG_KILL_AUDIO = "io.github.rlbroadcastkit.phonecam.DEBUG_KILL_AUDIO"
        /** Test hook: audio samples are discarded (chunks opened meanwhile get an empty audio track). */
        const val ACTION_DEBUG_MUTE_AUDIO = "io.github.rlbroadcastkit.phonecam.DEBUG_MUTE_AUDIO"
        private const val CHANNEL = "phonecam"
        private const val NOTIFICATION_ID = 1
        private const val RESTART_DELAY_MS = 2000L
        private const val STOP_WAIT_MS = 8000L
        private const val QUEUE_SAMPLES = 900   // about 12 s of video plus audio: a long flash stall, then drops
        private const val AUDIO_RATE = 48_000
    }
}
