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
