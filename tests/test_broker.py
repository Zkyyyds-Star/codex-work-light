"""Broker integration tests with an inert driver and isolated hook config."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import status_light
from light_state import LightState


class FakeDriver:
    def __init__(self):
        self.sync_requests = 0
        self.connected = False
        self.applied_revision = -1
        self.waits = []
        self.on_wait = None

    def snapshot(self):
        return {"connected": self.connected, "observed": None, "applied_revision": self.applied_revision, "last_error": None, "pending": True}

    def request_sync(self):
        self.sync_requests += 1

    async def wait_confirmed(self, revision, timeout):
        self.waits.append((revision, timeout))
        if self.on_wait:
            result = self.on_wait()
            if inspect.isawaitable(result):
                await result
        return self.applied_revision == revision


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.runtime.mkdir()
        self.write = Mock(wraps=status_light.atomic_json)
        self.writes_patch = patch.object(status_light, "atomic_json", self.write)
        self.directory_patch = patch.object(status_light, "data_dir", return_value=self.runtime)
        self.writes_patch.start()
        self.directory_patch.start()
        self.addCleanup(self.writes_patch.stop)
        self.addCleanup(self.directory_patch.stop)
        self.driver = FakeDriver()
        self.broker = status_light.Broker(state=LightState(), driver=self.driver)
        self.turn_sequence = 0

    async def activate(self, chat="chat-a", turn=None, observe=True):
        if observe:
            self.turn_sequence += 1
            await self.event("UserPromptSubmit", turn=turn or "activation-turn-%d" % self.turn_sequence, session_id=chat)
        return await self.broker.handle({"action": "activate", "chat_id": chat})

    async def event(self, name, turn="turn-1", **kwargs):
        event = {"hook_event_name": name, "session_id": "chat-a", "turn_id": turn}
        event.update(kwargs)
        return await self.broker.handle({"action": "event", "event": event})

    async def set_status(self, turn, state="completed", chat="chat-a", **kwargs):
        return await self.broker.handle({"action": "set", "chat_id": chat, "turn_id": turn, "state": state,
                                         "binding_nonce": self.broker.state.binding_nonce, **kwargs})

    @staticmethod
    def marker(result, state="completed"):
        return "<!-- aaa-wudi:%s turn=%s binding=%s -->" % (state, result["turn_id"], result["binding_nonce"])

    async def test_fresh_broker_is_disabled_and_hooks_do_not_enable_it(self):
        result = await self.broker.handle({"action": "status"})
        self.assertFalse(result["enabled"])
        self.assertFalse(result["desired"]["power"])
        await self.event("UserPromptSubmit")
        self.assertFalse(self.broker.state.enabled)
        self.assertEqual(self.driver.sync_requests, 0)

    async def test_activate_returns_token_and_persists_only_binding(self):
        result = await self.activate()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["turn_id"], "activation-turn-1")
        self.assertRegex(result["binding_nonce"], r"^[a-f0-9]{32}$")
        self.assertTrue(result["desired"]["power"])
        self.assertEqual(result["desired"]["brightness"], 75)
        self.assertEqual(self.driver.sync_requests, 1)
        self.assertEqual(self.write.call_args.args[1], {"bound_chat": "chat-a"})

    async def test_no_observed_turn_is_pending_until_next_prompt(self):
        result = await self.activate(observe=False)
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["turn_id"])
        self.assertEqual(result["tracking"], "pending_next_prompt")
        self.assertEqual(result["context"], "")
        self.assertEqual(result["reason"], "pending_next_prompt")
        self.assertEqual(result["phase"], "idle")
        self.assertEqual(result["desired"]["color"], "#000000")
        self.assertFalse(result["desired"]["power"])
        self.assertTrue(result["binding_accepted"])
        self.assertFalse(result["tracking_ready"])
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"], {})
        summary = status_light.compact_status(result, "chat-a", include_context=True)
        self.assertTrue(summary["notice"])
        self.assertTrue(summary["binding_accepted"])
        self.assertFalse(summary["tracking_ready"])
        result = await self.event("UserPromptSubmit", turn="next-real-turn")
        self.assertEqual(result["tracking"], "active")
        self.assertIn("turn=next-real-turn", result["context"])
        self.assertTrue(result["desired"]["power"])
        self.assertEqual(result["desired"]["color"], "#0000FF")
        self.assertTrue(result["tracking_ready"])
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"]["UserPromptSubmit"], 1)

    async def test_pending_takeover_and_refresh_stay_dark_until_current_root_prompt(self):
        old = await self.activate()
        pending = await self.activate("chat-b", observe=False)
        self.assertTrue(pending["enabled"])
        self.assertEqual(pending["bound_chat"], "chat-b")
        self.assertIsNone(pending["turn_id"])
        self.assertFalse(pending["desired"]["power"])
        refresh = await self.activate("chat-b", observe=False)
        self.assertNotEqual(pending["binding_nonce"], refresh["binding_nonce"])
        self.assertFalse(refresh["desired"]["power"])
        result = await self.event("Stop", turn=old["turn_id"], last_assistant_message=self.marker(old))
        self.assertFalse(result["accepted"])
        self.assertFalse(self.broker.state.snapshot()["desired"]["power"])
        result = await self.event("UserPromptSubmit", session_id="chat-b", turn="new-root", binding_nonce=pending["binding_nonce"])
        self.assertFalse(result["accepted"])
        self.assertFalse(self.broker.state.snapshot()["desired"]["power"])
        result = await self.event("UserPromptSubmit", session_id="chat-b", turn="new-root")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["binding_nonce"], refresh["binding_nonce"])
        self.assertEqual(result["desired"]["color"], "#0000FF")
        self.assertTrue(result["desired"]["power"])

    async def test_pending_reconnection_or_context_refresh_cannot_light_device(self):
        await self.activate(observe=False)
        for connected in (True, False, True):
            self.driver.connected = connected
            result = await self.broker.handle({"action": "status"})
            self.assertTrue(result["enabled"])
            self.assertEqual(result["tracking"], "pending_next_prompt")
            self.assertEqual(result["phase"], "idle")
            self.assertFalse(result["desired"]["power"])
            self.assertEqual(result["device"]["connected"], connected)
        for event in ("SessionStart", "PostCompact"):
            result = await self.event(event, turn=None)
            self.assertFalse(result["desired"]["power"])
            self.assertEqual(result.get("context", ""), "")

    async def test_long_running_active_turn_survives_metadata_ttl_without_heartbeat(self):
        active = await self.activate()
        future = status_light.time.time() + 24 * 60 * 60
        with patch.object(status_light.time, "time", return_value=future):
            self.assertIsNone(status_light.latest_root_turn("chat-a", self.runtime))
            result = await self.broker.handle({"action": "status"})
            self.assertEqual(result["tracking"], "active")
            self.assertEqual(result["turn_id"], active["turn_id"])
            self.assertTrue(result["desired"]["power"])
            self.assertEqual(result["desired"]["color"], "#0000FF")
            result = await self.event("PreToolUse", turn=active["turn_id"], tool_name="exec", tool_use_id="long-tool")
            self.assertTrue(result["accepted"])
            self.assertTrue(result["desired"]["power"])
            self.assertEqual(result["desired"]["color"], "#0000FF")

    async def test_observed_root_turn_is_used_on_activation(self):
        await self.event("UserPromptSubmit", turn="root-turn")
        result = await self.activate(observe=False)
        self.assertEqual(result["turn_id"], "root-turn")

    async def test_new_root_prompt_injects_context_with_exact_token(self):
        await self.activate()
        result = await self.event("UserPromptSubmit", turn="new-root")
        self.assertTrue(result["context_needed"])
        self.assertIn("turn=new-root", result["context"])
        self.assertIn("binding=" + result["binding_nonce"], result["context"])
        self.assertNotIn("powershell", result["context"])
        self.assertEqual(result["desired"]["color"], "#0000FF")
        self.assertEqual(self.driver.sync_requests, 2)

    async def test_context_refresh_is_only_for_bound_enabled_root(self):
        await self.activate()
        result = await self.event("SessionStart", turn=None)
        self.assertTrue(result["context_needed"])
        self.assertIn("context", result)
        result = await self.event("PostCompact", turn=None, session_id="other")
        self.assertFalse(result["accepted"])
        self.assertNotIn("context", result)

    async def test_agent_id_is_filtered_before_turn_recording_or_context(self):
        await self.activate()
        root_turn = status_light.latest_root_turn("chat-a", self.runtime)
        result = await self.event("UserPromptSubmit", turn="child-turn", agent_id="child")
        self.assertFalse(result["accepted"])
        self.assertNotIn("context", result)
        self.assertEqual(status_light.latest_root_turn("chat-a", self.runtime), root_turn)
        result = await self.event("SessionStart", turn=None, agent_id="child")
        self.assertFalse(result["accepted"])

    async def test_wrong_chat_missing_and_old_tokens_cannot_update(self):
        token = (await self.activate())["turn_id"]
        self.assertFalse((await self.set_status(token, chat="other"))["accepted"])
        self.assertFalse((await self.set_status(None))["accepted"])
        await self.event("UserPromptSubmit", turn="next-root")
        self.assertFalse((await self.set_status(token))["accepted"])
        self.assertTrue((await self.set_status("next-root"))["accepted"])

    async def test_stop_does_not_generate_green(self):
        token = (await self.activate())["turn_id"]
        result = await self.event("Stop", turn=token)
        self.assertFalse(result["desired"]["power"])
        self.assertEqual(result["diagnostic"], "result_not_reported")

    async def test_explicit_completed_survives_stop(self):
        token = (await self.activate())["turn_id"]
        await self.set_status(token)
        result = await self.event("Stop", turn=token)
        self.assertTrue(result["desired"]["power"])
        self.assertEqual(result["desired"]["color"], "#00FF00")

    async def test_deactivate_stays_dark_across_reconnection_and_hooks(self):
        await self.activate()
        result = await self.broker.handle({"action": "deactivate", "chat_id": "chat-a"})
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["bound_chat"])
        self.assertIsNone(result["binding_nonce"])
        self.assertEqual(json.loads((self.runtime / "binding.json").read_text(encoding="utf-8")), {"bound_chat": None})
        self.assertFalse(result["desired"]["power"])
        self.driver.connected = True
        await self.event("UserPromptSubmit", turn="new-root")
        result = await self.broker.handle({"action": "status"})
        self.assertTrue(result["device"]["connected"])
        self.assertFalse(result["desired"]["power"])
        self.assertFalse(result["enabled"])

    async def test_wait_confirmation_requires_current_revision(self):
        revision = (await self.activate())["revision"]
        self.driver.applied_revision = revision
        result = await self.broker.handle({"action": "wait", "revision": revision, "timeout": 1})
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.driver.waits[-1], (revision, 1))
        token = self.broker.state.turn_id
        self.driver.on_wait = lambda: self.broker.state.set_status("chat-a", token, "completed")
        result = await self.broker.handle({"action": "wait", "revision": revision, "timeout": 1})
        self.assertFalse(result["confirmed"])

    async def test_wait_and_resolution_each_request_device_revision_sync(self):
        token = (await self.activate())["turn_id"]
        result = await self.event("PreToolUse", turn=token, tool_name="request_user_input", tool_use_id="question")
        waiting_revision = result["revision"]
        self.assertEqual(result["desired"]["color"], "#FF0000")
        result = await self.event("PostToolUse", turn=token, tool_name="request_user_input", tool_use_id="question", tool_response={"answers": {"q": "yes"}})
        self.assertGreater(result["revision"], waiting_revision)
        self.assertEqual(result["desired"]["color"], "#0000FF")
        self.assertEqual(self.driver.sync_requests, 3)

    async def test_permission_post_clears_red_even_when_tool_exits_nonzero(self):
        token = (await self.activate())["turn_id"]
        await self.event("PreToolUse", turn=token, tool_name="exec", tool_use_id="command", tool_input={"cmd": "test"})
        result = await self.event("PermissionRequest", turn=token, tool_name="exec", tool_use_id="command")
        self.assertEqual(result["desired"]["color"], "#FF0000")
        result = await self.event("PostToolUse", turn=token, tool_name="exec", tool_use_id="command", tool_response={"exit_code": 1})
        self.assertEqual(result["desired"]["color"], "#0000FF")

    async def test_shutdown_rejects_reactivation_while_waiting_for_off_confirmation(self):
        await self.activate()
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def hold_confirmation():
            waiting.set()
            await release.wait()

        self.driver.on_wait = hold_confirmation
        shutdown = asyncio.create_task(self.broker.handle({"action": "shutdown"}))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        try:
            result = await self.activate()
            self.assertFalse(result["accepted"])
            self.assertFalse(self.broker.state.snapshot()["desired"]["power"])
        finally:
            release.set()
            await shutdown

    async def test_rejected_old_prompt_does_not_poison_activation_turn_cache(self):
        await self.event("UserPromptSubmit", turn="old-root")
        await self.activate(observe=False)
        await self.event("UserPromptSubmit", turn="new-root")
        result = await self.event("UserPromptSubmit", turn="old-root")
        self.assertFalse(result["accepted"])
        result = await self.activate(observe=False)
        self.assertEqual(result["turn_id"], "new-root")

    async def test_foreign_root_prompt_is_available_for_immediate_takeover(self):
        initial = await self.activate()
        previous = self.broker.state.snapshot()
        result = await self.event("UserPromptSubmit", turn="chat-b-live-turn", session_id="chat-b", prompt="PRIVATE")
        self.assertFalse(result["accepted"])
        self.assertEqual(previous, self.broker.state.snapshot())
        switched = await self.activate("chat-b", observe=False)
        self.assertEqual(switched["turn_id"], "chat-b-live-turn")
        self.assertNotEqual(initial["binding_nonce"], switched["binding_nonce"])
        self.assertIn("turn=chat-b-live-turn", switched["context"])
        result = await self.event("Stop", turn="chat-b-live-turn", session_id="chat-b", last_assistant_message=self.marker(switched))
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(result["desired"]["color"], "#00FF00")
        previous = self.broker.state.snapshot()
        self.assertFalse((await self.event("Stop", turn=initial["turn_id"], last_assistant_message=self.marker(initial)))["accepted"])
        self.assertEqual(previous, self.broker.state.snapshot())
        self.assertNotIn("PRIVATE", json.dumps(result["hook_diagnostics"]))

    async def test_same_chat_same_turn_refresh_rejects_old_set_and_stop_marker(self):
        initial = await self.activate()
        refreshed = await self.activate(observe=False)
        self.assertEqual(initial["turn_id"], refreshed["turn_id"])
        self.assertNotEqual(initial["binding_nonce"], refreshed["binding_nonce"])
        previous = self.broker.state.snapshot()
        result = await self.set_status(initial["turn_id"], binding_nonce=initial["binding_nonce"])
        self.assertFalse(result["accepted"])
        self.assertEqual(previous, self.broker.state.snapshot())
        result = await self.event("Stop", turn=initial["turn_id"], last_assistant_message=self.marker(initial))
        self.assertFalse(result["accepted"])
        self.assertEqual(previous, self.broker.state.snapshot())
        self.assertEqual(status_light.latest_root_turn("chat-a", self.runtime), refreshed["turn_id"])
        result = await self.event("Stop", turn=refreshed["turn_id"], last_assistant_message=self.marker(refreshed, "blocked"))
        self.assertEqual(result["desired"]["color"], "#FFFF00")

    async def test_very_old_prompt_cannot_poison_cache_after_sidecar_tombstones_roll_over(self):
        await self.activate(turn="oldest-turn")
        for number in range(70):
            await self.event("UserPromptSubmit", turn="root-%s" % number)
        result = await self.event("UserPromptSubmit", turn="oldest-turn")
        self.assertFalse(result["accepted"])
        self.assertEqual(status_light.latest_root_turn("chat-a", self.runtime), "root-69")
        result = await self.activate(observe=False)
        self.assertEqual(result["turn_id"], "root-69")

    async def test_broker_uses_metadata_recorded_while_no_controller_exists(self):
        status_light.observe_root_event({"hook_event_name": "UserPromptSubmit", "session_id": "fresh-chat", "turn_id": "fresh-root"}, self.runtime)
        self.broker = status_light.Broker(state=LightState(), driver=self.driver)
        self.assertFalse(self.broker.state.enabled)
        result = await self.activate("fresh-chat", observe=False)
        self.assertEqual(result["turn_id"], "fresh-root")
        self.assertIn("turn=fresh-root", result["context"])
        self.assertTrue(result["tracking_ready"])
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"], {})

    async def test_expired_or_closed_metadata_is_not_borrowed_for_activation(self):
        old = {"hook_event_name": "UserPromptSubmit", "session_id": "expired-chat", "turn_id": "expired-root"}
        status_light.observe_root_event(old, self.runtime, now=status_light.time.time() - 901)
        result = await self.activate("expired-chat", observe=False)
        self.assertIsNone(result["turn_id"])
        self.assertEqual(result["tracking"], "pending_next_prompt")
        live = {"hook_event_name": "UserPromptSubmit", "session_id": "closed-chat", "turn_id": "closed-root"}
        status_light.observe_root_event(live, self.runtime)
        status_light.observe_root_event(dict(live, hook_event_name="Stop"), self.runtime)
        result = await self.activate("closed-chat", observe=False)
        self.assertIsNone(result["turn_id"])
        self.assertEqual(result["context"], "")
        self.assertEqual(result["tracking"], "pending_next_prompt")

    async def test_broker_set_requires_nonce(self):
        active = await self.activate()
        previous = self.broker.state.snapshot()
        for nonce in (None, "", "malformed", 123):
            result = await self.set_status(active["turn_id"], binding_nonce=nonce)
            self.assertEqual(result["reason"], "missing_or_invalid_binding_nonce")
            self.assertEqual(previous, self.broker.state.snapshot())

    async def test_accepted_hook_counters_are_scoped_to_each_explicit_binding(self):
        first = await self.activate()
        await self.event("UserPromptSubmit", turn="next-root")
        result = await self.event("PreToolUse", turn="next-root", tool_name="exec", tool_use_id="call")
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"], {"UserPromptSubmit": 1, "PreToolUse": 1})
        refreshed = await self.activate(observe=False)
        self.assertNotEqual(first["binding_nonce"], refreshed["binding_nonce"])
        self.assertEqual(refreshed["hook_diagnostics"]["bound_event_counts"], {})
        self.assertIsNone(refreshed["hook_diagnostics"]["last_bound"])
        await self.event("UserPromptSubmit", turn="foreign-root", session_id="chat-b")
        result = await self.event("PreToolUse", turn="next-root", binding_nonce=first["binding_nonce"], tool_name="exec")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"], {})
        result = await self.event("PreToolUse", turn="next-root", tool_name="exec", tool_use_id="new-call")
        self.assertEqual(result["hook_diagnostics"]["bound_event_counts"], {"PreToolUse": 1})

    async def test_binding_persistence_failure_never_prevents_sync(self):
        await self.event("UserPromptSubmit", turn="current")
        with patch.object(status_light, "atomic_json", side_effect=OSError("disk unavailable")):
            active = await self.activate(observe=False)
            self.assertTrue(active["accepted"])
            self.assertEqual(active["warning"], "binding_not_saved")
            before = self.driver.sync_requests
            closed = await self.broker.handle({"action": "deactivate"})
        self.assertFalse(closed["desired"]["power"])
        self.assertEqual(closed["warning"], "binding_not_saved")
        self.assertEqual(self.driver.sync_requests, before + 1)
        self.assertEqual(status_light.compact_status(closed)["warning"], "binding_not_saved")

    async def test_rejected_binding_cannot_poison_next_activation(self):
        first = await self.activate()
        refreshed = await self.activate(observe=False)
        result = await self.event("UserPromptSubmit", turn="untrusted-new-turn", binding_nonce=first["binding_nonce"])
        self.assertFalse(result["accepted"])
        self.assertEqual(status_light.latest_root_turn("chat-a", self.runtime), refreshed["turn_id"])
        result = await self.activate(observe=False)
        self.assertEqual(result["turn_id"], refreshed["turn_id"])

    async def test_only_correlated_same_turn_answer_reopens_closed_metadata(self):
        active = await self.activate()
        turn = active["turn_id"]
        await self.event("PreToolUse", turn=turn, tool_name="request_user_input_async", tool_use_id="question",
                         tool_input={"questions": [{"title": "Continue?"}]})
        await self.event("Stop", turn=turn, last_assistant_message=self.marker(active, "needs_user"))
        self.assertIsNone(status_light.latest_root_turn("chat-a", self.runtime))
        await self.event("UserPromptSubmit", turn=turn, prompt="Unrelated message")
        self.assertIsNone(status_light.latest_root_turn("chat-a", self.runtime))
        prompt = "<send_user_message_question_reply>" + json.dumps([{
            "questionItemId": json.dumps(["request_user_input_async", "question", 0]), "answer": "yes"
        }]) + "</send_user_message_question_reply>"
        result = await self.event("UserPromptSubmit", turn=turn, prompt=prompt)
        self.assertTrue(result["resumed_turn"])
        self.assertEqual(status_light.latest_root_turn("chat-a", self.runtime), turn)
        refreshed = await self.activate(observe=False)
        self.assertEqual(refreshed["turn_id"], turn)

    async def test_invalid_wait_requests_are_rejected(self):
        for request in ({"action": "wait"}, {"action": "wait", "revision": True},
                        {"action": "wait", "revision": 0, "timeout": float("inf")},
                        {"action": "wait", "revision": 0, "timeout": "1"}):
            self.assertEqual((await self.broker.handle(request))["reason"], "invalid_wait")

    async def test_invalid_binding_records_do_not_crash_startup(self):
        for value in (None, [], 2, {"bound_chat": []}, {"bound_chat": "bad id"}):
            (self.runtime / "binding.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(status_light.Broker._load_binding())


class HookFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.directory_patch = patch.object(status_light, "data_dir", return_value=self.runtime)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)

    def test_non_object_json_is_ignored_without_error(self):
        for value in ([], 42, None, "text"):
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()))
            with patch.object(status_light.sys, "stdin", stdin), patch.object(status_light, "rpc", return_value={}), redirect_stdout(io.StringIO()):
                status_light.run_hook()

    def test_authentication_failure_is_ignored_without_error(self):
        stdin = io.TextIOWrapper(io.BytesIO(b'{"hook_event_name":"Stop"}'))
        with patch.object(status_light.sys, "stdin", stdin), patch.object(status_light, "rpc", side_effect=PermissionError("isolated fake failure")), redirect_stdout(io.StringIO()):
            status_light.run_hook()

    def test_absent_controller_hook_saves_identity_then_fails_without_starting_or_blocking(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "new-chat", "turn_id": "new-root", "prompt": "PRIVATE"}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        output = io.StringIO()
        with patch.object(status_light.sys, "stdin", stdin), patch.object(status_light, "rpc", side_effect=OSError("no broker")), patch.object(status_light, "ensure_broker") as start, redirect_stdout(output):
            status_light.run_hook()
        start.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(status_light.latest_root_turn("new-chat", self.runtime), "new-root")
        records = list((self.runtime / "turns").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertNotIn("PRIVATE", records[0].read_text(encoding="utf-8"))


class HookInstallTests(unittest.TestCase):
    def test_install_preserves_unrelated_hooks_without_duplicate_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_root = root / "codex"
            config_root.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            hooks_file = config_root / "hooks.json"
            existing = {"unrelated": {"keep": True}, "hooks": {"PreToolUse": [{"matcher": "exec", "hooks": [{"type": "command", "command": "echo preserve-this"}]}]}}
            hooks_file.write_text(json.dumps(existing), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(config_root)}), patch.object(status_light, "data_dir", return_value=runtime), patch.object(status_light, "runtime_config", return_value={"python": "X:/inert-python.exe"}):
                status_light.install_hooks()
                status_light.install_hooks()
            result = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(result["unrelated"], existing["unrelated"])
            groups = result["hooks"]["PreToolUse"]
            self.assertEqual(groups[0], existing["hooks"]["PreToolUse"][0])
            self.assertEqual(len(groups), 2)
            self.assertIn("PostToolUse", result["hooks"])


def confirmed_snapshot(chat="chat-a", turn="root-turn", nonce="a" * 32):
    state = LightState()
    state.activate(chat, turn, binding_nonce=nonce)
    result = state.snapshot()
    result.update({
        "tracking": "active",
        "device": {"connected": True, "observed": deepcopy(result["desired"]),
                   "applied_revision": result["revision"], "pending": False, "last_error": None},
        "hook_diagnostics": {"last_bound": {"event": "UserPromptSubmit", "received_at": 100,
                                             "accepted": True, "reason": "ok", "turn_id": turn}},
    })
    return result


class CompactStatusTests(unittest.TestCase):
    def test_hardware_confirmed_off_does_not_claim_pending_tracking_ready(self):
        state = LightState()
        state.activate("chat-a")
        result = confirmed_snapshot()
        result.update(state.snapshot())
        result["tracking"] = "pending_next_prompt"
        result["device"]["observed"]["power"] = False
        result["device"]["applied_revision"] = result["revision"]
        summary = status_light.compact_status(result, "chat-a")
        self.assertTrue(summary["confirmed"])
        self.assertTrue(summary["binding_accepted"])
        self.assertFalse(summary["tracking_ready"])
        self.assertEqual(summary["phase"], "idle")
        self.assertEqual(summary["tracking"], "pending_next_prompt")

    def test_normal_status_omits_turn_nonce_and_instruction_context(self):
        result = confirmed_snapshot()
        result["context"] = status_light.instruction_context(result)
        summary = status_light.compact_status(result, "chat-a")
        self.assertTrue(summary["confirmed"])
        self.assertTrue(summary["bound_to_current_chat"])
        for name in ("turn_id", "binding_nonce", "context"):
            self.assertNotIn(name, summary)
        encoded = json.dumps(summary)
        self.assertNotIn("root-turn", encoded)
        self.assertNotIn("a" * 32, encoded)
        self.assertNotIn("aaa-wudi:", encoded)
        self.assertEqual(summary["last_hook"]["event"], "UserPromptSubmit")

    def test_confirmation_requires_connection_revision_and_matching_hardware(self):
        base = confirmed_snapshot()
        cases = (
            {"connected": False},
            {"applied_revision": base["revision"] - 1},
            {"observed": None},
            {"observed": dict(base["desired"], power=False)},
            {"observed": dict(base["desired"], color="#00FF00")},
            {"observed": dict(base["desired"], brightness=20)},
            {"observed": dict(base["desired"], mode="RAINBOW")},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = deepcopy(base)
                result["device"].update(changes)
                self.assertFalse(status_light.compact_status(result, "chat-a")["confirmed"])

    def test_confirmed_power_off_does_not_require_firmware_color_to_reset(self):
        result = confirmed_snapshot()
        result["desired"]["power"] = False
        result["device"]["observed"].update(power=False, color="#00FF00", brightness=20)
        self.assertTrue(status_light.compact_status(result, "chat-a")["confirmed"])

    def test_context_is_exposed_only_for_accepted_current_chat_activation(self):
        result = confirmed_snapshot()
        result["context"] = status_light.instruction_context(result)
        for chat, accepted in (("other-chat", True), ("chat-a", False)):
            with self.subTest(chat=chat, accepted=accepted):
                result["accepted"] = accepted
                summary = status_light.compact_status(result, chat, include_context=True)
                self.assertNotIn("context", summary)
                self.assertNotIn("binding_nonce", summary)


class CliTests(unittest.TestCase):
    def run_cli(self, arguments, responses):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "chat-a", "CODEX_SESSION_ID": "parent-chat"}), \
                patch.object(status_light, "ensure_broker") as start, \
                patch.object(status_light, "rpc", side_effect=deepcopy(responses)) as request, \
                patch.object(status_light, "emit") as output:
            code = status_light.main(arguments)
        return code, output.call_args.args[0], request, start

    def test_default_activation_wait_preserves_turn_nonce_and_context(self):
        initial = confirmed_snapshot()
        initial.update(accepted=True, reason="ok", context=status_light.instruction_context(initial))
        waited = confirmed_snapshot()
        waited["confirmed"] = True
        code, output, request, start = self.run_cli(["activate"], [initial, waited])
        self.assertEqual(code, 0)
        start.assert_called_once_with()
        self.assertEqual(request.call_args_list[0].args[0], {"action": "activate", "chat_id": "chat-a"})
        self.assertEqual(request.call_args_list[1].args[0], {"action": "wait", "revision": initial["revision"], "timeout": 12})
        self.assertEqual(output["turn_id"], initial["turn_id"])
        self.assertEqual(output["binding_nonce"], initial["binding_nonce"])
        self.assertEqual(output["context"], initial["context"])
        self.assertTrue(output["confirmed"])

    def test_activation_superseded_during_wait_never_emits_new_binding_context(self):
        for chat in ("chat-b", "chat-a"):
            with self.subTest(chat=chat):
                initial = confirmed_snapshot()
                initial.update(accepted=True, context=status_light.instruction_context(initial))
                replaced = confirmed_snapshot(chat, "replacement-root", "b" * 32)
                replaced["revision"] = initial["revision"] + 1
                replaced["device"]["applied_revision"] = replaced["revision"]
                replaced["context"] = status_light.instruction_context(replaced)
                code, output, request, _ = self.run_cli(["activate"], [initial, replaced])
                self.assertEqual(code, 0)
                self.assertTrue(output["superseded"])
                self.assertEqual(output["reason"], "state_changed_while_waiting")
                for name in ("context", "turn_id", "binding_nonce"):
                    self.assertNotIn(name, output)
                self.assertNotIn("replacement-root", json.dumps(output))
                self.assertEqual(request.call_count, 2)

    def test_set_forwards_explicit_binding_nonce_without_starting_controller(self):
        current = confirmed_snapshot()
        current["accepted"] = True
        code, output, request, start = self.run_cli(["set", "completed", "--turn-id", "root-turn", "--binding-nonce", "a" * 32], [current])
        self.assertEqual(code, 0)
        start.assert_not_called()
        self.assertEqual(request.call_args.args[0]["binding_nonce"], "a" * 32)
        self.assertEqual(request.call_args.args[0]["chat_id"], "chat-a")
        self.assertNotIn("binding_nonce", output)

    def test_explicit_deactivate_is_global_and_does_not_activate(self):
        closed = confirmed_snapshot(chat=None)
        closed.update(enabled=False, accepted=True)
        closed["desired"]["power"] = False
        closed["device"]["observed"]["power"] = False
        code, output, request, start = self.run_cli(["deactivate", "--wait", "0"], [closed])
        self.assertEqual(code, 0)
        start.assert_not_called()
        self.assertEqual(request.call_args.args[0], {"action": "deactivate"})
        self.assertFalse(output["enabled"])


if __name__ == "__main__":
    unittest.main()
