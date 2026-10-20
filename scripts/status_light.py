"""Opt-in, local-only CLI and single-instance broker for aaa无敌.

The broker owns BLE. Hooks only submit small local events and never wait for BLE.
Only a binding is persisted; an enabled flag is deliberately never restored.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from light_state import LightState
from light_events import file_lock, latest_root_turn, observe_root_event, prune_expired

SKILL = Path(__file__).resolve().parents[1]
IDENTITY = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
MAX_MESSAGE = 1048576
LOGGER = logging.getLogger("aaa-wudi")


def runtime_config():
    try:
        value = json.loads((SKILL / "runtime.json").read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("runtime.json must be an object")
        for name in ("python", "data_dir"):
            if not isinstance(value.get(name), str) or not Path(value[name]).is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        if Path(value["python"]).suffix.lower() != ".exe" or not Path(value["python"]).is_file():
            raise ValueError("python must point to an installed Python .exe")
        if not isinstance(value.get("device_address"), str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value["device_address"]):
            raise ValueError("device_address must be a Bluetooth MAC address")
        return value
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"aaa无敌未正确安装，请先运行根目录 install.ps1：{error}") from error


def data_dir():
    return Path(runtime_config()["data_dir"])


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(16) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def frame(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE:
        raise ValueError("IPC message exceeds 1 MiB")
    return len(payload).to_bytes(4, "big") + payload


async def read_frame(reader):
    size = int.from_bytes(await reader.readexactly(4), "big")
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Invalid IPC frame size")
    return json.loads((await reader.readexactly(size)).decode("utf-8"))


def current_chat():
    # Child agents share SESSION_ID with their parent; THREAD_ID is authoritative.
    chat = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not chat or not IDENTITY.fullmatch(chat):
        raise ValueError("No valid current Codex chat identity; activation was not attempted.")
    return chat


def rpc(request, timeout=2.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("IPC timeout must be positive and finite")
    return asyncio.run(_rpc(request, timeout))


async def _rpc(request, timeout):
    endpoint = json.loads((data_dir() / "endpoint.json").read_text(encoding="utf-8"))
    if (not isinstance(endpoint, dict) or type(endpoint.get("port")) is not int
            or not 0 < endpoint["port"] < 65536 or not isinstance(endpoint.get("key"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", endpoint["key"])):
        raise ValueError("Invalid local controller endpoint; run activate to restart it")
    # 一个超时覆盖连接、发送、分段读取；慢速分片不能续期。
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"], limit=MAX_MESSAGE + 4)
        try:
            writer.write(frame({"key": endpoint["key"], "request": request}))
            await writer.drain()
            return await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()


def ensure_broker():
    try:
        return rpc({"action": "status"}, timeout=0.5)
    except (OSError, EOFError, ValueError):
        pass
    config = runtime_config()
    data_dir().mkdir(parents=True, exist_ok=True)
    log_path = data_dir() / "launcher.log"
    with log_path.open("ab") as log:
        subprocess.Popen(
            [config["python"], "-X", "utf8", "-B", str(Path(__file__).resolve()), "broker"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            return rpc({"action": "status"}, timeout=0.5)
        except (OSError, EOFError, ValueError):
            time.sleep(0.1)
    raise TimeoutError("Could not start local status-light controller; see launcher.log")


def instruction_context(snapshot):
    turn, nonce = snapshot.get("turn_id"), snapshot.get("binding_nonce")
    if not turn or not IDENTITY.fullmatch(turn) or not nonce:
        return ""
    return (
        "aaa无敌仅跟踪本根聊天：工作蓝/完成绿/等用户红/阻塞黄，常亮75%。"
        "仅在最终答复末尾单独一行输出隐藏标记："
        f"<!-- aaa-wudi:STATE turn={turn} binding={nonce} -->。"
        "STATE取completed、needs_user、blocked；方案交付待决定用needs_user，"
        "标记放在proposed_plan块外，勿用于commentary/示例。"
        "开始/提问/取消由Hooks处理；未解决等待不得报完成，故障不阻塞任务。"
    )


class Broker:
    def __init__(self, state=None, driver=None):
        self.state = state or LightState(self._load_binding())
        self.driver = driver
        self.started = time.monotonic()
        self.stopping = asyncio.Event()
        self.closing = False
        self.last_hook = self.last_bound_hook = self.last_rejection = None
        self.hook_count = 0
        self.bound_event_counts = {}

    @staticmethod
    def _load_binding():
        try:
            value = json.loads((data_dir() / "binding.json").read_text(encoding="utf-8"))
            chat = value.get("bound_chat") if isinstance(value, dict) else None
            return chat if isinstance(chat, str) and IDENTITY.fullmatch(chat) else None
        except (OSError, ValueError):
            return None

    def snapshot(self):
        result = self.state.snapshot()
        result.update({
            "device": self.driver.snapshot() if self.driver else {
                "connected": False, "observed": None, "applied_revision": -1,
                "last_error": "driver_not_started", "pending": True,
            },
            "broker_pid": os.getpid(), "uptime_seconds": round(time.monotonic() - self.started, 1),
            "display_name": "aaa无敌", "runtime_version": 4,
            "binding_accepted": bool(result["enabled"] and result["bound_chat"]),
            "tracking_ready": bool(result["enabled"] and result["bound_chat"] and result["turn_id"]),
            "tracking": "disabled" if not result["enabled"] else "active" if result["turn_id"] else "pending_next_prompt",
            "hook_diagnostics": {"count": self.hook_count, "last_received": self.last_hook,
                                 "last_bound": self.last_bound_hook, "last_rejected": self.last_rejection,
                                 "bound_event_counts": dict(self.bound_event_counts)},
        })
        return result

    def _record_hook(self, event, outcome):
        # Diagnostics deliberately contain neither prompts nor tool/message text.
        record = {
            "event": str(event.get("hook_event_name", ""))[:40],
            "chat_id": str(event.get("session_id", ""))[:200],
            "turn_id": str(event.get("turn_id", ""))[:200],
            "received_at": round(time.time(), 3),
            "accepted": bool(outcome.get("accepted")),
            "reason": outcome.get("reason", "ok"),
        }
        self.hook_count += 1
        self.last_hook = record
        if event.get("session_id") == self.state.bound_chat and not LightState._is_subagent(event):
            self.last_bound_hook = record
            if record["accepted"]:
                name = record["event"]
                self.bound_event_counts[name] = self.bound_event_counts.get(name, 0) + 1
        if not record["accepted"]:
            self.last_rejection = record

    async def handle(self, request):
        if not isinstance(request, dict):
            return {"accepted": False, "reason": "invalid_request"}
        action = request.get("action")
        if self.closing and action not in ("status", "wait"):
            return {"accepted": False, "reason": "controller_stopping"}
        if action == "status":
            return self.snapshot()
        if action == "wait":
            revision, timeout = request.get("revision"), request.get("timeout", 15)
            if (type(revision) is not int or revision < 0 or isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
                return {"accepted": False, "reason": "invalid_wait"}
            confirmed = self.driver and await self.driver.wait_confirmed(revision, min(30.0, timeout))
            result = self.snapshot()
            result["confirmed"] = bool(confirmed and result["revision"] == revision)
            return result
        if action == "activate":
            chat = request.get("chat_id", "")
            if not isinstance(chat, str) or not IDENTITY.fullmatch(chat):
                return {"accepted": False, "reason": "invalid_chat_id"}
            turn = latest_root_turn(chat, data_dir())
            if (chat, turn) in self.state._retired_turns and (chat, turn) != (self.state.bound_chat, self.state.turn_id):
                turn = None
            outcome = self.state.activate(chat, turn, binding_nonce=secrets.token_hex(16))
            self.last_bound_hook = None
            self.bound_event_counts.clear()
        elif action == "deactivate":
            outcome = self.state.deactivate(request.get("chat_id"))
        elif action == "set":
            turn = request.get("turn_id")
            if not isinstance(turn, str) or not IDENTITY.fullmatch(turn):
                return {"accepted": False, "reason": "missing_or_invalid_turn_id"}
            nonce = request.get("binding_nonce")
            if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
                return {"accepted": False, "reason": "missing_or_invalid_binding_nonce"}
            outcome = self.state.set_status(
                request.get("chat_id"), turn, request.get("state"),
                reason=str(request.get("reason", ""))[:500],
                resolves_waits=bool(request.get("resolves_waits", False)),
                binding_nonce=nonce,
            )
        elif action == "event":
            event = request.get("event")
            if not isinstance(event, dict):
                return {"accepted": False, "reason": "invalid_event"}
            # Root hooks carry session_id. Subagents share that id, so reject their
            # explicit identity BEFORE recording a new root turn or adding context.
            if LightState._is_subagent(event) or event.get("hook_event_name") in ("SubagentStart", "SubagentStop"):
                outcome = {"accepted": False, "reason": "subagent_event"}
                self._record_hook(event, outcome)
                return outcome
            name = event.get("hook_event_name")
            chat, turn = event.get("session_id"), event.get("turn_id")
            epoch_valid = all(event.get(key) is None or event[key] == getattr(self.state, key)
                              for key in ("binding_nonce", "binding_generation"))
            # These events have no turn id, or occur around compaction. They only
            # refresh context; they never start or enable a light by themselves.
            if (name == "UserPromptSubmit" and isinstance(chat, str) and isinstance(turn, str)
                    and (chat, turn) in self.state._retired_turns and (chat, turn) != (self.state.bound_chat, self.state.turn_id)):
                outcome = {"accepted": False, "reason": "stale_turn"}
            elif name in ("SessionStart", "PostCompact"):
                allowed = self.state.enabled and chat == self.state.bound_chat and epoch_valid
                outcome = {"accepted": bool(allowed), "changed": False, "context_needed": bool(allowed)}
            else:
                outcome = self.state.event(event)
            # 先验证当前绑定；旧 nonce、旧轮次不能污染身份缓存。
            # 其他根聊天仍可记录身份；重开同轮缓存必须消费真实匹配答案。
            if epoch_valid and (outcome.get("accepted") or outcome.get("reason") in ("different_chat", "not_enabled")):
                observe_root_event(event, data_dir(), allow_resume=bool(outcome.get("resumed_turn")))
            self._record_hook(event, outcome)
        elif action == "shutdown":
            self.closing = True
            outcome = self.state.deactivate()
        else:
            return {"accepted": False, "reason": "unknown_action"}
        if outcome.get("changed") and self.driver:
            self.driver.request_sync()
        if action in ("activate", "deactivate") and outcome.get("accepted"):
            try:
                atomic_json(data_dir() / "binding.json", {"bound_chat": self.state.bound_chat})
            except OSError:
                # 保存绑定仅供诊断；磁盘故障绝不能阻止熄灯或同步。
                outcome["warning"] = "binding_not_saved"
        if action == "shutdown" and self.driver:
            await self.driver.wait_confirmed(self.state.revision, 8)
        result = self.snapshot()
        result.update(outcome)
        if outcome.get("context_needed") or action == "activate":
            result["context"] = instruction_context(result)
        return result


class LocalServer:
    """每连接一个有界请求；认证和状态机都在同一事件循环内处理。"""
    def __init__(self, broker, directory, *, io_timeout=2, max_connections=16):
        self.broker, self.directory = broker, Path(directory)
        self.io_timeout, self.max_connections = io_timeout, max_connections
        self.key, self.tasks = secrets.token_hex(32), set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=MAX_MESSAGE + 4)
        try:
            atomic_json(self.directory / "endpoint.json", {"port": self.server.sockets[0].getsockname()[1], "key": self.key})
        except OSError:
            await self.close()
            raise
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) >= self.max_connections:
            writer.close()
            return
        self.tasks.add(task)
        shutdown = False
        try:
            packet = await asyncio.wait_for(read_frame(reader), self.io_timeout)
            if not isinstance(packet, dict) or not isinstance(packet.get("key"), str) or not secrets.compare_digest(packet["key"].encode(), self.key.encode()):
                response = {"accepted": False, "reason": "authentication_failed"}
            else:
                request = packet.get("request")
                response = await asyncio.wait_for(self.broker.handle(request), 35)
                shutdown = isinstance(request, dict) and request.get("action") == "shutdown" and response.get("accepted", True)
            writer.write(frame(response))
            await asyncio.wait_for(writer.drain(), self.io_timeout)
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError):
            pass
        finally:
            self.tasks.discard(task)
            writer.close()
            # 先发送 shutdown 回执，再通知主循环退出。
            if shutdown:
                self.broker.stopping.set()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        (self.directory / "endpoint.json").unlink(missing_ok=True)


async def serve():
    from light_transport import BleDriver

    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # 与身份缓存共用 OS 文件锁；进程崩溃也不会遗留持锁状态。
    with file_lock(directory / "broker.lock", timeout=0) as acquired:
        if not acquired:
            return
        handler = RotatingFileHandler(directory / "controller.log", maxBytes=256000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.setLevel(logging.INFO)
        LOGGER.addHandler(handler)
        broker = Broker()
        broker.driver = BleDriver(broker.state.snapshot, address=runtime_config()["device_address"])
        ipc = await LocalServer(broker, directory).start()
        stop_cleanup = threading.Event()

        def cleanup():
            while not stop_cleanup.is_set():
                # 监听已就绪；耗时扫描串行放后台，关闭时先停再等待线程。
                prune_expired(directory, stop=stop_cleanup)
                if stop_cleanup.wait(15 * 60):
                    return

        worker, cleaner = asyncio.create_task(broker.driver.run()), asyncio.create_task(asyncio.to_thread(cleanup))
        LOGGER.info("Controller started disabled (pid %s)", os.getpid())
        try:
            await broker.stopping.wait()
        finally:
            stop_cleanup.set()
            broker.stopping.set()
            await ipc.close()
            worker.cancel()
            await asyncio.gather(worker, cleaner, return_exceptions=True)
            LOGGER.removeHandler(handler)
            handler.close()


def run_hook():
    # Never surface a notification failure as a task/tool failure. No trust or
    # approval output is emitted: this hook never allows, denies or blocks work.
    name = None
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            return
        event = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(event, dict):
            return
        name = event.get("hook_event_name")
        # Save only short-lived identity metadata even when no controller exists.
        # Neither this write nor any hook starts/enables the BLE controller.
        try:
            result = rpc({"action": "event", "event": event}, timeout=0.9)
        except (OSError, EOFError):
            # A running broker validates old epochs before writing metadata.
            # When it is absent, keep just the identity for explicit activation.
            observe_root_event(event, data_dir())
            raise
        context = result.get("context")
        if context and name in ("SessionStart", "UserPromptSubmit", "PostCompact"):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}, ensure_ascii=False))
    except Exception:
        # Without a running broker, hooks must not start or enable one. A skill
        # invocation starts it explicitly, and a fresh broker is always disabled.
        pass
    finally:
        if name in ("Stop", "Interrupt"):
            print("{}")


def hook_command(python_path, script_path=None):
    """简单路径保持原命令；其余路径编码为 PowerShell 字面量，兼容 cmd。"""
    executable = str(python_path)
    script = str(Path(__file__).resolve() if script_path is None else script_path)
    if not Path(executable).is_absolute() or Path(executable).suffix.lower() != ".exe" or not Path(script).is_absolute():
        raise ValueError("Hook Python and script must use absolute paths; Python must be an .exe")
    if any(char in executable + script for char in '\0\r\n"'):
        raise ValueError("Hook paths contain invalid characters")
    if (not any(char.isspace() or char in "&|;<>`$%!'(){}[]?*=,#" for char in executable)
            and not any(char in script for char in "`$%!")):
        return f'{executable} -X utf8 -B "{script}" hook'
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    source = ("$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
              f"& {literal(executable)} -X utf8 -B {literal(script)} hook;exit $LASTEXITCODE")
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def is_skill_hook(command):
    """只读取命令文本，绝不执行已有 Hook；文案相同并不代表属于本技能。"""
    script = str(Path(__file__).resolve())
    if not isinstance(command, str):
        return False
    if script in command:
        return True
    encoded = re.search(r"\s-EncodedCommand\s+([A-Za-z0-9+/]+={0,2})\s*$", command, re.IGNORECASE)
    try:
        source = base64.b64decode(encoded[1], validate=True).decode("utf-16-le") if encoded else ""
        return "'" + script.replace("'", "''") + "'" in source
    except (ValueError, UnicodeError):
        return False


def install_hooks():
    config = runtime_config()
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"
    previous = path.read_text(encoding="utf-8-sig") if path.exists() else None
    document = json.loads(previous) if previous else {"hooks": {}}
    hooks = document.setdefault("hooks", {})
    command = hook_command(config["python"])
    names = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "PostCompact", "Stop", "Interrupt", "SessionEnd"]
    for name in names:
        groups = hooks.setdefault(name, [])
        # Remove only an earlier registration of this exact helper; retain all
        # unrelated hooks and leave config.toml/notify entirely alone.
        for group in list(groups):
            group["hooks"] = [item for item in group.get("hooks", []) if not is_skill_hook(item.get("command"))]
            if not group["hooks"]:
                groups.remove(group)
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 2,
                                   "statusMessage": "aaa无敌状态灯"}]})
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    changed = previous is None or content.strip() != previous.strip()
    if changed:
        if previous is not None:
            backup = data_dir() / ("hooks-before-install-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
            backup.write_text(previous, encoding="utf-8")
        atomic_json(path, document)
    return {"hooks_file": str(path), "changed": changed, "trust_required": changed, "events": names}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def compact_status(result, chat=None, include_context=False):
    if "desired" not in result:
        return result
    device = result.get("device", {})
    desired = result["desired"]
    observed = device.get("observed") or {}
    confirmed = bool(
        device.get("connected") and device.get("applied_revision") == result["revision"]
        and observed.get("power") == desired["power"]
        and (not desired["power"] or all(observed.get(key) == desired[key] for key in ("color", "brightness", "mode")))
    )
    summary = {
        "enabled": result["enabled"], "bound_chat": result["bound_chat"],
        "bound_to_current_chat": bool(chat and chat == result["bound_chat"]),
        "phase": result["phase"], "color": desired["color"],
        "brightness": desired["brightness"], "connected": bool(device.get("connected")),
        "confirmed": confirmed, "pending": bool(device.get("pending", True)),
        "tracking": result.get("tracking", "disabled"),
        "binding_accepted": bool(result.get("enabled") and result.get("bound_chat")),
        "tracking_ready": bool(result.get("enabled") and result.get("turn_id")),
        "diagnostic": result.get("diagnostic"),
    }
    summary.update({key: result[key] for key in ("accepted", "reason", "superseded", "warning") if key in result})
    if device.get("last_error"):
        summary["error"] = device["last_error"]
    last = (result.get("hook_diagnostics") or {}).get("last_bound")
    if last:
        summary["last_hook"] = {key: last[key] for key in ("event", "received_at", "accepted", "reason")}
    if include_context and summary["bound_to_current_chat"] and result.get("accepted", True):
        summary.update({key: result[key] for key in ("turn_id", "binding_nonce", "context") if key in result})
    if summary["tracking"] == "pending_next_prompt":
        summary["notice"] = "已绑定当前聊天；自动跟踪尚未就绪，保持熄灯，等待真实当前聊天事件。"
    return summary


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="aaa无敌 — explicitly enabled Codex task light")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in ("activate", "deactivate", "set", "status", "doctor", "hook", "broker", "install-hooks", "shutdown", "bootstrap")}
    for name in ("activate", "deactivate", "set", "status"):
        commands[name].add_argument("--verbose", action="store_true")
    for name, default in (("activate", 12), ("deactivate", 12), ("set", 0)):
        commands[name].add_argument("--wait", type=float, default=default)
    p = commands["set"]
    p.add_argument("state", choices=["working", "completed", "needs_user", "blocked"])
    p.add_argument("--turn-id", required=True)
    p.add_argument("--binding-nonce", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--resolves-waits", action="store_true")
    args = parser.parse_args(argv)
    if args.command in ("hook", "broker"):
        if args.command == "hook":
            run_hook()
        else:
            asyncio.run(serve())
        return 0
    try:
        if args.command == "install-hooks":
            emit(install_hooks())
            return 0
        if args.command in ("bootstrap", "activate"):
            ensure_broker()
        request = {"action": args.command if args.command in ("activate", "deactivate", "set", "shutdown") else "status"}
        if args.command in ("activate", "set"):
            request["chat_id"] = current_chat()
        if args.command == "set":
            request.update({key: getattr(args, key) for key in ("turn_id", "binding_nonce", "state", "reason", "resolves_waits")})
        result = rpc(request, timeout=12 if args.command == "shutdown" else 2)
        wait = 20 if args.command == "bootstrap" else getattr(args, "wait", 0)
        if wait > 0 and result.get("accepted", True):
            outcome = result
            result = rpc({"action": "wait", "revision": outcome["revision"], "timeout": wait}, timeout=wait + 2)
            result["accepted"] = outcome.get("accepted", True)
            if result["revision"] == outcome["revision"]:
                result.update({key: outcome[key] for key in ("reason", "context", "warning") if key in outcome})
            else:
                result.update(superseded=True, reason="state_changed_while_waiting")
        if args.command == "doctor":
            result.update(runtime_python=sys.executable,
                          hooks_file=str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"),
                          hook_trust="Codex manages trust; inspect /hooks after a new or changed hook definition.")
        if args.command in ("status", "activate", "deactivate", "set") and not getattr(args, "verbose", False):
            chat = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
            result = compact_status(result, chat, include_context=args.command == "activate" and not result.get("superseded"))
        emit(result)
        return 0 if result.get("accepted", True) else 2
    except (OSError, EOFError, ValueError) as error:
        emit({"accepted": False, "connected": False, "reason": str(error),
              "enabled": False if args.command in ("status", "doctor") else None})
        return 0 if args.command in ("status", "doctor") else 1


if __name__ == "__main__":
    raise SystemExit(main())
