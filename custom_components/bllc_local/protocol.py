"""Small asynchronous GAgent client for the verified BLLC product only.

All traffic goes to the configured private LAN IPv4 address. The product
definition is fixed locally; there are no cloud APIs or runtime dependencies.
Only the Mode data point can be written. Filter and timer values are untouched.

References:
  gizwits/Gizwits-GAgent, gagent/lan/{inc/lan.h,src/lantcp.c}
  Verified BLLC product data-point definition, protocol 00000004
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import socket
import struct
from typing import AsyncIterator

from .const import MANUAL_MODES, MODE_OFF, PRODUCT_KEY, TCP_PORT, UDP_PORT

PREFIX = b"\x00\x00\x00\x03"
REQUEST_TIMEOUT = 4.0
MAX_FRAME_SIZE = 4096


class BllcError(Exception):
    """A device operation failed without exposing credentials."""


class CommunicationError(BllcError):
    """The transport failed; a fresh read-only connection may recover it."""


class AuthenticationError(BllcError):
    """The device requires a different local credential."""


class IdentityError(BllcError):
    """The IP address belongs to a different or unsupported device."""


class ProtocolError(BllcError):
    """A reply has an invalid or unsupported layout."""


class ControlNotConfirmed(BllcError):
    """A command was sent but the requested mode was not read back."""


@dataclass(frozen=True)
class Identity:
    device_id: str = field(repr=False)
    mac: str = field(repr=False)
    product_key: str
    firmware: str


@dataclass(frozen=True)
class DeviceInfo:
    wifi_hardware: str
    wifi_software: str
    mcu_hardware: str
    mcu_software: str
    payload_protocol: str
    product_key: str


@dataclass(frozen=True)
class Status:
    mode: str
    temperature: float | None
    humidity: int | None
    filter_raw: int
    timer_raw: bytes = field(repr=False)


def validate_host(host: str) -> str:
    """Accept only literal private LAN addresses; never resolve DNS names."""
    try:
        address = ipaddress.IPv4Address(host.strip())
    except (ValueError, AttributeError) as err:
        raise ValueError("A private IPv4 address is required") from err
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError("A private LAN IPv4 address is required")
    return str(address)


def encode_frame(command: int, payload: bytes = b"") -> bytes:
    body = b"\x00" + command.to_bytes(2, "big") + payload
    size = len(body)
    if size > MAX_FRAME_SIZE:
        raise ProtocolError("Packet exceeds the supported size")
    encoded = bytearray()
    while size:
        byte = size & 127
        size >>= 7
        encoded.append(byte | (128 if size else 0))
    return PREFIX + encoded + body


def decode_datagram(packet: bytes) -> tuple[int, bytes]:
    if packet[:4] != PREFIX:
        raise ProtocolError("Unexpected discovery protocol")
    size, cursor = 0, 4
    for shift in range(0, 28, 7):
        if cursor >= len(packet):
            raise ProtocolError("Truncated packet length")
        byte = packet[cursor]
        cursor += 1
        size |= (byte & 127) << shift
        if not byte & 128:
            break
    else:
        raise ProtocolError("Invalid packet length")
    if size < 3 or size > MAX_FRAME_SIZE or len(packet) != cursor + size:
        raise ProtocolError("Packet length mismatch")
    body = packet[cursor:]
    if body[0] != 0:
        raise ProtocolError("Unsupported protocol flags")
    return int.from_bytes(body[1:3], "big"), body[3:]


def _field(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise ProtocolError("Truncated field length")
    length = int.from_bytes(data[offset:offset + 2], "big")
    end = offset + 2 + length
    if end > len(data):
        raise ProtocolError("Truncated field")
    return data[offset + 2:end], end


def _ascii(value: bytes) -> str:
    try:
        return value.decode("ascii")
    except UnicodeError as err:
        raise ProtocolError("Invalid text field") from err


def parse_identity(packet: bytes) -> Identity:
    command, data = decode_datagram(packet)
    if command != 4:
        raise ProtocolError("Unexpected discovery reply")
    device_id, offset = _field(data, 0)
    mac, offset = _field(data, offset)
    firmware, offset = _field(data, offset)
    product_key, offset = _field(data, offset)
    if not device_id or not mac:
        raise ProtocolError("Device identity is missing")
    product = _ascii(product_key)
    if product != PRODUCT_KEY:
        raise IdentityError("This product has not been verified for BLLC Local")
    # This firmware sends MAC as 12 ASCII hex digits; others use six bytes.
    if len(mac) == 12:
        try:
            mac_text = _ascii(mac).lower()
            bytes.fromhex(mac_text)
        except ValueError as err:
            raise ProtocolError("Invalid device MAC") from err
    elif len(mac) == 6:
        mac_text = mac.hex()
    else:
        raise ProtocolError("Invalid device MAC length")
    return Identity(_ascii(device_id), mac_text, product, _ascii(firmware))


def parse_info(data: bytes) -> DeviceInfo:
    if len(data) < 52:
        raise ProtocolError("Incomplete device information")
    _, offset = _field(data, 48)
    product_key, _ = _field(data, offset)
    info = DeviceInfo(
        *(_ascii(data[offset:offset + 8]) for offset in range(0, 40, 8)),
        product_key=_ascii(product_key),
    )
    if info.product_key != PRODUCT_KEY or info.payload_protocol != "00000004":
        raise IdentityError("Unsupported product or data protocol")
    return info


def parse_status(payload: bytes) -> Status:
    if len(payload) != 19 or payload[0] not in (3, 4):
        raise ProtocolError("Unexpected BLLC status payload")
    data = payload[1:]
    mode_index = data[0] & 7
    if mode_index > 4:
        raise ProtocolError("Invalid device mode")
    raw_temp = int.from_bytes(data[16:18], "big")
    return Status(
        mode=str(mode_index + 1),
        temperature=(raw_temp - 500) / 10 if 1 <= raw_temp <= 1000 else None,
        humidity=data[14] if data[14] <= 100 else None,
        filter_raw=data[1],
        timer_raw=data[2:14],
    )


def mode_payload(mode: str, current: Status) -> bytes:
    if mode not in (*MANUAL_MODES, MODE_OFF):
        raise ValueError("Only the verified manual modes and off may be selected")
    if len(current.timer_raw) != 12 or not 0 <= current.filter_raw <= 255:
        raise ProtocolError("Invalid current writable data")
    # Standard P0 requires the complete 14-byte writable data area, even when
    # only one attribute flag is set. Truncating it to Mode is accepted by
    # some MCUs but fails on others. Flag 0 alone selects Mode; carrying the
    # freshly read filter/timer values also preserves them defensively.
    return bytes((1, 1, int(mode) - 1, current.filter_raw)) + current.timer_raw


async def discover(host: str) -> Identity:
    """One unicast discovery, limited to the explicitly configured device."""
    host = validate_host(host)
    loop = asyncio.get_running_loop()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            await loop.sock_sendto(sock, encode_frame(3), (host, UDP_PORT))
            async with asyncio.timeout(REQUEST_TIMEOUT):
                while True:
                    packet, peer = await loop.sock_recvfrom(sock, MAX_FRAME_SIZE)
                    if peer[0] == host and peer[1] == UDP_PORT:
                        return parse_identity(packet)
    except (OSError, TimeoutError) as err:
        raise CommunicationError("The device did not answer local discovery") from err


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read exactly one frame, preserving subsequent buffered replies."""
    if await reader.readexactly(4) != PREFIX:
        raise ProtocolError("Unexpected TCP protocol")
    size = 0
    for shift in range(0, 28, 7):
        byte = (await reader.readexactly(1))[0]
        size |= (byte & 127) << shift
        if not byte & 128:
            break
    else:
        raise ProtocolError("Invalid packet length")
    if not 3 <= size <= MAX_FRAME_SIZE:
        raise ProtocolError("Invalid reply size")
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as err:
        # Legacy GAgent denies passcode requests with a stale header length.
        if size == 15 and err.partial == b"\x00\x00\x07\x00\x00":
            return 7, b"\x00\x00"
        raise
    if body[0] != 0:
        raise ProtocolError("Unsupported reply flags")
    return int.from_bytes(body[1:3], "big"), body[3:]


