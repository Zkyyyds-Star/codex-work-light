"""Pure state machine for the opt-in aaa-wudi task light.

No device, process, clock, filesystem, or environment access belongs here. The
trusted broker supplies the current root chat and turn identities. In particular,
session_id alone must never be used to infer that an arbitrary hook starts a turn.
"""

from copy import deepcopy
import json
import re
import uuid


COLORS = {
    "working": "#0000FF",
    "completed": "#00FF00",
    "needs_user": "#FF0000",
    "blocked": "#FFFF00",
    "idle": "#000000",
}

_STATUS_MARKER = re.compile(
    r"<!-- aaa-wudi:(completed|needs_user|blocked) turn=([^\s<>]+) binding=([^\s<>]+) -->"
)
_FENCE_START = re.compile(r"( {0,3}(?:(?:[-+*]|\d{1,9}[.)]) +)?)(`{3,}|~{3,})(.*)$")
_PLAN_TAG = re.compile(r" {0,3}(</?proposed_plan>)\s*")


def _final_status_marker(message):
    """Read the final root-message protocol line, never copied code or plans.

    A marker-looking message that fails the format is an unreported result.
    A well-formed marker remains available for identity validation, so a delayed
    marker cannot make a fresh binding fall through to an unreported Stop.
    """
    if not isinstance(message, str) or "<!-- aaa-wudi:" not in message:
        return None, None
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None, "invalid_status_marker"
    marker = _STATUS_MARKER.fullmatch(lines[-1])
    if marker is None:
        return None, "invalid_status_marker"
    fence_char = None
    fence_length = 0
    fence_indent = 0
    plan_depth = 0
    for line in lines[:-1]:
        if fence_char is not None:
            # 列表围栏随容器缩进结束；结束行也可能是新的根级围栏。
            if fence_indent and line.strip() and not line.startswith(" " * fence_indent):
                fence_char = None
            else:
                if re.fullmatch(r" {%s,%s}" % (fence_indent, fence_indent + 3)
                                + re.escape(fence_char) + r"{%s,}\s*" % fence_length, line):
                    fence_char = None
                continue
        fence = _FENCE_START.fullmatch(line)
        if fence is not None:
            prefix, token, info = fence.groups()
            # Backticks are forbidden in a backtick fence's information string.
            if token[0] != "`" or "`" not in info:
                fence_char, fence_length = token[0], len(token)
                fence_indent = len(prefix) if prefix.strip() else 0
                continue
        # Indented/quoted plan examples are not actual plan delimiters.
        if line.startswith(("    ", "\t", ">")):
            continue
        # The client protocol requires plan delimiters on their own lines.
        # A prose/inline-code mention of a tag must not open an actual plan.
        tag = _PLAN_TAG.fullmatch(line)
        if tag is not None:
            if tag.group(1).startswith("</"):
                plan_depth = max(0, plan_depth - 1)
            else:
                plan_depth += 1
    # 标记已保证无缩进，位于列表容器外；根级围栏仍必须显式闭合。
    if (fence_char is not None and not fence_indent) or plan_depth:
        return None, "invalid_status_marker"
    return marker.groups(), None


def _identity(value):
    return None if value is None or str(value).strip() == "" else str(value)


def _tool_name(value):
    return str(value or "").replace("__", ".").replace(":", ".").split(".")[-1]


def _actual_answer(value):
    if isinstance(value, (dict, list, tuple)):
        items = value.values() if isinstance(value, dict) else value
        return any(_actual_answer(item) for item in items)
    return isinstance(value, str) and bool(value.strip())


def _has_answers(response, depth=0):
    """Recognize real answers, including the JSON text MCP envelope.

    A successful tool invocation, a question list, an empty answers object, and an
    asynchronous request's acknowledgement do not constitute a user answer.
    """
    if depth > 8:
        return False
    if isinstance(response, str):
        try:
            return _has_answers(json.loads(response), depth + 1)
        except (TypeError, ValueError):
            return False
    if isinstance(response, list):
        return any(_has_answers(item, depth + 1) for item in response)
    if not isinstance(response, dict):
        return False
    return _actual_answer(response.get("answers")) or any(
        _has_answers(response[key], depth + 1)
        for key in ("result", "output", "response", "data", "structuredContent", "content", "text")
        if key in response
    )


