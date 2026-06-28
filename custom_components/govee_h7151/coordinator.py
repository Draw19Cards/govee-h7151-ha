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
    tank_problem: bool = False


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
) -> Optional[bytes]:
    await client.write_gatt_char(SEND_UUID, _safe_encrypt(plain, session_key), response=False)
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            remaining = deadline - asyncio.get_event_loop().time()
            enc = await asyncio.wait_for(queue.get(), timeout=max(0.1, remaining))
            resp = _safe_decrypt(enc, session_key)
            if resp[0] == plain[0] and resp[1] == plain[1]:
                return resp
        except asyncio.TimeoutError:
            break
    return None


async def _read_state(
    client: BleakClient, session_key: bytes, queue: asyncio.Queue
) -> H7151State:
    # Brief window to catch spontaneous push notifications (e.g. tank full/removed).
    # The device sends AA 01 with byte[3]=0x17 when the tank needs attention.
    tank_problem = False
    listen_until = asyncio.get_event_loop().time() + 0.5
    while asyncio.get_event_loop().time() < listen_until:
        try:
            remaining = listen_until - asyncio.get_event_loop().time()
            enc = await asyncio.wait_for(queue.get(), timeout=max(0.05, remaining))
            pkt = _safe_decrypt(enc, session_key)
            if pkt[0] == 0xAA and pkt[1] == 0x01 and pkt[3] == 0x17:
                tank_problem = True
        except asyncio.TimeoutError:
            break

    r1 = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x01))
    if r1 is None:
        raise RuntimeError("No response to AA 01")
    power = bool(r1[2])
    th_raw = (r1[5] << 16) | (r1[6] << 8) | r1[7]
    current_temp_c = (th_raw // 1000) / 10.0
    current_humidity = (th_raw % 1000) / 10.0

    r_mode = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x00"))
    mode_reg = r_mode[3] if (r_mode is not None and r_mode[2] == 0x00) else 0x01

    r_fan = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x01"))
    fan_speed = r_fan[3] if (r_fan is not None and r_fan[2] == 0x01) else 0

    r_hum = await _send_cmd(client, session_key, queue, _make_plain(0xAA, 0x05, b"\x03"))
    target_humidity = 0.0
    if r_hum is not None and r_hum[2] == 0x03:
        target_humidity = struct.unpack(">H", r_hum[5:7])[0] / 100.0

    return H7151State(
        power=power,
        mode=_decode_mode(mode_reg, fan_speed),
        fan_speed=fan_speed,
        current_humidity=current_humidity,
        current_temp_c=current_temp_c,
        target_humidity=target_humidity,
        tank_problem=tank_problem,
    )


# ── Coordinator ───────────────────────────────────────────────────────────────

class H7151Coordinator(DataUpdateCoordinator[H7151State]):
    """Manages BLE connection and periodic state updates."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=POLL_INTERVAL)
        self.address = address
        self.device_name = name
        self._lock = asyncio.Lock()

    async def _connect_and_run(self, operation):
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ) or bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=False
        )
        if not ble_device:
            raise UpdateFailed(f"Device {self.address} not found — is it powered on and in range?")
        queue: asyncio.Queue = asyncio.Queue()

        client = await establish_connection(BleakClient, ble_device, self.address)
        try:
            await client.start_notify(RECV_UUID, lambda _, d: queue.put_nowait(bytes(d)))
            await asyncio.sleep(0.1)
            session_key = await _key_exchange(client, queue)
            return await operation(client, session_key, queue)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _async_update_data(self) -> H7151State:
        async with self._lock:
            try:
                return await self._connect_and_run(_read_state)
            except Exception as err:
                raise UpdateFailed(f"BLE error communicating with {self.address}: {err}") from err

    async def async_set_power(self, on: bool) -> None:
        async with self._lock:
            async def op(client, key, queue):
                await _send_cmd(client, key, queue,
                                 _make_plain(0x33, 0x01, bytes([0x01 if on else 0x00])))
            await self._connect_and_run(op)
        await self.async_request_refresh()

    async def async_set_fan_speed(self, speed: int) -> None:
        async with self._lock:
            async def op(client, key, queue):
                await _send_cmd(client, key, queue,
                                 _make_plain(0x3A, 0x05, bytes([0x01, speed])))
            await self._connect_and_run(op)
        await self.async_request_refresh()

    async def async_set_target_humidity(self, pct: float) -> None:
        pct = max(35.0, min(85.0, pct))
        raw = int(round(pct * 100))
        payload = bytes([0x03, 0x00, 0x00, (raw >> 8) & 0xFF, raw & 0xFF])
        async with self._lock:
            async def op(client, key, queue):
                await _send_cmd(client, key, queue, _make_plain(0x3A, 0x05, payload))
            await self._connect_and_run(op)
        await self.async_request_refresh()

    async def async_set_dryer(self) -> None:
        async with self._lock:
            async def op(client, key, queue):
                await _send_cmd(client, key, queue,
                                 _make_plain(0x3A, 0x05, bytes([0x08, 0x01])))
            await self._connect_and_run(op)
        await self.async_request_refresh()
