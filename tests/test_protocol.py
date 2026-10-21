"""Wire-level regression checks. Synthetic identities only; no LAN traffic."""

import asyncio
import importlib
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

PACKAGE = "bllc_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/bllc_local")]
sys.modules[PACKAGE] = package
p = importlib.import_module(f"{PACKAGE}.protocol")


def field(value):
    return len(value).to_bytes(2, "big") + value


def discovery(device_id=b"synthetic-ventilator", product=None):
    body = field(device_id) + field(b"AABBCCDDEEFF") + field(b"04020015")
    body += field((product or p.PRODUCT_KEY).encode()) + bytes(8)
    return p.encode_frame(4, body)


def device_info():
    return (
        b"00MX308104020015000000010000000100000004"
        + bytes(8) + field(bytes(8)) + field(p.PRODUCT_KEY.encode())
    )


def status(mode=1):
    # Captured field layout, with synthetic timer/filter data to detect writes
    # that accidentally replace fields other than Mode.
    return bytes([3, mode-1, 83]) + bytes(range(12)) + bytes.fromhex("420002eb")


class Writer:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        pass


class ProtocolTests(unittest.TestCase):
    def test_captured_status_mapping(self):
        first = p.parse_status(bytes.fromhex("030000000000000000000000000000420002eb"))
        second = p.parse_status(bytes.fromhex("0300000000000000000000000000003b000300"))
        self.assertEqual((first.mode, first.temperature, first.humidity), ("1", 24.7, 66))
        self.assertEqual((second.mode, second.temperature, second.humidity), ("1", 26.8, 59))

    def test_write_only_mode_field(self):
        current = p.parse_status(status())
        suffix = bytes([83]) + bytes(range(12))
        self.assertEqual(p.mode_payload("5", current), bytes.fromhex("010104") + suffix)
        self.assertEqual(p.mode_payload("3", current), bytes.fromhex("010102") + suffix)
        self.assertEqual(p.mode_payload("2", current), bytes.fromhex("010101") + suffix)
        self.assertEqual(p.mode_payload("1", current), bytes.fromhex("010100") + suffix)
        self.assertEqual(len(p.mode_payload("3", current)), 16)
        with self.assertRaises(ValueError):
            p.mode_payload("4", current)

    def test_ascii_mac_and_legacy_discovery_without_tail(self):
        identity = p.parse_identity(discovery())
        self.assertEqual(identity.mac, "aabbccddeeff")
        self.assertEqual(identity.firmware, "04020015")
        self.assertNotIn("synthetic-ventilator", repr(identity))

    def test_product_mismatch(self):
        with self.assertRaises(p.IdentityError):
            p.parse_identity(discovery(product="b" * 32))

    def test_malformed_discovery_is_rejected(self):
        for malformed in [b"", discovery()[:-1], b"xxxx" + discovery()[4:]]:
            with self.subTest(packet=malformed[:5]):
                with self.assertRaises(p.ProtocolError):
                    p.parse_identity(malformed)

    def test_invalid_values_do_not_turn_into_real_measurements(self):
        raw = bytearray(status())
        raw[15] = 255
        raw[17:19] = bytes(2)
        value = p.parse_status(bytes(raw))
        self.assertIsNone(value.temperature)
        self.assertIsNone(value.humidity)

    def test_hosts_are_local_literals_only(self):
        self.assertEqual(p.validate_host("192.168.50.10"), "192.168.50.10")
        for value in ["example.com", "8.8.8.8", "127.0.0.1", "0.0.0.0", "224.0.0.1"]:
            with self.assertRaises(ValueError):
                p.validate_host(value)


class AsyncProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_and_coalesced_frames(self):
        reader = asyncio.StreamReader()
        first = p.encode_frame(0x14, b"x" * 130)
        second = p.encode_frame(9, b"\x00")
        task = asyncio.create_task(p.read_frame(reader))
        for part in [first[:3], first[3:7], first[7:40], first[40:] + second]:
            reader.feed_data(part)
            await asyncio.sleep(0)
        self.assertEqual(await task, (0x14, b"x" * 130))
        self.assertEqual(await p.read_frame(reader), (9, b"\x00"))

    async def test_legacy_empty_passcode(self):
        reader = asyncio.StreamReader()
        reader.feed_data(bytes.fromhex("000000030f0000070000"))
        reader.feed_eof()
        self.assertEqual(await p.read_frame(reader), (7, b"\x00\x00"))

    async def test_oversized_frame(self):
        reader = asyncio.StreamReader()
        reader.feed_data(bytes.fromhex("00000003808001"))
        with self.assertRaises(p.ProtocolError):
            await p.read_frame(reader)

    async def test_wrong_device_never_gets_login_or_control(self):
        client = p.BllcClient("192.168.50.10", "expected-device", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock()
        ) as connect:
            with self.assertRaises(p.IdentityError):
                await client.set_mode("3")
            connect.assert_not_awaited()

    async def test_control_readback_and_no_credential_request(self):
        reader = asyncio.StreamReader()
        writer = Writer()
        # Observed legacy empty ACK and pushed action-4 reports must not count
        # as confirmation. Neither do standard ACKs, regardless of sequence.
        reader.feed_data(b"".join([
            p.encode_frame(0x14, device_info()),
            p.encode_frame(9, b"\x00"),
            p.encode_frame(0x91, status(1)),
            p.encode_frame(0x91),
            p.encode_frame(0x91, b"\x04" + status(1)[1:]),
            p.encode_frame(0x94, struct.pack(">I", 99)),
            p.encode_frame(0x94, struct.pack(">I", 1)),
            p.encode_frame(0x91, status(3)),
        ]))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
        ):
            result = await client.set_mode("3")
        self.assertEqual(result.mode, "3")
        self.assertEqual(result.filter_raw, 83)
        self.assertEqual(result.timer_raw, bytes(range(12)))
        writes = [p.decode_datagram(value) for value in writer.writes]
        self.assertEqual([command for command, _ in writes], [0x13, 8, 0x90, 0x93, 0x90])
        self.assertEqual(writes[3][1], bytes.fromhex("0000000101010253") + bytes(range(12)))
        self.assertFalse(writer.closed)
        await client.async_close()
        self.assertTrue(writer.closed)

    async def test_ack_without_changed_readback_is_not_success(self):
        reader = asyncio.StreamReader()
        writer = Writer()
        reader.feed_data(b"".join([
            p.encode_frame(0x14, device_info()), p.encode_frame(9, b"\x00"),
            p.encode_frame(0x91, status(1)), p.encode_frame(0x91),
            p.encode_frame(0x91, b"\x04" + status(3)[1:]),
            *[p.encode_frame(0x91, status(1)) for _ in range(4)],
        ]))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
        ), patch.object(p.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(p.ControlNotConfirmed):
                await client.set_mode("3")
        await client.async_close()
        self.assertTrue(writer.closed)

    async def test_rejected_authentication_never_controls(self):
        reader = asyncio.StreamReader()
        writer = Writer()
        reader.feed_data(p.encode_frame(0x14, device_info()) + p.encode_frame(9, b"\x01"))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
        ):
            with self.assertRaises(p.AuthenticationError):
                await client.set_mode("3")
        self.assertEqual([p.decode_datagram(value)[0] for value in writer.writes], [0x13, 8])
        self.assertTrue(writer.closed)

    async def test_current_mode_is_not_rewritten(self):
        reader = asyncio.StreamReader()
        writer = Writer()
        reader.feed_data(p.encode_frame(0x14, device_info()) + p.encode_frame(9, b"\x00") + p.encode_frame(0x91, status(5)))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
        ):
            self.assertEqual((await client.set_mode("5")).mode, "5")
        self.assertEqual([p.decode_datagram(value)[0] for value in writer.writes], [0x13, 8, 0x90])
        await client.async_close()

    async def test_polling_reuses_authenticated_connection(self):
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(p.encode_frame(0x14, device_info()) + p.encode_frame(9, b"\x00")
                         + p.encode_frame(0x91, status(1)) + p.encode_frame(0x91, status(2)))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))
        ) as connect:
            self.assertEqual((await client.read_status()).mode, "1")
            self.assertEqual((await client.read_status()).mode, "2")
            connect.assert_awaited_once()
        await client.async_close()

    async def test_read_reconnects_once_after_transport_failure(self):
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(p.encode_frame(0x14, device_info()) + p.encode_frame(9, b"\x00")
                         + p.encode_frame(0x91, status(2)))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(side_effect=[OSError("synthetic disconnect"), (reader, writer)])
        ) as connect, patch.object(p.asyncio, "sleep", AsyncMock()):
            self.assertEqual((await client.read_status()).mode, "2")
            self.assertEqual(connect.await_count, 2)
        await client.async_close()

    async def test_lost_control_response_is_queried_without_replaying_write(self):
        first, second = asyncio.StreamReader(), asyncio.StreamReader()

        class DisconnectAfterControl(Writer):
            def write(self, value):
                super().write(value)
                if p.decode_datagram(value)[0] == 0x93:
                    first.feed_eof()

        first_writer, second_writer = DisconnectAfterControl(), Writer()
        for reader, mode in [(first, 1), (second, 3)]:
            reader.feed_data(p.encode_frame(0x14, device_info()) + p.encode_frame(9, b"\x00")
                             + p.encode_frame(0x91, status(mode)))
        client = p.BllcClient("192.168.50.10", "synthetic-ventilator", "placeholder")
        with patch.object(p, "discover", AsyncMock(return_value=p.parse_identity(discovery()))), patch.object(
            p.asyncio, "open_connection", AsyncMock(side_effect=[(first, first_writer), (second, second_writer)])
        ), patch.object(p.asyncio, "sleep", AsyncMock()):
            self.assertEqual((await client.set_mode("3")).mode, "3")
        commands = [p.decode_datagram(value)[0] for value in first_writer.writes + second_writer.writes]
        self.assertEqual(commands.count(0x93), 1)
        self.assertTrue(first_writer.closed)
        await client.async_close()


if __name__ == "__main__":
    unittest.main()
