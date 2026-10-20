"""Localhost protocol tests: temporary runtime files, no BLE or installed broker."""

import asyncio
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import status_light


class FakeBroker:
    def __init__(self):
        self.stopping = asyncio.Event()
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        return {"accepted": True, "echo": request}


class IpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory_patch = patch.object(status_light, "data_dir", return_value=self.directory)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.broker = FakeBroker()
        self.ipc = await status_light.LocalServer(self.broker, self.directory, io_timeout=.15).start()
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        await self.ipc.close()
        await asyncio.gather(*(writer.wait_closed() for writer in self.writers), return_exceptions=True)

    async def connect(self):
        port = self.ipc.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return reader, writer

    async def test_rpc_round_trip_and_loopback_binding(self):
        response = await asyncio.to_thread(status_light.rpc, {"action": "status", "文字": "中文"})
        self.assertTrue(response["accepted"])
        self.assertEqual(response["echo"]["文字"], "中文")
        self.assertEqual(self.ipc.server.sockets[0].getsockname()[0], "127.0.0.1")
        self.assertEqual(self.ipc.max_connections, 16)

    async def test_bad_key_is_rejected_before_dispatch(self):
        reader, writer = await self.connect()
        writer.write(status_light.frame({"key": "wrong", "request": {"action": "shutdown"}}))
        await writer.drain()
        response = await status_light.read_frame(reader)
        self.assertEqual(response, {"accepted": False, "reason": "authentication_failed"})
        self.assertEqual(self.broker.requests, [])
        self.assertFalse(self.broker.stopping.is_set())

    async def test_invalid_frames_close_without_dispatch_or_breaking_server(self):
        for packet in (struct.pack("!I", 0), struct.pack("!I", status_light.MAX_MESSAGE + 1), b"\0\0\0\x01{"):
            reader, writer = await self.connect()
            writer.write(packet)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), .5), b"")
        self.assertEqual(self.broker.requests, [])
        self.assertTrue((await asyncio.to_thread(status_light.rpc, {"action": "status"}))["accepted"])

    async def test_partial_request_times_out_and_other_clients_continue(self):
        reader, writer = await self.connect()
        writer.write(b"\0\0")
        await writer.drain()
        self.assertTrue((await asyncio.to_thread(status_light.rpc, {"action": "status"}))["accepted"])
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b"")
        self.assertEqual(len(self.broker.requests), 1)

    async def test_capacity_rejects_extra_client_without_waiting_queue(self):
        self.ipc.io_timeout = 1
        for _ in range(16):
            await self.connect()
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b"")
        self.assertEqual(len(self.ipc.tasks), 16)

    async def test_shutdown_reply_arrives_before_stop_notification(self):
        response = await asyncio.to_thread(status_light.rpc, {"action": "shutdown"})
        self.assertTrue(response["accepted"])
        await asyncio.wait_for(self.broker.stopping.wait(), .5)

    async def test_restart_changes_key_and_removes_stale_endpoint(self):
        old_key = self.ipc.key
        await self.ipc.close()
        self.assertFalse((self.directory / "endpoint.json").exists())
        self.ipc = await status_light.LocalServer(self.broker, self.directory).start()
        self.assertNotEqual(old_key, self.ipc.key)
        reader, writer = await self.connect()
        writer.write(status_light.frame({"key": old_key, "request": {"action": "status"}}))
        await writer.drain()
        self.assertFalse((await status_light.read_frame(reader))["accepted"])

    async def test_client_total_deadline_cannot_be_extended_by_slow_chunks(self):
        tasks = set()

        async def slow_peer(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                await status_light.read_frame(reader)
                for byte in status_light.frame({"accepted": True}):
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(.025)
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                tasks.discard(task)

        server = await asyncio.start_server(slow_peer, "127.0.0.1", 0)
        status_light.atomic_json(self.directory / "endpoint.json", {"port": server.sockets[0].getsockname()[1], "key": "a" * 64})
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.to_thread(status_light.rpc, {"action": "status"}, .08)
            self.assertLess(time.monotonic() - started, .5)
        finally:
            server.close()
            await server.wait_closed()
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    async def test_message_limit_is_enforced_before_payload_allocation(self):
        with self.assertRaises(ValueError):
            status_light.frame("x" * status_light.MAX_MESSAGE)
        reader, writer = await self.connect()
        writer.write(struct.pack("!I", 0xFFFFFFFF))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b"")

    async def test_service_listens_before_cleanup_and_joins_cleanup_on_shutdown(self):
        await self.ipc.close()
        entered, exited = threading.Event(), threading.Event()

        def prune(directory, stop):
            entered.set()
            stop.wait(3)
            exited.set()

        class InertDriver:
            def __init__(self, provider, *, address):
                self.address = address

            def snapshot(self):
                return {"connected": False, "observed": None, "pending": True}

            def request_sync(self):
                pass

            async def run(self):
                await asyncio.Future()

            async def wait_confirmed(self, revision, timeout):
                return False

        config = {"python": sys.executable, "data_dir": str(self.directory), "device_address": "AA:BB:CC:DD:EE:FF"}
        with patch("light_transport.BleDriver", InertDriver), patch.object(status_light, "prune_expired", prune), \
                patch.object(status_light, "runtime_config", return_value=config):
            service = asyncio.create_task(status_light.serve())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                response = await asyncio.to_thread(status_light.rpc, {"action": "status"})
                self.assertFalse(response["enabled"])
                self.assertFalse(response["device"]["connected"])
                self.assertTrue((await asyncio.to_thread(status_light.rpc, {"action": "shutdown"}))["accepted"])
                await asyncio.wait_for(service, 1)
                self.assertTrue(exited.is_set())
                self.assertFalse((self.directory / "endpoint.json").exists())
            finally:
                service.cancel()
                await asyncio.gather(service, return_exceptions=True)


