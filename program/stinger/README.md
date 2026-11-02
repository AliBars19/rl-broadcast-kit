# Stinger

`stinger.webm` is the OBS stinger transition: blue and gold diagonal panels sweep across the frame from left to right, cover it completely with a centred mark with your channel name (`channel` in `config.js`), then clear off to the right.

- File: `program/stinger/stinger.webm` (VP9 with alpha, `yuva420p`, 1920x1080, 60 fps, about 400 kB). It is **not committed** because it carries your channel name: render it once with the command under Re-rendering
- Duration: 1.30 s (78 frames)
- Fully covered: 550 ms to 780 ms
- **Transition point: 600 ms** (inside the covered window, so the scene switch is never visible)
- Human preview: `stinger-preview.mp4` (the WebM decoded with alpha over a grey checkerboard; produced by the renderer, not committed)

## OBS settings

Settings, Scene Transitions (or the transition dropdown in the Scenes dock), add a **Stinger**:

| Setting | Value |
|---|---|
| Video File | `program/stinger/stinger.webm` |
| Transition Point Type | Time (milliseconds) |
| Transition Point | `600` |
| Audio Fade Style | Fade Out (the stinger has no audio track, so this has no effect) |
| Track Matte | off (the alpha channel is inside the WebM) |
| Duration | leave at the video length (about 1300 ms) |

Make it the default transition for scene changes. Not tested inside OBS by this tool: it was checked by decoding the WebM with `libvpx-vp9` and reading pixels. If OBS ever shows a black background instead of transparency, it is not decoding the alpha plane; re-encode the frames as ProRes 4444 or QuickTime Animation in a `.mov` instead.

## Rendering (do this first)

```
python3 -I tools/program/render_stinger.py            # writes stinger.webm and stinger-preview.mp4 next to stinger.html
python3 -I tools/program/render_stinger.py --keep-temp
```

It screenshots `stinger.html?t=<seconds>` frame by frame with six parallel headless Chromes (about 45 s), encodes the WebM, builds the preview, then decodes the WebM again and fails the run unless the top-left pixel of the first and last frames is transparent and a pixel of the cover frame is opaque.

## Timeline (stinger.html)

| Time (s) | What |
|---|---|
| 0.00 to 0.55 | Deep blue, blue and navy panels slide in; the navy one carries gold edge stripes, the blue one a gold arc |
| 0.44 to 0.64 | Channel mark slides in, gold rules draw |
| 0.55 to 0.78 | Frame fully covered by the navy panel and the mark |
| 0.70 to 0.86 | Mark slides out and fades |
| 0.78 to 1.28 | Panels clear to the right in reverse order, revealing the new scene |

Editing the timeline: change the numbers in `stinger.html`, then update `DURATION`, `COVER_START_MS`, `COVER_END_MS` and `TRANSITION_MS` at the top of `tools/program/render_stinger.py` and this file.
