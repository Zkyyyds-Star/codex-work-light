"""Isolated Hook identity metadata tests; no broker, device, or chat history."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("light_events", SCRIPTS / "light_events.py")
events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(events)


class RootTurnMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "runtime"

    def event(self, name="UserPromptSubmit", chat="chat-a", turn="turn-1", now=100, allow_resume=False, **extra):
        payload = {"hook_event_name": name, "session_id": chat, "turn_id": turn}
        payload.update(extra)
        return events.observe_root_event(payload, self.data, now=now, allow_resume=allow_resume)

    def latest(self, chat="chat-a", now=100):
        return events.latest_root_turn(chat, self.data, now=now)

    def record(self, chat="chat-a"):
        path = events._record_path(self.data / "turns", chat)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_observes_all_root_chats_without_broker_or_binding(self):
        self.assertTrue(self.event(chat="chat-a", turn="turn-a"))
        self.assertTrue(self.event(chat="chat-b", turn="turn-b"))
        self.assertEqual(self.latest("chat-a"), "turn-a")
        self.assertEqual(self.latest("chat-b"), "turn-b")
        files = list((self.data / "turns").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(events._RECORD_NAME.fullmatch(path.name) for path in files))
        self.assertFalse(any("chat-a" in path.name for path in files))

    def test_only_allowed_metadata_is_saved(self):
        self.event(prompt="PRIVATE", tool_input={"secret": "PRIVATE"}, last_assistant_message="PRIVATE")
        record = self.record()
        self.assertEqual(set(record), {"version", "chat_id", "turn_id", "seen_at", "closed", "retired_turns"})
        self.assertNotIn("PRIVATE", json.dumps(record))

    def test_malformed_and_subagent_events_create_nothing(self):
        for bad in (None, [], {}, {"hook_event_name": [], "session_id": "a", "turn_id": "x"},
                    {"hook_event_name": "UserPromptSubmit", "session_id": "a/b", "turn_id": "x"}):
            self.assertFalse(events.observe_root_event(bad, self.data, now=100))
        for field in ("is_subagent", "subagent_id", "parent_agent_id", "parent_tool_use_id", "agent_id"):
            self.assertFalse(self.event(**{field: "child"}))
        for bad in (None, "", "x" * 201, "../slash", "space x", 123):
            self.assertFalse(self.event(chat=bad))
            self.assertFalse(self.event(turn=bad))
        self.assertFalse(self.data.exists())

    def test_unknown_tool_cannot_create_or_bridge_turn(self):
        self.assertFalse(self.event("PreToolUse"))
        self.assertIsNone(self.latest())
        self.event()
        self.assertFalse(self.event("PreToolUse", turn="old", now=105))
        self.assertFalse(self.event("PostToolUse", turn="new", now=105))
        self.assertEqual(self.latest(now=105), "turn-1")

    def test_latest_turn_survives_module_restart_but_is_not_old_turn(self):
        self.event(turn="turn-a")
        self.event(turn="turn-b", now=101)
        reloaded_spec = importlib.util.spec_from_file_location("light_events_restart", SCRIPTS / "light_events.py")
        reloaded = importlib.util.module_from_spec(reloaded_spec)
        reloaded_spec.loader.exec_module(reloaded)
        self.assertEqual(reloaded.latest_root_turn("chat-a", self.data, now=102), "turn-b")
        self.assertFalse(self.event(turn="turn-a", now=102))
        self.assertEqual(self.latest(now=102), "turn-b")

    def test_duplicate_prompt_cannot_refresh_or_reopen(self):
        self.event()
        self.assertFalse(self.event(now=110))
        self.assertEqual(self.record()["seen_at"], 100)
        self.event("Stop", now=111)
        self.assertIsNone(self.latest(now=111))
        self.assertFalse(self.event(now=112))
        self.assertIsNone(self.latest(now=112))

    def test_only_broker_authorized_same_turn_answer_reopens_metadata(self):
        self.event(turn="older")
        self.event(now=101)
        self.event("Stop", now=102)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "chat-a", "turn_id": "turn-1", "allow_resume": True}
        self.assertFalse(events.observe_root_event(payload, self.data, now=103))
        self.assertTrue(self.event(now=104, allow_resume=True))
        self.assertEqual(self.latest(now=104), "turn-1")
        self.assertEqual(self.record()["retired_turns"], ["older"])
        self.assertFalse(self.event(now=105))
        self.assertEqual(self.record()["seen_at"], 104)
        self.assertFalse(self.event(turn="older", now=106, allow_resume=True))
        self.assertEqual(self.latest(now=106), "turn-1")

    def test_same_turn_answer_authorization_follows_actual_state_machine_result(self):
        state = events.LightState()
        state.activate("chat-a", "turn-1", "binding")
        def deliver(name, now, **fields):
            payload = {"session_id": "chat-a", "turn_id": "turn-1", "hook_event_name": name, **fields}
            outcome = state.event(payload)
            observed = events.observe_root_event(payload, self.data, now=now, allow_resume=outcome["resumed_turn"])
            return outcome, observed
        deliver("UserPromptSubmit", 100)
        deliver("PreToolUse", 101, tool_name="request_user_input_async", tool_use_id="q", tool_input={"questions": [{}]})
        deliver("Stop", 102, last_assistant_message="<!-- aaa-wudi:needs_user turn=turn-1 binding=binding -->")
        self.assertIsNone(self.latest(now=102))
        prompt = "<send_user_message_question_reply>" + json.dumps([{
            "questionItemId": json.dumps(["request_user_input_async", "q", 0]), "answer": "Yes",
        }]) + "</send_user_message_question_reply>"
        outcome, observed = deliver("UserPromptSubmit", 103, prompt=prompt)
        self.assertTrue(outcome["resumed_turn"])
        self.assertTrue(observed)
        self.assertEqual(self.latest(now=103), "turn-1")
        outcome, observed = deliver("UserPromptSubmit", 104, prompt=prompt)
        self.assertFalse(outcome["resumed_turn"])
        self.assertFalse(observed)
        self.assertEqual(self.record()["seen_at"], 103)

    def test_late_close_does_not_close_newer_and_exact_close_does(self):
        for closer in ("Stop", "Interrupt", "SessionEnd"):
            with self.subTest(closer=closer):
                old, new = "old-" + closer, "new-" + closer
                self.event(turn=old)
                self.event(turn=new, now=101)
                self.assertFalse(self.event(closer, turn=old, now=102))
                self.assertEqual(self.latest(now=102), new)
                self.assertTrue(self.event(closer, turn=new, now=103))
                self.assertIsNone(self.latest(now=103))
                self.assertFalse(self.event(turn=new, now=104))

    def test_session_end_without_identity_cannot_close_current(self):
        self.event()
        self.assertFalse(self.event("SessionEnd", turn=None))
        self.assertEqual(self.latest(), "turn-1")

    def test_expiry_and_clock_rollback_never_reuse_stale(self):
        self.event()
        self.assertIsNone(self.latest(now=1001))
        self.assertIsNone(self.latest(now=99))
        self.assertFalse(self.event("PreToolUse", now=1001))
        self.assertFalse(self.event(now=1001))
        self.assertIsNone(self.latest(now=1001))
        self.assertTrue(self.event(turn="turn-new", now=1002))
        self.assertEqual(self.latest(now=1002), "turn-new")

    def test_only_exact_current_tool_can_renew_an_active_turn(self):
        self.event()
        self.assertTrue(self.event("PreToolUse", now=999))
        self.assertEqual(self.latest(now=1800), "turn-1")
        self.assertFalse(self.event("PreToolUse", turn="wrong", now=1801))
        self.event("Interrupt", now=1801)
        self.assertFalse(self.event("PreToolUse", now=1802))

    def test_retirement_is_bounded_and_known_replay_is_rejected(self):
        for index in range(80):
            self.event(turn="turn-%d" % index, now=100 + index)
        record = self.record()
        self.assertEqual(len(record["retired_turns"]), 64)
        self.assertIn("turn-78", record["retired_turns"])
        self.assertFalse(self.event(turn="turn-78", now=181))
        self.assertEqual(self.latest(now=181), "turn-79")

    def test_busy_lock_fails_open_within_small_bound(self):
        self.event()
        path = events._record_path(self.data / "turns", "chat-a")
        with events._chat_lock(path) as acquired:
            self.assertTrue(acquired)
            started = time.monotonic()
            self.assertFalse(self.event(turn="new"))
            self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(self.latest(), "turn-1")

    def test_killed_lock_owner_does_not_block_future_hooks(self):
        self.event()
        path = events._record_path(self.data / "turns", "chat-a")
        child_code = (
            "import pathlib,sys,time;sys.path.insert(0,sys.argv[1]);"
            "import light_events;"
            "lock=light_events._chat_lock(pathlib.Path(sys.argv[2]));"
            "acquired=lock.__enter__();"
            "print('locked' if acquired else 'failed',flush=True);"
            "time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", child_code, str(SCRIPTS), str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup_child)
        self.assertEqual(child.stdout.readline().strip(), "locked")
        self.assertFalse(self.event(turn="new", now=101))
        child.kill()
        child.communicate(timeout=5)
        self.assertTrue(self.event(turn="new", now=102))
        self.assertEqual(self.latest(now=102), "new")

    def test_prune_only_deletes_own_validated_expired_json(self):
        self.event(chat="expired", now=100)
        turns = self.data / "turns"
        foreign = turns / "foreign.json"
        foreign.write_text('{"secret":"untouched"}', encoding="utf-8")
        invalid = turns / ("f" * 64 + ".json")
        invalid.write_text('{"secret":"untouched"}', encoding="utf-8")
        self.event(chat="current", now=1001)
        self.assertTrue(events._record_path(turns, "expired").exists())
        self.assertEqual(events.prune_expired(self.data, now=1001), 1)
        self.assertFalse(events._record_path(turns, "expired").exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(invalid.exists())

    def test_io_failure_is_advisory_and_leaves_no_temp_or_json(self):
        with patch.object(events.os, "replace", side_effect=PermissionError("busy")):
            self.assertFalse(self.event())
        files = list((self.data / "turns").iterdir())
        self.assertTrue(all(path.is_file() and path.suffix == ".lock" for path in files))
        self.assertTrue(self.event())

    def test_observer_never_scans_other_chats(self):
        with patch.object(events.os, "scandir", side_effect=AssertionError("foreground scan")):
            self.assertTrue(self.event())

    def test_pruning_streams_past_permanent_locks_and_fresh_records(self):
        self.event()
        turns = self.data / "turns"
        for index in range(140):
            (turns / ("%064x.lock" % index)).touch()
            self.event(chat="fresh-%d" % index, now=1900)
            self.event(chat="old-%d" % index, now=1)
        self.assertEqual(events.prune_expired(self.data, now=2000), 141)
        self.assertEqual(len(list(turns.glob("*.json"))), 140)
        self.assertGreaterEqual(len(list(turns.glob("*.lock"))), 140)

    def test_pruning_skips_busy_locks_and_honors_stop(self):
        self.event()
        path = events._record_path(self.data / "turns", "chat-a")
        with events._chat_lock(path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(events.prune_expired(self.data, now=2000), 0)
        stop = threading.Event()
        stop.set()
        self.assertEqual(events.prune_expired(self.data, stop, now=2000), 0)
        self.assertTrue(path.exists())
        self.assertEqual(events.prune_expired(self.data, now=2000), 1)

    def test_pruning_stops_between_files(self):
        self.event(chat="first")
        self.event(chat="second")
        turns = self.data / "turns"
        with events.os.scandir(turns) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name.endswith(".lock"))
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        with patch.object(events.os, "scandir") as scan:
            scan.return_value.__enter__.return_value = iter(ordered)
            self.assertEqual(events.prune_expired(self.data, stop, now=2000), 1)
        self.assertEqual(len(list(turns.glob("*.json"))), 1)
        self.assertEqual(stop.is_set.call_count, 2)

    def test_pruning_skips_symbolic_link_entries(self):
        self.event()
        path = events._record_path(self.data / "turns", "chat-a")
        entry = MagicMock()
        entry.name = path.name
        entry.is_symlink.return_value = True
        with patch.object(events.os, "scandir") as scan, patch.object(events, "_read") as read:
            scan.return_value.__enter__.return_value = iter([entry])
            self.assertEqual(events.prune_expired(self.data, now=2000), 0)
            read.assert_not_called()
        self.assertTrue(path.exists())

    def test_pruning_rechecks_expiry_after_taking_lock(self):
        self.event()
        path = events._record_path(self.data / "turns", "chat-a")
        initial = self.record()
        renewed = dict(initial, seen_at=1900)
        with patch.object(events, "_read", side_effect=[initial, renewed]):
            self.assertEqual(events.prune_expired(self.data, now=2000), 0)
        self.assertTrue(path.exists())

    def test_pruning_continues_after_individual_read_error(self):
        self.event(chat="first")
        self.event(chat="second")
        original_read = events._read
        calls = 0
        def fail_once(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("temporarily inaccessible")
            return original_read(path)
        with patch.object(events, "_read", side_effect=fail_once):
            self.assertEqual(events.prune_expired(self.data, now=2000), 1)
        self.assertEqual(len(list((self.data / "turns").glob("*.json"))), 1)

    def test_pruning_missing_directory_and_invalid_clock_are_advisory(self):
        self.assertEqual(events.prune_expired(self.data, now=2000), 0)
        self.assertFalse(self.data.exists())
        self.assertEqual(events.prune_expired(self.data, now=float("nan")), 0)


if __name__ == "__main__":
    unittest.main()
