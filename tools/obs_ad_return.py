#!/usr/bin/env python3
"""obs_ad_return.py: chain OBS scenes when media inputs end.

Listens to OBS (obs-websocket v5, via obsws.py) for MediaInputPlaybackEnded.
Each rule names a scene, a media input and a next scene. When a rule's input
ends while the program scene is still that rule's scene, the program scene
switches to the rule's next scene. If you already left the scene, or no
rule matches, nothing happens.

The default rules chain the ad break into the intro and then the facecam:
    Ad Break (Ad - Sponsor ends) -> Intro
    Intro    (Intro - Video ends) -> Facecam

Usage:
    python3 -I obs_ad_return.py [--rule SCENE INPUT NEXT ...]

    --rule SCENE INPUT NEXT   add a rule; repeatable. Given at least once, these
                              rules replace the defaults above.

Runs until SIGTERM or Ctrl+C. When OBS is not running it retries forever with
a 2 s to 30 s backoff. One log line per state change goes to stdout. The
websocket password is read by obsws.py at runtime and is never logged.
"""

import argparse
import os
import signal
import socket
import sys
import time
from typing import Any, Callable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import obsws  # noqa: E402

SUBSCRIBE_SCENES = 1 << 2
SUBSCRIBE_MEDIA_INPUTS = 1 << 8
EVENT_SUBSCRIPTIONS = SUBSCRIBE_SCENES | SUBSCRIBE_MEDIA_INPUTS
OP_EVENT = 5
BACKOFF_START = 2
BACKOFF_MAX = 30

EVENT_ENDED = "MediaInputPlaybackEnded"
EVENT_SCENE = "CurrentProgramSceneChanged"


class Stop(Exception):
    """Raised by the SIGTERM/SIGINT handler to unwind the run loop."""


class Rule(NamedTuple):
    scene: str
    input: str
    next_scene: str


class Config(NamedTuple):
    rules: tuple[Rule, ...]


AD_RULE = Rule("Ad Break", "Ad - Sponsor", "Intro")
INTRO_RULE = Rule("Intro", "Intro - Video", "Facecam")
DEFAULT_RULES = (AD_RULE, INTRO_RULE)


class Event(NamedTuple):
    kind: str
    data: dict[str, Any]


class Action(NamedTuple):
    request_type: str
    data: dict[str, Any]


def log(line: str) -> None:
    sys.stdout.write("obs-ad-return: %s\n" % line)
    sys.stdout.flush()


# ---------- pure decision logic ----------

def parse_event(message: dict[str, Any]) -> Event | None:
    """Return an Event for an op 5 message, or None for anything else."""
    if message.get("op") != OP_EVENT:
        return None
    body = message.get("d")
    if not isinstance(body, dict) or not isinstance(body.get("eventType"), str):
        return None
    data = body.get("eventData")
    return Event(body["eventType"], data if isinstance(data, dict) else {})


def track_scene(event: Event, current: str | None) -> str | None:
    """Return the program scene after `event` (unchanged unless it is a switch)."""
    if event.kind == EVENT_SCENE and isinstance(event.data.get("sceneName"), str):
        return event.data["sceneName"]
    return current


def decide(event: Event, current: str | None, config: Config) -> Action | None:
    """Return the switch for the first matching rule, or None when nothing should happen."""
    if event.kind != EVENT_ENDED:
        return None
    name = event.data.get("inputName")
    for rule in config.rules:
        if rule.input == name and rule.scene == current and rule.next_scene != rule.scene:
            return Action("SetCurrentProgramScene", {"sceneName": rule.next_scene})
    return None


def describe_rules(config: Config) -> str:
    """Human-readable list of what the helper watches, for the connect log line."""
    return ", ".join(
        "'%s' (%s -> %s)" % (rule.input, rule.scene, rule.next_scene) for rule in config.rules
    )


def next_delay(delay: float) -> float:
    return min(delay * 2, BACKOFF_MAX)


