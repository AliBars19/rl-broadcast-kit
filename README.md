# RL Broadcast Kit

Broadcast-style overlays and small tools for streaming Rocket League with OBS: starting, break and BRB screens, a live scoreboard with a control dock, boost panels, lower thirds, an Up Next bar, a stinger and intro renderer, and a clip finder. Everything is plain HTML, CSS and JavaScript (1920x1080 browser sources) plus a few dependency-free Node and Python helpers. Brand, text and an optional sponsor all come from one file, `config.js`.

> Unofficial, fan-made project, not affiliated with or endorsed by Psyonix or Epic Games. Rocket League is a trademark of its owner. This kit is made for use with Rocket League and ships no game assets, logos or footage.

<!-- screenshots go here -->

## Status

Built and run on Fedora Linux with the Flatpak build of OBS. The pages themselves are ordinary HTML and are cross-platform, but only Linux has been tested. The helpers use only the Node and Python standard libraries, but Windows and macOS are untested. See [docs/LINUX.md](docs/LINUX.md) for the Linux notes.

## Requirements

| Needed for | Requirement |
|---|---|
| Everything | OBS Studio 30 or newer (browser sources, custom browser docks, obs-websocket 5) |
| Scoreboard server, relay, tests | Node.js 18 or newer (22 recommended) |
| Tools and tests | Python 3.10 or newer |
| Intro, stinger, Shorts renderers | `ffmpeg` and `ffprobe` on PATH, Google Chrome (`google-chrome` on PATH) |
| GPU encodes | The renderers call `h264_nvenc` (NVIDIA). See `tools/program/README.md` for switching to `libx264` |

Fonts (Chakra Petch, IBM Plex Mono, IBM Plex Sans) are bundled in `fonts/`, so the pages work offline.

## Quick start

```sh
git clone <this repository> rl-broadcast-kit
cd rl-broadcast-kit

# 1. Put your own names in the config (see "Config reference")
$EDITOR config.js

# 2. Start the scoreboard server and its control panel (localhost only, port 49125)
node scoreboard-server.js
#    Use SB_PORT=49200 node scoreboard-server.js to pick another port.

# 3. Optional: the Rocket League Stats API relay for boost panels and live team names
node rl-relay.js

# 4. Check everything
node --test
```

Then add the pages to OBS (next section). `scoreboard-state.json` is created by the server at runtime and is git-ignored.

To keep the helpers running in the background on Linux, copy the templates in `systemd/` (`*.service.example`) to `~/.config/systemd/user/`, replace `/path/to/rl-broadcast-kit` with the real folder and run `systemctl --user enable --now <name>`.

## Adding the pages to OBS

Add each page as a **Browser Source** with **Local file** ticked, width **1920**, height **1080**, FPS 60. Tick **Shutdown source when not visible** and **Refresh browser when scene becomes active** so entrance animations replay and countdowns restart on every scene switch. Position every source at 0, 0.

| Page | Type | What it is |
|---|---|---|
| `starting.html` | full screen | Simple Starting Soon countdown (`?min=10` overrides the minutes) |
| `program/countdown.html` | full screen | Program-style Starting Soon: event wordmark, big countdown, FIRST UP line, promo cards. Follows the dock timer |
| `program/upnext.html` | full screen | Between-games screen with one bar: brand mark, NEXT UP matchup, BACK IN timer or GAME N, headline |
| `program/brb.html` | full screen | Be Right Back with a count-up "away for" timer and promo cards |
| `program/facecam.html` | full screen | Camera scene with a large camera slot and a series card. Camera slot: x 56, y 153, 1376x774 (put the camera source above this page) |
| `program/bg.html` | full screen | The shared animated background alone |
| `brb.html`, `ending.html` | full screen | Simple BRB and GG screens |
| `scoreboard.html` | transparent | Team bars either side of Rocket League's own score boxes. `?demo=1` shows sample data |
| `boost.html` | transparent | Player boost panels from the Stats API relay. `?demo=1` shows sample data |
| `stream-info.html` | transparent | Top-left bar: live dot, name, game, rotating handles |
| `lower-third.html` | transparent | Slides in after 4 s, repeats on a timer. `?always=1` pins it |
| `webcam-frame.html` | transparent | Corner brackets for a 576x324 camera at x 32, y 711 |
| `program/lower-bar.html` | transparent | Optional bottom bar for a facecam scene (brand, FIRST UP, countdown, headline) |

The **Custom Browser Dock** for scoreboard control: in OBS open Docks, Custom Browser Docks, name it "Scoreboard" and use the URL `http://127.0.0.1:49125/`. The same page opens in any browser on the same PC.

Pages that talk to the server accept `?sb=http://127.0.0.1:<port>/events` if you moved it off 49125.

### Rocket League safe zones

The game draws the scoreboard top-centre and the boost meter bottom-right, so the overlays keep to the left side. The scoreboard overlay puts the team of the player named in `boost.me` on the left bar, as the game does. Test the sides with `?me=orange` or `?me=blue`.

## Scene flow suggestion

| Scene | Sources, top to bottom |
|---|---|
| Starting Soon | `program/countdown.html` |
| Gameplay | game capture, `scoreboard.html`, `boost.html`, `stream-info.html`, `lower-third.html`, camera, `webcam-frame.html` |
| Up Next (between games) | `program/upnext.html` |
| Ad Break (optional) | one Media Source with your own ad video |
| Intro (optional) | one Media Source with your intro video |
| Facecam | camera source above `program/facecam.html` |
| BRB | `program/brb.html` |
| Ending | `ending.html` |

A typical match night: Starting Soon, then Intro, Gameplay for each game, Up Next between games (set the BACK IN timer from the dock), Facecam for chat, BRB when away, Ending.

