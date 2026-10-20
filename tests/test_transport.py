import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from light_transport import RX, SERVICE, STATE, TX, BleDriver

ADDRESS = "00:11:22:33:44:55"  # 仅为模拟设备标识，不会连接真实蓝牙。


def desired(revision=0, power=False, color="#0000FF"):
    return {"revision": revision, "desired": {"power": power, "color": color, "brightness": 75, "mode": "SOLID"}}


class FakeServices:
    def __init__(self, missing=False):
        self.missing = missing

    def get_service(self, service):
        assert service == SERVICE
        return None if self.missing else object()


class FakeClient:
    def __init__(self, address, disconnected_callback, *, on_write=None, acknowledge=True, bad_readback=False, missing_service=False, notify_gate=None):
        assert address == ADDRESS
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.commands = []
        self.on_write = on_write
        self.acknowledge = acknowledge
        self.bad_readback = bad_readback
        self.services = FakeServices(missing_service)
        self.state = {"power": True, "color": "#FFFFFF", "brightness": 20, "mode": "RAINBOW", "output": "#333333"}
        self.notify = None
        self.notify_gate = notify_gate
        self.notifying = asyncio.Event()

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)

    def force_disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)

    async def start_notify(self, characteristic, callback):
        assert characteristic == TX
        self.notifying.set()
        if self.notify_gate is not None:
            await self.notify_gate.wait()
        self.notify = callback

    async def read_gatt_char(self, characteristic):
        assert characteristic == STATE
        state = dict(self.state)
        if self.bad_readback and self.commands:
            state["brightness"] = 20
        return bytearray(json.dumps(state).encode())

    async def write_gatt_char(self, characteristic, value, response):
        assert characteristic == RX and response is True
        command = value.decode("ascii")
        assert "\n" not in command
        self.commands.append(command)
        if command == "OFF":
            self.state["power"] = False
        elif command == "ON":
            self.state["power"] = True
        elif command.startswith("MODE "):
            self.state["mode"] = command[5:]
        elif command.startswith("COLOR "):
            self.state["color"] = command[6:]
        elif command.startswith("BRIGHT "):
            self.state["brightness"] = int(command[7:])
        else:
            raise AssertionError(command)
        self.state["output"] = (
            "#" + "".join(f"{int(int(self.state['color'][i:i+2], 16) * self.state['brightness'] / 100):02X}" for i in (1, 3, 5))
            if self.state["power"] else "#000000"
        )
        if self.on_write is not None:
            self.on_write(command)
        if self.acknowledge:
            self.notify(TX, bytearray(f"OK {command}".encode()))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, state, **fake_options):
        self.clients = []

        def factory(address, **kwargs):
            client = FakeClient(address, **kwargs, **fake_options)
            self.clients.append(client)
            return client

        driver = BleDriver(lambda: state, address=ADDRESS, client_factory=factory, command_timeout=0.05, retry_delays=(0.2,))
        self.tasks.append(asyncio.create_task(driver.run()))
        return driver

    async def until(self, predicate, timeout=0.8):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition did not become true")
            await asyncio.sleep(0.005)

    async def test_disabled_connection_only_turns_off(self):
        driver = self.start(desired())
        self.assertTrue(await driver.wait_confirmed(0, 0.5))
        self.assertEqual(self.clients[0].commands, ["OFF"])
        self.assertFalse(driver.snapshot()["observed"]["power"])
        self.assertFalse(driver.snapshot()["pending"])

    async def test_enable_configures_75_percent_before_on(self):
        driver = self.start(desired(1, True))
        self.assertTrue(await driver.wait_confirmed(1, 0.5))
        self.assertEqual(self.clients[0].commands, ["MODE SOLID", "COLOR #0000FF", "BRIGHT 75", "ON"])
        self.assertEqual(driver.snapshot()["observed"]["output"], "#0000BF")
        self.assertEqual(driver.snapshot()["applied_revision"], 1)

    async def test_missing_service_sends_no_commands(self):
        driver = self.start(desired(1, True), missing_service=True)
        self.assertFalse(await driver.wait_confirmed(1, 0.08))
        self.assertEqual(self.clients[0].commands, [])
        self.assertIsNone(self.clients[0].notify)
        self.assertFalse(driver.snapshot()["connected"])
        self.assertIsNone(driver.snapshot()["applied_revision"])
        self.assertIn("Required BLE service is missing", driver.snapshot()["last_error"])

    async def test_new_revision_interrupts_old_sequence(self):
        state = desired(1, True)

        def replace(command):
            if state["revision"] == 1 and command == "MODE SOLID":
                state.update(desired(2, True, "#FF0000"))
                driver.request_sync()

        driver = self.start(state, on_write=replace)
        await self.until(lambda: state["revision"] == 2)
        self.assertTrue(await driver.wait_confirmed(2, 0.5))
        self.assertFalse(await driver.wait_confirmed(1, 0.05))
        self.assertNotIn("COLOR #0000FF", self.clients[0].commands)
        self.assertEqual(self.clients[0].commands.count("ON"), 1)
        self.assertEqual(driver.snapshot()["applied_revision"], 2)

    async def test_revision_replaced_after_on_never_confirms_old_state(self):
        state = desired(1, True)

        def replace(command):
            if state["revision"] == 1 and command == "ON":
                state.update(desired(2, False))
                driver.request_sync()

        driver = self.start(state, on_write=replace)
        await self.until(lambda: state["revision"] == 2)
        self.assertTrue(await driver.wait_confirmed(2, 0.5))
        self.assertEqual(self.clients[0].commands[-1], "OFF")
        self.assertEqual(driver.snapshot()["applied_revision"], 2)
        self.assertFalse(driver.snapshot()["observed"]["power"])

    async def test_disconnect_does_not_claim_connected(self):
        driver = self.start(desired())
        self.assertTrue(await driver.wait_confirmed(0, 0.5))
        self.clients[0].force_disconnect()
        self.assertFalse(driver.snapshot()["connected"])
        self.assertTrue(driver.snapshot()["pending"])
        await self.until(lambda: driver.snapshot()["last_error"] is not None)
        driver.request_sync()
        await self.until(lambda: len(self.clients) >= 2 and not driver.snapshot()["pending"])
        self.assertTrue(all(client.commands == ["OFF"] for client in self.clients))

    async def test_ack_timeout_is_background_failure(self):
        driver = self.start(desired(1, True), acknowledge=False)
        self.assertFalse(await driver.wait_confirmed(1, 0.12))
        status = driver.snapshot()
        self.assertFalse(status["connected"])
        self.assertIsNone(status["applied_revision"])
        self.assertIn("TimeoutError", status["last_error"])
        self.assertFalse(self.tasks[0].done())

    async def reconnect_requires_new_confirmation(self, power):
        driver = self.start(desired(1, power))
        self.assertTrue(await driver.wait_confirmed(1, 0.5))
        gate = asyncio.Event()
        replacement = FakeClient(ADDRESS, driver._disconnected, notify_gate=gate)
        driver._client_factory = lambda *_args, **_kwargs: replacement
        driver._command_timeout = 0.5
        self.clients[0].force_disconnect()
        driver.request_sync()
        await asyncio.wait_for(replacement.notifying.wait(), 0.5)
        snapshot = driver.snapshot()
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["pending"])
        self.assertIsNone(snapshot["applied_revision"])
        self.assertIsNone(snapshot["observed"])
        self.assertEqual(replacement.commands, [])
        self.assertFalse(await driver.wait_confirmed(1, 0.01))
        gate.set()
        self.assertTrue(await driver.wait_confirmed(1, 0.5))
        self.assertEqual(driver.snapshot()["observed"]["power"], power)

    async def test_reconnect_enabled_waits_for_fresh_readback(self):
        await self.reconnect_requires_new_confirmation(True)

    async def test_reconnect_disabled_waits_for_off_readback(self):
        await self.reconnect_requires_new_confirmation(False)

    async def test_readback_mismatch_never_confirms(self):
        driver = self.start(desired(1, True), bad_readback=True)
        self.assertFalse(await driver.wait_confirmed(1, 0.08))
        self.assertIsNone(driver.snapshot()["applied_revision"])
        self.assertIn("ProtocolError", driver.snapshot()["last_error"])


if __name__ == "__main__":
    unittest.main()
