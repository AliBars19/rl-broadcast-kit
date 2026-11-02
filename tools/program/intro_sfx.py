"""Synthesised sound effects for the intro (ffmpeg lavfi only, no samples, no music).

Each effect is a filter chain that starts at a lavfi source and ends delayed to its place on
the intro timeline. `sfx_command` mixes all of them into one stereo WAV the length of the intro.
"""
from chrome_frames import ToolError

RATE = 48000
RISER_SECONDS = 1.8
FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
STEREO = "aformat=channel_layouts=stereo"

# Each value is the source plus its filters; chain_for() appends the delay.
_CHAINS = {
    # filtered pink noise: fast swell into the cut, longer decay, band sweeping upward
    "whoosh": (
        f"anoisesrc=d=0.6:c=pink:r={RATE}:a=0.8,"
        "asendcmd=c='0.0 lowpass frequency 900;0.12 lowpass frequency 3200;0.35 lowpass frequency 7000',"
        "lowpass=f=900:p=1,highpass=f=220,"
        "afade=t=in:st=0:d=0.12:curve=qsin,afade=t=out:st=0.12:d=0.48:curve=exp,volume=10dB," + STEREO),
    # low sine with a quick pitch drop plus a short noise crack, long decay
    "boom": (
        f"aevalsrc='0.95*sin(2*PI*(38*t+9*(1-exp(-10*t))))*exp(-2.6*t)"
        f"+0.35*(random(0)*2-1)*exp(-22*t)':d=1.6:s={RATE},lowpass=f=1800," + STEREO),
    # lighter, shorter version of the boom
    "hit": (
        f"aevalsrc='0.8*sin(2*PI*(70*t+5*(1-exp(-18*t))))*exp(-8*t)"
        f"+0.25*(random(0)*2-1)*exp(-40*t)':d=0.6:s={RATE},lowpass=f=3000," + STEREO),
    # rising sine plus noise, amplitude growing with t squared
    "riser": (
        f"aevalsrc='(0.45*sin(2*PI*(180*t+700*t*t))+0.22*(random(0)*2-1))*pow(t/{RISER_SECONDS},2)'"
        f":d={RISER_SECONDS}:s={RATE},highpass=f=120,afade=t=out:st={RISER_SECONDS - 0.08:.2f}:d=0.08," + STEREO),
}


def chain_for(kind, time_s):
    """Filter chain for one effect placed `time_s` seconds into the intro (clamped at 0)."""
    if kind not in _CHAINS:
        raise ToolError(f"unknown sound effect {kind!r}: use one of {', '.join(sorted(_CHAINS))}")
    ms = max(0, int(round(time_s * 1000)))
    return f"{_CHAINS[kind]},adelay={ms}|{ms}"


def sfx_command(events, total_s, out_path):
    """One WAV of `total_s` seconds with every (time, kind) event mixed in."""
    secs = f"{total_s:.3f}"
    if not events:
        return [*FFMPEG, "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo", "-t", secs,
                "-c:a", "pcm_s16le", out_path]
    chains = [f"{chain_for(kind, t)}[s{i}]" for i, (t, kind) in enumerate(events)]
    inputs = "".join(f"[s{i}]" for i in range(len(events)))
    mix = (f"{inputs}amix=inputs={len(events)}:normalize=0,alimiter=limit=0.95,"
           f"atrim=end={secs},apad=whole_dur={secs}[a]")
    return [*FFMPEG, "-filter_complex", ";".join([*chains, mix]), "-map", "[a]", "-t", secs,
            "-ar", str(RATE), "-c:a", "pcm_s16le", out_path]
