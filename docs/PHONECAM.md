# PhoneCam: an Android phone as a low-latency OBS camera

PhoneCam turns an Android phone into the camera for your stream and records a better copy on the phone at the same time. It was built and tested on Linux with a Pixel 6 and the Flatpak build of OBS; other Android 13+ phones with a working Camera2 stack should behave similarly but are untested.

The PhoneCam Android app (`tools/phonecam/android`) owns the phone camera and does two jobs with one hardware encoder:

1. **Live feed** to OBS: H.264 over `adb forward` (USB), turned into MPEG-TS on `udp://127.0.0.1:49400`, which an OBS media source reads.
2. **Recording** on the phone while the stream runs: 1080p60 H.264 at 20 Mbps CBR with the phone microphone as an AAC track, cut into chunks of about 5 minutes on keyframes, each a complete playable MP4. The PC pulls every finished chunk over USB during the stream, checks it, and only then deletes it from the phone.

Why the phone records: highlight clips that crop a small facecam box out of a 1080p stream and blow it up look soft. The phone's own recording is the full-resolution frame of your face.

The scripts and the app are plain: Python standard library on the PC (numpy only for `phone-cam sync`), no third-party Android libraries. Nothing in them needs an account or the network.

## Requirements

| Needed for | Requirement |
|---|---|
| Everything | An Android 13+ phone with USB debugging on, `adb` on PATH, Python 3.10+, `bash`, `flock` |
| Live feed | OBS Studio 30+ with a Media Source reading the UDP port (below) |
| Recording trigger | The OBS WebSocket server enabled (see "OBS WebSocket password" in the README) |
| Join, offload checks | `ffmpeg` and `ffprobe` on PATH |
| `phone-cam sync` | numpy, run with plain `python3` (not `-I`) |
| Building the app | JDK 17 and the Android SDK (platform 36, build-tools 36.0.0). No Android Studio needed |
| `phone-cam --scrcpy` (fallback, no recording) | `scrcpy` 3.x on PATH, or `SCRCPY_DIR` pointing at its folder |

## Day to day

```
tools/phonecam/phone-cam                  # start (detached). Plug in and unlock the phone first.
tools/phonecam/phone-cam status           # running, app state, live latency, last log lines
tools/phonecam/phone-cam stop             # stop everything and free the camera
```

Put the script on your PATH (for example a symlink in `~/.local/bin/phone-cam`) if you want to type just `phone-cam`. It finds the other tools next to itself. Start streaming in OBS and the phone starts recording; stop streaming and it stops. Chunks appear in `~/Videos/phonecam/YYYY-MM-DD/cam/` (the date the stream started) as `cam-SESSION-NNNN-EPOCHMS.mp4`. After the stream, when the phone is empty, a desktop notification says the recording is ready.

| Command | What it does |
|---|---|
| `phone-cam rec start` / `rec stop` / `rec status` | Manual recording (testing, or a backup if the OBS trigger fails). A manual recording keeps going when a stream ends. |
| `phone-cam offload [--watch]` | Pull finished chunks now (the daemon does this by itself every 20 s while recording). |
| `phone-cam join [DATE]` | Join the chunks into `cam/cam-joined.mp4`, lossless (ffmpeg concat, `-c copy`). Not automatic: it writes about as much as the recording. |
| `phone-cam sync [DATE]` | Find the offset between the camera recording and `stream-full.mp4` (put your stream recording in the dated folder next to `cam/`) by audio cross-correlation and write `cam/sync.json`. Needs numpy. |
| `phone-cam trash DATE [--dry-run]` | After the clips are done: move `cam/` to the Trash (`gio trash`, restorable). See "Safety of `trash`" below. |
| `phone-cam --scrcpy` | A fallback that uses scrcpy and does not record. The camera can only be opened by one client, so use one mode at a time (`phone-cam stop` first). |

`sync.json` says `stream_time = cam_time + offset_s`, where cam time 0 is the first frame of the first chunk (or of `cam-joined.mp4`). It also reports `drift_ppm` (the phone's audio clock against the PC's), a confidence figure and the spread of the per-window estimates.

## Settings

Everything has a default. Change them with environment variables (for a service, with `Environment=` lines).

