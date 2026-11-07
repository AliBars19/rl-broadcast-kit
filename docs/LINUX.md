# Linux notes

The kit was built and run on Fedora Linux with the Flatpak build of OBS Studio. This page collects the things that cost time. Only Linux has been tested; the pages themselves are plain HTML and should work elsewhere.

## Capturing Rocket League

OBS on Linux has no Game Capture source. The setup that worked:

- Install the OBS VkCapture plugin from Flathub, plus the matching Vulkan layer for the Flatpak runtime in use (for example `VulkanLayer.OBSVkCapture`, runtime 25.08 at the time of writing).
- Launch the game with the capture layer enabled by setting the environment variable `OBS_VKCAPTURE=1` in the launcher's per-game environment settings.
- In OBS add the plugin's source (source id `vkcapture-source`, shown as "Game Capture" in the Add menu). If the game runs on a second GPU (PRIME offload), enable that in the launcher so the capture layer sees the right Vulkan device.
- Start order that was reliable: OBS first, then the launcher, then the game.

## PulseAudio hang inside the Flatpak sandbox

On the tested machine, any PulseAudio client inside the OBS Flatpak sandbox hung (even `pactl info`), and OBS froze at startup when a scene contained a PulseAudio input capture source: no window, and the WebSocket answered with status 207 (not ready).

Workaround: do not use PulseAudio sources in Flatpak OBS. Install the **PipeWire Audio Capture** plugin from Flathub and add its sources for the microphone and application audio instead. If OBS will not open after a scene change, edit the scene collection JSON with OBS closed and remove the offending `pulse_input_capture` source.

Useful PipeWire pattern: create two virtual sinks (for example `obs_game` and `obs_browser`) with a loopback to your speakers, send the game and the browser source audio to them, and capture each sink in OBS. That gives separate faders without echo.

## Browser docks need X11

On Wayland the OBS Flatpak has no Custom Browser Docks entry. Force X11 for OBS:

```sh
flatpak override --user --socket=x11 --env=QT_QPA_PLATFORM=xcb com.obsproject.Studio
# undo with: flatpak override --user --reset com.obsproject.Studio
```

Then add the scoreboard dock (`http://127.0.0.1:49125/`) under Docks, Custom Browser Docks.

## Do not kill OBS while the game is connected

Quitting the OBS Flatpak with `flatpak kill` while the capture layer is attached took the game down with it in testing. Stop the stream and the recording, close the source or the scene first, then close OBS normally.

## WebSocket server

The OBS WebSocket server binds to all interfaces. Keep a password on it (see the README for how the tools read it), and switch it off when you are not scripting OBS. Do not forward port 4455.

## Services

Copy the templates in `systemd/` to `~/.config/systemd/user/`, replace the placeholder path, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now rl-relay scoreboard-server
journalctl --user -u scoreboard-server -f
```

User services only start while your user session is running; add `loginctl enable-linger $USER` if they should start at boot.

## Phone as a webcam

The kit's own solution is PhoneCam: an Android app plus a few scripts that send the phone camera to OBS over USB and record a full-quality copy on the phone while you stream. See [PHONECAM.md](PHONECAM.md). A DroidCam-style app also works as a plain webcam: forward its port with `adb forward tcp:4747 tcp:4747` after every replug or reboot, then point the OBS DroidCam source at `127.0.0.1` port 4747. Wireless works too but adds latency. The camera can only be opened by one app at a time, so stop the other one first.

## Rendering tools

`ffmpeg` builds differ in which encoders they include. The renderers call `h264_nvenc` (NVIDIA) and `libvpx-vp9`; check with `ffmpeg -encoders | grep -E "nvenc|vpx|x264"`. Chrome is started headless with a fresh profile per frame and killed after a timeout; the render tools keep at most six Chrome processes alive at once, about 300 MB each.
