# Program motion tools

Tools for the broadcast-style program look: the OBS stinger, the intro video and the end title for a hand-cut intro. Both render HTML graphics with headless Chrome and compose with ffmpeg. Standard library Python plus `ffmpeg`, `ffprobe` and `google-chrome` on PATH. Encoding uses `h264_nvenc` (NVIDIA GPU) and `libvpx-vp9`. Without an NVIDIA GPU, change the `ENCODE` list at the top of `build_intro.py` (and `encode_command` in `add_end_title.py`) to `libx264`.

## How the graphics are rendered

Every animated page (`program/stinger/stinger.html`, `program/intro/*.html`) registers paused Web Animations through `program/motion.js`. The page is opened as `page.html?t=<seconds>` and every animation is scrubbed to that time, so a frame depends only on `t`. `chrome_frames.py` screenshots one frame per Chrome (transparent background, fresh profile, six at a time, whole process group killed after 40 s) and writes `00000.png`, `00001.png`, ... Frames in a static stretch of an animation are hard links to the last rendered frame (each graphic lists its moving ranges in `intro_spec.GRAPHICS`), which roughly halves the Chrome work.

## Stinger

```
python3 -I tools/program/render_stinger.py
```

Writes `program/stinger/stinger.webm` and `stinger-preview.mp4` (the webm is git-ignored: it shows your channel name from `config.js`, so every user renders their own), then checks that alpha survived the VP9 encode. See `program/stinger/README.md` for the duration, the OBS transition point (600 ms) and the OBS settings.

## Intro

```
python3 -I tools/program/build_intro.py my-intro.json
python3 -I tools/program/build_intro.py SPEC.json --dry-run     # validate, print the timeline, render nothing
python3 -I tools/program/build_intro.py SPEC.json --keep-temp   # keep graphic frames and per-shot files
```

`shots/replays.example.json` is a ready layout for eight replay recordings: copy it, point every `source` at your own footage, adjust `start`/`end`, and set `opponent`. Nothing here ships any footage or game assets.

### Spec format

```json
{
  "output": "~/Videos/rl-broadcast-kit/intro.mp4",
  "title": "YOUR EVENT 26",
  "subtitle": "YOUR LEAGUE",
  "matchday": 2,
  "opponent": "ORANGE TEAM",
  "home_team": "BLUE TEAM",
  "channel": "YOUR CHANNEL",
  "sponsor_logo": "program/assets/sponsor.svg",
  "music": "~/Music/my-track.mp3",
  "shots": [
    {"source": "~/Videos/replay-shot-1.mkv", "start": 1.0, "end": 3.6, "graphic": "presents"},
    {"source": "~/Videos/replay-shot-3.mkv", "start": "00:00:01", "end": "00:00:02.5", "speed": 0.5, "graphic": "wordmark"}
  ]
}
```

- Required: `title` (22 characters), `subtitle` (28), `matchday` (1 to 99, or a word of at most 8 characters), `opponent` (28), `shots` (1 to 12).
- Optional: `output` (must end in `.mp4`; a bare file name goes into `~/Videos/rl-broadcast-kit/`, no `output` means `intro.mp4` there), `music` (an audio file you own; the default is none), `channel` (24, default: `channel` in `config.js`), `home_team` (28, default: the blue team name in `config.js`), `sponsor_logo` (default: the `sponsor` logo in `config.js`, or none).
- The sponsor is optional. With no `sponsor_logo` and no sponsor in `config.js` the `sponsor` graphic is rejected, and the end title simply leaves the PRESENTED BY line out.
- Brand values are read from `config.js` through `tools/brand.py`, which evaluates the file with `node`. If `node` is missing the placeholder defaults are used.
- Shot fields: `source`, `start`, `end` (required; `HH:MM:SS.f`, `MM:SS` or plain seconds, same parser as `render_short.py`), `speed` (0.25 to 2, default 1), `graphic` (see below, or `null`). A shot must play for 0.4 to 12 s after speed.
- Relative paths resolve against the spec file's folder, `~` is expanded. Unknown fields are rejected. Em and en dashes are rejected in every text field. The whole intro must be 15 to 30 s long.

### Graphics

| id | What | Needs the shot to play at least | SFX |
|---|---|---|---|
| `presents` | "<channel> PRESENTS" band | 2.4 s | whoosh |
| `wordmark` | title slam with the subtitle block | 3.0 s | boom on the slam |
| `sponsor` | PRESENTED BY and the sponsor logo (needs a sponsor logo) | 2.4 s | whoosh |
| `matchup` | home team vs the opponent, blue and orange panels | 3.0 s | whoosh, hit |
| `lockup` | wordmark, subtitle, MATCHDAY N over a darkened frame | 3.4 s | boom |

