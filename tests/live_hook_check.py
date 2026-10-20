"""Bounded, read-only acceptance of a real desktop Stop and BLE readback.

Run while the bound root turn is working, then finish that actual turn. This
observer never submits events, sets a color, or opens a Bluetooth connection.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import status_light as control


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    path = control.data_dir() / "desktop-hook-verification.json"
    chat = control.current_chat()
    initial = control.rpc({"action": "status"})
    identity = (chat, initial.get("turn_id"), initial.get("binding_nonce"))
    if initial.get("bound_chat") != chat or not all(identity):
        raise RuntimeError("Current root chat must be bound with a real turn before observation")
    baseline = initial["hook_diagnostics"]["bound_event_counts"].get("Stop", 0)
    report = {
        "result": "pending", "source": "real desktop hooks; no event replay",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat, "turn_id": identity[1], "initial_stop_count": baseline,
    }
    control.atomic_json(path, report)
    deadline = time.monotonic() + max(1, min(args.timeout, 300))
    latest = initial
    reason = "timeout_waiting_for_real_stop_and_green_readback"
    while time.monotonic() < deadline:
        try:
            latest = control.rpc({"action": "status"}, timeout=.9)
        except (OSError, EOFError, TimeoutError):
            reason = "controller_unavailable"
            time.sleep(.25)
            continue
        if (latest.get("bound_chat"), latest.get("turn_id"), latest.get("binding_nonce")) != identity:
            reason = "binding_or_turn_changed"
            break
        counts = latest["hook_diagnostics"]["bound_event_counts"]
        summary = control.compact_status(latest, chat)
        if (counts.get("Stop", 0) > baseline
                and all(counts.get(event, 0) > 0 for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"))
                and summary["phase"] == "completed" and summary["confirmed"]
                and summary["color"] == "#00FF00" and summary["brightness"] == 75
                and latest["device"]["observed"].get("mode") == "SOLID"):
            report["result"] = "passed"
            reason = "real_message_tools_stop_and_green_readback_confirmed"
            break
        time.sleep(.2)
    if report["result"] != "passed":
        report["result"] = "failed"
    report.update({
        "finished_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
        "status": control.compact_status(latest, chat),
        "bound_event_counts": latest.get("hook_diagnostics", {}).get("bound_event_counts", {}),
        "observed": latest.get("device", {}).get("observed"),
    })
    control.atomic_json(path, report)
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