class Session:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def send(self, command: int, payload: bytes = b"") -> None:
        self.writer.write(encode_frame(command, payload))
        await self.writer.drain()

    async def request(self, command: int, payload: bytes = b"") -> bytes:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            await self.send(command, payload)
            while True:
                response_command, body = await read_frame(self.reader)
                if response_command != command + 1:
                    continue  # Unsolicited status reports may precede an ACK.
                if command == 0x90 and (not body or body[0] == 4):
                    # MX3081 firmware 04020015 emits an empty 0x91 control ACK
                    # and unsolicited action-4 reports instead of 0x94. Only
                    # action-3 is the response to our explicit status query.
                    continue
                return body


class BllcClient:
    """Serialize each device's polls and verified mode changes."""

    def __init__(self, host: str, device_id: str = "", passcode: str = ""):
        self.host = validate_host(host)
        self.device_id = device_id
        self.passcode = passcode
        self.identity: Identity | None = None
        self.info: DeviceInfo | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._active_session: Session | None = None

    async def _disconnect(self) -> None:
        session, self._active_session = self._active_session, None
        if session is not None:
            session.writer.close()
            try:
                async with asyncio.timeout(1):
                    await session.writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def async_close(self) -> None:
        """Release the local connection on flow completion, unload or shutdown."""
        async with self._lock:
            await self._disconnect()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Session]:
        try:
            if self._active_session is not None and (
                self._active_session.reader.at_eof()
                or self._active_session.writer.is_closing()
            ):
                await self._disconnect()
            if self._active_session is None:
                identity = await discover(self.host)
                if self.device_id and identity.device_id != self.device_id:
                    raise IdentityError("A different device now occupies this IP address")
                self.identity = identity
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    reader, writer = await asyncio.open_connection(
                        self.host, TCP_PORT, family=socket.AF_INET, limit=MAX_FRAME_SIZE
                    )
                session = self._active_session = Session(reader, writer)
                self.info = parse_info(await session.request(0x13))
                if not self.passcode:
                    credentials = await session.request(6)
                    value, offset = _field(credentials, 0)
                    if not value:
                        raise AuthenticationError("The local passcode is not available")
                    if offset != len(credentials) or len(value) > 64:
                        raise ProtocolError("Invalid local credential response")
                    self.passcode = _ascii(value)
                value = self.passcode.encode("ascii")
                if not 1 <= len(value) <= 64:
                    raise AuthenticationError("Invalid local passcode")
                result = await session.request(8, len(value).to_bytes(2, "big") + value)
                if result != b"\x00":
                    raise AuthenticationError("The local passcode was rejected")
                self.device_id = identity.device_id
            yield self._active_session
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            await self._disconnect()
            raise CommunicationError("Local communication failed") from err
        except BaseException:
            await self._disconnect()
            raise

    async def _read_status_with_reconnect(self) -> Status:
        for attempt in range(2):
            try:
                async with self._session() as session:
                    return parse_status(await session.request(0x90, b"\x02"))
            except CommunicationError:
                if attempt:
                    raise
                await asyncio.sleep(0.5)
        raise AssertionError("Unreachable")

    async def read_status(self) -> Status:
        async with self._lock:
            return await self._read_status_with_reconnect()

    async def set_mode(self, mode: str) -> Status:
        """Write only Mode, then confirm it from a fresh device status reply."""
        if mode not in (*MANUAL_MODES, MODE_OFF):
            raise ValueError("Only the verified manual modes and off may be selected")
        async with self._lock:
            before = await self._read_status_with_reconnect()
            if before.mode == mode:
                return before
            try:
                async with self._session() as session:
                    payload = mode_payload(mode, before)
                    self._sequence = (self._sequence + 1) & 0xFFFFFFFF
                    sequence = self._sequence
                    await session.send(0x93, struct.pack(">I", sequence) + payload)
                    # This old server processes one frame per recv. The gap
                    # avoids a control and query arriving as one input buffer
                    # and lets the MCU finish the mode change.
                    await asyncio.sleep(1)
                    for attempt in range(4):
                        if attempt:
                            await asyncio.sleep(0.5)
                        status = parse_status(await session.request(0x90, b"\x02"))
                        if status.mode == mode:
                            return status
            except CommunicationError:
                # A lost response must never replay a physical command.
                # Reconnect and query the resulting device state instead.
                status = await self._read_status_with_reconnect()
                if status.mode == mode:
                    return status
            raise ControlNotConfirmed("The requested mode was not confirmed by the device")
