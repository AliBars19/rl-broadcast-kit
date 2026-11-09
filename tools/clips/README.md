# Clip candidate finder

`find_candidates.py` scans a long stream recording and proposes highlight moments from audio loudness. Standard library plus `ffmpeg`/`ffprobe` on PATH; no numpy or Pillow needed.

```
python3 -I tools/clips/find_candidates.py /path/to/stream-full.mp4 [--out DIR] [--top 80]
```

Output goes to `<video dir>/analysis/` unless `--out` is given. A 2 hour 1080p60 recording takes about 8 minutes.

## What it writes

- `loudness.csv`: one row per second (`t, rms_db, peak_db, high_rms_db`). The high band is everything above 2 kHz, which separates crowd, explosion and shout energy from bass.
- `candidates.json`: the top 80 peaks with `rank, t, hh:mm:ss, score, rms_db, peak_db, high_rms_db` and `frames` (five thumbnails at t-6, t-3, t, t+3, t+6, paths relative to the analysis dir).
- `frames/`: the candidate thumbnails (384 px wide).
- `sheets/cand-001.jpg ...`: one 5x1 contact sheet per candidate with the timestamp burned into each tile.
- `overview/overview-NN.jpg`: contact sheets of the whole stream, one frame every 30 s, 6 columns x 5 rows, 320 px tiles, hh:mm:ss burned in.

## How candidates are chosen

The score blends three z-scores over 3 s smoothed audio: overall RMS (0.4), high-band RMS (0.4) and how far RMS jumps above the previous 30 s (0.2). Peaks are picked greedily, best first, at least 10 s apart. The first 3 minutes and any stretch of 20+ s below -60 dB RMS are ignored.

## Tests

```
cd tools/clips && python3 -I -m unittest -v
```

The tests cover timestamp formatting, silence filtering, eligibility, scoring and peak picking.

# Goal detector

`find_goals.py` finds every goal in a spectator recording by watching the top-centre score HUD for changes. Standard library plus `ffmpeg`/`ffprobe` on PATH.

```
python3 -I tools/clips/find_goals.py /path/to/stream-full.mp4 [--out DIR] [--workers 6] [--reuse]
```

Output goes to `<video dir>/analysis/` unless `--out` is given. A 2 hour 1080p60 recording takes roughly 10 to 20 minutes, almost all of it decoding (see below).

## What it writes

- `goals.json`: `{"match_windows": [...], "goals": [...]}`.
  - Each goal has `n`, `t` (seconds), `hh:mm:ss`, `side` (`blue` or `orange`), `confidence` (0..1), `match` (the window it falls in), `blue` and `orange` (the running score in that window) and `sheet`.
  - Each match window has `match`, `series`, `start`, `end` (+ `_hms`), `final_blue` and `final_orange`. A window is one game: the scoreboard disappears between the games of a series, and the score restarts at 0-0 when it comes back. Windows less than 5 minutes apart share a `series` number.
- `goal-sheets/goal-NNN.jpg`: a 6 tile contact strip per goal at t-8, t-4, t-1, t+2, t+5, t+9 seconds (320 px tiles, timestamp burned in), for a human to confirm each goal.
- `hud-fingerprints.bin`: the raw HUD samples (768 bytes per sample at 4 per second), so `--reuse` can rerun detection without decoding the video again.

## How it works

One `ffmpeg` pass samples the video at 4 fps and shrinks three HUD regions to 16x16 gray: the blue score box (x 785..865), the orange score box (x 1060..1140) and a strip of the team-name bars (x 500..760, y 20..60). The clock is never read because it changes every second. The ffmpeg build used here only has the single-threaded OpenH264 decoder, so the video is cut into 5 minute chunks decoded in parallel (`--workers`).

1. **Scoreboard present.** The reference look of each box is the stable value that most other stable values resemble (digits recur all game, a static facecam is only one long run). Samples within 30 (mean abs pixel difference) of it count as scoreboard. Both boxes must match. Stretches of at least 2 minutes, bridged over dropouts of up to 20 s (the replay dim), are the match windows. Goals outside a window are ignored and the score counter restarts in every window.
2. **Score change.** Inside a window each box is reduced to a glyph signature: how far each pixel of the digit area sits above the box background. That ignores brightness and the slow 3D tilt of the box. A change counts when the new signature persists for at least 3 s; shorter excursions (flicker, fades) are absorbed.
3. **Goal flash.** Every real goal blinks the whole HUD for a fraction of a second. A change is only a goal if the box leaves its normal look within 2 samples of it. This is what separates a 2 to 3 change (small glyph difference) from the digit box wobbling.
4. **Scene cuts.** The team-name strip must stay put either side of a change, so a cut to the facecam or a results screen is not read as a goal.
5. **Echo.** A goal also nudges the other box; when blue and orange both change within 2 s the weaker one is dropped.

The reported time is the first sample showing the new value minus 0.25 s. `confidence` is half for a confirmed change plus half for how far the glyph moved, discounted by drift of the team-name strip, so 2 to 3 changes score lower (about 0.65) than 0 to 1 (about 0.9) without being doubtful.

