"""Explicit live-device acceptance check; ends disabled and dark.

Run deliberately with the skill's Python. unittest discovery does not run this.
Hook payloads are replayed; the real desktop Plan UI is not switched by this test.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import status_light as control
from light_state import LightState
from light_transport import BleDriver


def cli(*args, success=True):
    if args[0] in ("activate", "deactivate", "status", "set"):
        args = (*args, "--verbose")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "status_light.py"), *args],
        capture_output=True, encoding="utf-8", timeout=35,
    )
    value = json.loads(result.stdout)
    if success:
        assert result.returncode == 0 and value.get("accepted", True), value
    else:
        assert result.returncode != 0 and not value.get("accepted"), value
    return value


def hook(event, **fields):
    payload = {"hook_event_name": event, "session_id": control.current_chat(), **fields}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "status_light.py"), "hook"],
        input=json.dumps(payload), capture_output=True, encoding="utf-8", timeout=4,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def confirmed(snapshot, color=None, power=True):
    snapshot = control.rpc({"action": "wait", "revision": snapshot["revision"], "timeout": 10}, timeout=12)
    assert snapshot["confirmed"], snapshot
    actual = snapshot["device"]["observed"]
    assert actual["power"] is power, snapshot
    if power:
        assert actual["brightness"] == 75 and actual["mode"] == "SOLID", snapshot
        assert actual["color"] == color and int(actual["output"][1:], 16), snapshot
    else:
        assert actual["output"] == "#000000", snapshot
    print("VERIFIED", snapshot["phase"], actual, flush=True)
    return snapshot


def main():
    # Replayed test events must never seed the real user's current-turn cache.
    test_chat = "hardware-suite:" + uuid.uuid4().hex
    os.environ["CODEX_THREAD_ID"] = test_chat
    os.environ["CODEX_SESSION_ID"] = test_chat
    report = []
    try:
        control.rpc({"action": "shutdown"}, timeout=12)
        time.sleep(0.3)
    except (OSError, EOFError, TimeoutError):
        pass
    initial = cli("bootstrap")
    assert not initial["enabled"] and not initial["device"]["observed"]["power"], initial
    try:
        waiting = cli("activate", "--wait", "10")
        assert waiting["bound_chat"] == test_chat and control.compact_status(waiting, test_chat)["bound_to_current_chat"], waiting
        assert waiting["binding_accepted"] and not waiting["tracking_ready"], waiting
        assert waiting["turn_id"] is None and waiting["tracking"] == "pending_next_prompt", waiting
        report.append(confirmed(waiting, power=False))
        hook("UserPromptSubmit", turn_id="device-start:" + uuid.uuid4().hex)
        active = cli("activate", "--wait", "15")
        turn = active["turn_id"]
        nonce = active["binding_nonce"]
        report.append(confirmed(active, "#0000FF"))
        for state, color in [("needs_user", "#FF0000"), ("working", "#0000FF"),
                             ("blocked", "#FFFF00"), ("working", "#0000FF"),
                             ("completed", "#00FF00")]:
            args = ["set", state, "--turn-id", turn, "--binding-nonce", nonce, "--wait", "8"]
            if state == "working":
                args.append("--resolves-waits")
            result = cli(*args)
            report.append(confirmed(result, color))
            if state in ("needs_user", "blocked", "completed"):
                hook("Stop", turn_id=turn)
                assert cli("status")["phase"] == state
        hook("UserPromptSubmit", turn_id="unrelated-turn", session_id="another-chat")
        assert cli("status")["phase"] == "completed"
        next_turn = "device-check:" + uuid.uuid4().hex
        context = hook("UserPromptSubmit", turn_id=next_turn)
        assert next_turn in context["hookSpecificOutput"]["additionalContext"]
        report.append(confirmed(cli("status"), "#0000FF"))
        hook("UserPromptSubmit", turn_id="child-turn", agent_id="child-agent")
        assert cli("status")["turn_id"] == next_turn
        plan = cli("status")
        plan_marker = marker("needs_user", plan)
        hook("Stop", turn_id=next_turn, last_assistant_message="<proposed_plan>\nTest plan\n</proposed_plan>\n" + plan_marker)
        report.append(confirmed(cli("status"), "#FF0000"))
        next_turn = "device-resume:" + uuid.uuid4().hex
        hook("UserPromptSubmit", turn_id=next_turn)
        report.append(confirmed(cli("status"), "#0000FF"))
        other_chat, other_turn = "hardware-b:" + uuid.uuid4().hex, "takeover:" + uuid.uuid4().hex
        hook("UserPromptSubmit", session_id=other_chat, turn_id=other_turn)
        other = control.rpc({"action": "activate", "chat_id": other_chat})
        assert other["turn_id"] == other_turn, other
        report.append(confirmed(other, "#0000FF"))
        hook("Stop", turn_id=next_turn, last_assistant_message=marker("completed", plan))
        assert cli("status")["bound_chat"] == other_chat
        refreshed = control.rpc({"action": "activate", "chat_id": other_chat})
        assert refreshed["binding_nonce"] != other["binding_nonce"]
        hook("Stop", session_id=other_chat, turn_id=other_turn, last_assistant_message=marker("completed", other))
        report.append(confirmed(cli("status"), "#0000FF"))
        next_turn = "device-back:" + uuid.uuid4().hex
        hook("UserPromptSubmit", turn_id=next_turn)
        back = cli("activate")
        assert back["turn_id"] == next_turn
        cli("set", "completed", "--turn-id", turn, "--binding-nonce", nonce, success=False)
        hook("Interrupt", turn_id=next_turn)
        report.append(confirmed(cli("status"), power=False))
        cli("set", "completed", "--turn-id", next_turn, "--binding-nonce", back["binding_nonce"], success=False)
    finally:
        stopped = control.rpc({"action": "deactivate"})
        report.append(confirmed(stopped, power=False))
    hook("UserPromptSubmit", turn_id="disabled-turn")
    stopped = cli("status")
    assert not stopped["enabled"] and stopped["bound_chat"] is None and not stopped["desired"]["power"]
    cli("shutdown")
    time.sleep(0.3)
    asyncio.run(check_live_reconnect(report))
    restarted = cli("bootstrap")
    assert not restarted["enabled"] and restarted["bound_chat"] is None
    report.append(confirmed(restarted, power=False))
    control.atomic_json(control.data_dir() / "hardware-verification.json", {
        "checks": report, "platform_hooks": "replayed in isolated synthetic chat; desktop automatic delivery unverified",
        "runtime_version": 4, "result": "passed",
    })
    print("LIVE ACCEPTANCE PASSED; controller disabled and device OFF.", flush=True)


def marker(state, snapshot):
    return f"<!-- aaa-wudi:{state} turn={snapshot['turn_id']} binding={snapshot['binding_nonce']} -->"


async def check_live_reconnect(report):
    """One test process exclusively owns BLE while the regular broker is stopped."""
    state = LightState()
    driver = BleDriver(state.snapshot, address=control.runtime_config()["device_address"])
    worker = asyncio.create_task(driver.run())

    async def verify(label, color=None, power=True):
        assert await driver.wait_confirmed(state.revision, 22), driver.snapshot()
        observed = driver.snapshot()["observed"]
        assert observed["power"] is power, observed
        if power:
            assert observed["color"] == color and observed["brightness"] == 75 and observed["mode"] == "SOLID", observed
        else:
            assert observed["output"] == "#000000", observed
        report.append({"check": label, "observed": observed})
        print("VERIFIED", label, observed, flush=True)

    try:
        await verify("reconnect_initial_off", power=False)
        state.activate("hardware-reconnect", "reconnect-turn")
        driver.request_sync()
        await verify("reconnect_working_blue", "#0000FF")
        previous = driver._client
        await previous.disconnect()
        state.set_status("hardware-reconnect", "reconnect-turn", "completed", binding_nonce=state.binding_nonce)
        driver.request_sync()
        await verify("enabled_reconnect_latest_green", "#00FF00")
        assert driver._client is not previous
        state.deactivate()
        driver.request_sync()
        await verify("reconnect_disabled_off", power=False)
        previous = driver._client
        await previous.disconnect()
        driver.request_sync()
        await verify("disabled_reconnect_stays_off", power=False)
        assert driver._client is not previous
    finally:
        state.deactivate()
        driver.request_sync()
        await driver.wait_confirmed(state.revision, 12)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    main()