`tools/obs_ad_return.py` (see Tools) can switch the scene back automatically when the ad or intro video finishes. The stinger transition is rendered by `tools/program/render_stinger.py`; add it in OBS as a **Stinger** transition with transition point 600 ms (details in `program/stinger/README.md`).

## Scoreboard control

The server in `scoreboard-server.js` keeps the series state (wins, best-of, series text and the program fields) and streams it to every page. The dock panel has team win buttons, Best of 3/5/7, the series text, Swap and Reset (tap twice), plus a Program section: countdown buttons (2, 3, 5, 10 minutes or custom), stage, Up Next headline and subline, and the two FIRST UP names.

Keyboard in the dock: `1` / `2` blue / orange +1, `Shift+1` / `Shift+2` -1, `3` `5` `7` best of, `S` swap, `R` reset (twice), `T` edit the series text.

HTTP API (localhost only): `GET /state`, `GET /events` (Server-Sent Events), `POST /cmd` with JSON `{"action": ..., "value": ...}`. Actions: `blue+`, `blue-`, `orange+`, `orange-`, `bestOf`, `reset`, `tab`, `swap`, `countdown` (seconds), `countdownClear`, `program` (object). If the server is not running, every page falls back to what `config.js` says.

## Config reference

Everything lives in `config.js` (loaded by every page, and by the tools through `tools/brand.py`).

| Key | Meaning |
|---|---|
| `name` | Your display name (lower third, stream info) |
| `channel` | Channel or brand name: Up Next bar, lower bar, intro credit, stinger, Shorts tag |
| `mark` | One letter for the square brand mark |
| `tagline`, `event`, `game` | Lower third subtitle, top-left label on simple screens, game name |
| `handles` | `twitch`, `youtube`, `tiktok`. An empty string hides that platform everywhere |
| `startingMinutes`, `nextStream`, `ticker` | Simple countdown length, Ending text, scrolling lines |
| `lowerThird` | `showSeconds`, `everyMinutes` (0 = once) |
| `boost` | `me` is your in-game name (picks the left side); `relay` is the relay WebSocket address |
| `infoRotateSeconds` | How often the stream-info handle rotates |
| `scoreboard` | `blue` / `orange` with `name`, `abbr`, `wins`; `autoNames`, `bestOf`, `tab`, `idleText` |
| `sponsor` | **Optional.** `null` removes every PRESENTED BY spot. Otherwise `{ name, logo, lockup }` with image paths relative to the repo root |
| `promos` | Cards on the program screens: `tag`, `text`, `sub`, optional `logo` |
| `programDefaults` | `stage`, `minutes`, `eventName` (last word is drawn in amber), `eventSub` (`A \| B` is drawn with a bar) |

### Optional sponsor

Leave `sponsor: null` and the PRESENTED BY label, logo and spacing are removed cleanly from the Starting Soon, Up Next, BRB and Facecam screens, the lower bar and the intro end title. To use one, drop your own logo into `program/assets/` (a light logo reads best on the blue field) and set, for example:

```js
sponsor: { name: "Sponsor Name", logo: "program/assets/sponsor.svg" },
```

`lockup` is an optional larger version for the big spots; it falls back to `logo`. With only a `name` the sponsor is shown as text.

## Tools

| Tool | What it does |
|---|---|
| `tools/obsws.py` | Tiny OBS WebSocket v5 command-line client (standard library only) |
| `tools/obs_ad_return.py` | Switches the OBS scene when an ad or intro media source ends |
| `tools/program/` | Stinger renderer, intro builder and end-title compositor. See `tools/program/README.md` |
| `tools/clips/` | Highlight finder, goal detector, feed-event detector and the vertical Shorts renderer. See `tools/clips/README.md` |

### OBS WebSocket password

Never commit it. `tools/obsws.py` and `tools/obs_ad_return.py` read it at runtime, first match wins:

1. the `OBS_WEBSOCKET_PASSWORD` environment variable;
2. the first line of the file named by `OBS_WEBSOCKET_PASSWORD_FILE`, or of `.obs-password` in the repository root (git-ignored; create it yourself with `chmod 600`);
3. the `server_password` field of the obs-websocket `config.json` named by `OBS_WEBSOCKET_CONFIG`.

Enable the server in OBS under Tools, WebSocket Server Settings. It listens on port 4455. Switch it off when you do not need it.

```sh
echo 'your-password' > .obs-password && chmod 600 .obs-password
python3 -I tools/obsws.py GetVersion
python3 -I tools/obs_ad_return.py --rule "Ad Break" "Ad - Sponsor" "Up Next"
```

## Rocket League Stats API

`boost.html` and the live team names use the game's Stats API through `rl-relay.js`, which turns it into a WebSocket for browser sources. To enable the Stats API and read about its settings, see the game's own page: <https://www.rocketleague.com/en/developer/stats-api>. The relay only listens on 127.0.0.1. Ports can be changed with the `RL_PORT` and `WS_PORT` environment variables. Boost for opponents is only available when the game exposes it (for example in replays or when spectating); during live play the panels fall back to name tags.

## Tests

```sh
node --test                                          # scoreboard server
(cd tools && python3 -I -m unittest discover)        # OBS helpers and brand loader
(cd tools/program && python3 -I -m unittest discover)
(cd tools/clips && python3 -I -m unittest discover)
```

No test starts OBS, Chrome or ffmpeg.

## Licence and credits

Code is MIT licensed, see [LICENSE](LICENSE). Bundled fonts are under the SIL Open Font License, see [THIRD-PARTY.md](THIRD-PARTY.md). Not affiliated with or endorsed by Psyonix or Epic Games. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue.
