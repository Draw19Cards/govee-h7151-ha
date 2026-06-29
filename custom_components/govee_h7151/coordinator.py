"""BLE protocol + DataUpdateCoordinator for Govee H7151 Dehumidifier."""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MODE_AUTO, MODE_DRYER, MODE_HIGH, MODE_LOW, MODE_MEDIUM

_LOGGER = logging.getLogger(__name__)

SEND_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
RECV_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
PRODUCT_KEY = b"MakingLifeSmarte"
POLL_INTERVAL = timedelta(seconds=30)
# Hard ceiling on a full connect + exchange + disconnect. A BLE call (connect,
# notify, or disconnect on a dead link) can otherwise block forever, which would
# stall the coordinator's poll loop and leave the device stuck "unavailable"
# with no recovery. Kept below POLL_INTERVAL so polls never pile up.
OP_TIMEOUT = 20


# ── Crypto (Safe.java) ────────────────────────────────────────────────────────

def _aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
    c = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    e = c.encryptor()
    return e.update(block) + e.finalize()


def _aes_ecb_decrypt(key: bytes, block: bytes) -> bytes:
    c = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    d = c.decryptor()
    return d.update(block) + d.finalize()


def _rc4_xor(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + S[i]) & 255
        S[i], S[j] = S[j], S[i]
        out.append(S[(S[i] + S[j]) & 255] ^ byte)
    return bytes(out)


def _safe_encrypt(data: bytes, key: bytes) -> bytes:
    out, i = bytearray(), 0
    while i + 16 <= len(data):
        out.extend(_aes_ecb_encrypt(key, data[i:i + 16]))
        i += 16
    if i < len(data):
        out.extend(_rc4_xor(key, data[i:]))
    return bytes(out)


def _safe_decrypt(data: bytes, key: bytes) -> bytes:
    out, i = bytearray(), 0
    while i + 16 <= len(data):
        out.extend(_aes_ecb_decrypt(key, data[i:i + 16]))
        i += 16
    if i < len(data):
        out.extend(_rc4_xor(key, data[i:]))
    return bytes(out)


# ── Packet helpers ────────────────────────────────────────────────────────────

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


# ── BLE session helpers ───────────────────────────────────────────────────────

async def _key_exchange(client: BleakClient, queue: asyncio.Queue) -> bytes:
    """E7 01 / E7 02 handshake; returns 16-byte session key."""
    plain1 = bytearray([0xE7, 0x01]) + os.urandom(17)
    plain1.append(_xor_checksum(plain1))
    await client.write_gatt_char(SEND_UUID, _safe_encrypt(bytes(plain1), PRODUCT_KEY), response=False)

    session_key: Optional[bytes] = None
    deadline = asyncio.get_event_loop().time() + 6.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            enc = await asyncio.wait_for(queue.get(), timeout=1.0)
            plain = _safe_decrypt(enc, PRODUCT_KEY)
            if plain[0] == 0xE7 and plain[1] == 0x01:
                session_key = bytes(plain[2:18])
                break
        except asyncio.TimeoutError:
            pass
    if not session_key:
        raise RuntimeError("Key exchange failed: no session key received")

    plain2 = bytearray([0xE7, 0x02]) + os.urandom(17)
    plain2.append(_xor_checksum(plain2))
    await client.write_gatt_char(SEND_UUID, _safe_encrypt(bytes(plain2), PRODUCT_KEY), response=False)

    deadline = asyncio.get_event_loop().time() + 6.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            enc = await asyncio.wait_for(queue.get(), timeout=1.0)
            plain = _safe_decrypt(enc, PRODUCT_KEY)
            if plain[0] == 0xE7 and plain[1] == 0x02:
                break
        except asyncio.TimeoutError:
            pass

    await asyncio.sleep(0.3)
    while not queue.empty():
        queue.get_nowait()

    return session_key