def handle_message(
    message: dict[str, Any],
    current: str | None,
    config: Config,
    send: Callable[[Action], bool],
    emit: Callable[[str], None],
) -> str | None:
    """Process one OBS message and return the (possibly new) program scene."""
    event = parse_event(message)
    if event is None:
        return current
    action = decide(event, current, config)
    if action is None:
        return track_scene(event, current)
    input_name = event.data.get("inputName")
    next_scene = action.data["sceneName"]
    if send(action):
        emit("%s ended, switched %s -> %s" % (input_name, current, next_scene))
        return next_scene
    emit("%s ended, switch to %s failed" % (input_name, next_scene))
    return current


# ---------- OBS connection ----------

def connect_obs() -> socket.socket:
    return obsws.open_session(EVENT_SUBSCRIPTIONS, timeout=None)


def close_quietly(sock: socket.socket) -> None:
    try:
        obsws.close_session(sock)
    except OSError:
        pass


def make_sender(sock: socket.socket) -> Callable[[Action], bool]:
    def send(action: Action) -> bool:
        response = obsws.call_request(sock, action.request_type, action.data)
        return (response.get("requestStatus") or {}).get("result") is True
    return send


def read_program_scene(sock: socket.socket) -> str | None:
    response = obsws.call_request(sock, "GetCurrentProgramScene", {})
    name = (response.get("responseData") or {}).get("currentProgramSceneName")
    return name if isinstance(name, str) else None


def listen(sock: socket.socket, config: Config, emit: Callable[[str], None]) -> None:
    """Handle events until the connection drops (raises ObsError or OSError)."""
    send = make_sender(sock)
    current = read_program_scene(sock)
    while True:
        current = handle_message(obsws.receive_json(sock), current, config, send, emit)


def describe(exc: Exception) -> str:
    if isinstance(exc, obsws.ObsError):
        return str(exc)
    detail = getattr(exc, "strerror", None)
    return detail or type(exc).__name__


def run(
    config: Config,
    emit: Callable[[str], None],
    connect: Callable[[], socket.socket] = connect_obs,
    sleep: Callable[[float], None] = time.sleep,
    listen: Callable[..., None] = listen,
) -> None:
    """Connect, listen, reconnect forever. Returns only when Stop is raised."""
    delay: float = BACKOFF_START
    last_failure: str | None = None
    try:
        while True:
            try:
                sock = connect()
            except (obsws.ObsError, OSError) as exc:
                reason = describe(exc)
                if reason != last_failure:
                    emit("OBS unreachable (%s), retrying in the background" % reason)
                    last_failure = reason
                sleep(delay)
                delay = next_delay(delay)
                continue
            delay, last_failure = BACKOFF_START, None
            emit("connected to OBS, watching %s" % describe_rules(config))
            try:
                listen(sock, config, emit)
            except (obsws.ObsError, OSError) as exc:
                emit("disconnected (%s)" % describe(exc))
            finally:
                close_quietly(sock)
            sleep(delay)
            delay = next_delay(delay)
    except Stop:
        emit("stopping")


# ---------- command line ----------

def parse_cli(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(description="Chain OBS scenes when media inputs end.")
    parser.add_argument(
        "--rule",
        nargs=3,
        action="append",
        metavar=("SCENE", "INPUT", "NEXT"),
        help="when INPUT ends while SCENE is live, switch to NEXT (repeatable)",
    )
    args = parser.parse_args(argv)
    if not args.rule:
        return Config(rules=DEFAULT_RULES)
    return Config(rules=tuple(Rule(*values) for values in args.rule))


def _raise_stop(signum: int, frame: Any) -> None:
    raise Stop()


def main(argv: list[str]) -> int:
    config = parse_cli(argv)
    signal.signal(signal.SIGTERM, _raise_stop)
    signal.signal(signal.SIGINT, _raise_stop)
    run(config, log)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
