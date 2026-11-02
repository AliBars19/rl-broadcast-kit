"""Tests for obs_ad_return.py (decision logic, parsing, backoff, loop) and for
the obsws.py entry points the helper reuses. Stdlib unittest only; no OBS needed.

Run: python3 -I -m unittest discover -s tools -p "test_obs*.py"
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import obs_ad_return as ad  # noqa: E402
import obsws  # noqa: E402

AD_RULE = ad.Rule("Ad Break", "Ad - Sponsor", "Intro")
INTRO_RULE = ad.Rule("Intro", "Intro - Video", "Facecam")
CONFIG = ad.Config(rules=(AD_RULE, INTRO_RULE))


def event(event_type, **data):
    return {"op": 5, "d": {"eventType": event_type, "eventIntent": 1, "eventData": data}}


class ParseEventTests(unittest.TestCase):
    def test_event_message_is_parsed(self):
        parsed = ad.parse_event(event("MediaInputPlaybackEnded", inputName="X"))
        self.assertEqual(parsed, ad.Event("MediaInputPlaybackEnded", {"inputName": "X"}))

    def test_non_event_ops_are_ignored(self):
        self.assertIsNone(ad.parse_event({"op": 7, "d": {"requestId": "1"}}))

    def test_malformed_event_is_ignored(self):
        self.assertIsNone(ad.parse_event({"op": 5, "d": "nope"}))
        self.assertIsNone(ad.parse_event({"op": 5, "d": {"eventData": {}}}))

    def test_missing_event_data_becomes_empty_dict(self):
        parsed = ad.parse_event({"op": 5, "d": {"eventType": "Foo"}})
        self.assertEqual(parsed, ad.Event("Foo", {}))


class DecideTests(unittest.TestCase):
    def ended(self, name="Ad - Sponsor"):
        return ad.Event("MediaInputPlaybackEnded", {"inputName": name})

    def switch(self, scene):
        return ad.Action("SetCurrentProgramScene", {"sceneName": scene})

    def test_ad_ended_on_ad_scene_switches_to_intro(self):
        self.assertEqual(ad.decide(self.ended(), "Ad Break", CONFIG), self.switch("Intro"))

    def test_intro_ended_on_intro_scene_switches_to_facecam(self):
        ended = self.ended("Intro - Video")
        self.assertEqual(ad.decide(ended, "Intro", CONFIG), self.switch("Facecam"))

    def test_intro_ended_while_program_is_on_ad_break_is_ignored(self):
        self.assertIsNone(ad.decide(self.ended("Intro - Video"), "Ad Break", CONFIG))

    def test_intro_ended_while_program_is_on_facecam_is_ignored(self):
        self.assertIsNone(ad.decide(self.ended("Intro - Video"), "Facecam", CONFIG))

    def test_ad_ended_while_program_is_on_intro_is_ignored(self):
        self.assertIsNone(ad.decide(self.ended(), "Intro", CONFIG))

    def test_user_left_ad_scene_means_no_action(self):
        self.assertIsNone(ad.decide(self.ended(), "Gameplay", CONFIG))

    def test_unknown_current_scene_means_no_action(self):
        self.assertIsNone(ad.decide(self.ended(), None, CONFIG))

    def test_other_input_ending_is_ignored(self):
        self.assertIsNone(ad.decide(self.ended("Some Other Clip"), "Ad Break", CONFIG))

    def test_other_event_types_are_ignored(self):
        started = ad.Event("MediaInputPlaybackStarted", {"inputName": "Ad - Sponsor"})
        self.assertIsNone(ad.decide(started, "Ad Break", CONFIG))

    def test_rule_with_next_scene_equal_to_scene_is_refused(self):
        same = ad.Config(rules=(ad.Rule("Ad Break", "Ad - Sponsor", "Ad Break"),))
        self.assertIsNone(ad.decide(self.ended(), "Ad Break", same))

    def test_refused_rule_does_not_block_a_later_rule(self):
        config = ad.Config(rules=(
            ad.Rule("Ad Break", "Ad - Sponsor", "Ad Break"),
            AD_RULE,
        ))
        self.assertEqual(ad.decide(self.ended(), "Ad Break", config), self.switch("Intro"))

    def test_first_matching_rule_wins(self):
        config = ad.Config(rules=(
            ad.Rule("Ad Break", "Ad - Sponsor", "Sponsor"),
            ad.Rule("Ad Break", "Ad - Sponsor", "Intro"),
        ))
        self.assertEqual(ad.decide(self.ended(), "Ad Break", config), self.switch("Sponsor"))

    def test_custom_rule_is_respected(self):
        custom = ad.Config(rules=(ad.Rule("Sponsor", "Clip", "Break"),))
        ended = ad.Event("MediaInputPlaybackEnded", {"inputName": "Clip"})
        self.assertEqual(ad.decide(ended, "Sponsor", custom), self.switch("Break"))


class ChainTests(unittest.TestCase):
    def test_ad_end_then_intro_end_reaches_facecam(self):
        sent, logs = [], []

        def send(action):
            sent.append(action)
            return True

        scene = ad.handle_message(
            event("MediaInputPlaybackEnded", inputName="Ad - Sponsor"),
            "Ad Break", CONFIG, send, logs.append,
        )
        self.assertEqual(scene, "Intro")
        scene = ad.handle_message(
            event("CurrentProgramSceneChanged", sceneName="Intro"),
            scene, CONFIG, send, logs.append,
        )
        self.assertEqual(scene, "Intro")
        scene = ad.handle_message(
            event("MediaInputPlaybackEnded", inputName="Intro - Video"),
            scene, CONFIG, send, logs.append,
        )
        self.assertEqual(scene, "Facecam")
        self.assertEqual(sent, [
            ad.Action("SetCurrentProgramScene", {"sceneName": "Intro"}),
            ad.Action("SetCurrentProgramScene", {"sceneName": "Facecam"}),
        ])
        self.assertEqual(logs, [
            "Ad - Sponsor ended, switched Ad Break -> Intro",
            "Intro - Video ended, switched Intro -> Facecam",
        ])


class SceneTrackingTests(unittest.TestCase):
    def test_scene_change_event_updates_current(self):
        changed = ad.Event("CurrentProgramSceneChanged", {"sceneName": "Ad Break"})
        self.assertEqual(ad.track_scene(changed, "Gameplay"), "Ad Break")

    def test_other_events_keep_current(self):
        other = ad.Event("InputMuteStateChanged", {"inputName": "Mic"})
        self.assertEqual(ad.track_scene(other, "Gameplay"), "Gameplay")

    def test_scene_change_without_name_keeps_current(self):
        changed = ad.Event("CurrentProgramSceneChanged", {})
        self.assertEqual(ad.track_scene(changed, "Gameplay"), "Gameplay")


class HandleMessageTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.logs = []

    def send(self, action):
        self.sent.append(action)
        return True

    def test_ad_end_sends_switch_logs_and_tracks_new_scene(self):
        message = event("MediaInputPlaybackEnded", inputName="Ad - Sponsor")
        scene = ad.handle_message(message, "Ad Break", CONFIG, self.send, self.logs.append)
        self.assertEqual(scene, "Intro")
        self.assertEqual(self.sent, [ad.Action("SetCurrentProgramScene", {"sceneName": "Intro"})])
        self.assertEqual(self.logs, ["Ad - Sponsor ended, switched Ad Break -> Intro"])

    def test_failed_switch_keeps_scene_and_logs(self):
        message = event("MediaInputPlaybackEnded", inputName="Ad - Sponsor")
        scene = ad.handle_message(message, "Ad Break", CONFIG, lambda action: False, self.logs.append)
        self.assertEqual(scene, "Ad Break")
        self.assertEqual(self.logs, ["Ad - Sponsor ended, switch to Intro failed"])

    def test_user_left_scene_sends_nothing_and_keeps_scene(self):
        message = event("MediaInputPlaybackEnded", inputName="Ad - Sponsor")
        scene = ad.handle_message(message, "Gameplay", CONFIG, self.send, self.logs.append)
        self.assertEqual((scene, self.sent, self.logs), ("Gameplay", [], []))

    def test_scene_change_is_tracked_without_sending(self):
        message = event("CurrentProgramSceneChanged", sceneName="Ad Break")
        scene = ad.handle_message(message, "Gameplay", CONFIG, self.send, self.logs.append)
        self.assertEqual((scene, self.sent, self.logs), ("Ad Break", [], []))

    def test_response_messages_are_ignored(self):
        scene = ad.handle_message({"op": 7, "d": {}}, "Gameplay", CONFIG, self.send, self.logs.append)
        self.assertEqual((scene, self.sent), ("Gameplay", []))


class SubscriptionTests(unittest.TestCase):
    def test_mask_includes_scenes_and_media_inputs_only(self):
        self.assertEqual(ad.EVENT_SUBSCRIPTIONS, (1 << 2) | (1 << 8))
        self.assertEqual(ad.EVENT_SUBSCRIPTIONS, 260)

    def test_identify_passes_the_mask_to_obs(self):
        sent = []
        with mock.patch.object(obsws, "send_json", lambda sock, op, data: sent.append((op, data))), \
                mock.patch.object(obsws, "receive_json", lambda sock: {"op": obsws.OP_IDENTIFIED}):
            obsws.identify(object(), {}, ad.EVENT_SUBSCRIPTIONS)
        self.assertEqual(sent[0][0], obsws.OP_IDENTIFY)
        self.assertEqual(sent[0][1]["eventSubscriptions"], 260)

    def test_identify_default_still_subscribes_to_nothing(self):
        sent = []
        with mock.patch.object(obsws, "send_json", lambda sock, op, data: sent.append(data)), \
                mock.patch.object(obsws, "receive_json", lambda sock: {"op": obsws.OP_IDENTIFIED}):
            obsws.identify(object(), {})
        self.assertEqual(sent[0]["eventSubscriptions"], 0)


class BackoffTests(unittest.TestCase):
    def test_sequence_doubles_from_two_and_caps_at_thirty(self):
        delays = [ad.BACKOFF_START]
        for _ in range(6):
            delays.append(ad.next_delay(delays[-1]))
        self.assertEqual(delays, [2, 4, 8, 16, 30, 30, 30])

    def test_delay_never_exceeds_cap(self):
        self.assertEqual(ad.next_delay(1000), ad.BACKOFF_MAX)


class RunLoopTests(unittest.TestCase):
    def test_failed_connects_back_off_and_log_once(self):
        sleeps, logs = [], []

        def connect():
            raise ConnectionRefusedError("refused")

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 6:
                raise ad.Stop()

        ad.run(CONFIG, logs.append, connect=connect, sleep=sleep)
        self.assertEqual(sleeps, [2, 4, 8, 16, 30, 30])
        self.assertEqual([line for line in logs if "unreachable" in line], logs[:1])
        self.assertEqual(logs[-1], "stopping")

    def test_connect_then_drop_logs_both_and_resets_backoff(self):
        sleeps, logs, calls = [], [], []

        def connect():
            calls.append(1)
            if len(calls) == 1:
                return object()
            raise ConnectionRefusedError("refused")

        def listen(sock, config, log):
            raise obsws.ObsError("OBS closed the connection unexpectedly")

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise ad.Stop()

        with mock.patch.object(ad, "close_quietly"):
            ad.run(CONFIG, logs.append, connect=connect, sleep=sleep, listen=listen)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(
            logs[0],
            "connected to OBS, watching 'Ad - Sponsor' (Ad Break -> Intro), "
            "'Intro - Video' (Intro -> Facecam)",
        )
        self.assertTrue(logs[1].startswith("disconnected"))

    def test_stop_during_listen_exits_cleanly(self):
        def listen(sock, config, log):
            raise ad.Stop()

        with mock.patch.object(ad, "close_quietly"):
            ad.run(CONFIG, lambda line: None, connect=lambda: object(), sleep=lambda s: None, listen=listen)

    def test_unreadable_config_is_logged_without_crashing(self):
        logs = []

        def sleep(seconds):
            raise ad.Stop()

        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(obsws, "PASSWORD_FILE", "/nonexistent/.obs-password"):
            def connect():
                obsws.read_password()

            ad.run(CONFIG, logs.append, connect=connect, sleep=sleep)
        self.assertIn("cannot read OBS websocket config", logs[0])


class PasswordSourceTests(unittest.TestCase):
    def test_environment_variable_wins(self):
        with mock.patch.dict(os.environ, {"OBS_WEBSOCKET_PASSWORD": "from-env"}, clear=True):
            self.assertEqual(obsws.read_password(), "from-env")

    def test_password_file_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pw")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("from-file\nignored\n")
            with mock.patch.dict(os.environ, {"OBS_WEBSOCKET_PASSWORD_FILE": path}, clear=True):
                self.assertEqual(obsws.read_password(), "from-file")

    def test_obs_config_json_is_the_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"server_password": "from-config"}, handle)
            with mock.patch.dict(os.environ, {"OBS_WEBSOCKET_CONFIG": path}, clear=True), \
                    mock.patch.object(obsws, "PASSWORD_FILE", os.path.join(tmp, "missing")):
                self.assertEqual(obsws.read_password(), "from-config")

    def test_nothing_configured_explains_how(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(obsws, "PASSWORD_FILE", "/nonexistent/.obs-password"):
            with self.assertRaises(obsws.ObsError) as ctx:
                obsws.read_password()
        self.assertIn("OBS_WEBSOCKET_PASSWORD", str(ctx.exception))


class DescribeRulesTests(unittest.TestCase):
    def test_lists_each_rule_with_its_scenes(self):
        self.assertEqual(
            ad.describe_rules(CONFIG),
            "'Ad - Sponsor' (Ad Break -> Intro), 'Intro - Video' (Intro -> Facecam)",
        )


class ArgsTests(unittest.TestCase):
    def test_defaults_are_the_ad_then_intro_chain(self):
        self.assertEqual(ad.DEFAULT_RULES, (AD_RULE, INTRO_RULE))
        self.assertEqual(ad.parse_cli([]), ad.Config(rules=(AD_RULE, INTRO_RULE)))

    def test_single_rule_replaces_the_defaults(self):
        config = ad.parse_cli(["--rule", "A", "B", "C"])
        self.assertEqual(config, ad.Config(rules=(ad.Rule("A", "B", "C"),)))

    def test_repeated_rules_keep_their_order(self):
        config = ad.parse_cli(["--rule", "A", "B", "C", "--rule", "D", "E", "F"])
        self.assertEqual(config, ad.Config(rules=(ad.Rule("A", "B", "C"), ad.Rule("D", "E", "F"))))

    def test_rule_needs_exactly_three_values(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ad.parse_cli(["--rule", "A", "B"])

    def test_old_flags_are_gone(self):
        for argv in (["--ad-scene", "A"], ["--ad-input", "B"], ["--return-scene", "C"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ad.parse_cli(argv)


class ObswsCliTests(unittest.TestCase):
    def test_parse_args_request_only(self):
        self.assertEqual(obsws.parse_args(["obsws.py", "GetVersion"]), ("GetVersion", {}))

    def test_parse_args_with_json(self):
        got = obsws.parse_args(["obsws.py", "SetCurrentProgramScene", '{"sceneName": "BRB"}'])
        self.assertEqual(got, ("SetCurrentProgramScene", {"sceneName": "BRB"}))

    def test_parse_args_rejects_bad_usage(self):
        for argv in (["obsws.py"], ["obsws.py", "A", "{}", "extra"], ["obsws.py", "A", "nope"], ["obsws.py", "A", "[1]"]):
            with self.assertRaises(obsws.ObsError):
                obsws.parse_args(argv)

    def test_main_reports_unreachable_obs_with_exit_one(self):
        with mock.patch.object(obsws, "run_request", side_effect=ConnectionRefusedError("x")), \
                mock.patch.object(obsws, "eprint") as eprint:
            self.assertEqual(obsws.main(["obsws.py", "GetVersion"]), 1)
        self.assertIn("cannot reach OBS", eprint.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