Tuning notes: the thresholds are constants at the top of the file (`BOX_TOL`, `PRESENT_TOL`, `GLYPH_*`, `FLASH_MARGIN_SAMPLES`). They were fitted to one broadcast overlay; a different scoreboard needs the `CROPS` boxes and probably the glyph window adjusted.

## Tests

```
cd tools/clips && python3 -I -m unittest -v
```

The tests cover persistent-change detection, flicker rejection, the scoreboard reference, flash gate, window grouping, score counting and report building with synthetic fingerprints.

# Short renderer

`render_short.py` turns clip timestamps into a vertical 1080x1920 YouTube Short (a "Top Plays" countdown style, navy and amber palette). Standard library plus `ffmpeg`, `ffprobe` and `google-chrome` on PATH. Video is encoded with `h264_nvenc` (Main profile, about 8 Mbps, 30 fps, yuv420p, `+faststart`) and AAC 192k stereo, with the game audio kept.

```
python3 -I tools/clips/render_short.py tools/clips/shorts/top3-goals.example.json
python3 -I tools/clips/render_short.py SPEC.json --dry-run     # validate and print the length budget only
python3 -I tools/clips/render_short.py SPEC.json --keep-temp   # keep the HTML, PNGs and scene files
```

A 50 s Short takes about one minute. The output folder is created if needed. Examples are in `tools/clips/shorts/` (`top3-goals.example.json` is a ranked "TOP 3 GOALS", `best-moments.example.json` has no ranks). Copy one, point `source` and `output` at your own files, and edit the clips. They contain placeholder paths only.

## Spec format

One JSON file per Short:

```json
{
  "title": "TOP 3 GOALS",
  "subtitle": "YOUR EVENT 26 | MATCHDAY 1",
  "handle": "@yourchannel",
  "source": "/path/to/stream-full.mp4",
  "output": "/path/to/shorts/top3-goals.mp4",
  "clips": [
    {"rank": 3, "start": "00:35:08.0", "end": "00:35:22.0", "player": "PLAYER ONE",
     "team": "BLUE TEAM", "caption": "AIR DRIBBLE FINISH", "detail": "vs ORANGE TEAM | GAME 3"}
  ]
}
```

- Required: `title`, `source`, `output` (must end in `.mp4`), `clips` (1 to 5). Optional: `subtitle`, `handle` (default: the first handle in `config.js`, must start with `@`), `channel` (default: `channel` in `config.js`; shown in the small tag), `end_title` (default `FOLLOW <HANDLE>`), `end_line` (default `NEW CLIPS EVERY MATCHDAY`). Brand defaults are read from `config.js` with `node` through `tools/brand.py`.
- Clip fields: `start` and `end` are required, as `HH:MM:SS.f`, `MM:SS` or plain seconds. A clip must be 1 to 30 s and end inside the source. `player`, `team`, `caption`, `detail` are optional text; `rank` is a whole number 1 to 9; `reaction` is `true` (default) or `false`, see Layouts below.
- Clips play in the order listed, so list a countdown as rank 3, 2, 1 (best last). Give every clip a rank or none of them. Ranked clips get a 1.1 s full-screen rank card and a progress indicator (boxes 3 2 1) under the video; unranked Shorts ("BEST MOMENTS") skip both.
- Text limits: title 28 characters, subtitle 44, handle 24, player 20, team 28, caption 30, detail 40. Titles, players, teams and captions are uppercased. Em and en dashes are rejected (house rule: hyphen, colon or comma). Unknown fields are rejected so typos do not pass silently.

## Layouts

The layout assumes spectated or recorded play followed by your own reactions, so the facecam is half the content.

- `"reaction": true` (default). Top to bottom: top block (tag, title, subtitle) y 0..380; the full 16:9 game frame scaled to 1080x608 at y 380..988; the reaction panel at y 988..1588, which is the facecam rectangle cropped from the source frame (x 36, y 786, 440x244) and scaled to 1080x600 (about 2.45x, light unsharp, thin amber frame, a "<channel> REACTS" chip, and the rank progress boxes top right); then the bottom block y 1588..1920 with the rank number, player, team, detail, the caption bar and the handle strip. 380 + 608 + 600 + 332 = 1920. Inside the game frame the original facecam corner is covered with a blurred, darkened patch so the face is not shown twice.
- `"reaction": false`. The classic layout: the game frame centred at y 656..1264 with the facecam left in place inside it, the top block above and the player, team, caption, progress and handle strip below. The frame is still 1080x608 because the whole 16:9 frame is always kept.

`reaction` is per clip, so a Short can mix both. The facecam position is fixed for the current overlay (`CAM_CROP` and `CAM_PATCH` at the top of `render_short.py`).

## Length guard

The total is `1.5 s title card + per clip (1.1 s rank card if ranked + clip length) + 3.0 s end card`, with clip lengths snapped to whole frames. The renderer prints it before doing any work and exits non-zero without rendering if it exceeds 58.0 s. After rendering it measures the real duration with ffprobe and deletes the output and fails if it is 59.5 s or more. Five 9 s ranked clips (about 62 s) are refused; five 8 s clips fit.

## How it works