| Variable | Default | Meaning |
|---|---|---|
| `PHONECAM_OUT_ROOT` | `~/Videos/phonecam` | Where recordings go on the PC: `<root>/YYYY-MM-DD/cam/` |
| `PHONECAM_UDP_PORT` | `49400` | UDP port the relay sends MPEG-TS to; the OBS media source reads the same port |
| `PHONECAM_LIVE_PORT` | `49401` | Local port that `adb forward` maps to the app's live socket (the app listens on 49401 on the phone) |
| `PHONECAM_SERIAL` | the ready device that has the app, else the first | adb serial of the phone, when several devices are plugged in |
| `PHONECAM_DIR` | next to the `phone-cam` script | Folder with `live_relay.py` and `daemon.py` |
| `PHONECAM_FREE_CAMERA_PKGS` | empty | Space separated Android packages to force-stop before the camera is opened (another camera app that holds it) |
| `PHONECAM_STATE` | `~/.local/state/phonecam` | Where the manual-recording marker lives |
| `PHONE_CAM_MODE` | `app` | `scrcpy` selects the fallback; same as the `--scrcpy` flag |
| `PHONE_CAM_APP_EXTRAS` | empty | Extra start options for the app, as `--es key value` pairs (below) |
| `SCRCPY_DIR` | empty (`scrcpy` from PATH) | Fallback only: folder holding the `scrcpy` binary |

The OBS media source is named whatever you like; this page calls it **Phone Camera Feed**. The scripts never touch OBS scenes, only the WebSocket output events.

## Add the camera to OBS

Add a **Media Source** named "Phone Camera Feed": untick Local File, Input `udp://127.0.0.1:49400`, Input Format `mpegts`, Network Buffering 0 MB, untick "Close file when inactive", and tick hardware decoding. In the advanced FFmpeg options use `fflags=nobuffer flags=low_delay probesize=32768 analyzeduration=0 max_delay=0`. The relay writes the stream tables and the H.264 parameter sets with every frame, so these small probe sizes work however late OBS joins. If you changed `PHONECAM_UDP_PORT`, use that port here.

## Build and install the app

Command line Gradle, no Android Studio. Gradle 9.0.0 comes through the wrapper, Android Gradle Plugin 8.13.2, Kotlin 2.1.20 (from Google Maven and Maven Central; no AndroidX).

```
cd tools/phonecam/android
export ANDROID_HOME=$HOME/Android/Sdk JAVA_HOME=/path/to/jdk-17    # or create local.properties with sdk.dir=...
./gradlew --no-daemon testDebugUnitTest assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
for p in CAMERA RECORD_AUDIO POST_NOTIFICATIONS; do adb shell pm grant io.github.rlbroadcastkit.phonecam android.permission.$p; done
./gradlew --stop
```

On a shared machine, run the build under limits: `systemd-run --user --scope --quiet -p CPUQuota=300% -p MemoryMax=4G nice -n 10 ./gradlew ...`. `local.properties` and build outputs are git-ignored. `org.gradle.jvmargs=-Xmx2g` is set in `gradle.properties`. Installing keeps the permissions; they only need granting once. The app has no launcher icon: `phone-cam` starts it (`am start -n io.github.rlbroadcastkit.phonecam/.LaunchActivity`).

Start options (strings, passed as `--es key value` through `PHONE_CAM_APP_EXTRAS`): `codec` (`avc` default, or `hevc`), `bitrate` (20000000), `fps` (60), `width` and `height` (1920x1080), `chunk_s` (300), `min_free_mb` (2048), `gop_s` (1), `camera` (`0`, the back camera on most phones). Tuning knobs used for the latency tests: `lowlat`, `oprate`, `template`, `usecase`, `nr`, `torch`, `mode=torch`.

## How it works

```
phone camera --Camera2--> ONE MediaCodec encoder (H.264, CBR 20 Mbps, no B-frames, 1 s GOP)
   |                              |-- chunk router --> MediaMuxer MP4 chunks (+ phone mic, AAC) in
   |                              |                    /sdcard/Android/data/io.github.rlbroadcastkit.phonecam/files/chunks
   |                              '-- live socket 127.0.0.1:49401 on the phone (framed packets, capture time on each)
   '-- foreground service (camera + microphone), works with the screen off

PC:  adb forward tcp:49401 --> live_relay.py (own MPEG-TS muxer) --> udp://127.0.0.1:49400 --> OBS "Phone Camera Feed"
     daemon.py: obs-websocket StreamStateChanged --> adb am startservice REC_START / REC_STOP
                every 20 s: offload.py pulls finished chunks (nice 19, ionice idle, 40 MB/s cap)
```

Decisions and reasons:

