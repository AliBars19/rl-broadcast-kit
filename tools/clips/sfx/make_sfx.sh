#!/usr/bin/env bash
# Synthesise the placeholder sound effects used by render_tiktok.py.
# Everything comes from ffmpeg generators (aevalsrc, anoisesrc) and filters: no sound is
# taken from any video. Output: 48 kHz stereo 16-bit WAV, peak -1 dBFS, deterministic
# (fixed noise seeds). They are simple placeholders: judge them by ear and swap in your own.
# Usage: tools/clips/sfx/make_sfx.sh   (rewrites the five WAVs next to this script)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FF=(ffmpeg -y -hide_banner -loglevel error -nostdin -threads 4)
PEAK_DB=-1

# finish NAME: scale TMP/NAME.f32.wav so its peak is PEAK_DB, write 48 kHz stereo s16.
finish() {
  local name="$1" max gain
  # astats reads the float file as it is (volumedetect would clip anything above 0 dBFS)
  max=$(ffmpeg -hide_banner -nostdin -threads 4 -i "$TMP/$name.wav" -af astats -f null - 2>&1 \
        | sed -n 's/.*Peak level dB: \(.*\)$/\1/p' | tail -1)
  gain=$(awk -v m="$max" -v p="$PEAK_DB" 'BEGIN { printf "%.3f", p - m }')
  "${FF[@]}" -i "$TMP/$name.wav" -af "volume=${gain}dB,pan=stereo|c0=c0|c1=c0" -ar 48000 \
    -c:a pcm_s16le "$HERE/$name.wav"
}

# card_hit: harsh steady buzz. 46 Hz sawtooth (harmonics) plus a resonant band near 650 Hz;
# starts and stops instantly apart from a 3 ms de-click.
"${FF[@]}" -f lavfi -i "aevalsrc='2*(46*t-floor(46*t))-1':s=48000:d=0.75" \
  -filter_complex "[0:a]asplit=2[a][b];[b]bandpass=f=650:width_type=q:w=7,volume=17dB[r];\
[a][r]amix=inputs=2:normalize=0,afade=t=in:d=0.003,afade=t=out:st=0.747:d=0.003" \
  -c:a pcm_f32le "$TMP/card_hit.wav"
finish card_hit

# impact: sub boom, 70 Hz falling to 35 Hz, exponential decay, plus a very short bright click.
"${FF[@]}" \
  -f lavfi -i "aevalsrc='sin(2*PI*(35*t+5.25*(1-exp(-t/0.15))))*exp(-4*t)':s=48000:d=1.0" \
  -f lavfi -i "aevalsrc='random(0)*exp(-t*300)':s=48000:d=0.02" \
  -filter_complex "[1:a]highpass=f=3000,volume=0.6[c];\
[0:a][c]amix=inputs=2:normalize=0:duration=longest,afade=t=out:st=0.98:d=0.02" \
  -c:a pcm_f32le "$TMP/impact.wav"
finish impact

# whoosh: noise swell through a rising band-pass (four bands handed over in turn), loudest
# at the end, then cut in 5 ms.
"${FF[@]}" -f lavfi -i "aevalsrc='random(0)':s=48000:d=0.5" \
  -filter_complex "[0:a]asplit=4[n1][n2][n3][n4];\
[n1]bandpass=f=350:width_type=q:w=1.5,volume='exp(-pow((t-0.05)/0.12,2))':eval=frame[b1];\
[n2]bandpass=f=900:width_type=q:w=1.5,volume='exp(-pow((t-0.20)/0.12,2))':eval=frame[b2];\
[n3]bandpass=f=2200:width_type=q:w=1.5,volume='exp(-pow((t-0.35)/0.12,2))':eval=frame[b3];\
[n4]bandpass=f=5000:width_type=q:w=1.5,volume='exp(-pow((t-0.50)/0.12,2))':eval=frame[b4];\
[b1][b2][b3][b4]amix=inputs=4:normalize=0,volume='pow(t/0.5,1.5)':eval=frame,\
afade=t=out:st=0.495:d=0.005" \
  -c:a pcm_f32le "$TMP/whoosh.wav"
finish whoosh

# riser: tone sweeping 200 Hz to 2 kHz (exponential) under a rising noise band, both growing
# to the peak at 2.0 s, where it stops in 3 ms.
"${FF[@]}" \
  -f lavfi -i "aevalsrc='0.6*sin(2*PI*173.7*(pow(10,t/2)-1))*pow(t/2,2)':s=48000:d=2.0" \
  -f lavfi -i "aevalsrc='random(0)*pow(t/2,3)':s=48000:d=2.0" \
  -filter_complex "[1:a]asplit=3[n1][n2][n3];\
[n1]bandpass=f=500:width_type=q:w=1.5,volume='exp(-pow((t-0.4)/0.5,2))':eval=frame[b1];\
[n2]bandpass=f=1800:width_type=q:w=1.5,volume='exp(-pow((t-1.2)/0.5,2))':eval=frame[b2];\
[n3]bandpass=f=5500:width_type=q:w=1.5,volume='exp(-pow((t-2.0)/0.6,2))':eval=frame[b3];\
[b1][b2][b3]amix=inputs=3:normalize=0,volume=2.5[nz];\
[0:a][nz]amix=inputs=2:normalize=0,afade=t=out:st=1.997:d=0.003" \
  -c:a pcm_f32le "$TMP/riser.wav"
finish riser

# hit: bright snappy noise burst with a fast decay plus a short falling thump.
"${FF[@]}" \
  -f lavfi -i "aevalsrc='random(0)*exp(-t*28)':s=48000:d=0.25" \
  -f lavfi -i "aevalsrc='0.7*sin(2*PI*(90*t+14*(1-exp(-t/0.03))))*exp(-t*30)':s=48000:d=0.25" \
  -filter_complex "[0:a]highpass=f=1800[h];[h][1:a]amix=inputs=2:normalize=0,\
afade=t=in:d=0.001,afade=t=out:st=0.24:d=0.01" \
  -c:a pcm_f32le "$TMP/hit.wav"
finish hit

echo "wrote $(ls "$HERE"/*.wav | wc -l) files to $HERE"