1. Every graphic is an HTML page in `graphics/` (`title.html`, `rank.html`, `clip.html` plus `clip-ranked.part.html` / `clip-plain.part.html`, `end.html`, shared `short.css` with the bundled fonts from `fonts/`). The renderer fills the `{{placeholders}}` from the spec (HTML-escaped; font size shrinks to fit the text) and screenshots each page to a transparent 1080x1920 PNG with headless Chrome (fresh profile per shot, 40 s timeout, whole process group killed on timeout).
2. Each scene becomes its own intermediate `.mov` in a temp dir: the title and end cards slide up and fade in over a navy background with silent audio; rank cards slide in from the right with a synthetic whoosh; each clip is the source seeked with `-ss` before `-i` and cut with `-t`, a blurred and darkened cover-fit copy as background, the real frame scaled to 1080x608 (plus the cropped facecam panel and the blurred patch in the reaction layout), the overlay PNG on top, a 0.2 s fade in, and 0.15 s audio fades.
3. The scenes are joined with the concat demuxer: video is stream-copied, audio is encoded once as AAC so there are no seams. The temp dir is deleted afterwards.

Design rules baked into the graphics (and checked by a static test in `test_render_short.py`): letter-spacing 0, no glow, shadow, radial gradient or spotlight, no dashes in copy, no logos, only fade, slide and scale motion.

## Tests

```
cd tools/clips && python3 -I -m unittest -v test_render_short
```

The tests cover timestamp parsing, the length budget and 58 s / 59.5 s guards, spec validation errors, text fitting, template filling, the graphics design-rule scan, and the exact ffmpeg and Chrome argument lists (nothing is encoded in unit tests).

# Feed event detector

`find_feed_events.py` finds the short-lived stat popups in the top right of a Rocket League spectator broadcast (name chip plus an icon: goal, shot, assist, save, demolition and so on) and groups them by icon. It reads the game windows from `goals.json`, so run `find_goals.py` first. Standard library plus `ffmpeg` on PATH.

```
python3 -I tools/clips/find_feed_events.py /path/to/stream-full.mp4 [--out DIR] [--workers 12] [--reuse] [--cluster-tol 0.22]
```

## What it writes

- `feed-events.json`: `clusters` (`id`, `count`, `kinds`, `blue`, `orange`, `near_goal`, `median_duration`, `examples`, `montage`) and `events` (`n`, `t` first seen, `hh:mm:ss`, `duration`, `side` blue/orange, `chips`, `kind`, `cluster`, `match`, `icon_box`, `near_goal`, `strip`). `near_goal` is true when the popup starts within -1..+3.5 s of a goal by the same side in `goals.json`; it is a hint for naming a cluster, not proof.
- `feed-icons/cluster-NN.png`: up to 12 example icon crops per cluster, 4x enlarged, with the time and event number burned in. Cluster 1 is the biggest.
- `feed-events/ev-NNN.jpg`: for each event a 4x2 strip of full frames at t-1 s .. t+6 s (480 px tiles, running timestamp burned in).
- `feed-crops.bin` and `feed-observations.json`: the decoded 180x75 RGB crops (about 40 KB per sample, 4 samples per second) and the per-sample detections. `--reuse` skips decoding and detection; `--reuse --redetect` keeps the crops and reruns detection; changing only `--cluster-tol` with `--reuse` reclusters in seconds (`--no-sheets` / `--no-strips` skip the ffmpeg image work).

## How it works

1. **Decode.** Only the game windows are decoded, in 150 s chunks in parallel (the OpenH264 decoder is single-threaded). Each chunk is sampled at 4 fps, cropped to x 1560..1920, y 0..150 and area-averaged to half size.
2. **Per frame.** Popups sit in rows 55.5 px apart (the player panel hides everything below y=150, so row 2 is only partly visible and rows 3+ are hidden until the stack shifts up). A row holds a popup when it has a run of pastel blue (`#6c86d6`-ish) or orange chip colour sampled above and below the name text. Two layouts exist: chip then icon at the right end, and a demolition style `chip, icon, chip` whose icon sits in the gap. A single chip also needs warm/gold icon pixels in the icon box.
3. **Icon fingerprint.** 12x12 cells, two channels (share of gold pixels, share of white pixels), so the background behind the transparent icon does not matter.
4. **Tracking.** Popups expire from the top and join at the bottom, so each sample is aligned to the ordered list of live popups with a shift (expired from the front); colour, layout and icon decide the match. Gaps up to 1 s are bridged; anything seen for under 1 s is dropped. `t` is the first sample showing the popup, `duration` runs to the last.
5. **Clustering.** The median fingerprint of an event (skipping the pop-in animation) is clustered greedily by cosine distance with `--cluster-tol`, refined twice, clusters with close centres are merged, and clusters of one or two events (blurred or half covered icons) are joined to the nearest bigger cluster within 0.3. Single-chip and demolition layouts are clustered separately.

Limits: two popups of the same side in the same row with nothing between them are one event; a popup that starts in a hidden row (fourth or lower) is first seen when it shifts up, so its `t` can be a second or two late. Backgrounds that are strongly orange or saturated blue can produce false popups; check the strips.

# TikTok renderer