async def _send_cmd(
    client: BleakClient,
    session_key: bytes,
    queue: asyncio.Queue,
    plain: bytes,
    match_len: int = 2,
) -> Optional[bytes]:
    await client.write_gatt_char(SEND_UUID, _safe_encrypt(plain, session_key), response=False)
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            remaining = deadline - asyncio.get_event_loop().time()
            enc = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
            resp = _safe_decrypt(enc, session_key)
            if resp[:match_len] == plain[:match_len]:
                return resp
        except asyncio.TimeoutError:
            break
    return None


async def _read_state(
    client: BleakClient, session_key: bytes, queue: asyncio.Queue
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


# ── Coordinator ───────────────────────────────────────────────────────────────

class H7151Coordinator(DataUpdateCoordinator[H7151State]):
    """Connect-per-poll BLE coordinator for the H7151.

    The device only supports a single BLE connection and drops it shortly
    after each exchange, so we connect, do our work, and disconnect every
    cycle. Connecting through bleak_retry_connector.establish_connection lets
    habluetooth track and release the connection slot correctly; holding the
    connection (or connecting around habluetooth) leaks the single slot.
    """

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=POLL_INTERVAL)
        self.address = address
        self.device_name = name
        self._lock = asyncio.Lock()
        self._notify_queue: asyncio.Queue = asyncio.Queue()

    async def _connect(self) -> tuple[BleakClient, bytes]:
        """Open a connection and negotiate a session key."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ) or bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=False
        )
        if not ble_device:
            raise UpdateFailed(
                f"Device {self.address} not found — is it powered on and in range?"
            )

        # Drop any stale notifications from a previous connection.
        while not self._notify_queue.empty():
            try:
                self._notify_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        _LOGGER.debug("%s: connecting", self.address)
        client = await establish_connection(
            BleakClient, ble_device, self.address, max_attempts=1
        )
        await client.start_notify(
            RECV_UUID, lambda _, d: self._notify_queue.put_nowait(bytes(d))
        )
        await asyncio.sleep(0.1)
        session_key = await _key_exchange(client, self._notify_queue)
        _LOGGER.debug("%s: session ready", self.address)
        return client, session_key

    @staticmethod
    async def _disconnect(client: BleakClient) -> None:
        try:
            await client.disconnect()
        except Exception as err:  # pragma: no cover - best effort cleanup
            _LOGGER.debug("disconnect error (ignored): %s", err)

    async def async_disconnect(self) -> None:
        """No persistent connection is held; nothing to clean up on unload."""

    async def _run_connected(self, op):
        """Connect, run op(client, session_key), then always disconnect.

        Serialized by _lock and bounded by OP_TIMEOUT so a hung BLE call can
        never stall the poll loop or hold the lock indefinitely.
        """
        async with self._lock:
            async with asyncio.timeout(OP_TIMEOUT):
                client, session_key = await self._connect()
                try:
                    return await op(client, session_key)
                finally:
                    await self._disconnect(client)

    async def _async_update_data(self) -> H7151State:
        try:
            return await self._run_connected(
                lambda client, key: _read_state(client, key, self._notify_queue)
            )
        except Exception as err:
            raise UpdateFailed(
                f"BLE error communicating with {self.address}: {err}"
            ) from err

    async def _write(self, plain: bytes) -> None:
        await self._run_connected(
            lambda client, key: _send_cmd(client, key, self._notify_queue, plain)
        )

    async def async_set_power(self, on: bool) -> None:
        await self._write(_make_plain(0x33, 0x01, bytes([0x01 if on else 0x00])))
        await self.async_request_refresh()

    async def async_set_fan_speed(self, speed: int) -> None:
        await self._write(_make_plain(0x3A, 0x05, bytes([0x01, speed])))
        await self.async_request_refresh()

    async def async_set_target_humidity(self, pct: float) -> None:
        pct = max(35.0, min(85.0, pct))
        raw = int(round(pct * 100))
        payload = bytes([0x03, 0x00, 0x00, (raw >> 8) & 0xFF, raw & 0xFF])
        await self._write(_make_plain(0x3A, 0x05, payload))
        await self.async_request_refresh()

    async def async_set_dryer(self) -> None:
        await self._write(_make_plain(0x3A, 0x05, bytes([0x08, 0x01])))
        await self.async_request_refresh()
