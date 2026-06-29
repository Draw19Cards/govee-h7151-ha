"""BLE protocol for the GoveeLife H7151 Dehumidifier.

Pure protocol layer: encryption, packet framing, key exchange, and the
read/command exchanges. No Home Assistant dependencies so it can be reasoned
about (and tested) on its own. A single H7151Device instance is reused across
connections; each connection negotiates a fresh session key.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from dataclasses import dataclass

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import MODE_AUTO, MODE_DRYER, MODE_HIGH, MODE_LOW, MODE_MEDIUM

_LOGGER = logging.getLogger(__name__)

SEND_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
RECV_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
PRODUCT_KEY = b"MakingLifeSmarte"

# The H7151 allows only one BLE connection and drops it readily, so a single
# connect attempt often loses a race against another client (e.g. the Govee
# phone app). Retry a few times so a poll/command rides through a transient
# collision. Bounded overall by the coordinator's operation timeout.
CONNECT_ATTEMPTS = 3


# ── Crypto ────────────────────────────────────────────────────────────────────

def _aes_ecb(key: bytes, block: bytes, *, decrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    worker = cipher.decryptor() if decrypt else cipher.encryptor()
    return worker.update(block) + worker.finalize()


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out.append(s[(s[i] + s[j]) & 255] ^ byte)
    return bytes(out)


def _safe_crypt(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    """AES/ECB on each 16-byte block; RC4 on the remainder."""
    out, i = bytearray(), 0
    while i + 16 <= len(data):
        out.extend(_aes_ecb(key, data[i:i + 16], decrypt=decrypt))
        i += 16
    if i < len(data):
        out.extend(_rc4(key, data[i:]))
    return bytes(out)


def _encrypt(data: bytes, key: bytes) -> bytes:
    return _safe_crypt(data, key, decrypt=False)


def _decrypt(data: bytes, key: bytes) -> bytes:
    return _safe_crypt(data, key, decrypt=True)


# ── Packet framing ──────────────────────────────────────────────────────────

def _xor_checksum(data: bytes) -> int:
    r = 0
    for b in data:
        r ^= b
    return r


def _make_plain(prefix: int, cmd: int, payload: bytes = b"") -> bytes:
    body = bytearray([prefix, cmd])
    body.extend(payload[:17])
    body.extend(b"\x00" * (19 - len(body)))
    body.append(_xor_checksum(body))
    return bytes(body)


# Command builders ---------------------------------------------------------------

def make_power(on: bool) -> bytes:
    return _make_plain(0x33, 0x01, bytes([0x01 if on else 0x00]))


def make_fan(speed: int) -> bytes:
    return _make_plain(0x3A, 0x05, bytes([0x01, speed]))


def make_humidity(pct: float) -> bytes:
    raw = int(round(pct * 100))
    return _make_plain(0x3A, 0x05, bytes([0x03, 0x00, 0x00, (raw >> 8) & 0xFF, raw & 0xFF]))


def make_dryer() -> bytes:
    return _make_plain(0x3A, 0x05, bytes([0x08, 0x01]))


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class H7151State:
    power: bool
    mode: str
    fan_speed: int
    current_humidity: float
    current_temp_c: float
    target_humidity: float


def _decode_mode(mode_reg: int, fan_speed: int) -> str:
    if mode_reg == 0x03:
        return MODE_AUTO
    if mode_reg == 0x08:
        return MODE_DRYER
    return {1: MODE_LOW, 2: MODE_MEDIUM}.get(fan_speed, MODE_HIGH)


# ── Exchanges ─────────────────────────────────────────────────────────────────

async def _key_exchange(client: BleakClient, queue: asyncio.Queue[bytes]) -> bytes:
    """E7 01 / E7 02 handshake; returns the 16-byte session key."""
    tx1 = bytearray([0xE7, 0x01]) + os.urandom(17)
    tx1.append(_xor_checksum(tx1))
    await client.write_gatt_char(SEND_UUID, _encrypt(bytes(tx1), PRODUCT_KEY), response=False)

    session_key: bytes | None = None
    deadline = asyncio.get_event_loop().time() + 6.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            enc = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        plain = _decrypt(enc, PRODUCT_KEY)
        if plain[0] == 0xE7 and plain[1] == 0x01:
            session_key = bytes(plain[2:18])
            break
    if not session_key:
        raise RuntimeError("Key exchange failed: no session key received")

    tx2 = bytearray([0xE7, 0x02]) + os.urandom(17)
    tx2.append(_xor_checksum(tx2))
    await client.write_gatt_char(SEND_UUID, _encrypt(bytes(tx2), PRODUCT_KEY), response=False)

    deadline = asyncio.get_event_loop().time() + 6.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            enc = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        plain = _decrypt(enc, PRODUCT_KEY)
        if plain[0] == 0xE7 and plain[1] == 0x02:
            break

    await asyncio.sleep(0.3)
    while not queue.empty():
        queue.get_nowait()
    return session_key


async def _send_cmd(
    client: BleakClient,
    session_key: bytes,
    queue: asyncio.Queue[bytes],
    plain: bytes,
    match_len: int = 2,
) -> bytes | None:
    """Write a packet and return the matching response (matched on match_len bytes).

    AA 05 subcommands all share the AA 05 prefix, so match_len=3 is used for
    those to avoid consuming a stale sibling response from the notify queue.
    """
    await client.write_gatt_char(SEND_UUID, _encrypt(plain, session_key), response=False)
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            enc = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
        except asyncio.TimeoutError:
            break
        resp = _decrypt(enc, session_key)
        if resp[:match_len] == plain[:match_len]:
            return resp
    return None


async def _read_state(
    client: BleakClient, session_key: bytes, queue: asyncio.Queue[bytes]
) -> H7151State:
    r1 = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x01))
    if r1 is None:
        raise RuntimeError("No response to AA 01")
    power = bool(r1[2])
    th_raw = (r1[5] << 16) | (r1[6] << 8) | r1[7]
    current_temp_c = (th_raw // 1000) / 10.0
    current_humidity = (th_raw % 1000) / 10.0

    r_mode = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x00"), match_len=3)
    mode_reg = r_mode[3] if r_mode is not None else 0x01

    r_fan = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x01"), match_len=3)
    fan_speed = r_fan[3] if r_fan is not None else 0

    r_hum = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x03"), match_len=3)
    target_humidity = 0.0
    if r_hum is not None:
        target_humidity = struct.unpack(">H", r_hum[5:7])[0] / 100.0

    return H7151State(
        power=power,
        mode=_decode_mode(mode_reg, fan_speed),
        fan_speed=fan_speed,
        current_humidity=current_humidity,
        current_temp_c=current_temp_c,
        target_humidity=target_humidity,
    )


# ── Device ────────────────────────────────────────────────────────────────────

class H7151Device:
    """Stateless-per-connection BLE client for the H7151."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def _connect(self, ble_device: BLEDevice) -> tuple[BleakClient, bytes]:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        client = await establish_connection(
            BleakClient, ble_device, ble_device.address, max_attempts=CONNECT_ATTEMPTS
        )
        await client.start_notify(
            RECV_UUID, lambda _, d: self._queue.put_nowait(bytes(d))
        )
        await asyncio.sleep(0.1)
        session_key = await _key_exchange(client, self._queue)
        return client, session_key

    @staticmethod
    async def _disconnect(client: BleakClient) -> None:
        try:
            await client.disconnect()
        except Exception as err:  # pragma: no cover - best effort cleanup
            _LOGGER.debug("disconnect error (ignored): %s", err)

    async def async_poll(self, ble_device: BLEDevice) -> H7151State:
        client, session_key = await self._connect(ble_device)
        try:
            return await _read_state(client, session_key, self._queue)
        finally:
            await self._disconnect(client)

    async def async_command(self, ble_device: BLEDevice, plain: bytes) -> H7151State:
        """Send a command, then read back and return the resulting state."""
        client, session_key = await self._connect(ble_device)
        try:
            await _send_cmd(client, session_key, self._queue, plain)
            await asyncio.sleep(0.3)
            return await _read_state(client, session_key, self._queue)
        finally:
            await self._disconnect(client)
