"""Short-lived root-turn identities for aaa-wudi Hooks.

This module observes only chat/turn identities. It never starts the broker,
reads a transcript, inspects prompt/tool text, or controls a device. Turn IDs
are opaque: the bounded retired list prevents known old prompts from rewinding
the record, without assuming that IDs have a sortable generation order.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from light_state import LightState

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEFAULT_TTL = 15 * 60.0
MAX_RETIRED = 64
LOCK_TIMEOUT = 0.1
_IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z", re.ASCII)
_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json\Z", re.ASCII)
_EVENTS = {"UserPromptSubmit", "PreToolUse", "Stop", "Interrupt", "SessionEnd"}
_CLOSE_EVENTS = {"Stop", "Interrupt", "SessionEnd"}


def _valid_identity(value):
    return isinstance(value, str) and bool(_IDENTITY.fullmatch(value))


def _clock(now, ttl):
    try:
        current = time.time() if now is None else float(now)
        lifetime = float(ttl)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(current) or current < 0 or not math.isfinite(lifetime) or lifetime <= 0:
        return None
    return current, lifetime


def _fresh(record, now, ttl):
    age = now - record["seen_at"]
    return 0 <= age <= ttl


def _record_path(directory, chat_id):
    digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    return directory / (digest + ".json")


def _read(path, chat_id=None):
    try:
        if path.is_symlink() or path.stat().st_size > 32768:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("version") != 1:
            return None
        if not _valid_identity(record.get("chat_id")) or not _valid_identity(record.get("turn_id")):
            return None
        if chat_id is not None and record["chat_id"] != chat_id:
            return None
        if _record_path(path.parent, record["chat_id"]).name != path.name:
            return None
        stamp = record.get("seen_at")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            return None
        if type(record.get("closed")) is not bool:
            return None
        retired = record.get("retired_turns")
        if not isinstance(retired, list) or len(retired) > MAX_RETIRED or any(not _valid_identity(item) for item in retired):
            return None
        return record
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return None


@contextmanager
def file_lock(path, timeout=LOCK_TIMEOUT):
    """Lock a resource's fixed .lock file; the OS releases it on process exit."""
    # 聊天记录和服务实例共用此锁。不能删除锁文件，否则等待者可能锁住不同 inode。
    lock = Path(path).with_suffix(".lock")
    deadline = time.monotonic() + timeout
    acquired = False
    stream = None
    try:
        if not lock.is_symlink():
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
                stream = os.fdopen(descriptor, "r+b", buffering=0)
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                while True:
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.005, remaining))
            except OSError:
                pass
        yield acquired
    finally:
        if stream is not None:
            if acquired:
                try:
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()


_chat_lock = file_lock  # 聊天记录调用前已通过哈希文件名或 _record_path 限定路径。


def _write(path, record):
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="." + path.stem + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None and temporary.parent == path.parent:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def prune_expired(data_dir, stop=None, now=None, ttl=DEFAULT_TTL):
    """Stream expired identity records; run in the broker's background worker."""
    clock = _clock(now, ttl)
    if clock is None:
        return 0
    current, lifetime = clock
    removed = 0
    try:
        directory = Path(data_dir).expanduser().resolve() / "turns"
        with os.scandir(directory) as entries:
            for entry in entries:
                if stop is not None and stop.is_set():
                    break
                try:
                    if not _RECORD_NAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    path = directory / entry.name
                    # 先筛选再取锁，避免新鲜缓存也与前台 Hook 争锁。
                    record = _read(path)
                    if record is None or current - record["seen_at"] <= lifetime:
                        continue
                    with _chat_lock(path, timeout=0) as acquired:
                        if not acquired:
                            continue
                        # Hook 可能刚续期；删除前必须在同一聊天锁内重新核验。
                        record = _read(path)
                        if record is not None and current - record["seen_at"] > lifetime and not path.is_symlink():
                            path.unlink()
                            removed += 1
                except OSError:
                    continue
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return removed


def _retire(retired, turn_id):
    return [item for item in retired if item != turn_id][-MAX_RETIRED + 1:] + [turn_id]


def observe_root_event(event, data_dir, *, now=None, ttl=DEFAULT_TTL, allow_resume=False):
    """Observe one root Hook; return whether its metadata was written.

    Call this before any binding/enablement filter. Unknown tools cannot create
    a turn. A close event without an exact turn ID is deliberately ignored,
    including SessionEnd payloads that omit turn_id. Only the broker may set
    allow_resume after the state machine consumes a correlated same-turn answer.
    """
    if not isinstance(event, dict) or LightState._is_subagent(event):
        return False
    name = event.get("hook_event_name")
    chat_id, turn_id = event.get("session_id"), event.get("turn_id")
    clock = _clock(now, ttl)
    if not isinstance(name, str) or name not in _EVENTS or not _valid_identity(chat_id) or not _valid_identity(turn_id) or clock is None:
        return False
    current, lifetime = clock
    try:
        directory = Path(data_dir).expanduser().resolve() / "turns"
        directory.mkdir(parents=True, exist_ok=True)
        path = _record_path(directory, chat_id)
        with _chat_lock(path) as acquired:
            if not acquired:
                return False
            record = _read(path, chat_id)
            if name == "UserPromptSubmit":
                if record is not None:
                    if turn_id == record["turn_id"]:
                        # 授权来自状态机结果，而非 Hook 负载；普通重放不能重开或续期。
                        if allow_resume is not True:
                            return False
                        retired = [item for item in record["retired_turns"] if item != turn_id]
                    else:
                        if turn_id in record["retired_turns"]:
                            return False
                        retired = _retire(record["retired_turns"], record["turn_id"])
                else:
                    retired = []
                updated = {"version": 1, "chat_id": chat_id, "turn_id": turn_id,
                           "seen_at": current, "closed": False, "retired_turns": retired}
            elif record is None or record["turn_id"] != turn_id or record["closed"]:
                return False
            elif name == "PreToolUse":
                if not _fresh(record, current, lifetime):
                    return False
                updated = dict(record, seen_at=current)
            elif name in _CLOSE_EVENTS:
                updated = dict(record, closed=True, retired_turns=_retire(record["retired_turns"], turn_id))
            else:
                return False
            if not _write(path, updated):
                return False
        return True
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def latest_root_turn(chat_id, data_dir, *, now=None, ttl=DEFAULT_TTL):
    """Return only a fresh, open root turn; never create files or a broker."""
    clock = _clock(now, ttl)
    if not _valid_identity(chat_id) or clock is None:
        return None
    current, lifetime = clock
    try:
        directory = Path(data_dir).expanduser().resolve() / "turns"
        record = _read(_record_path(directory, chat_id), chat_id)
        if record is None or record["closed"] or not _fresh(record, current, lifetime):
            return None
        return record["turn_id"]
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
