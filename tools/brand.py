"""Brand values for the tools, read from the repo's config.js.

config.js is plain JavaScript, so it is evaluated with `node` (already required for the
scoreboard server) inside an empty vm context. If node or the file is missing the placeholder
defaults are returned, so the tools still run (they just carry the generic names).
"""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CONFIG_PATH = os.path.join(REPO, "config.js")

DEFAULTS = {
    "channel": "YOUR CHANNEL",
    "mark": "Y",
    "handle": "@yourchannel",
    "home_team": "BLUE TEAM",
    "event_name": "YOUR EVENT 26",
    "event_sub": "YOUR LEAGUE | ROCKET LEAGUE",
    "sponsor_logo": None,
}

_NODE_SNIPPET = (
    "const vm=require('vm'),fs=require('fs');"
    "const ctx={window:{}};vm.createContext(ctx);"
    "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx,{timeout:2000});"
    "process.stdout.write(JSON.stringify(ctx.window.STREAM||{}));"
)


def defaults():
    return dict(DEFAULTS)


def _handle(streams):
    handles = (streams.get("handles") or {})
    for key in ("youtube", "tiktok", "twitch"):
        value = str(handles.get(key) or "").strip()
        if value:
            return value if value.startswith("@") else "@" + value
    return DEFAULTS["handle"]


def _sponsor_logo(stream, repo):
    sponsor = stream.get("sponsor") or {}
    rel = sponsor.get("logo") or sponsor.get("lockup")
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(repo, rel)
    return os.path.normpath(path) if os.path.isfile(path) else None


def from_stream(stream, repo=REPO):
    """Brand dict from an already-parsed STREAM object (a dict)."""
    scoreboard = stream.get("scoreboard") or {}
    channel = str(stream.get("channel") or DEFAULTS["channel"]).strip()
    mark = str(stream.get("mark") or channel[:1] or DEFAULTS["mark"]).strip()[:1]
    program = stream.get("programDefaults") or {}
    return {
        "event_name": str(program.get("eventName") or DEFAULTS["event_name"]).strip(),
        "event_sub": str(program.get("eventSub") or DEFAULTS["event_sub"]).strip(),
        "channel": channel,
        "mark": mark.upper(),
        "handle": _handle(stream),
        "home_team": str((scoreboard.get("blue") or {}).get("name") or DEFAULTS["home_team"]).strip(),
        "sponsor_logo": _sponsor_logo(stream, repo),
    }


def load_brand(config_path=CONFIG_PATH, run=subprocess.run):
    """Read config.js with node; fall back to DEFAULTS when that is not possible."""
    try:
        done = run(["node", "-e", _NODE_SNIPPET, config_path], capture_output=True,
                   text=True, timeout=10, check=True)
        stream = json.loads(done.stdout)
        if not isinstance(stream, dict):
            return defaults()
        return from_stream(stream, os.path.dirname(os.path.abspath(config_path)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return defaults()