`render_tiktok.py` turns clip timestamps into a vertical 1080x1920, 60 fps TikTok in the black-card style: a caption card, a clip, a card, a clip, and so on, each card carrying the same sound. Standard library plus `ffmpeg` and `ffprobe` on PATH. Video is encoded with `h264_nvenc` (preset p5, vbr, cq 21, 12M, maxrate 16M) and AAC 192k stereo at 48 kHz. ffmpeg runs with `-threads 4`.

```
python3 -I tools/clips/render_tiktok.py tools/clips/tiktoks/example.json
python3 -I tools/clips/render_tiktok.py SPEC.json --dry-run     # validate, print the budget and the timeline only
python3 -I tools/clips/render_tiktok.py SPEC.json --keep-temp   # keep the intermediates
```

A 20 s video takes about 35 s on a desktop NVIDIA GPU. On a shared machine, start it under limits (`systemd-run --user --scope -p CPUQuota=300% -p MemoryMax=4G nice -n 10 python3 -I tools/clips/render_tiktok.py SPEC.json`) and do not run two renders at once. The output folder is created if needed. `tiktoks/example.json` is the starting point: copy it and replace the `/path/to/...` placeholders with your own files. The captions use a bold TrueType font, by default Noto Sans Bold at `/usr/share/fonts/google-noto/NotoSans-Bold.ttf`; set the `TIKTOK_FONT` environment variable to another `.ttf` if yours lives elsewhere.

## Spec format

One JSON file per video:

```json
{
  "caption": "Defense.",
  "output": "/path/to/tiktoks/defense.mp4",
  "card_seconds": 0.75,
  "opening_card": true,
  "music": null,
  "clips": [
    {"source": "/path/to/stream.mp4", "start": "01:27:11.0", "end": "01:27:20.0",
     "sfx": [{"at": 6.2, "name": "impact", "gain_db": 0}]}
  ]
}
```

- `layout` (optional, default `"kit"`): the picture crop, which depends on the overlay the footage was recorded with. Unknown names are refused. A clip can override it with its own `layout` field (below), so one video can mix clips recorded with different overlays. Cards, the caption (y 640) and the audio are the same for every layout.
  - `bottom-cam`: an example of a second geometry, for an overlay with the facecam in the bottom left corner and a spectator bar at the bottom of the frame. The kit's own pages do not produce this picture; it is here as a model for your own overlay (see "Adding your own layout"). Crops are described under Picture.
  - `kit`: the kit's own gameplay scene (scoreboard bars along the top, `webcam-frame.html` as a box at about x 30..610, y 708..1035, no spectator bar). Scoreboard strip (99 px) over a taller game crop (948x954 at x 540, y 126, scaled to 1080x1087), so the picture is 1186 px tall and centred. The 72 px of the facecam box that remain at the crop's left edge are covered, before the crop, with a mirrored, lightly blurred copy of the pitch beside it (source x 618..696, y 704..1040, flipped, boxblur 2, laid over x 540..618). The patch is 6 px wider than the box needs because the brackets leave a compression halo up to x 614, and every number in it is even because yuv420p crop and overlay round odd values and left one bracket row showing. The blocks are stacked in yuv444p so the odd heights do not smear chroma between them.
  - `kit-reaction`: the same strip and game, then the facecam underneath (crop 560x316 at x 40, y 716, scaled to 1080x609 with a light `unsharp`), 1795 px tall and centred. The crop sits inside the picture of the box, so no bracket, frame or label shows.
  - `broadcast`: for plain 16:9 footage such as an official broadcast (typically the hook): a video with no facecam and no stream overlay, so nothing is cropped away. The whole frame, centre cropped by the clip's `zoom` (below), is scaled to 1080x608 and placed at y 706..1314, which has the same vertical centre as the kit game block, so the caption (y 640, just above the picture) and the TikTok bottom zone (the last 370 px stay clear) behave as on the other layouts. The rest of the 1080x1920 frame is the same clip scaled to cover, blurred (gblur sigma 8, built at a quarter of the size so it is cheap) and darkened (`eq` brightness -0.25, saturation 0.8), so there are no black bars. The audio is the source's own (the casters) and goes through the same chain as every clip (`clip_comp`, fades, `loudnorm`). The source may have any frame rate (30, 50, 60, 59.94) and any 16:9 size of 1280x720 or more (1914x1080 broadcasts, 1440p, 4K; width and height must be within 1 % of 16:9); the frame rate is normalised to 60 fps from time 0 of the clip, so the scene has exactly length x 60 frames like every other clip. `cam` windows are refused on a broadcast clip. It can be set on one clip, on the hook, or (unusually) at the top level; the top-level default stays `kit`.