def _async_answers(prompt):
    """Read only correlated answer identities from the desktop reply envelope."""
    if not isinstance(prompt, str):
        return {}
    match = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", prompt, re.DOTALL)
    if match is None:
        return {}
    try:
        items = json.loads(match.group(1))
    except (TypeError, ValueError):
        return {}
    if not isinstance(items, list):
        return {}
    answered = {}
    for item in items:
        if not isinstance(item, dict) or not _actual_answer(item.get("answer")):
            continue
        try:
            identity = json.loads(item.get("questionItemId", ""))
        except (TypeError, ValueError):
            continue
        if (not isinstance(identity, list) or len(identity) != 3
                or identity[0] != "request_user_input_async"
                or not isinstance(identity[1], str)
                or type(identity[2]) is not int or identity[2] < 0):
            continue
        answered.setdefault(identity[1], set()).add(identity[2])
    return answered


class LightState:
    """Only explicit activation enables the light; restart always starts dark."""

    def __init__(self, bound_chat=None):
        self.enabled = False
        self.bound_chat = _identity(bound_chat)
        self.turn_id = self.binding_nonce = None
        self.phase = "idle"
        self.waits = {}
        self.diagnostic = "not_enabled"
        self.revision = self.binding_generation = self._sequence = 0
        self._open_tools = {}
        self._retired_turns = set()

    def snapshot(self):
        phase = "needs_user" if self.waits and self.enabled else self.phase
        return {
            "enabled": self.enabled,
            "bound_chat": self.bound_chat,
            "turn_id": self.turn_id,
            "phase": phase,
            "waits": deepcopy(self.waits),
            "diagnostic": self.diagnostic,
            "revision": self.revision,
            "binding_generation": self.binding_generation,
            "binding_nonce": self.binding_nonce,
            "desired": {
                "power": bool(self.enabled and self.bound_chat and phase != "idle"),
                "color": COLORS[phase],
                "brightness": 75,
                "mode": "SOLID",
            },
        }

    def _result(self, before=None, accepted=True, reason="ok", context_needed=False, resumed_turn=False):
        changed = before is not None and before != self.snapshot()
        if changed:
            self.revision += 1
        return {
            "accepted": accepted,
            "changed": changed,
            "reason": reason,
            "context_needed": context_needed,
            "resumed_turn": resumed_turn,
        }

    def _reject(self, reason):
        return self._result(accepted=False, reason=reason)

    def _retire_current_turn(self):
        if self.bound_chat is not None and self.turn_id is not None:
            self._retired_turns.add((self.bound_chat, self.turn_id))

    def activate(self, chat_id, turn_id=None, binding_nonce=None):
        chat_id = _identity(chat_id)
        if chat_id is None:
            return self._reject("missing_chat_id")
        before = self.snapshot()
        self._retire_current_turn()
        self.bound_chat = chat_id
        self.turn_id = _identity(turn_id)
        self.enabled = True
        # Binding intent is independent of actual task-event connectivity.
        # Without a trusted root turn, arm the binding while keeping it dark;
        # the next valid UserPromptSubmit will establish working state.
        self.phase = "working" if self.turn_id is not None else "idle"
        self.waits.clear()
        self._open_tools.clear()
        self.binding_generation += 1
        self.binding_nonce = _identity(binding_nonce) or uuid.uuid4().hex
        self.diagnostic = "activated" if self.turn_id is not None else "pending_next_prompt"
        return self._result(before, reason="ok" if self.turn_id is not None else "pending_next_prompt")

    def deactivate(self, chat_id=None):
        if chat_id is not None and _identity(chat_id) != self.bound_chat:
            return self._reject("different_chat")
        before = self.snapshot()
        self._retire_current_turn()
        self.enabled = False
        self.bound_chat = None
        self.binding_nonce = None
        self.phase = "idle"
        self.turn_id = None
        self.waits.clear()
        self._open_tools.clear()
        self.binding_generation += 1
        self.diagnostic = "deactivated"
        return self._result(before)

    def _check_current(self, chat_id, turn_id, require_turn=True):
        if not self.enabled:
            return "not_enabled"
        if _identity(chat_id) != self.bound_chat:
            return "different_chat"
        incoming = _identity(turn_id)
        if require_turn and (incoming is None or self.turn_id is None):
            return "missing_turn_id"
        if incoming is not None and incoming != self.turn_id:
            return "stale_turn"
        return None

    def set_status(self, chat_id, turn_id, state, reason="", wait_id=None, resolves_waits=False, binding_nonce=None):
        if state not in COLORS or state == "idle":
            return self._reject("invalid_state")
        error = self._check_current(chat_id, turn_id)
        if error:
            return self._reject(error)
        if binding_nonce is not None and binding_nonce != self.binding_nonce:
            return self._reject("stale_binding")
        before = self.snapshot()
        if resolves_waits:
            self.waits.clear()
        # 最终红灯是已有等待的汇总，不能再叠加一个永远未答的人工问题。
        if state == "needs_user" and not self.waits:
            key = str(wait_id) if wait_id is not None else "manual"
            self.waits[key] = {
                "kind": "manual",
                "tool_name": "",
                "turn_id": self.turn_id,
            }
        self.phase = "needs_user" if self.waits else state
        self.diagnostic = str(reason or ("unresolved_waits" if self.waits and state != "needs_user" else state))
        return self._result(before, reason="unresolved_waits" if self.waits and state != "needs_user" else "ok")

    def _new_key(self, prefix):
        self._sequence += 1
        return "%s:%s" % (prefix, self._sequence)

    @staticmethod
    def _is_subagent(payload):
        return bool(
            payload.get("is_subagent")
            or payload.get("subagent_id")
            or payload.get("parent_agent_id")
            or payload.get("parent_tool_use_id")
            or payload.get("agent_id")
        )

    def _find_open_tool(self, payload, name):
        tool_id = _identity(payload.get("tool_use_id"))
        if tool_id is not None:
            return tool_id
        candidates = [
            key for key, item in self._open_tools.items()
            if item["turn_id"] == self.turn_id and item["tool_name"] == name
            and ("tool_input" not in payload or item["tool_input"] == payload["tool_input"])
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _add_wait(self, key, kind, name, question_count=None):
        # A permission prompt for an async question must not downgrade its lock
        # into one that the async acknowledgement would clear.
        if key not in self.waits:
            self.waits[key] = {"kind": kind, "tool_name": name, "turn_id": self.turn_id}
            if kind == "async_user_input" and question_count:
                self.waits[key]["unanswered_questions"] = list(range(question_count))
        self._settle_waits()

    def _settle_waits(self, diagnostic="user_input_received", pending="waiting_for_user"):
        self.phase = "needs_user" if self.waits else "working"
        self.diagnostic = pending if self.waits else diagnostic

    def _resolve_async_answers(self, prompt):
        changed = False
        for key, indices in _async_answers(prompt).items():
            wait = self.waits.get(key)
            if not wait or wait["kind"] != "async_user_input":
                continue
            pending = wait.get("unanswered_questions")
            if pending is not None:
                remaining = [index for index in pending if index not in indices]
                if remaining == pending:
                    continue
                if remaining:
                    wait["unanswered_questions"] = remaining
                else:
                    del self.waits[key]
            else:
                del self.waits[key]
            changed = True
        return changed

    def event(self, payload):
        if not isinstance(payload, dict):
            return self._reject("invalid_payload")
        name = str(payload.get("hook_event_name") or "")
        if name in ("SubagentStart", "SubagentStop") or self._is_subagent(payload):
            return self._reject("subagent_event")
        chat_id = _identity(payload.get("session_id", payload.get("chat_id")))
        if chat_id != self.bound_chat:
            return self._reject("different_chat")
        if not self.enabled:
            return self._reject("not_enabled")
        generation = payload.get("binding_generation")
        if generation is not None and generation != self.binding_generation:
            return self._reject("stale_binding")
        nonce = payload.get("binding_nonce")
        if nonce is not None and nonce != self.binding_nonce:
            return self._reject("stale_binding")
        turn_id = _identity(payload.get("turn_id"))
        before = self.snapshot()

        if name == "UserPromptSubmit":
            if turn_id is None:
                return self._reject("missing_turn_id")
            if turn_id == self.turn_id:
                # Desktop answers may steer the existing turn, rather than
                # creating a new turn. Match the actual question call; an
                # unrelated/empty prompt or an acknowledgement clears nothing.
                if self._resolve_async_answers(payload.get("prompt")):
                    self._settle_waits()
                    # 只有真正消费了匹配答案，Broker 才能重开同轮身份缓存。
                    return self._result(before, context_needed=True, resumed_turn=True)
                return self._result(before, reason="duplicate_turn", context_needed=True)
            if (chat_id, turn_id) in self._retired_turns:
                return self._reject("stale_turn")
            self._retire_current_turn()
            self.turn_id = turn_id
            self._open_tools.clear()
            # A new user message answers a manually reported request. Async and
            # unresolved permission requests require an actual answer/decision.
            self.waits = {key: value for key, value in self.waits.items() if value["kind"] != "manual"}
            self._resolve_async_answers(payload.get("prompt"))
            self._settle_waits("new_turn", "unresolved_waits")
            return self._result(before, context_needed=True)

        if name in ("Interrupt", "TurnCancelled", "TurnAborted", "SessionEnd"):
            error = self._check_current(chat_id, turn_id, require_turn=False)
            if error:
                return self._reject(error)
            self.waits.clear()
            self._open_tools.clear()
            self.phase = "idle"
            self.diagnostic = "session_ended" if name == "SessionEnd" else "cancelled"
            # Retire cancellation's token as well: late tool results or a late
            # explicit completion from the cancelled work must not relight it.
            self._retire_current_turn()
            self.turn_id = None
            if name == "SessionEnd":
                self.enabled = False
                self.binding_generation += 1
                self.binding_nonce = None
            return self._result(before)

        error = self._check_current(chat_id, turn_id)
        if error:
            return self._reject(error)

        if name == "Stop":
            marker, marker_error = _final_status_marker(payload.get("last_assistant_message"))
            if marker is not None:
                state, marker_turn, marker_binding = marker
                if marker_turn != self.turn_id:
                    return self._reject("stale_status_turn")
                if marker_binding != self.binding_nonce:
                    return self._reject("stale_status_binding")
                return self.set_status(chat_id, turn_id, state)
            if self.waits:
                self._settle_waits()
            elif self.phase not in ("completed", "blocked"):
                self.phase = "idle"
                self.diagnostic = marker_error or "result_not_reported"
            return self._result(before)

        tool_name = _tool_name(payload.get("tool_name"))
        if name == "PreToolUse":
            key = _identity(payload.get("tool_use_id")) or self._new_key("tool")
            self._open_tools[key] = {"tool_name": tool_name, "tool_input": deepcopy(payload.get("tool_input")), "turn_id": self.turn_id}
            if tool_name in ("request_user_input", "request_user_input_async"):
                tool_input = payload.get("tool_input")
                questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
                count = len(questions) if isinstance(questions, list) else None
                self._add_wait(key, "async_user_input" if tool_name.endswith("_async") else "sync_user_input", tool_name, count)
            return self._result(before)

        if name == "PermissionRequest":
            key = self._find_open_tool(payload, tool_name) or self._new_key("permission")
            self._add_wait(key, "permission", tool_name)
            return self._result(before)

        if name in ("PostToolUse", "PostToolUseFailure"):
            key = self._find_open_tool(payload, tool_name)
            wait = self.waits.get(key)
            if wait and (
                wait["kind"] == "permission"
                or (wait["kind"] == "sync_user_input" and name == "PostToolUse" and _has_answers(payload.get("tool_response")))
            ):
                del self.waits[key]
                self._settle_waits()
            if key is not None:
                self._open_tools.pop(key, None)
            return self._result(before)

        return self._reject("unsupported_event")