- **One encoder, teed.** The camera has one client, so scrcpy cannot share it with the app. One encoder feeds both the file and the socket, so the recording is exactly what was streamed and the phone does half the encoding work (heat, battery).
- **H.264 at 20 Mbps, not HEVC.** HEVC was measured on the test PC: software decoding of the live stream cost about 0.8 of a CPU core against roughly a third of that for H.264, and latency tests with HEVC showed stalls up to 580 ms against 150 ms for H.264. The goal of phone-side recording is zero extra strain on the PC during the stream. If clips still look soft, raise `bitrate` (24 to 30 Mbps costs 900 MB to 1.1 GB per chunk) or switch with `codec=hevc`; the whole chain supports both.
- **Chunks of 300 s, cut on keyframes.** The encoder makes a keyframe every second, so a chunk is 300 to 301 s, about 750 MB. A chunk is written as `NAME.mp4.part` and renamed to `NAME.mp4` only after the MP4 index is complete, so the PC never sees a half file. Plan for about 1.5 GB of phone storage (one chunk being written plus one waiting for offload; use `chunk_s=200` for about 1 GB). If the cable is out, the app keeps recording until free space drops under `min_free_mb`, then stops writing chunks and keeps the live feed running.
- **Audio** is the phone microphone, mono 48 kHz AAC 128 kbps, stamped on the same clock as the video. It exists to line the camera up with the stream audio (`phone-cam sync`).
- **Offload verification.** The phone computes `sha256sum` and size of the finished chunk; the PC streams the file with `adb exec-out cat` into a hidden `.part` file while hashing it on the fly, compares size and sha256, renames into place, and only then runs `rm` on the phone. A mismatch, a failed pull or a different file already on the PC keeps the phone copy and logs it.
- **Live feed format.** `live_relay.py` wraps each frame into MPEG-TS itself (PAT and PMT before every frame, parameter sets in front of every frame, a trailing access-unit delimiter so ffmpeg's parser releases the frame without waiting for the next one) with the capture time as PTS, and sends 1316-byte UDP datagrams. No ffmpeg process is in the live path.
- **Chunk life cycle.** `NAME.mp4.part` while writing, `NAME.mp4` when the index is complete, `NAME.mp4.broken` when the muxer could not be stopped cleanly, `NAME.mp4.orphan` when a run was killed and left a `.part`. The phone never deletes any of them; the daemon pulls all four kinds with the same size and sha256 check and puts `.broken` and `.orphan` files into `cam/broken/` (never joined, kept for repair). If the microphone dies, the app keeps the last known audio format so every chunk keeps an audio track while the microphone restarts. `phone-cam join` adds silent audio to chunks without a usable track instead of refusing; it still refuses other differences (video codec or size, audio codec).
- **Threads.** The camera thread only publishes live frames and hands recording samples to a bounded queue. All muxer work runs on a separate recording thread; if flash stalls and the queue fills, recording samples are dropped and counted and the live feed is never blocked.
- **Warnings.** One desktop notification per failure kind, with retries backed off, and the text shown as `WARNING:` in `phone-cam status`: OBS not reachable for 2 minutes (the stream will not be recorded), OBS websocket silent while the OBS process runs (recording continues), OBS process gone for 3 minutes during a stream (the recording stops), the phone cannot record (for example storage full), the phone unreachable for a minute while a recording is wanted. The "Camera recording ready" notice is only given when the phone is reachable, nothing is left on the phone and the app has no chunk still closing.
- **Watchdogs.** The relay leaves with an error when no frame arrives for 5 seconds; the supervisor restarts it, and after 3 quick relay failures in a row it also stops the phone app so the next attempt starts a fresh one. Every adb call names the device (`PHONECAM_SERIAL`, else the ready device that has the app, else the first ready one). The supervisor never stops an app that reports `recording` with advancing frames.
- **Two offloaders.** The daemon's sweep and `phone-cam offload` share an `flock` on `cam/.offload.lock`: whoever holds it does the folder, the other skips its chunks. Each pull writes a unique, exclusively created temp file.
- **Self-healing.** `phone-cam` waits for the phone (adb state `device`), re-runs `adb forward`, relaunches the app if it is gone, restarts the relay when it exits, and restarts the daemon. The app keeps recording while the cable is out; the daemon offloads what piled up after the replug.
- **OBS trigger.** `daemon.py` reuses `tools/obsws.py` (same authentication; the password is never read or logged by the new code), subscribes to Outputs events, and treats `StreamStateChanged` `STARTING` or `STARTED` as "record" and `STOPPED` as "stop". On connect it also asks `GetStreamStatus`, so a daemon restarted mid-stream starts recording at once. While OBS is closed it retries every 2 to 10 s.
- **Camera permission trick.** Android only lets a foreground service open the camera if the app was visible when it started. `phone-cam` therefore starts an invisible trampoline activity (`am start`), which starts the service and finishes. It also works with the phone locked.

## Safety of `trash`

`phone-cam trash` can only move a folder that passes every one of these checks, so it cannot be pointed at an arbitrary directory:

- it is named `cam` and is not a symlink;
- its parent is named `YYYY-MM-DD` and sits directly in the out root (`PHONECAM_OUT_ROOT`);
- it contains only files PhoneCam writes: chunk files, `cam-joined.mp4`, `sync.json`, lock and partial files, and a `broken/` folder of chunks.

Anything else is refused with a message. The folder goes to the desktop Trash through `gio trash`, so it can be restored.

## Latency

Figures from the one test phone at 1080p60, H.264 20 Mbps, nothing else on the USB link. Every frame carries its capture time (the camera's sensor timestamp) and the phone clock when it was sent; the PC keeps a phone-to-PC clock offset with PING/PONG round trips. `live_relay.py` logs the delays every 30 s and writes them to `$XDG_RUNTIME_DIR/phone-cam.relay.json` (`phone-cam status` shows them). `measure_latency.py` measures the whole chain by switching the phone torch and timing the first brighter decoded picture (an upper bound: the torch itself needs one to three frames).

| Stage | Result |
|---|---|
| Sensor timestamp to encoder output (phone) | median 72 ms, p95 80 ms |
| Phone to PC over USB and `adb forward` | median 3.9 ms, p95 about 21 ms |
| Relay (own MPEG-TS muxer, UDP) | below 1 ms |
| Torch command to decoded picture, whole chain, H.264 | about 130 to 140 ms median |

The delay is dominated by the phone's camera pipeline (about 45 ms before the app sees a frame) plus the hardware encoder (about 27 ms); nothing in the app or the PC chain can remove that. The practical fix is to add a **Sync Offset of about +130 ms to the microphone source** in OBS (Advanced Audio Properties) so your voice matches your lips; try 120 to 150 and judge by eye with a clap.

Cost on the PC during an 18-minute test with the live feed and four offloads: the relay about 6 percent of one core, the daemon about 1 percent (11 percent while a chunk is pulled), the adb server about 2 percent. The pull briefly widens the live transport jitter, which is why the offload stream is deliberately gentle.

## Heat

The camera pipeline is the main heat source. The test phone's skin temperature reached about 48 C within two minutes and stayed there with no throttling, but a 2 to 2.5 hour stream deserves care: take the case off, keep the back of the phone free in the air or in a mount that exposes it, and consider a small fan. `phone-cam status` shows the app's `thermal_status` and `battery_temp_c`.

## Files

- `tools/phonecam/android/`: the app (Kotlin, no dependencies). JVM unit tests: chunk rotation, protocol framing, option parsing (`./gradlew testDebugUnitTest`).
- `tools/phonecam/*.py`: the PC side. Tests: `cd tools/phonecam && python3 -m unittest` (needs ffmpeg; numpy for the sync test, which skips under `python3 -I` when numpy is missing).
- `tools/phonecam/phone-cam`: the supervisor. Shell tests: `bash tools/test_phone_cam_app.sh`, `bash tools/test_phone_cam.sh` (scrcpy path); they use a fake adb and need no phone.
- `systemd/phone-cam.service.example`: optional user service.
- `tools/phonecam/measure_latency.py`, `decode_delay.py`: the torch based latency measurements.

## Known limits

- The live socket on the phone (`127.0.0.1:49401`) has no authentication: any app on the phone can connect and read the feed, or send the torch command. It is loopback only, so nothing else on the network can.
- The MPEG-TS timestamps restart from 1 s whenever the relay restarts, so OBS sees a time jump and reconnects.
- Video PES packets above 64 KB use length 0 (allowed for video; the trailing access-unit delimiter makes ffmpeg release such a frame anyway).
- Debug hooks in the app: `am startservice ... -a io.github.rlbroadcastkit.phonecam.DEBUG_KILL_AUDIO` (fails the microphone pipeline) and `DEBUG_MUTE_AUDIO --es on 1|0` (microphone alive but silent). They exist for soak tests.

## Gotchas

- The camera can be opened by one client: stop `phone-cam --scrcpy`, any other camera app and any scrcpy session first (list packages in `PHONECAM_FREE_CAMERA_PKGS` to have the supervisor force-stop them).
- A chunk that is being written when the app is killed has no index: the file is kept as `.orphan` (not playable without repair, for example with untrunc) and the recording resumes in a new chunk. The app finishes the current chunk cleanly on `REC_STOP`, `phone-cam stop` and a normal service stop.
- The phone microphone is held for as long as the app runs (so recording can start without delay), which shows the microphone indicator.
- `adb exec-out cat` shares the one USB link with the live socket; the 40 MB/s cap keeps the live feed's transport delay unchanged.
- Chunk files from a session whose id is not `YYYYMMDD-HHMMSS` (for example hand-made test sessions) are left on the phone and logged as `unknown-name`.
- `phone-cam join` refuses chunks whose stream sets differ (for example one without audio); move the odd chunk out of the folder or join in two parts.