- `layout` on a clip (optional, same values): overrides the top-level `layout` for that clip only, picture and `cam` rules included. A clip without it uses the top-level value, so a spec that never uses the field renders the same commands as before. Each clip's picture is centred with its own padding (bottom-cam starts at y 432, kit at y 367, kit-reaction at y 62); the caption stays at y 640 on every layout. `--dry-run` names the layout after a clip in the timeline (`clip 2 (kit)`) when it differs from the top level.
- `zoom` (optional, only on a clip whose layout is `broadcast`, a number from 1.0 to 1.6, default 1.25): how far the picture is zoomed in. A centre crop of `iw/zoom` by `ih/zoom` (kept 16:9 and even) is taken before the scale to 1080 wide, so 1.0 shows the whole frame, 1.25 cuts 20 % off each dimension (the broadcast's own score bug and corner graphics stay mostly in view) and 1.6 is a tight crop. Out of range is refused with `clip 1 zoom 2 is outside 1 to 1.6`; `zoom` on a clip of another layout is refused. There is no vertical offset: the crop is always centred.
- `cam` (optional, per clip, `bottom-cam` or `kit` as the clip's own layout; `kit-reaction` is refused, and `broadcast` is refused with its own message, judged on the layout the clip really uses): a list of facecam windows, `{"start": 4.0, "end": 6.5, "mode": "full", "x": 162}`, times in seconds from the clip start. Windows must lie inside the clip, be at least 0.5 s long, be in order and not overlap (touching is fine); unknown keys are refused. The cut is hard, no fades, and the audio is untouched. They are laid over the finished 4:4:4 picture with `overlay=...:format=yuv444:enable='gte(t,a)*lt(t,b)'` (the edges are snapped to the 1/60 s frame grid and sit half a frame early, so a window covers exactly its frames and touching windows never share one), so the kit picture without `cam` is byte for byte the normal `kit` one. The numbers per layout are one table, `CAM_GEOMETRY` in `render_tiktok.py`, measured on real frames.
  - `full`: the facecam replaces the game block in the same rectangle, so the scoreboard strip stays, scaled with lanczos to the size of that block and sharpened with the usual `unsharp`. The caption is hidden during a `full` window (it would sit across the face) with `enable` on the drawtext. `x` (optional, only on `full`) is the source x of the crop's left edge, an even whole number:
    - kit: x 0, y 466, a 314x316 crop of the source at y 716 scaled to 1080x1087. `x` 36 to 290 (crop inside source x 36..604), default 162, centred on the picture of the box (x 34..605).
    - bottom-cam: x 0, y 531, a 274x242 crop at y 788 scaled to 1080x956. The facecam picture is x 36..476, y 788..1030 in the source (440x242), so `x` is 36 to 202 and the default 118 is as near its centre as an even number gets.
  - `inset`: the facecam in the bottom left corner of the game block, 24 px from the left of the frame and about 24 px above the bottom of the block (the bottom edge is odd on kit and bottom-cam is 1487, so y is rounded down to an even number and the real gap is 25 px). The caption stays.
    - kit: the crop 560x316 at x 40, y 716 (as `kit-reaction`) scaled to 600x338 at x 24, y 1190.
    - bottom-cam: the whole facecam picture, 440x242 at x 36, y 788, scaled to 600x330 at x 24, y 1132.
  - Chroma on bottom-cam: the 99 px strip is an odd height, and stacked as 4:2:0 its chroma bleeds into the game block (a faint orange line on the seam, seen on a real frame). A bottom-cam clip with `cam` is therefore stacked in 4:4:4 like kit (strip, game and window blocks, then the overlays in `format=yuv444`, and one subsample at the end). A bottom-cam clip without `cam` keeps its exact 4:2:0 command. The overlay x and the inset y are even; the bottom-cam full window sits at y 531 (432 + 99), which is odd because it must cover the game block exactly, and that is safe because nothing is rounded to chroma pairs in 4:4:4.
  - `--dry-run` lists every window.
- `sfx_dir` (optional): a folder to look `sfx[].name`, `card_sound` and `crowd_bed` up in, default `tools/clips/sfx/`. Sound files that are licensed or taken from someone else's video live in such a folder outside the repo and must never be committed. Names are still bare (`[A-Za-z0-9_-]+`, no path, no extension).
- `card_sound` (optional): `{"name": "card_hit_alt", "gain_db": 0}`, the sound played at every card, from `sfx_dir`. Default is the built-in `card_hit` at 0 dB (always from `tools/clips/sfx/`). The sound is cut to the card length with a 5 ms fade; a sound shorter than the card leaves the rest of the card silent.
- `crowd_bed` (optional, default `null`): `{"name": "crowd_bed", "gain_db": -12}`, a continuous crowd under every clip and never under a card. One looped input is split per clip; each clip starts at its own point in the file (clip n starts `n * 7.3 s` in, wrapped at the file length), fades in over 0.15 s and ends with the clip's own 0.06 s fade out, so the near-silent gap before the card stays.
- `crowd_stem` (optional, default `null`): `{"path": "/abs/crowd.wav", "gain_db": 0}`, one WAV built by `make_crowd_stem.py` (see below) and mixed from video time 0, under the same limiter and both `loudnorm` passes as every other input. `gain_db` is -60 to +6 (default 0). It replaces `crowd_bed` and the crowd sfx pasted into the spec, so a spec with both `crowd_stem` and `crowd_bed` is refused. `check_sources` requires the file to exist and to be within 0.1 s of the planned total, otherwise it asks you to rebuild the stem; a `--dry-run` prints a `crowd stem` line.
- `clip_comp` (optional, default false): a light `acompressor` (threshold -24 dB, ratio 3, attack 15 ms, release 250 ms, makeup 2) on the stream audio of each clip only, before any mixing, to bring the commentary forward. The constants are at the top of the file.
- `caption`: 1 to 24 characters, no em or en dash, no control characters. `output` must end in `.mp4`. `card_seconds`: 0.4 to 1.0 (default 0.75). `opening_card`: default true. Unknown fields are rejected everywhere.
- `hook` (optional, default none): one short, strong moment placed in front of everything, a flash-forward of the best part of the video (aim for about 4 to 6.5 s). It is an object with exactly the fields of a `clips` entry (`source`, `start`, `end`, `layout`, `cam`, `sfx`) and the same checks: 1 to 75 s, inside a 1920x1080 source (or any 16:9 source of 1280x720 and up when the hook uses the `broadcast` layout), cam windows inside its own length (times from the hook's start), `sfx` inside the hook and none that would begin before time 0. Errors name it (`hook: ...`, `hook cam window 1: ...`, `hook sfx 'name': ...`). It is not counted in the 20 clips but is part of the planned total, so the 15 to 180 s guard includes it. The order is hook, card, clip 1, card, clip 2, and so on: the card after the hook is the opening card, with the usual card sound and caption. A hook is always followed by a card, so with a hook `opening_card` has no effect (a spec with `"opening_card": false` and a hook still gets the card between the hook and clip 1); without a hook `opening_card` works as before and a spec without `hook` (or with `"hook": null`) gives exactly the timeline it always did. `--dry-run` calls it `hook` in the timeline. `crowd_bed` runs under the hook like under any clip (the hook is the first of the clips in play order, so it starts at 0 s into the bed file and clip n at (n + 1) times 7.3 s).
- `clips`: 1 to 20. Each is 1 to 75 s, must end inside its source, and the source must be 1920x1080 (checked with ffprobe), or for a `broadcast` clip any 16:9 video of at least 1280x720. `start` and `end` take `HH:MM:SS.f`, `MM:SS` or plain seconds (same parser as `render_short.py`). Clip lengths snap to whole 60 fps frames.
- `sfx`: optional list per clip. `at` is seconds from the clip's start and must fall inside the clip. A negative `at` down to -1.0 starts the sound over the card in front of the clip (a riser or whoosh); one that would start before time 0 of the whole video is refused. `name` is a WAV in `tools/clips/sfx/` without the extension. `gain_db` is -30 to +6 (default 0). Optional shaping, all applied to the sound before it is delayed into place: `start` (seconds into the file, default 0), `duration` (seconds of it to play, default the rest), `fade_in` and `fade_out` (seconds, default 0; with an explicit `duration` they must fit inside it), and `stop_at_clip_end` (default false), which ends the sound exactly when its clip ends with a 0.08 s fade (or the longer `fade_out`), so a crowd roar never plays over the black card. The check that a sound may not run past the end of the video uses the trimmed length, and refuses a `start` past the end of the file or fades longer than what plays.
- `music`: `null`, or `{"path": "...", "gain_db": -16, "start": 0}`. It is looped or cut to the full length, ducked under the clip audio with `sidechaincompress`, and fades out over the last second. Music is optional and has only been checked by its argument list so far, never by a real render.
- Checked before rendering: NaN and Infinity are refused, `output` must not be (a link to) a clip source or the music file, and a sound may not run past the end of the whole video (the WAV length is read with ffprobe; the message names the clip and the sound).
- Length guard: a planned total under 15 s or over 180 s is refused before anything renders. The budget is printed first.

## Picture

Source frames are 1920x1080 (a `broadcast` clip is different, see `layout` above). The scoreboard strip (crop 1380x126 at x 270, y 0, scaled to 1080x99) is stacked over the game block (crop 948x839 at x 486, y 126, scaled to 1080x956, lanczos) and padded on black, vertically centred. That drops the facecam at bottom left and the spectator bar. The caption is Noto Sans Bold 64 px, white with a 4 px black border, centred, y 640, drawn from a text file so spec text never enters a filter string. Cards are pure black with the same caption in the same place. Opening card first (unless `opening_card` is false), one card between each pair of clips, none after the last. With a `hook`: hook, card, clip 1, card, clip 2 (the card after the hook always exists). Hard cuts, no picture fades. The crops and font are constants at the top of `render_tiktok.py`. Every clip picture ends with `setsar=1` before the caption (the scales otherwise leave a sample aspect ratio such as 253:252, or 2560:2559 for `broadcast`, and the first scene's ratio is the one the whole video gets).

## Audio

- Clip audio keeps the stream audio, with a 0.05 s fade in and a 0.06 s fade out so a near-silent gap sits just before each card.
- `card_hit.wav` plays at a constant gain (`CARD_HIT_GAIN_DB`, 0 dB for now) exactly when each card starts, the opening card included. The hit is cut to the card length with a 5 ms fade, so a short card never lets it run into the next clip, and a longer card just has silence after it.
- Every input to the final mix is resampled to 48 kHz stereo first, so 96 kHz 24-bit files mix correctly.
- Spec sfx are placed with `adelay` at absolute times. Everything is mixed with `amix normalize=0`, so adding effects never lowers the clips, then a limiter at 0.3 and `loudnorm` to I=-14, TP=-1.5 and the maximum LRA target of 50, run twice (measure, then a linear pass). The LRA target is high on purpose: loudnorm silently falls back to dynamic mode when the measured LRA is over it, and the card sound raises the LRA. The second pass reports its `normalization_type`; the renderer logs it and prints a warning unless it is `linear`. The limiter is there because the card sound peaks far above the stream audio: without it loudnorm could only reach about -16.6 LUFS. The measured result of the example render is `linear`, -14.1 LUFS with a true peak of -1.3 dBFS after AAC encoding.

## How it works

1. Each card and clip is encoded to its own `.mov` (nvenc video, PCM audio, no effects).
2. The scenes are joined with the concat demuxer, video stream-copied.
3. One ffmpeg audio pass (after a measuring pass) mixes the card sounds, effects and optional music over the concat audio and writes the final MP4 with the video copied.
4. The real duration is measured with ffprobe. If it differs from the plan by more than 0.25 s the output is deleted and the run fails. The temp dir is removed unless `--keep-temp`.

## Placeholder sounds

`sfx/make_sfx.sh` synthesises `card_hit`, `impact`, `whoosh`, `riser` and `hit` with ffmpeg generators and filters (48 kHz stereo 16-bit, peak -1 dBFS, deterministic, nothing sampled from any video) and rewrites the WAVs next to it. They are simple placeholders: judge them by ear and swap in your own through `sfx_dir` if you prefer.

## Crowd stem

`make_crowd_stem.py` builds the WAV for `crowd_stem`: a crowd that follows the commentary and swells at goals and saves, instead of one flat looped bed plus the same cheer pasted at each moment. It needs numpy (`pip install numpy`, or your distribution's package), which `python3 -I` cannot see when it is installed in the user site, so run it **without** `-I` (the renderer itself still runs with `-I`):

```
cd tools/clips && python3 make_crowd_stem.py PLAN.json
```

ffmpeg and ffprobe must be on PATH (audio goes through ffmpeg pipes as 48 kHz stereo float32). The output is a 32-bit float WAV exactly `round(total * 48000)` samples long (the planned length of the render spec), written atomically. The same seed gives the same file; it takes a few seconds. Plan format:

```json
{
  "spec": "tools/clips/tiktoks/example.json",
  "output": "/path/to/tiktoks/defense-crowd.wav",
  "sounds_dir": "/path/to/sounds/",
  "seed": 7,
  "bed": {"name": "crowd_bed", "level_db": -34, "follow_db": 6},
  "room": {"wet": 0.25, "rt60": 1.4, "highpass_hz": 150, "lowpass_hz": 6500},
  "moments": [
    {"clip": 0, "at": 10.8, "sound": "crowd_roar", "sound_start": 0.0, "level_db": -24,
     "rise": 1.2, "tail": 3.0, "bed_swell_db": 8}
  ]
}
```

- `spec`: the render spec (its clips and card times give every position; a relative path is tried from the current folder, then from the repo root). `output` ends in `.wav` and must not be (a link to) the bed, a moment sound or a clip source. `sounds_dir`: the folder the bare sound names are looked up in. `seed`: a whole number, default 0. Unknown keys are refused everywhere, with a message naming them.
- `bed`: `name`, `level_db` (-80 to 0) and `follow_db` (0 to 24, default 0). Every sound file is first scaled so its own RMS is 0 dBFS, so `level_db` is the RMS level in dBFS and comparable across files. The bed is looped with a 1 s equal-power crossfade at the seam, each clip starting at its own seeded point, with 40 ms fades at the clip edges.
- Following the commentary: the clip's own audio (the first audio stream, as the render takes `[0:a]`) is decoded from the source, the RMS of its 300 to 3000 Hz band is taken in dB, smoothed (0.3 s attack, 1.2 s release) and compared with the clip's median. At or below the median the bed gets +0 dB, at +12 dB or more it gets `follow_db`, linearly between.
- `moments`: `clip` (0 is the first clip; `"hook"` is the hook of a spec that has one, and is refused when it has none), `at` (seconds into that clip, from 0 up to the clip end), `sound`, `level_db`, and optionally `sound_start` (seconds into the file where the sound begins; the file position `sound_start + rise` is put exactly on `at`, whatever the random pitch factor; default a seeded position that leaves room for the whole moment; a `sound_start` that leaves less than 0.1 s of file after `sound_start + rise` is refused, not played as silence), `rise` (0.1 to 10, default 1.2), `tail` (0.1 to 15, default 3) and `bed_swell_db` (0 to 24, default 0). The bed gets an extra swell of `bed_swell_db`: a raised cosine rise over `rise` seconds ending at `at`, hold 0.3 s, raised cosine decay over `tail` (overlapping moments take the larger swell). The sound fades in over `rise` (raised cosine), stays full for 0.3 s after `at` and fades out over `tail` (equal power); both are cut at the clip end with an 80 ms fade. A sound whose file ends before its moment does (shorter than rise + hold + tail) is faded out over 20 ms at its last sample and a warning is printed. Each moment is resampled by a seeded random factor within plus or minus 3 % (pitch and speed together), so a repeated file never sounds identical twice.
- `room` (optional; without it the crowd stays dry): every clip's crowd is convolved (FFT) with a synthetic impulse response, decorrelated left and right exponentially decaying noise that falls 60 dB over `rt60` seconds, blended with the dry signal by `wet`, then run through second order highpass and lowpass filters. The reverb tail is cut at the clip edge with the 40 ms fade.
- `no_bed_clips` (optional, default `[]`): clip indexes (0-based, as `moments[].clip`, and `"hook"` for the hook) whose crowd bed is left out, for a clip where the commentary is a break and a crowd would be wrong. The clip's moments and room still play. Each entry must be a whole number that is a clip index, or `"hook"` when the spec has one, with no duplicates.
- Cards are digital silence: the stem is zero outside the clips. If the finished stem peaks over 0 dBFS the whole stem is scaled down and a warning names how many dB. One summary line per clip (`hook:` first, when there is one, then `clip 1:` ...) prints the bed RMS and the peak moment level. The stem takes its positions from the render spec's own timeline, so the hook and the card after it are accounted for; each clip's bed starts at its own point of the loop, numbered in play order (the hook first).

Then point the render spec at it: `"crowd_stem": {"path": "/path/to/tiktoks/defense-crowd.wav"}` and drop `crowd_bed`. The kit ships no crowd recordings: use your own (a recording you made, or a sound you are licensed to use). A plan to copy is `tiktoks/example-crowd-plan.json`. Judge the result by ear.

## Adding your own layout

The three stacked layouts are code, not configuration: each one is a set of crops of the 1920x1080 source, measured on a real frame of one overlay. If your overlay differs from the kit's pages, the quickest way to fit the renderer to it is to treat `bottom-cam` as your preset and edit its numbers:

1. Grab a frame of your recording (`ffmpeg -ss 600 -i stream.mp4 -frames:v 1 frame.png`) and measure, in source pixels, the scoreboard strip, the game area you want to keep and, if you use facecam windows, the camera picture.
2. Set `GAME_CROP` and `GAME_SIZE` (the game area, used only by `bottom-cam`) at the top of `render_tiktok.py`; a crop is `w, h, x, y`, and its aspect ratio should equal the size's so nothing stretches. `STRIP_CROP` and `STRIP_SIZE` (the scoreboard strip on top) are shared by every stacked layout including `kit`, so change them only if your scoreboard sits elsewhere in every layout you use. For facecam windows also set `BOTTOM_CAM_PICTURE`, `BOTTOM_CAM_FULL_CROP`, `BOTTOM_CAM_X_RANGE` and `BOTTOM_CAM_INSET_SIZE`. Use even numbers wherever you can: yuv420p rounds odd crops and offsets. The padding that centres the picture is worked out from the sizes.
3. Run `--dry-run`, then render a 15 s test and look at the first frame of each clip. The tests pin the current numbers (the exact filter strings such as `BOTTOM_CAM_FILTER` in `test_render_tiktok.py` and the geometry tests), so update them to the new values; they exist to catch accidental changes.

A new layout name needs one more step: add it to `LAYOUTS`, `STACK_HEIGHT` (and `CAM_GEOMETRY` for facecam windows) and give `_picture_graph` a branch that returns its filter chain, following `_bottom_cam_graph`.

## What is and is not included

The TikTok tools ship no footage and no third-party sounds. The five WAVs in `sfx/` are synthesised by `sfx/make_sfx.sh` from ffmpeg generators; nothing in them comes from any recording. The examples in `tiktoks/` use placeholder paths (`/path/to/...`) and do not render until you point them at your own recordings. Crowd recordings for the crowd stem and any music are yours to supply (the kit does not bundle any, and a sound taken from someone else's video should live outside the repository, see `sfx_dir`).

## Tests

```
cd tools/clips && python3 -I -m unittest discover -s . -p 'test_render_tiktok*.py' -v
cd tools/clips && python3 -I -m unittest          # the whole suite
```

`test_make_crowd_stem.py` needs numpy: under `-I` it skips itself, so run it with `cd tools/clips && python3 -m unittest test_make_crowd_stem -v` (about 8 s; the sounds and the commentary are synthetic arrays injected in place of ffmpeg decoding, one test round-trips a real WAV through the ffmpeg pipes). `python3 -m unittest` in `tools/clips` runs everything, `python3 -I -m unittest` runs everything but that file.

`-I` removes the current folder from the import path, so `python3 -I -m unittest test_render_tiktok` cannot find the module; use `discover` as above. The tests cover the timeline arithmetic with and without an opening card, the length guard, every validation error, the source checks (against a stub ffprobe), the exact ffmpeg argument lists for a clip, a card, the concat and the mix (with and without music), that spec text never reaches a filter string, and the new sound fields (folders, card sound, crowd bed, sfx shaping, compressor), the render flow with a fake runner (nothing is encoded), plus small real-ffmpeg tests on synthetic tones (a 96 kHz bed present in clips and silent in cards, a `stop_at_clip_end` sound gone during the card) and a one-frame drawtext check (both skipped when ffmpeg is missing): the real mix graph must keep the length and raise the level where an effect sits, and the escaped caption path must be readable for awkward folder names.