class RuntimeConfigTests(unittest.TestCase):
    def test_configuration_is_validated_without_installed_runtime(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(status_light, "SKILL", Path(temporary)):
            path = Path(temporary) / "runtime.json"
            with self.assertRaisesRegex(ValueError, "install.ps1"):
                status_light.runtime_config()
            valid = {"python": sys.executable, "data_dir": temporary, "device_address": "AA:BB:CC:DD:EE:FF"}
            for changes in ({"python": "python.exe"}, {"data_dir": "relative"},
                            {"device_address": "not-an-address"}, {"python": None}):
                path.write_text(json.dumps(dict(valid, **changes)), encoding="utf-8")
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "install.ps1"):
                    status_light.runtime_config()
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(status_light.runtime_config(), valid)

    def test_malformed_endpoint_and_timeouts_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(status_light, "data_dir", return_value=Path(temporary)):
            for endpoint in ([], {"port": True, "key": "a" * 64}, {"port": 1, "key": "bad"}):
                status_light.atomic_json(Path(temporary) / "endpoint.json", endpoint)
                with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "endpoint"):
                    status_light.rpc({"action": "status"})
            for timeout in (0, -1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    status_light.rpc({}, timeout)

    @unittest.skipUnless(sys.platform == "win32", "Windows byte lock")
    def test_instance_lock_blocks_another_process_and_is_released_after_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broker.lock"
            source = ("import os,sys;sys.path.insert(0,sys.argv[1]);from light_events import file_lock\n"
                      "with file_lock(sys.argv[2],timeout=0) as acquired:\n"
                      " print(int(acquired),flush=True)\n"
                      " os._exit(0)\n")
            arguments = [sys.executable, "-X", "utf8", "-B", "-c", source, str(Path(status_light.__file__).parent), str(path)]
            with status_light.file_lock(path, timeout=0) as acquired:
                self.assertTrue(acquired)
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.stdout.strip(), "0", result.stderr)
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "1", result.stderr)
            with status_light.file_lock(path, timeout=0) as acquired:
                self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
