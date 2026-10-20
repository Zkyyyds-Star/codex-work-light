"""Single-connection BLE worker for the aaa-wudi state-light broker."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
STATE = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\Z")


class ProtocolError(RuntimeError):
    pass


def _default_client(address: str, **kwargs: Any) -> Any:
    from bleak import BleakClient

    return BleakClient(address, **kwargs)


class BleDriver:
    def __init__(
        self,
        provider: Callable[[], dict],
        *,
        address: str,
        client_factory: Callable[..., Any] | None = None,
        command_timeout: float = 4.0,
        connect_timeout: float = 15.0,
        retry_delays: tuple[float, ...] = (1, 2, 5, 10, 30),
    ) -> None:
        self._provider = provider
        self._address = address
        self._client_factory = client_factory or _default_client
        self._command_timeout = command_timeout
        self._connect_timeout = connect_timeout
        self._retry_delays = retry_delays or (1,)
        self._client: Any = None
        self._connected = False
        self._observed: dict | None = None
        self._applied_revision: int | None = None
        self._last_error: str | None = None
        # 三个事件分别负责应用新状态、打断重试退避、唤醒确认等待，不能合并。
        self._wake = asyncio.Event()
        self._retry_interrupt = asyncio.Event()
        self._progress = asyncio.Event()
        self._acks: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    def _is_connected(self) -> bool:
        return bool(self._connected and self._client is not None and self._client.is_connected)

    def request_sync(self) -> None:
        self._wake.set()
        self._retry_interrupt.set()
        self._progress.set()

    def snapshot(self) -> dict:
        current = self._provider()
        connected = self._is_connected()
        return {
            "connected": connected,
            "observed": dict(self._observed) if self._observed is not None else None,
            "applied_revision": self._applied_revision,
            "last_error": self._last_error,
            "pending": not connected or current["revision"] != self._applied_revision,
        }

    async def wait_confirmed(self, revision: int, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._progress.clear()
            if self._provider()["revision"] != revision:
                return False
            if self._is_connected() and self._applied_revision == revision:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._progress.wait(), remaining)
            except TimeoutError:
                return False

    def _disconnected(self, client: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._handle_disconnect, client)

    def _handle_disconnect(self, client: Any) -> None:
        if client is self._client:
            self._connected = False
            self._applied_revision = None
            self._observed = None
            self._last_error = "Bluetooth disconnected"
            self._wake.set()
            self._progress.set()

    def _notification(self, _sender: Any, data: bytearray) -> None:
        text = bytes(data).decode("utf-8", errors="replace").strip("\x00\r\n ")
        if self._loop is not None and not self._loop.is_closed():
            for line in text.splitlines():
                self._loop.call_soon_threadsafe(self._enqueue_ack, line.strip())

    def _enqueue_ack(self, text: str) -> None:
        if self._acks.full():
            self._acks.get_nowait()
        self._acks.put_nowait(text)

    def _require_connection(self, client: Any) -> None:
        if client is not self._client or not self._is_connected():
            raise ConnectionError("Bluetooth disconnected")

    async def _command(self, client: Any, command: str) -> None:
        self._require_connection(client)
        while not self._acks.empty():
            self._acks.get_nowait()
        await asyncio.wait_for(
            client.write_gatt_char(RX, command.encode("ascii"), response=True),
            self._command_timeout,
        )
        deadline = asyncio.get_running_loop().time() + self._command_timeout
        while True:
            self._require_connection(client)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"No acknowledgement for {command}")
            response = await asyncio.wait_for(self._acks.get(), remaining)
            if response.startswith("ERR"):
                raise ProtocolError(response)
            if response == f"OK {command}":
                return

    async def _read_state(self, client: Any) -> dict:
        self._require_connection(client)
        raw = await asyncio.wait_for(client.read_gatt_char(STATE), self._command_timeout)
        state = json.loads(bytes(raw).decode("utf-8").strip("\x00\r\n "))
        if not isinstance(state, dict):
            raise ProtocolError("STATE must be a JSON object")
        self._observed = state
        return state

    @staticmethod
    def _verify(observed: dict, desired: dict) -> None:
        power = desired["power"]
        if observed.get("power") is not power:
            raise ProtocolError("STATE power did not match the requested state")
        output = observed.get("output")
        if not isinstance(output, str) or not _HEX.fullmatch(output):
            raise ProtocolError("STATE output is missing or invalid")
        if not power:
            if int(output[1:], 16) != 0:
                raise ProtocolError("STATE output is nonzero after OFF")
            return
        if (
            observed.get("mode") != "SOLID"
            or str(observed.get("color", "")).upper() != desired["color"].upper()
            or observed.get("brightness") != 75
            or int(output[1:], 16) == 0
        ):
            raise ProtocolError("STATE color, mode, brightness or output did not match")

    async def _apply_latest(self, client: Any) -> None:
        while True:
            current = self._provider()
            revision = current["revision"]
            desired = dict(current["desired"])
            if desired["power"]:
                color = desired["color"]
                if not isinstance(color, str) or not _HEX.fullmatch(color):
                    raise ProtocolError("Invalid requested RGB color")
                commands = ["MODE SOLID", f"COLOR {color.upper()}", "BRIGHT 75", "ON"]
            else:
                commands = ["OFF"]
            for command in commands:
                if self._provider()["revision"] != revision:
                    break
                await self._command(client, command)
            if self._provider()["revision"] != revision:
                continue
            observed = await self._read_state(client)
            if self._provider()["revision"] != revision:
                continue
            self._require_connection(client)
            self._verify(observed, desired)
            self._applied_revision = revision
            self._last_error = None
            self._progress.set()
            return

    async def _close(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        # 确认只属于当前连接；重连必须重新写入并回读，不能沿用旧设备状态。
        self._applied_revision = None
        self._observed = None
        self._progress.set()
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), self._command_timeout)
            except Exception:
                pass

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("BleDriver.run already has a worker")
        self._running = True
        self._loop = asyncio.get_running_loop()
        failures = 0
        try:
            while True:
                self._wake.clear()
                self._retry_interrupt.clear()
                try:
                    client = self._client_factory(self._address, disconnected_callback=self._disconnected)
                    self._client = client
                    await asyncio.wait_for(client.connect(), self._connect_timeout)
                    self._connected = bool(client.is_connected)
                    self._require_connection(client)
                    services = getattr(client, "services", None)
                    if services is None or services.get_service(SERVICE) is None:
                        raise ProtocolError(f"Required BLE service is missing: {SERVICE}")
                    await asyncio.wait_for(client.start_notify(TX, self._notification), self._command_timeout)
                    while True:
                        self._wake.clear()
                        self._require_connection(client)
                        if self._provider()["revision"] != self._applied_revision:
                            await self._apply_latest(client)
                            failures = 0
                            self._retry_interrupt.clear()
                        self._require_connection(client)
                        if self._wake.is_set() or self._provider()["revision"] != self._applied_revision:
                            continue
                        await self._wake.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    await self._close()
                    delay = self._retry_delays[min(failures, len(self._retry_delays) - 1)]
                    failures += 1
                    try:
                        await asyncio.wait_for(self._retry_interrupt.wait(), delay)
                    except TimeoutError:
                        pass
        finally:
            await self._close()
            self._running = False
