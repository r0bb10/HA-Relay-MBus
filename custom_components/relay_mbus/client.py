"""Bounded, read-only asyncio client for the observed Relay DL6 service."""

from __future__ import annotations

import asyncio
import binascii
from collections.abc import Sequence
from datetime import datetime, timezone
import math
import re
from time import monotonic
from typing import Literal, overload

from .decoder import decode_telegram
from .inventory import CONFIGURATION_SIZE, SUPPORTED_IDENTITY, parse_inventory
from .models import PollResult

ESC = b"\x1b\x1b\x1b"


class RelayError(Exception):
    """Base error, with messages safe to display without credentials or IDs."""


class RelayConnectionError(RelayError):
    """Connection failure, peer closure, or bounded operation timeout."""


class RelayProtocolError(RelayError, ValueError):
    """Unexpected response or invalid frame (not proof of bad credentials)."""


class RelayAuthError(RelayError):
    """Reserved for positively identified authentication errors."""


class RelayClient:
    """One sequential session per operation, serialized by an endpoint lock.

    Share the lock across all clients/probes for the same endpoint. Once a command
    is in flight the boundary is ambiguous until its complete reply is consumed;
    cleanup never sends logout into an incomplete binary response.
    """

    def __init__(self, host: str, port: int, passcode: str, *,
                 lock: asyncio.Lock | None = None, connect_timeout: float = 5,
                 command_timeout: float = 5, meter_timeout: float = 15,
                 cleanup_timeout: float = 2, identity_limit: int = 128,
                 inventory_timeout: float = 15) -> None:
        if not re.fullmatch(r"[0-9]{8}", passcode):
            raise ValueError("Passcode must contain eight ASCII digits")
        if not 1 <= port <= 65535:
            raise ValueError("Port outside supported range")
        if any(not math.isfinite(value) or value <= 0 for value in
               (connect_timeout, command_timeout, meter_timeout, cleanup_timeout,
                inventory_timeout)):
            raise ValueError("Timeouts must be finite and positive")
        if not 1 <= identity_limit <= 1024:
            raise ValueError("Identity bound outside supported range")
        self._host, self._port, self._passcode = host, port, passcode
        self._lock = lock if lock is not None else asyncio.Lock()
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._meter_timeout = meter_timeout
        self._cleanup_timeout = cleanup_timeout
        self._identity_limit = identity_limit
        self._inventory_timeout = inventory_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._authenticated = False
        self._boundary = False
        self._closed = False
        self._active: set[asyncio.Task] = set()

    async def _read(self, count: int) -> bytes:
        assert self._reader is not None
        return await self._reader.readexactly(count)

    async def _send(self, body: bytes) -> None:
        assert self._writer is not None
        self._boundary = False
        self._writer.write(ESC + body + b"\r")
        await self._writer.drain()

    async def _ack(self, command: bytes) -> None:
        expected = ESC + command + b"\r"
        if await self._read(len(expected)) != expected:
            raise RelayProtocolError("Unexpected service acknowledgement")

    async def _control(self, command: bytes, payload: bytes = b"") -> None:
        async with asyncio.timeout(self._command_timeout):
            await self._send(command + payload)
            await self._ack(command)
            self._boundary = True

    async def _identity(self) -> str:
        async with asyncio.timeout(self._command_timeout):
            await self._send(b"\x28")
            if await self._read(4) != ESC + b"\x28":
                raise RelayProtocolError("Unexpected identity response")
            result = bytearray()
            for _ in range(self._identity_limit + 1):
                byte = await self._read(1)
                if byte == b"\r":
                    if not result or any(c < 32 or c == 127 for c in result):
                        raise RelayProtocolError("Invalid logger identity")
                    self._boundary = True
                    return result.decode("latin-1")
                result.extend(byte)
            raise RelayProtocolError("Logger identity exceeds bound")

    async def _meter_frame(self, meter_id: str) -> bytes:
        async with asyncio.timeout(self._meter_timeout):
            echo = ESC + b"\x15" + meter_id.encode("ascii")
            await self._send(b"\x15" + meter_id.encode("ascii"))
            if await self._read(len(echo)) != echo:
                raise RelayProtocolError("Unexpected meter response echo")
            # Service length is exactly one unsigned byte, not a delimited line.
            length = (await self._read(1))[0]
            frame = await self._read(length)
            if await self._read(1) != b"\r":
                raise RelayProtocolError("Invalid service response trailer")
            self._boundary = True
            return frame

    async def _logout(self) -> None:
        async with asyncio.timeout(self._cleanup_timeout):
            await self._send(b"\x03")
            await self._ack(b"\x03")
            await self._ack(b"\x03")
            self._authenticated = False
            self._boundary = True

    async def _transfer_send(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()

    async def _packet(self, expected: int) -> bytes:
        marker = await self._read(1)
        if marker not in (b"\x01", b"\x02"):
            raise RelayProtocolError("Unexpected YMODEM packet marker")
        size = 128 if marker == b"\x01" else 1024
        body = await self._read(size + 4)
        number, inverse = body[:2]
        if number != expected or number ^ inverse != 255:
            raise RelayProtocolError("Unexpected YMODEM sequence")
        payload = body[2:-2]
        if binascii.crc_hqx(payload, 0) != int.from_bytes(body[-2:], "big"):
            raise RelayProtocolError("YMODEM CRC mismatch")
        return payload

    async def _inventory(self, identity: str) -> tuple[str, ...]:
        if identity != SUPPORTED_IDENTITY:
            raise RelayProtocolError("Unsupported logger inventory firmware")
        # One bounded transfer, with no retransmissions or speculative commands.
        async with asyncio.timeout(self._inventory_timeout):
            await self._send(b"\x17")
            await self._ack(b"\x17")
            # Remain off-boundary until the final header has been acknowledged.
            await self._transfer_send(b"C")
            header = await self._packet(0)
            fields = header.split(b"\0", 2)
            if len(fields) < 3 or fields[0].lower() != b"mr004fa.bin":
                raise RelayProtocolError("Unexpected configuration filename")
            metadata = fields[1].split()
            if not metadata or metadata[0] != str(CONFIGURATION_SIZE).encode("ascii"):
                raise RelayProtocolError("Unexpected announced configuration size")
            await self._transfer_send(b"\x06C")
            blocks = []
            for sequence in (1, 2):
                block = await self._packet(sequence)
                if len(block) != 1024:
                    raise RelayProtocolError("Unexpected configuration block layout")
                blocks.append(block)
                await self._transfer_send(b"\x06")
            if await self._read(1) != b"\x04":
                raise RelayProtocolError("Missing first YMODEM EOT")
            await self._transfer_send(b"\x15")
            if await self._read(1) != b"\x04":
                raise RelayProtocolError("Missing second YMODEM EOT")
            await self._transfer_send(b"\x06C")
            if any(await self._packet(0)):
                raise RelayProtocolError("Unexpected additional YMODEM file")
            await self._transfer_send(b"\x06")
            self._boundary = True
            try:
                return parse_inventory(b"".join(blocks), identity)
            except ValueError as err:
                raise RelayProtocolError(str(err)) from None

    async def _cleanup(self) -> None:
        try:
            if self._authenticated and self._boundary:
                try:
                    await self._logout()
                except (RelayError, OSError, TimeoutError, asyncio.IncompleteReadError):
                    pass
        finally:
            writer, self._writer = self._writer, None
            self._reader = None
            self._authenticated = self._boundary = False
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(self._cleanup_timeout):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass

    async def poll(self, meter_ids: Sequence[str]) -> PollResult:
        """Poll explicitly selected meters; never retry ambiguous operations."""
        ids = tuple(meter_ids)
        if not ids or any(not isinstance(x, str) or not re.fullmatch(r"[0-9]{8}", x)
                          for x in ids) or len(set(ids)) != len(ids):
            raise ValueError("Select distinct eight-digit ASCII meter IDs")
        return await self._session(ids)

    async def probe_identity(self) -> str:
        """Authenticate and identify the logger without selecting baud or meters.

        Shares polling's endpoint lock, bounded lifecycle and cancellation cleanup;
        successful authentication does not depend on any meter being online.
        """
        return (await self._session(())).identity

    async def discover_meters(self) -> tuple[str, ...]:
        """Download configured IDs only; never select baud or read a meter."""
        return await self._session((), discover=True)

    @overload
    async def _session(self, ids: tuple[str, ...], *, discover: Literal[False] = False) -> PollResult: ...

    @overload
    async def _session(self, ids: tuple[str, ...], *, discover: Literal[True]) -> tuple[str, ...]: ...

    async def _session(self, ids: tuple[str, ...], *, discover: bool = False) -> PollResult | tuple[str, ...]:
        """Share authentication, endpoint locking and bounded cleanup for all operations."""
        if self._closed:
            raise RelayConnectionError("Client is closed")
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        started = monotonic()
        try:
            # Includes lock wait; budget grows only with explicitly selected IDs.
            control_phases = 3 if ids else 2
            total = (self._connect_timeout + control_phases * self._command_timeout
                     + len(ids) * self._meter_timeout + self._cleanup_timeout
                     + (self._inventory_timeout if discover else 0))
            async with asyncio.timeout(total):
                async with self._lock:
                    if self._closed:
                        raise RelayConnectionError("Client is closed")
                    try:
                        async with asyncio.timeout(self._connect_timeout):
                            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
                        await self._control(b"\x02", self._passcode.encode("ascii"))
                        self._authenticated = True
                        identity = await self._identity()
                        if discover:
                            inventory = await self._inventory(identity)
                            await self._logout()
                            return inventory
                        if ids:
                            await self._control(b"11")
                        meters, errors = {}, {}
                        for meter_id in ids:
                            frame = await self._meter_frame(meter_id)
                            try:
                                meters[meter_id] = decode_telegram(frame, meter_id)
                            except ValueError:
                                # Full service envelope consumed: next command is safe.
                                errors[meter_id] = "Invalid M-Bus telegram"
                        await self._logout()
                        return PollResult(identity=identity, meters=meters, errors=errors,
                                          polled_at=datetime.now(timezone.utc), duration=monotonic() - started)
                    finally:
                        # Shield bounded cleanup from the caller's cancellation. Keep
                        # the endpoint lock until socket ownership is released.
                        cleanup = asyncio.create_task(self._cleanup())
                        interrupted = False
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                # Repeated close/unload cancellation must not cancel
                                # cleanup itself or release the lock before close.
                                interrupted = True
                        cleanup.result()
                        if interrupted:
                            raise asyncio.CancelledError
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as err:
            raise RelayConnectionError("Relay connection closed or timed out") from err
        finally:
            self._active.discard(task)

    async def async_close(self) -> None:
        """Permanently close and cancel/wait active session callers."""
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
