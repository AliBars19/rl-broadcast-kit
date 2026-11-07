#!/bin/bash
# Tests for the app mode of tools/phonecam/phone-cam with a fake adb and fake relay, daemon and CLI.
# No phone, USB, OBS or camera needed. Only pids the test or the supervisor started are signalled.
#
# Run: bash tools/test_phone_cam_app.sh        (takes about 30 s)

set -u
unset PHONE_CAM_MODE

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PC="$HERE/phonecam/phone-cam"
ROOT=$(mktemp -d)
PASS=0
FAIL=0
CLEAN_PIDS=()

cleanup() {
    local d pid
    for d in "$ROOT"/*/run; do
        [ -d "$d" ] && XDG_RUNTIME_DIR="$d" PHONECAM_DIR="$d/../tools" "$PC" stop >/dev/null 2>&1
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
last() { tail -n 1 "$1" 2>/dev/null; }
until_ok() {
    local n=$(( $1 * 10 )); shift
    while [ "$n" -gt 0 ]; do "$@" && return 0; sleep 0.1; n=$((n - 1)); done
    return 1
}

# new_env NAME: sandbox with a fake adb and fake tools; exports XDG_RUNTIME_DIR, PHONECAM_DIR, PATH.
new_env() {
    T="$ROOT/$1"
    mkdir -p "$T/bin" "$T/tools" "$T/run" "$T/state"
    echo device >"$T/adb_state"
    echo yes >"$T/app_up"
    cat >"$T/bin/adb" <<EOF2
#!/bin/bash
if [ "\${1:-}" = devices ]; then printf 'List of devices attached\nOTHERDEV\tdevice\nFAKESER\tdevice\n'; exit 0; fi
if [ "\${1:-}" = -s ] && [ "\${2:-}" = OTHERDEV ]; then echo "\$*" >>"$T/adb_otherdev"; cat >/dev/null; exit 0; fi
if [ "\${1:-}" = -s ] && [ "\${2:-}" = FAKESER ]; then shift 2; else echo "\$*" >>"$T/adb_noserial"; fi
echo "\$*" >>"$T/adb_calls"
case "\$1" in
    get-state) s=\$(cat "$T/adb_state"); echo "\$s"; [ "\$s" = device ]; exit ;;
esac
case "\$*" in
    *"pm list packages"*) cat >/dev/null; echo "package:io.github.rlbroadcastkit.phonecam" ;;
    forward*) [ -e "$T/forward_fails" ] && exit 1 ;;
    *pidof*) [ "\$(cat "$T/app_up")" = yes ] && echo 4242 ;;
    *status.json*)
        c=\$(cat "$T/clock" 2>/dev/null || echo 1000); echo \$((c + 1500000)) >"$T/clock"
        if [ -e "$T/rec_fps" ]; then echo '{"running":true,"recording":true,"frames":0,"fps":59,"phone_clock_us":'\$c'}'
        elif [ -e "$T/rec_stuck" ]; then echo '{"running":true,"recording":true,"frames":500,"fps":0,"phone_clock_us":'\$c'}'
        elif [ -e "$T/rec_frozen" ]; then echo '{"running":true,"recording":true,"frames":500,"fps":59,"phone_clock_us":1000}'
        elif [ -e "$T/rec_busy" ]; then n=\$(cat "$T/frames" 2>/dev/null || echo 0); echo \$((n + 100)) >"$T/frames"
            echo '{"running":true,"recording":true,"frames":'\$n',"phone_clock_us":'\$c'}'
        else [ "\$(cat "$T/app_up")" = yes ] && echo '{"running":true,"recording":false,"fps":60.0}'; fi ;;
    *LaunchActivity*) echo yes >"$T/app_up" ;;
esac
exit 0
EOF2
    echo run >"$T/relay_mode"
    printf '#!/usr/bin/env python3\nimport os,sys,time\nopen("%s/relays","a").write(str(os.getpid())+"\\n")\nif open("%s/relay_mode").read().strip()=="fail": sys.exit(3)\ntime.sleep(1000)\n' "$T" "$T" >"$T/tools/live_relay.py"
    printf '#!/usr/bin/env python3\nimport os,sys,time\nopen("%s/daemons","a").write(str(os.getpid())+"\\n")\nwhile True:\n    print("daemon alive", flush=True)\n    time.sleep(0.3)\n' "$T" >"$T/tools/daemon.py"
    printf '#!/usr/bin/env python3\nimport sys\nopen("%s/tool_args","a").write(" ".join(sys.argv[1:])+"\\n")\n' "$T" >"$T/tools/phonecam.py"
    chmod +x "$T/bin/adb"
    export XDG_RUNTIME_DIR="$T/run" PHONECAM_DIR="$T/tools" PHONECAM_STATE="$T/state"
    export PATH="$T/bin:$ORIG_PATH"
    RL="$T/relays"; DM="$T/daemons"
}
ORIG_PATH=$PATH
both_started() { [ "$(lines "$RL")" -ge "$1" ] && [ "$(lines "$DM")" -ge "$1" ]; }
relays_ge() { [ "$(lines "$RL")" -ge "$1" ]; }
daemons_ge() { [ "$(lines "$DM")" -ge "$1" ]; }
pidfile_gone() { [ ! -e "$XDG_RUNTIME_DIR/phone-cam.pid" ]; }

echo "app mode starts relay and daemon once"
new_env basic
"$PC" >/dev/null
until_ok 6 both_started 1
sup=$(cat "$XDG_RUNTIME_DIR/phone-cam.pid")
sleep 1
check "one relay and one daemon" [ "$(lines "$RL")$(lines "$DM")" = 11 ]
check "adb forward for the live port" grep -q "^forward tcp:49401 tcp:49401" "$T/adb_calls"
check "app already up: not launched" [ "$(grep -c LaunchActivity "$T/adb_calls")" = 0 ]
st=$("$PC" status)
check "status first line says running" grep -q "^running (supervisor pid $sup)" <<<"$st"
check "status names the mode" grep -q "^mode: app" <<<"$st"
check "status shows the app state" grep -q '^app: {"running":true' <<<"$st"
r1=$(last "$RL"); d1=$(last "$DM")
out=$("$PC" stop); rc=$?
check "stop exits 0" [ "$rc" = 0 ]
check "relay gone" dead "$r1"
check "daemon gone" dead "$d1"
check "pid file removed" pidfile_gone
check "stop tells the app to stop" grep -q "startservice.*io.github.rlbroadcastkit.phonecam.STOP" "$T/adb_calls"

echo "ports are configurable"
new_env ports
export PHONECAM_LIVE_PORT=50401 PHONECAM_UDP_PORT=50400
"$PC" >/dev/null
until_ok 6 both_started 1
check "adb forward uses the configured local port and the app's fixed port" grep -q "^forward tcp:50401 tcp:49401" "$T/adb_calls"
"$PC" stop >/dev/null
unset PHONECAM_LIVE_PORT PHONECAM_UDP_PORT

echo "relay exit restarts the relay only"
new_env relay
"$PC" >/dev/null
until_ok 6 both_started 1
r1=$(last "$RL"); d1=$(last "$DM")
kill "$r1"
check "relay restarted" until_ok 10 relays_ge 2
check "daemon untouched" alive "$d1"
check "new relay alive" alive "$(last "$RL")"
check "forward re-run" [ "$(grep -c '^forward' "$T/adb_calls")" -ge 2 ]
"$PC" stop >/dev/null

echo "daemon exit restarts the daemon only"
new_env daemon
"$PC" >/dev/null
until_ok 6 both_started 1
r1=$(last "$RL"); d1=$(last "$DM")
kill "$d1"
check "daemon restarted" until_ok 10 daemons_ge 2
check "relay untouched" alive "$r1"
"$PC" stop >/dev/null

echo "app not running is launched"
new_env launch
echo no >"$T/app_up"
"$PC" >/dev/null
check "relay starts after the launch" until_ok 10 relays_ge 1
check "launched through the activity" grep -q "am start -n io.github.rlbroadcastkit.phonecam/.LaunchActivity" "$T/adb_calls"
"$PC" stop >/dev/null

echo "unplug and replug"
new_env replug
"$PC" >/dev/null
until_ok 6 both_started 1
echo offline >"$T/adb_state"
kill "$(last "$RL")"
sleep 4
check "no second relay while unplugged" [ "$(lines "$RL")" = 1 ]
check "waiting logged" grep -q "waiting for phone" "$XDG_RUNTIME_DIR/phone-cam.log"
echo device >"$T/adb_state"
check "relay returns after replug" until_ok 10 relays_ge 2
"$PC" stop >/dev/null

echo "every adb call names the device"
new_env serial
"$PC" >/dev/null
until_ok 6 both_started 1
sleep 1
"$PC" stop >/dev/null
check "adb was called" [ -s "$T/adb_calls" ]
check "no call without -s FAKESER" [ ! -e "$T/adb_noserial" ]
check "the device without the app was only asked about packages" [ "$(grep -vc 'pm list packages' "$T/adb_otherdev")" = 0 ]

echo "log trimming keeps the daemon's output"
new_env trim
for i in $(seq 1 700); do echo "old line $i"; done >"$XDG_RUNTIME_DIR/phone-cam.log"
"$PC" >/dev/null
until_ok 6 both_started 1
r1=$(last "$RL")
kill "$r1"                         # next attempt trims the log
until_ok 10 relays_ge 2
sleep 1
n1=$(grep -c "daemon alive" "$XDG_RUNTIME_DIR/phone-cam.log")
sleep 1.5
n2=$(grep -c "daemon alive" "$XDG_RUNTIME_DIR/phone-cam.log")
check "the log was trimmed" [ "$(grep -c 'old line' "$XDG_RUNTIME_DIR/phone-cam.log")" -le 500 ]
check "daemon lines keep arriving after the trim" [ "$n2" -gt "$n1" ]
"$PC" stop >/dev/null

echo "a relay that keeps failing restarts the phone app"
new_env fails
echo fail >"$T/relay_mode"
"$PC" >/dev/null
check "app is told to stop after repeated relay failures" until_ok 40 grep -q "startservice.*io.github.rlbroadcastkit.phonecam.STOP" "$T/adb_calls"
check "log says why" grep -q "relay failed 3 times in a row" "$XDG_RUNTIME_DIR/phone-cam.log"
"$PC" stop >/dev/null

echo "a failing relay does not stop an app that is recording"
new_env busy
echo fail >"$T/relay_mode"
touch "$T/rec_busy"
"$PC" >/dev/null
check "the supervisor notices and leaves the app alone" until_ok 60 grep -q "leaving the app alone" "$XDG_RUNTIME_DIR/phone-cam.log"
check "no STOP intent was sent" [ "$(grep -c 'startservice.*STOP' "$T/adb_calls")" = 0 ]
"$PC" stop >/dev/null

echo "a recording app that just restarted its camera (frames 0, fps live) is left alone"
new_env restarted
echo fail >"$T/relay_mode"
touch "$T/rec_fps"
"$PC" >/dev/null
check "the supervisor leaves it alone" until_ok 60 grep -q "leaving the app alone" "$XDG_RUNTIME_DIR/phone-cam.log"
check "no STOP intent was sent" [ "$(grep -c 'startservice.*STOP' "$T/adb_calls")" = 0 ]
"$PC" stop >/dev/null

echo "an app that says recording but is stuck (no frames, no fps) is restarted"
new_env stuck
echo fail >"$T/relay_mode"
touch "$T/rec_stuck"
"$PC" >/dev/null
check "a STOP intent is sent" until_ok 60 grep -q "startservice.*STOP" "$T/adb_calls"
"$PC" stop >/dev/null

echo "a frozen status file (clock not advancing) is not a live recording"
new_env frozen
echo fail >"$T/relay_mode"
touch "$T/rec_frozen"
"$PC" >/dev/null
check "a STOP intent is sent to the wedged app" until_ok 80 grep -q "startservice.*STOP" "$T/adb_calls"
check "no 'leaving the app alone' line" [ "$(grep -c 'leaving the app alone' "$XDG_RUNTIME_DIR/phone-cam.log")" = 0 ]
"$PC" stop >/dev/null

echo "adb forward failures are not relay failures"
new_env fwd
touch "$T/forward_fails"
"$PC" >/dev/null
sleep 25
check "no STOP intent after many failed forwards" [ "$(grep -c 'startservice.*STOP' "$T/adb_calls")" = 0 ]
check "no relay was started" [ "$(lines "$RL")" = 0 ]
"$PC" stop >/dev/null

echo "stop refuses a pid that is not phone-cam"
new_env stale
sleep 300 &
other=$!
CLEAN_PIDS+=("$other")
echo "$other" >"$XDG_RUNTIME_DIR/phone-cam.pid"
exec 8>"$XDG_RUNTIME_DIR/phone-cam.lock"; flock -n 8     # pretend a supervisor holds the lock
out=$("$PC" stop 2>&1); rc=$?
exec 8>&-
check "stop fails" [ "$rc" = 1 ]
check "says why" grep -q "not a phone-cam process" <<<"$out"
check "unrelated process still alive" alive "$other"
kill "$other"
rm -f "$XDG_RUNTIME_DIR/phone-cam.pid"

echo "status shows the daemon's warnings"
new_env warn
"$PC" >/dev/null
until_ok 6 both_started 1
echo '{"warnings": ["OBS is not reachable"]}' >"$XDG_RUNTIME_DIR/phone-cam.daemon.json"
check "warning is printed" grep -q "WARNING.*OBS is not reachable" <<<"$("$PC" status)"
echo '{"warnings": []}' >"$XDG_RUNTIME_DIR/phone-cam.daemon.json"
check "no warning line when clean" [ -z "$("$PC" status | grep WARNING)" ]
"$PC" stop >/dev/null

echo "SIGTERM does not stop the app"
new_env term
"$PC" --foreground &
sup=$!
CLEAN_PIDS+=("$sup")
until_ok 6 both_started 1
r1=$(last "$RL"); d1=$(last "$DM")
kill -TERM "$sup"
wait "$sup"; rc=$?
check "supervisor exits 0" [ "$rc" = 0 ]
check "relay gone" dead "$r1"
check "daemon gone" dead "$d1"
check "app left running (no STOP intent)" [ "$(grep -c 'STOP' "$T/adb_calls")" = 0 ]

echo "subcommands go to the python CLI"
new_env cli
"$PC" rec start
"$PC" join 2026-11-07
"$PC" trash 2026-11-07 --dry-run
check "rec start forwarded" grep -qx "rec start" "$T/tool_args"
check "join forwarded" grep -qx "join 2026-11-07" "$T/tool_args"
check "trash forwarded" grep -qx "trash 2026-11-07 --dry-run" "$T/tool_args"

echo "missing tools"
new_env missing
out=$(PHONECAM_DIR="$T/nowhere" "$PC" 2>&1); rc=$?
check "exits 1" [ "$rc" = 1 ]
check "names the folder" grep -q "tools missing" <<<"$out"
check "touched nothing" pidfile_gone

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" = 0 ]