Each graphic can be used once. `lockup` must be on the last shot; the picture fades to black over its last 0.5 s. A graphic starts at the beginning of its shot.

### What the build does

1. Renders each used graphic to a transparent PNG sequence (60 fps, as long as its shot).
2. Per shot: seek with `-ss` before `-i`, cut, apply `speed` (`setpts`, `atempo`; slow motion also darkens the game audio), scale and crop to 1920x1080 at 60 fps, overlay the graphic frames, add a 7 frame white or blue flash after each cut, rebuild the audio at 48 kHz stereo, encode with NVENC (high profile, constant quality 22, 20 Mbps cap) into an intermediate `.mov`. The last shot also fades to black.
3. Synthesises the sound effects with ffmpeg lavfi into one WAV: a filtered pink noise whoosh on every cut, a low sine boom with a noise crack on the wordmark slam and the lockup, a lighter hit on the matchup card, a rising riser that ends where the lockup starts.
4. Joins the shots (video is copied, not re-encoded), mixes game audio (volume 0.6, or 0.3 when music is supplied), the effects and optional music, fades the audio out over the last 0.5 s, encodes AAC 192k, writes `+faststart`, and checks that the length matches the plan.

The temp folder (`/tmp/build_intro_*`) is deleted afterwards unless `--keep-temp` is given.

## End title for a finished intro

```
python3 -I tools/program/add_end_title.py ~/Videos/intro.mp4 ~/Videos/rl-broadcast-kit/intro.mp4 --hit 14.058
python3 -I tools/program/add_end_title.py IN.mp4 OUT.mp4 --hit auto --search 13.8 14.3   # measure the hit
```

For an intro you cut by hand: composites `program/intro/endtitle.html` (the event name from `programDefaults.eventName` in the wordmark style, the event line, and PRESENTED BY with the sponsor logo when one is configured) over the final shot. The cut, the length and the audio are untouched: the audio stream is copied (`-c:a copy`, so its MD5 matches the input) and the output has the same frame count. The input file is only read; the tool refuses to run when the output path is the input path.

- The title slams in on the audio hit (`--hit`, seconds). `--hit auto` measures it: 2 ms RMS windows over `--search LO HI`, the hit is the first window 10 dB above the median of the previous 100 ms. The first overlay frame is the first video frame at or after the hit.
- Timeline after the hit: a flat navy layer eases to 60 percent over 0.5 s, the wordmark settles from scale 1.45 by 0.3 s, the event line wipes in 0.45 to 0.85 s, PRESENTED BY and the lockup fade in 1.1 to 1.6 s. Nothing fades out: the source fades to black behind the title, so the last frame is the title on near black (OBS holds that frame during the outgoing stinger).
- Colour: the RGBA frames are converted to YUV with an explicit BT.709 limited matrix (`scale=out_color_matrix=bt709:out_range=tv`); without it ffmpeg uses BT.601 and shifts the gold. The output is tagged BT.709, tv range. The gold in "26" decodes to about (245, 182, 61) for #f5b83d.
- Encode: `h264_nvenc`, High, yuv420p, 60 fps CFR, CQ 19 (about 25 Mbps for the 11 Mbps source), closed GOPs of 60, `+faststart`. At most four Chrome workers render the 98 moving frames; the rest are hard links. About one minute.
- Check afterwards: `ffprobe`, and `ffmpeg -i X -map 0:a -c copy -f md5 -` on both files.

## Tests

```
cd tools/program && python3 -I -m unittest -v
```

Covers frame and length math, spec validation, graphic id rules, the SFX plan, every ffmpeg and Chrome argument list, the alpha check, and a static scan of all graphics for the house design rules (letter-spacing 0, no glow, blur, shadow or radial gradient, no dashes, no external resources). Nothing is encoded and no Chrome starts in the unit tests.

## Files

| File | Purpose |
|---|---|
| `chrome_frames.py` | time-driven Chrome frame rendering, frame math |
| `render_stinger.py` | stinger render, encode, alpha check |
| `intro_spec.py` | spec validation, graphic table, timeline and SFX plan |
| `intro_sfx.py` | ffmpeg lavfi sound effects |
| `build_intro.py` | shot encoding, final mix, command line |
| `add_end_title.py` | end title over a finished intro: hit detection, overlay, encode |
| `shots/` | `standin.json`, `replays.example.json` |
