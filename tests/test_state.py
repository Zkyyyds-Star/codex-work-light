"""Behavior tests; uses no device and no third-party dependency."""

import importlib.util
import json
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "light_state.py"
SPEC = importlib.util.spec_from_file_location("light_state", MODULE)
light_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(light_state)
LightState = light_state.LightState


class LightStateTests(unittest.TestCase):
    def setUp(self):
        self.state = LightState()
        self.state.activate("chat-a", "turn-1")

    def event(self, event_name, **fields):
        payload = {"session_id": "chat-a", "turn_id": "turn-1", "hook_event_name": event_name}
        payload.update(fields)
        return self.state.event(payload)

    def assert_color(self, color, power=True):
        desired = self.state.snapshot()["desired"]
        self.assertEqual(desired, {"power": power, "color": color, "brightness": 75, "mode": "SOLID"})

    def marker(self, state="completed", turn_id=None, binding_nonce=None):
        return "<!-- aaa-wudi:%s turn=%s binding=%s -->" % (
            state,
            self.state.turn_id if turn_id is None else turn_id,
            self.state.binding_nonce if binding_nonce is None else binding_nonce,
        )

    def reply(self, call_id="question", index=0, answer="Continue"):
        return "<send_user_message_question_reply>\n" + json.dumps([{
            "questionItemId": json.dumps(["request_user_input_async", call_id, index]),
            "answer": answer,
        }]) + "\n</send_user_message_question_reply>"

    def test_restart_is_disabled_even_with_persisted_binding(self):
        restarted = LightState(bound_chat="chat-a")
        self.assertFalse(restarted.snapshot()["desired"]["power"])
        self.assertFalse(restarted.event({"session_id": "chat-a", "turn_id": "new", "hook_event_name": "UserPromptSubmit"})["accepted"])

    def test_activate_sets_blue_and_all_status_colors(self):
        self.assert_color("#0000FF")
        for status, color in (("completed", "#00FF00"), ("blocked", "#FFFF00"), ("working", "#0000FF"), ("needs_user", "#FF0000")):
            self.assertTrue(self.state.set_status("chat-a", "turn-1", status)["accepted"])
            self.assert_color(color)

    def test_no_status_can_enable_inactive_state(self):
        self.state.deactivate()
        self.assertFalse(self.state.set_status("chat-a", "turn-1", "working")["accepted"])
        self.assert_color("#000000", False)

    def test_cross_chat_and_stale_turn_do_not_change_state(self):
        before = self.state.snapshot()
        self.assertFalse(self.state.set_status("chat-b", "turn-1", "completed")["accepted"])
        self.assertFalse(self.state.set_status("chat-a", "old", "completed")["accepted"])
        self.assertFalse(self.event("Stop", session_id="chat-b")["accepted"])
        self.assertFalse(self.event("Stop", turn_id="old")["accepted"])
        self.assertEqual(before, self.state.snapshot())

    def test_missing_turn_is_rejected_except_cancellation(self):
        self.state.activate("chat-a")
        self.assertFalse(self.state.set_status("chat-a", None, "completed")["accepted"])
        self.assertFalse(self.event("PreToolUse", turn_id=None, tool_name="read")["accepted"])
        self.assertFalse(self.event("UserPromptSubmit", turn_id=None)["accepted"])
        self.assertTrue(self.event("Interrupt", turn_id=None)["accepted"])
        self.assert_color("#000000", False)

    def test_activation_without_trusted_turn_binds_armed_and_dark(self):
        result = self.state.activate("chat-b")
        self.assertTrue(result["accepted"])
        self.assertTrue(self.state.enabled)
        self.assertEqual(self.state.bound_chat, "chat-b")
        self.assertIsNone(self.state.turn_id)
        self.assertEqual(self.state.phase, "idle")
        self.assertEqual(self.state.diagnostic, "pending_next_prompt")
        self.assert_color("#000000", False)

    def test_pending_rebinding_refreshes_nonce_without_starting_work(self):
        self.state.activate("chat-b")
        old_nonce = self.state.binding_nonce
        old_generation = self.state.binding_generation
        self.state.activate("chat-b")
        self.assertNotEqual(old_nonce, self.state.binding_nonce)
        self.assertGreater(self.state.binding_generation, old_generation)
        self.assertIsNone(self.state.turn_id)
        self.assert_color("#000000", False)
        result = self.event("UserPromptSubmit", session_id="chat-b", turn_id="next-root", binding_nonce=old_nonce)
        self.assertFalse(result["accepted"])
        self.assert_color("#000000", False)

    def test_pending_old_chat_and_retired_turn_events_cannot_start_work(self):
        old_marker = self.marker()
        self.state.activate("chat-a")
        before = self.state.snapshot()
        for name, fields in (
            ("UserPromptSubmit", {}),
            ("Stop", {"last_assistant_message": old_marker}),
            ("PreToolUse", {"tool_name": "exec", "tool_use_id": "late-tool"}),
            ("PostToolUse", {"tool_name": "request_user_input", "tool_response": {"answers": {"q": "yes"}}}),
            ("UserPromptSubmit", {"session_id": "chat-b", "turn_id": "foreign-root"}),
            ("UserPromptSubmit", {"turn_id": "child-root", "agent_id": "child"}),
        ):
            with self.subTest(name=name, fields=fields):
                self.assertFalse(self.event(name, **fields)["accepted"])
                self.assertEqual(before, self.state.snapshot())
        self.assertFalse(self.state.set_status("chat-a", "turn-1", "working", binding_nonce=self.state.binding_nonce)["accepted"])
        self.assert_color("#000000", False)

    def test_pending_only_valid_new_root_prompt_starts_blue(self):
        self.state.activate("chat-a")
        nonce = self.state.binding_nonce
        self.assertFalse(self.event("PreToolUse", turn_id="new-root", tool_name="read")["accepted"])
        self.assertFalse(self.event("UserPromptSubmit", turn_id=None)["accepted"])
        self.assert_color("#000000", False)
        result = self.event("UserPromptSubmit", turn_id="new-root")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["context_needed"])
        self.assertEqual(self.state.turn_id, "new-root")
        self.assertEqual(self.state.binding_nonce, nonce)
        self.assert_color("#0000FF")

    def test_green_and_yellow_survive_stop_until_new_turn(self):
        for status, color in (("completed", "#00FF00"), ("blocked", "#FFFF00")):
            self.state.set_status("chat-a", self.state.turn_id, status)
            self.event("Stop", turn_id=self.state.turn_id)
            self.assert_color(color)
            self.event("UserPromptSubmit", turn_id="next-" + status)
            self.assert_color("#0000FF")

    def test_stop_does_not_guess_completion(self):
        self.event("Stop")
        self.assert_color("#000000", False)
        self.assertEqual(self.state.diagnostic, "result_not_reported")
        self.assertTrue(self.state.enabled)

    def test_interrupt_off_then_next_turn_resumes_blue(self):
        self.state.set_status("chat-a", "turn-1", "needs_user")
        self.event("Interrupt")
        self.assert_color("#000000", False)
        self.assertTrue(self.state.enabled)
        self.assertFalse(self.state.waits)
        self.assertFalse(self.state.set_status("chat-a", "turn-1", "completed")["accepted"])
        self.assertFalse(self.event("PreToolUse", tool_name="request_user_input", tool_use_id="late")["accepted"])
        self.assertFalse(self.event("UserPromptSubmit")["accepted"])
        self.assert_color("#000000", False)
        self.event("UserPromptSubmit", turn_id="turn-2")
        self.assert_color("#0000FF")

    def test_session_end_disables_until_explicit_activation(self):
        self.event("SessionEnd", turn_id=None)
        self.assertFalse(self.state.enabled)
        self.assertEqual(self.state.bound_chat, "chat-a")
        self.assertFalse(self.event("UserPromptSubmit", turn_id="turn-2")["accepted"])

    def test_async_acknowledgement_and_normal_tools_never_clear_red(self):
        self.event("PreToolUse", tool_name="functions.request_user_input_async", tool_use_id="question")
        self.event("PostToolUse", tool_name="functions.request_user_input_async", tool_use_id="question", tool_response={"answers": {"q": "ack"}})
        self.event("PreToolUse", tool_name="exec", tool_use_id="ordinary")
        self.event("PostToolUse", tool_name="exec", tool_use_id="ordinary", tool_response={"success": True})
        self.event("Stop")
        self.assert_color("#FF0000")
        self.assertIn("question", self.state.waits)

    def test_new_prompt_keeps_async_wait_and_explicit_resolution_clears_it(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        result = self.event("UserPromptSubmit", turn_id="turn-2")
        self.assertTrue(result["context_needed"])
        self.assert_color("#FF0000")
        self.assertFalse(self.state.set_status("chat-a", "turn-1", "working", resolves_waits=True)["accepted"])
        self.state.set_status("chat-a", "turn-2", "working", resolves_waits=True)
        self.assert_color("#0000FF")

    def test_correlated_desktop_answer_resumes_same_turn(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_input={"questions": [{}]})
        self.event("PostToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_response={"accepted": True})
        self.assert_color("#FF0000")
        result = self.event("UserPromptSubmit", prompt=self.reply())
        self.assertTrue(result["changed"])
        self.assertTrue(result["context_needed"])
        self.assertTrue(result["resumed_turn"])
        self.assertEqual(self.state.diagnostic, "user_input_received")
        self.assertFalse(self.state.waits)
        self.assert_color("#0000FF")

    def test_final_wait_marker_and_same_turn_answers_complete_without_manual_lock(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_input={"questions": [{}, {}]})
        self.event("PostToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_response={"accepted": True})
        self.event("Stop", last_assistant_message="Waiting.\n" + self.marker("needs_user"))
        self.assertEqual(set(self.state.waits), {"question"})
        result = self.event("UserPromptSubmit", prompt=self.reply(index=0))
        self.assertTrue(result["resumed_turn"])
        self.assert_color("#FF0000")
        self.event("Stop", last_assistant_message=self.marker("needs_user"))
        self.assertEqual(set(self.state.waits), {"question"})
        result = self.event("UserPromptSubmit", prompt=self.reply(index=1))
        self.assertTrue(result["resumed_turn"])
        self.assertFalse(self.state.waits)
        self.assert_color("#0000FF")
        self.event("Stop", last_assistant_message=self.marker())
        self.assert_color("#00FF00")
        self.assertFalse(self.event("UserPromptSubmit", prompt=self.reply(index=1))["resumed_turn"])
        self.assert_color("#00FF00")

    def test_correlated_desktop_answer_can_resume_new_turn(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        result = self.event("UserPromptSubmit", turn_id="turn-2", prompt=self.reply())
        self.assertFalse(result["resumed_turn"])
        self.assertFalse(self.state.waits)
        self.assertEqual(self.state.turn_id, "turn-2")
        self.assert_color("#0000FF")

    def test_desktop_answer_clears_only_answered_question(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_input={"questions": [{}, {}]})
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="other", tool_input={"questions": [{}]})
        self.event("UserPromptSubmit", prompt=self.reply(index=0))
        self.assertEqual(self.state.waits["question"]["unanswered_questions"], [1])
        self.assert_color("#FF0000")
        self.event("UserPromptSubmit", prompt=self.reply(index=1))
        self.assertEqual(set(self.state.waits), {"other"})
        self.assert_color("#FF0000")
        self.event("UserPromptSubmit", prompt=self.reply(call_id="other"))
        self.assert_color("#0000FF")

    def test_unrelated_empty_malformed_or_copied_answers_keep_red(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_input={"questions": [{}]})
        for prompt in (
            self.reply(call_id="other"), self.reply(index=9), self.reply(answer=""),
            self.reply(answer=[]), self.reply(index=True), self.reply(index=-1),
            "Example:\n" + self.reply(), "```\n" + self.reply() + "\n```",
            "<send_user_message_question_reply>invalid JSON</send_user_message_question_reply>",
            "Continue", "<send_user_message_question_reply>{}</send_user_message_question_reply>",
        ):
            with self.subTest(prompt=prompt):
                before = self.state.snapshot()
                result = self.event("UserPromptSubmit", prompt=prompt)
                self.assertFalse(result["resumed_turn"])
                self.assertEqual(before, self.state.snapshot())

    def test_desktop_answer_identity_filters_run_before_resolution(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        for fields in ({"session_id": "other"}, {"turn_id": None}, {"agent_id": "child"}, {"binding_nonce": "old"}):
            with self.subTest(fields=fields):
                result = self.event("UserPromptSubmit", prompt=self.reply(), **fields)
                self.assertFalse(result["accepted"])
                self.assertFalse(result["resumed_turn"])
                self.assert_color("#FF0000")
        self.event("UserPromptSubmit", turn_id="turn-2")
        before = self.state.snapshot()
        self.assertFalse(self.event("UserPromptSubmit", prompt=self.reply())["accepted"])
        self.assertEqual(before, self.state.snapshot())

    def test_sync_questions_only_clear_when_actual_answers_return(self):
        for empty in ({}, {"answers": {}}, {"answers": {"q": {"answers": []}}}, {"ok": True}, "not JSON"):
            self.event("PreToolUse", tool_name="request_user_input", tool_use_id="question")
            self.event("PostToolUse", tool_name="request_user_input", tool_use_id="question", tool_response=empty)
            self.assert_color("#FF0000")
        response = {"content": [{"type": "text", "text": json.dumps({"answers": {"q": {"answers": ["Choice"]}}})}]}
        self.event("PostToolUse", tool_name="request_user_input", tool_use_id="question", tool_response=response)
        self.assert_color("#0000FF")

    def test_sync_answer_clears_only_matching_wait(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="async")
        self.event("PreToolUse", tool_name="request_user_input", tool_use_id="sync")
        self.event("PostToolUse", tool_name="request_user_input", tool_use_id="sync", tool_response={"answers": {"q": "yes"}})
        self.assertEqual(set(self.state.waits), {"async"})
        self.assert_color("#FF0000")

    def test_manual_user_request_resolves_on_next_user_prompt(self):
        self.state.set_status("chat-a", "turn-1", "needs_user")
        self.event("UserPromptSubmit", turn_id="turn-2")
        self.assert_color("#0000FF")

    def test_waits_override_completion_and_blocked(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        for status in ("completed", "blocked", "working"):
            self.state.set_status("chat-a", "turn-1", status)
            self.assert_color("#FF0000")

    def test_permission_is_correlated_through_pre_tool_when_id_missing(self):
        self.event("PreToolUse", tool_name="exec", tool_use_id="call", tool_input={"cmd": "test"})
        self.event("PermissionRequest", tool_name="exec", tool_input={"cmd": "test"})
        self.assertIn("call", self.state.waits)
        self.event("PostToolUse", tool_name="exec", tool_use_id="other", tool_response={"ok": True})
        self.assert_color("#FF0000")
        self.event("PostToolUse", tool_name="exec", tool_use_id="call", tool_response={"ok": True})
        self.assert_color("#0000FF")

    def test_ambiguous_permission_is_not_cleared_by_unrelated_post(self):
        for tool_id in ("one", "two"):
            self.event("PreToolUse", tool_name="exec", tool_use_id=tool_id)
        self.event("PermissionRequest", tool_name="exec")
        self.event("PostToolUse", tool_name="exec", tool_use_id="one")
        self.event("PostToolUse", tool_name="exec", tool_use_id="two")
        self.assert_color("#FF0000")

    def test_permission_does_not_downgrade_async_wait(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        self.event("PermissionRequest", tool_name="request_user_input_async", tool_use_id="question")
        self.event("PostToolUse", tool_name="request_user_input_async", tool_use_id="question", tool_response={"ok": True})
        self.assert_color("#FF0000")

    def test_rebinding_clears_waits_and_rejects_old_chat_and_generation(self):
        self.state.set_status("chat-a", "turn-1", "needs_user")
        generation = self.state.binding_generation
        self.state.activate("chat-b", "turn-b")
        self.assertFalse(self.state.waits)
        self.assertFalse(self.event("Stop")["accepted"])
        result = self.event("Stop", session_id="chat-b", turn_id="turn-b", binding_generation=generation)
        self.assertFalse(result["accepted"])
        self.assert_color("#0000FF")

    def test_old_prompt_cannot_replace_new_turn_and_duplicate_is_idempotent(self):
        self.event("UserPromptSubmit", turn_id="turn-2")
        self.state.set_status("chat-a", "turn-2", "completed")
        before = self.state.snapshot()
        self.assertFalse(self.event("UserPromptSubmit")["accepted"])
        result = self.event("UserPromptSubmit", turn_id="turn-2")
        self.assertFalse(result["changed"])
        self.assertEqual(before, self.state.snapshot())

    def test_subagents_cannot_independently_change_color(self):
        before = self.state.snapshot()
        for name, fields in (("SubagentStart", {}), ("SubagentStop", {}), ("Stop", {"agent_id": "child"}), ("UserPromptSubmit", {"is_subagent": True, "turn_id": "child-turn"})):
            self.assertFalse(self.event(name, **fields)["accepted"])
        self.assertEqual(before, self.state.snapshot())

    def test_snapshot_is_json_serializable_and_cannot_mutate_state(self):
        self.state.set_status("chat-a", "turn-1", "needs_user")
        snapshot = self.state.snapshot()
        json.dumps(snapshot)
        snapshot["waits"].clear()
        self.assertTrue(self.state.waits)

    def test_revision_changes_only_with_visible_state(self):
        revision = self.state.revision
        result = self.event("PreToolUse", tool_name="exec", tool_use_id="call")
        self.assertFalse(result["changed"])
        self.assertEqual(self.state.revision, revision)
        self.state.set_status("chat-a", "turn-1", "completed")
        self.assertGreater(self.state.revision, revision)

    def test_each_activation_refreshes_nonce_including_same_chat_and_turn(self):
        old_nonce = self.state.binding_nonce
        old_generation = self.state.binding_generation
        self.state.activate("chat-a", "turn-1")
        self.assertNotEqual(old_nonce, self.state.binding_nonce)
        self.assertRegex(self.state.binding_nonce, r"^[0-9a-f]{32}$")
        self.assertEqual(self.state.binding_generation, old_generation + 1)
        self.assertEqual(self.state.snapshot()["binding_nonce"], self.state.binding_nonce)
        self.assert_color("#0000FF")
        result = self.event("UserPromptSubmit")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["context_needed"])
        self.assertFalse(result["changed"])

    def test_broker_can_supply_binding_nonce(self):
        self.state.activate("chat-b", "turn-b", binding_nonce="supplied-nonce")
        self.assertEqual(self.state.snapshot()["binding_nonce"], "supplied-nonce")

    def test_deactivate_clears_binding_and_rejects_late_events(self):
        marker = self.marker()
        self.state.deactivate("chat-a")
        self.assertIsNone(self.state.bound_chat)
        self.assertIsNone(self.state.binding_nonce)
        self.assertIsNone(self.state.turn_id)
        self.assertFalse(self.event("Stop", last_assistant_message=marker)["accepted"])
        self.assertFalse(self.event("UserPromptSubmit", turn_id="turn-2")["accepted"])
        self.assert_color("#000000", False)

    def test_fresh_restart_has_no_reusable_nonce(self):
        restarted = LightState(bound_chat="chat-a")
        self.assertIsNone(restarted.binding_nonce)
        self.assertFalse(restarted.snapshot()["desired"]["power"])

    def test_manual_set_nonce_rejects_same_turn_prior_binding(self):
        old_nonce = self.state.binding_nonce
        self.state.activate("chat-a", "turn-1")
        before = self.state.snapshot()
        result = self.state.set_status("chat-a", "turn-1", "completed", binding_nonce=old_nonce)
        self.assertEqual(result["reason"], "stale_binding")
        self.assertEqual(before, self.state.snapshot())
        self.assertTrue(self.state.set_status("chat-a", "turn-1", "completed", binding_nonce=self.state.binding_nonce)["accepted"])
        self.assert_color("#00FF00")

    def test_final_markers_report_all_terminal_colors(self):
        for state, color in (("completed", "#00FF00"), ("needs_user", "#FF0000"), ("blocked", "#FFFF00")):
            with self.subTest(state=state):
                self.state.activate("chat-a", "turn-1")
                result = self.event("Stop", last_assistant_message="Task result.\n\n" + self.marker(state) + "\r\n\r\n")
                self.assertTrue(result["accepted"])
                self.assert_color(color)
                self.event("Stop")
                self.assert_color(color)

    def test_plan_marker_must_follow_closed_plan_block(self):
        message = "<proposed_plan>\n# A plan\nApply the change.\n</proposed_plan>\n\n" + self.marker("needs_user")
        self.event("Stop", last_assistant_message=message)
        self.assert_color("#FF0000")
        self.assertEqual(self.state.waits["manual"]["kind"], "manual")
        self.event("UserPromptSubmit", turn_id="turn-2")
        self.assert_color("#0000FF")

    def test_plan_marker_inside_or_after_unclosed_plan_is_unreported(self):
        for message in (
            "<proposed_plan>\n" + self.marker("needs_user") + "\n</proposed_plan>",
            "<proposed_plan>\nA plan\n" + self.marker("needs_user"),
        ):
            with self.subTest(message=message):
                self.state.activate("chat-a", "turn-1")
                self.event("Stop", last_assistant_message=message)
                self.assert_color("#000000", False)
                self.assertEqual(self.state.diagnostic, "invalid_status_marker")

    def test_copied_fenced_inline_or_malformed_marker_is_unreported(self):
        marker = self.marker()
        messages = (
            "```text\n" + marker + "\n```",
            "```text\n" + marker,
            "~~~~python\n" + marker,
            "    " + marker,
            "> " + marker,
            "`" + marker + "`",
            "Quoted example: " + marker,
            marker + "\nMore result text.",
            marker.replace("completed", "working"),
            marker.replace(" turn=", "  turn="),
            marker.replace(" binding=", " credential="),
        )
        for message in messages:
            with self.subTest(message=message):
                self.state.activate("chat-a", "turn-1")
                self.event("Stop", last_assistant_message=message)
                self.assert_color("#000000", False)
                self.assertEqual(self.state.diagnostic, "invalid_status_marker")

    def test_fence_closes_before_valid_final_marker(self):
        marker = self.marker()
        message = "Example:\n```text\n" + marker + "\n````\nActual result.\n" + marker
        self.event("Stop", last_assistant_message=message)
        self.assert_color("#00FF00")

    def test_list_fences_end_before_final_marker(self):
        for example in (
            "- ```text\n  value\n  ```\n",
            "1. ~~~~text\n   value\n   ~~~~~\n",
            "  - ```text\n    <proposed_plan>\n    ```\n",
            "- ```text\n  value\nA root paragraph ends the list.\n",
        ):
            with self.subTest(example=example):
                self.state.activate("chat-a", "turn-1")
                self.event("Stop", last_assistant_message=example + self.marker())
                self.assert_color("#00FF00")

    def test_dedented_fence_starts_new_root_code_block(self):
        message = "- ```text\n  value\n```\n" + self.marker()
        self.event("Stop", last_assistant_message=message)
        self.assert_color("#000000", False)
        self.assertEqual(self.state.diagnostic, "invalid_status_marker")

    def test_prose_or_code_mentions_of_plan_tags_do_not_open_a_plan(self):
        message = "The `<proposed_plan>` delimiter is documented.\n```text\n<proposed_plan>\n```\n" + self.marker()
        self.event("Stop", last_assistant_message=message)
        self.assert_color("#00FF00")

    def test_stale_marker_binding_or_turn_does_not_change_current_lamp(self):
        for marker in (self.marker(binding_nonce="retired-nonce"), self.marker(turn_id="old-turn")):
            with self.subTest(marker=marker):
                before = self.state.snapshot()
                result = self.event("Stop", last_assistant_message=marker)
                self.assertFalse(result["accepted"])
                self.assertEqual(before, self.state.snapshot())

    def test_same_chat_same_turn_rebind_rejects_old_marker(self):
        marker = self.marker()
        self.state.activate("chat-a", "turn-1")
        before = self.state.snapshot()
        result = self.event("Stop", last_assistant_message=marker)
        self.assertEqual(result["reason"], "stale_status_binding")
        self.assertEqual(before, self.state.snapshot())
        self.event("Stop", last_assistant_message=self.marker())
        self.assert_color("#00FF00")

    def test_replay_after_cancel_and_new_turn_cannot_relight(self):
        marker = self.marker()
        self.event("Interrupt")
        before = self.state.snapshot()
        self.assertFalse(self.event("Stop", last_assistant_message=marker)["accepted"])
        self.assertEqual(before, self.state.snapshot())
        self.event("UserPromptSubmit", turn_id="turn-2")
        before = self.state.snapshot()
        self.assertFalse(self.event("Stop", turn_id="turn-2", last_assistant_message=marker)["accepted"])
        self.assertEqual(before, self.state.snapshot())

    def test_foreign_missing_identity_and_child_markers_cannot_report_result(self):
        marker = self.marker()
        for fields in ({"session_id": "chat-b"}, {"session_id": None}, {"turn_id": None}, {"agent_id": "child"}):
            with self.subTest(fields=fields):
                before = self.state.snapshot()
                self.assertFalse(self.event("Stop", last_assistant_message=marker, **fields)["accepted"])
                self.assertEqual(before, self.state.snapshot())

    def test_marker_in_non_final_payload_field_does_not_report_result(self):
        self.event("Stop", message=self.marker(), transcript=self.marker())
        self.assert_color("#000000", False)
        self.assertEqual(self.state.diagnostic, "result_not_reported")

    def test_tool_waits_override_terminal_markers_and_invalid_results(self):
        self.event("PreToolUse", tool_name="request_user_input_async", tool_use_id="question")
        for message in (self.marker(), self.marker("blocked"), "Quoted example: " + self.marker()):
            self.event("Stop", last_assistant_message=message)
            self.assert_color("#FF0000")
            self.assertIn("question", self.state.waits)

    def test_injected_payload_nonce_must_match_binding(self):
        before = self.state.snapshot()
        result = self.event("Stop", last_assistant_message=self.marker(), binding_nonce="old-binding")
        self.assertEqual(result["reason"], "stale_binding")
        self.assertEqual(before, self.state.snapshot())


if __name__ == "__main__":
    unittest.main()
