#!/bin/bash
# Tests for tools/phonecam/phone-cam using fake adb, ffmpeg and scrcpy. No phone, UDP or OBS needed.
# Each case gets its own temp folder (XDG_RUNTIME_DIR, fake tools, SCRCPY_DIR). Only pids the
# test or the supervisor started are signalled, never by process name.
#
# Run: bash tools/test_phone_cam.sh        (takes about 20 s)

set -u
export PHONE_CAM_MODE=scrcpy   # these tests cover the scrcpy fallback path; see test_phone_cam_app.sh for the app mode

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PC="$HERE/phonecam/phone-cam"
ROOT=$(mktemp -d)
PASS=0
FAIL=0
CLEAN_PIDS=()

cleanup() {
    local d pid
    for d in "$ROOT"/*/run; do
        [ -d "$d" ] && XDG_RUNTIME_DIR="$d" SCRCPY_DIR="$d/../scrcpy" "$PC" stop >/dev/null 2>&1
    done
    for pid in "${CLEAN_PIDS[@]}"; do kill -KILL "$pid" 2>/dev/null; done
    rm -rf "$ROOT"
}
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }
check() { if "${@:2}"; then ok "$1"; else bad "$1"; fi; }

alive() { kill -0 "$1" 2>/dev/null; }
dead()  { ! kill -0 "$1" 2>/dev/null; }
lines() { [ -f "$1" ] && wc -l <"$1" || echo 0; }

# until SECONDS CMD...: poll CMD every 0.1 s until it succeeds or SECONDS pass.
until_ok() {
    local n=$(( $1 * 10 )); shift
    while [ "$n" -gt 0 ]; do "$@" && return 0; sleep 0.1; n=$((n - 1)); done
    return 1
}

# new_env NAME: fresh sandbox with fake tools; exports XDG_RUNTIME_DIR, SCRCPY_DIR, PATH.
new_env() {
    T="$ROOT/$1"
    mkdir -p "$T/bin" "$T/scrcpy" "$T/run"
    echo device >"$T/adb_state"
    cat >"$T/bin/adb" <<EOF
#!/bin/bash
echo "\$*" >>"$T/adb_calls"
if [ "\${1:-}" = get-state ]; then
    s=\$(cat "$T/adb_state"); echo "\$s"; [ "\$s" = device ]; exit
fi
exit 0
EOF
    printf '#!/bin/bash\necho $$ >>"%s/readers"\nexec sleep 1000\n' "$T" >"$T/bin/ffmpeg"
    printf '#!/bin/bash\necho $$ >>"%s/writers"\nexec sleep 1000\n' "$T" >"$T/scrcpy/scrcpy"
    chmod +x "$T/bin/adb" "$T/bin/ffmpeg" "$T/scrcpy/scrcpy"
    export XDG_RUNTIME_DIR="$T/run" SCRCPY_DIR="$T/scrcpy"
    export PATH="$T/bin:$ORIG_PATH"
    R="$T/readers"; W="$T/writers"
}
ORIG_PATH=$PATH

last() { tail -n 1 "$1" 2>/dev/null; }
both_started() { [ "$(lines "$R")" -ge "$1" ] && [ "$(lines "$W")" -ge "$1" ]; }
fifo_gone() { [ ! -e "$XDG_RUNTIME_DIR/phone-cam.fifo" ]; }
pidfile_gone() { [ ! -e "$XDG_RUNTIME_DIR/phone-cam.pid" ]; }

echo "single instance"
new_env single
export PHONECAM_FREE_CAMERA_PKGS="com.example.camapp"
"$PC" >/dev/null
until_ok 5 both_started 1
sup=$(cat "$XDG_RUNTIME_DIR/phone-cam.pid")
out=$("$PC"); rc=$?
check "second start exits 0" [ "$rc" = 0 ]
check "second start says already running" grep -q "already running" <<<"$out"
out=$("$PC" --foreground); rc=$?
check "second --foreground exits 0" [ "$rc" = 0 ]
sleep 1
check "one reader and one writer only" [ "$(lines "$R")$(lines "$W")" = 11 ]
check "supervisor still the same" [ "$(cat "$XDG_RUNTIME_DIR/phone-cam.pid")" = "$sup" ]
check "status says running" grep -q "^running (supervisor pid $sup)" <<<"$("$PC" status)"
check "force-stop called once, for the configured package" grep -qx "shell am force-stop com.example.camapp" "$T/adb_calls"
check "force-stop called once" [ "$(grep -c force-stop "$T/adb_calls")" = 1 ]
"$PC" stop >/dev/null
unset PHONECAM_FREE_CAMERA_PKGS

echo "no package is force-stopped by default"
new_env nostop
"$PC" >/dev/null
until_ok 5 both_started 1
check "no force-stop without PHONECAM_FREE_CAMERA_PKGS" [ "$(grep -c force-stop "$T/adb_calls")" = 0 ]
"$PC" stop >/dev/null

echo "scrcpy from PATH when SCRCPY_DIR is unset"
new_env onpath
cp "$T/scrcpy/scrcpy" "$T/bin/scrcpy"
unset SCRCPY_DIR
"$PC" >/dev/null
check "scrcpy found on PATH and started" until_ok 5 both_started 1
"$PC" stop >/dev/null

echo "writer exit restarts both"
new_env writer
"$PC" >/dev/null
until_ok 5 both_started 1
r1=$(last "$R"); w1=$(last "$W")
kill "$w1"
check "both restarted" until_ok 8 both_started 2
check "old reader gone" dead "$r1"
check "old writer gone" dead "$w1"
check "new pair alive" alive "$(last "$R")"
check "new writer alive" alive "$(last "$W")"
check "log names the writer" grep -q "writer (scrcpy) exited" "$XDG_RUNTIME_DIR/phone-cam.log"
"$PC" stop >/dev/null

echo "reader exit restarts both"
new_env reader
"$PC" >/dev/null
until_ok 5 both_started 1
r1=$(last "$R"); w1=$(last "$W")
kill "$r1"
check "both restarted" until_ok 8 both_started 2
check "old reader gone" dead "$r1"
check "old writer gone" dead "$w1"
check "new reader alive" alive "$(last "$R")"
check "new writer alive" alive "$(last "$W")"
check "log names the reader" grep -q "reader (ffmpeg) exited" "$XDG_RUNTIME_DIR/phone-cam.log"
"$PC" stop >/dev/null

echo "adb not ready waits, then starts"
new_env adb
echo unauthorized >"$T/adb_state"
"$PC" >/dev/null
sleep 4
check "nothing started while waiting" [ "$(lines "$R")$(lines "$W")" = 00 ]
check "waiting logged once" [ "$(grep -c 'waiting for phone' "$XDG_RUNTIME_DIR/phone-cam.log")" = 1 ]
check "adb polled more than once" [ "$(grep -c get-state "$T/adb_calls")" -ge 2 ]
echo device >"$T/adb_state"
check "starts once the phone is ready" until_ok 6 both_started 1
"$PC" stop >/dev/null

echo "stop leaves unrelated ffmpeg alone"
new_env stop
mkdir "$T/other"
cp "$(command -v sleep)" "$T/other/ffmpeg"
"$T/other/ffmpeg" 300 &
other=$!
CLEAN_PIDS+=("$other")
"$PC" >/dev/null
until_ok 5 both_started 1
sup=$(cat "$XDG_RUNTIME_DIR/phone-cam.pid"); r1=$(last "$R"); w1=$(last "$W")
out=$("$PC" stop); rc=$?
check "stop exits 0" [ "$rc" = 0 ]
check "supervisor gone" dead "$sup"
check "reader gone" dead "$r1"
check "writer gone" dead "$w1"
check "unrelated ffmpeg alive" alive "$other"
check "fifo removed" fifo_gone
check "pid file removed" pidfile_gone
check "status says stopped" grep -q "^stopped" <<<"$("$PC" status)"
out=$("$PC" stop); rc=$?
check "stop when stopped exits 0" [ "$rc" = 0 ]
kill "$other"

echo "SIGTERM cleanup"
new_env term
"$PC" --foreground &
sup=$!
CLEAN_PIDS+=("$sup")
until_ok 5 both_started 1
r1=$(last "$R"); w1=$(last "$W")
kill -TERM "$sup"
wait "$sup"; rc=$?
check "supervisor exits 0" [ "$rc" = 0 ]
check "reader gone" dead "$r1"
check "writer gone" dead "$w1"
check "fifo removed" fifo_gone
check "pid file removed" pidfile_gone
check "log says stopped" grep -q "supervisor stopped" "$XDG_RUNTIME_DIR/phone-cam.log"

echo "missing scrcpy"
new_env missing
out=$(SCRCPY_DIR="$T/nowhere" "$PC" 2>&1); rc=$?
check "exits 1" [ "$rc" = 1 ]
check "names scrcpy" grep -q "scrcpy missing" <<<"$out"
check "touched nothing" pidfile_gone

echo "no scrcpy anywhere"
new_env nopath
rm -f "$T/bin/scrcpy"
if PATH="$T/bin:/usr/bin:/bin" command -v scrcpy >/dev/null 2>&1; then
    echo "  skip scrcpy is installed in /usr/bin on this machine"
else
    out=$(env -u SCRCPY_DIR PATH="$T/bin:/usr/bin:/bin" "$PC" 2>&1); rc=$?
    check "exits 1" [ "$rc" = 1 ]
    check "says scrcpy is not on PATH" grep -q "scrcpy not found on PATH" <<<"$out"
    check "touched nothing" pidfile_gone
fi

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" = 0 ]
