"""
H7151 BLE protocol driver.

Encryption: AES/ECB/NoPadding per 16-byte block + RC4 on remainder (from Safe.java).
Key exchange uses product key "MakingLifeSmarte"; session key is negotiated per connection.

Usage:
    async with H7151.connect() as dev:
        state = await dev.get_state()
        print(state)
        await dev.set_power(True)
        await dev.set_target_humidity(50)
"""

import asyncio
import os
import struct
from dataclasses import dataclass
from typing import Optional

from Crypto.Cipher import AES
from bleak import BleakClient, BleakScanner

# ── BLE UUIDs ────────────────────────────────────────────────────────────────
SEND_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"  # write-without-response
RECV_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"  # notify

# ── Static product key (from LibTools.c() / app_communication resource) ──────
PRODUCT_KEY = b"MakingLifeSmarte"  # 16-byte AES-128 key

TARGET_NAMES = ("H7151", "ihoment_H7151")


# ── Encryption primitives (Safe.java) ────────────────────────────────────────

def _rc4_xor(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + S[i]) & 255
        S[i], S[j] = S[j], S[i]
        out.append(S[(S[i] + S[j]) & 255] ^ byte)
    return bytes(out)


def _safe_encrypt(data: bytes, key: bytes) -> bytes:
    """Safe.a.d(): AES/ECB/NoPadding on 16-byte blocks, RC4 on remainder."""
    out = bytearray()
    i = 0
    while i + 16 <= len(data):
        out.extend(AES.new(key, AES.MODE_ECB).encrypt(data[i:i + 16]))
        i += 16
    if i < len(data):
        out.extend(_rc4_xor(key, data[i:]))
    return bytes(out)


def _safe_decrypt(data: bytes, key: bytes) -> bytes:
    """Safe.a.b(): AES/ECB/NoPadding on 16-byte blocks, RC4 on remainder."""
    out = bytearray()
    i = 0
    while i + 16 <= len(data):
        out.extend(AES.new(key, AES.MODE_ECB).decrypt(data[i:i + 16]))
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
    """Build 20-byte plaintext packet: [prefix, cmd, ...payload..., xor_checksum]."""
    body = bytearray([prefix, cmd])
    body.extend(payload[:17])
    body.extend(b"\x00" * (19 - len(body)))
    body.append(_xor_checksum(body))
    return bytes(body)


def _encrypt_packet(plain: bytes, session_key: bytes) -> bytes:
    return _safe_encrypt(plain, session_key)


def _decrypt_packet(enc: bytes, session_key: bytes) -> bytes:
    return _safe_decrypt(enc, session_key)


# ── Key exchange (Controller4Aes.java) ───────────────────────────────────────

def _make_request_session_key() -> bytes:
    """Build TX1: E7 01 [17 random bytes] [xor_checksum], encrypted with product key."""
    plain = bytearray([0xE7, 0x01])
    plain.extend(os.urandom(17))
    plain.append(_xor_checksum(plain))
    return _safe_encrypt(bytes(plain), PRODUCT_KEY)


def _make_confirm_session_key() -> bytes:
    """Build TX2: E7 02 [17 random bytes] [xor_checksum], encrypted with product key."""
    plain = bytearray([0xE7, 0x02])
    plain.extend(os.urandom(17))
    plain.append(_xor_checksum(plain))
    return _safe_encrypt(bytes(plain), PRODUCT_KEY)


def _parse_session_key_response(notification: bytes) -> Optional[bytes]:
    """Decrypt device notification with product key; return 16-byte session key if valid."""
    plain = _safe_decrypt(notification, PRODUCT_KEY)
    if plain[0] == 0xE7 and plain[1] == 0x01:
        return plain[2:18]
    return None


def _parse_confirm_ack(notification: bytes) -> bool:
    """Return True if this notification is the confirm ACK (E7 02)."""
    plain = _safe_decrypt(notification, PRODUCT_KEY)
    return plain[0] == 0xE7 and plain[1] == 0x02


# ── State dataclass ───────────────────────────────────────────────────────────

MODE_LOW    = "low"
MODE_MEDIUM = "medium"
MODE_HIGH   = "high"
MODE_AUTO   = "auto"
MODE_DRYER  = "dryer"

def _decode_mode(mode_reg: int, fan_speed: int) -> str:
    """Decode mode from AA 05 00 byte[3] and AA 05 01 byte[3]."""
    if mode_reg == 0x03:
        return MODE_AUTO
    if mode_reg == 0x08:
        return MODE_DRYER
    # mode_reg == 0x01: fan speed mode — use fan_speed to pick L/M/H
    if fan_speed == 1:
        return MODE_LOW
    if fan_speed == 2:
        return MODE_MEDIUM
    return MODE_HIGH


@dataclass
class H7151State:
    power: bool              # True = on
    mode: str                # "low"/"medium"/"high"/"auto"/"dryer"
    fan_speed: int           # 1=low, 2=med, 3=high (from aa 05 01)
    current_humidity: float  # %RH (e.g. 62.2) — packed 24-bit at aa01[5:8]
    current_temp_c: float    # °C (e.g. 20.8) — packed 24-bit at aa01[5:8]
    target_humidity: float   # %RH target setpoint (e.g. 50.0)


# ── Device class ──────────────────────────────────────────────────────────────

class H7151:
    def __init__(self, client: BleakClient):
        self._client = client
        self._session_key: Optional[bytes] = None
        self._notify_queue: asyncio.Queue = asyncio.Queue()

    # ── Context manager ──────────────────────────────────────────────────────

    @classmethod
    async def connect(cls, timeout: float = 30.0) -> "H7151":
        """Scan, connect, and perform BLE key exchange. Returns connected H7151."""
        found = None

        def _cb(dev, adv):
            nonlocal found
            if found:
                return
            if any(t in (dev.name or "") for t in TARGET_NAMES):
                found = dev

        print("Scanning for H7151...")
        async with BleakScanner(_cb):
            for _ in range(int(timeout / 0.5)):
                if found:
                    break
                await asyncio.sleep(0.5)

        if not found:
            raise RuntimeError("H7151 not found — is it powered on and in range?")

        print(f"Found: {found.name} @ {found.address}")
        client = BleakClient(found, timeout=20)
        await client.connect()
        dev = cls(client)
        await dev._subscribe_and_exchange_keys()
        return dev

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.disconnect()

    async def disconnect(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass

    # ── Internal BLE ops ─────────────────────────────────────────────────────

    def _on_notify(self, _char, data: bytearray):
        self._notify_queue.put_nowait(bytes(data))

    async def _wait_notify(self, timeout: float = 6.0) -> bytes:
        return await asyncio.wait_for(self._notify_queue.get(), timeout=timeout)

    async def _write(self, data: bytes):
        await self._client.write_gatt_char(SEND_UUID, data, response=False)

    async def _subscribe_and_exchange_keys(self):
        """Subscribe to notifications and run the two-step key exchange."""
        await self._client.start_notify(RECV_UUID, self._on_notify)
        await asyncio.sleep(0.1)

        # Step 1: request session key
        await self._write(_make_request_session_key())
        deadline = asyncio.get_event_loop().time() + 6.0
        session_key = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                notif = await asyncio.wait_for(self._notify_queue.get(), timeout=1.0)
                session_key = _parse_session_key_response(notif)
                if session_key:
                    break
            except asyncio.TimeoutError:
                pass

        if not session_key:
            raise RuntimeError("Key exchange failed: no session key received")

        self._session_key = session_key
        print(f"Session key: {session_key.hex()}")

        # Step 2: confirm session key
        await self._write(_make_confirm_session_key())
        deadline = asyncio.get_event_loop().time() + 6.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                notif = await asyncio.wait_for(self._notify_queue.get(), timeout=1.0)
                if _parse_confirm_ack(notif):
                    break
            except asyncio.TimeoutError:
                pass

        print("Key exchange complete.")

        # Drain any duplicate notifications left over from the handshake
        await asyncio.sleep(0.3)
        drained = 0
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()
            drained += 1
        if drained:
            print(f"Drained {drained} stale handshake notification(s)")

    async def _send_cmd(self, plain: bytes) -> Optional[bytes]:
        """Encrypt and send a command; return decrypted response notification."""
        assert self._session_key, "Not connected / key exchange not done"
        expected_prefix = plain[0]
        expected_cmd    = plain[1]
        enc = _encrypt_packet(plain, self._session_key)
        await self._write(enc)
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp_enc = await asyncio.wait_for(self._notify_queue.get(),
                                                   timeout=deadline - asyncio.get_event_loop().time())
                resp = _decrypt_packet(resp_enc, self._session_key)
                if resp[0] == expected_prefix and resp[1] == expected_cmd:
                    return resp
                # Skip unexpected packet (spontaneous push or stale notification)
                print(f"  [skip] unexpected notify: {resp[:4].hex(' ')}")
            except asyncio.TimeoutError:
                break
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_state(self) -> H7151State:
        """Query device state: power, fan speed, current & target humidity/temp."""
        # aa 01 — main state: power, mode, 24-bit packed temp/humidity at [5:8]
        # Encoding from TherCalcuUtils.java:
        #   24-bit value = temp_tenths_C * 1000 + hum_tenths_pct
        #   temp_C = floor(value / 1000) / 10.0
        #   hum_pct = (value % 1000) / 10.0
        resp = await self._send_cmd(_make_plain(0xAA, 0x01))
        if resp is None or resp[0] != 0xAA or resp[1] != 0x01:
            raise RuntimeError(f"get_state: no/bad response: {resp}")
        power = bool(resp[2])
        th_raw = (resp[5] << 16) | (resp[6] << 8) | resp[7]
        current_temp_c   = (th_raw // 1000) / 10.0
        current_humidity = (th_raw % 1000) / 10.0

        # aa 05 00 — mode register (0x01=fan mode, 0x03=auto, 0x08=dryer)
        resp_mode = await self._send_cmd(_make_plain(0xAA, 0x05, bytes([0x00])))
        mode_reg = resp_mode[3] if resp_mode and resp_mode[2] == 0x00 else 0x01

        # aa 05 01 — fan speed (1=low, 2=med, 3=high)
        resp_fan = await self._send_cmd(_make_plain(0xAA, 0x05, bytes([0x01])))
        fan_speed = resp_fan[3] if resp_fan and resp_fan[2] == 0x01 else 0

        # aa 05 03 — target humidity
        resp_hum = await self._send_cmd(_make_plain(0xAA, 0x05, bytes([0x03])))
        target_humidity = 0.0
        if resp_hum and resp_hum[2] == 0x03:
            target_humidity = struct.unpack(">H", resp_hum[5:7])[0] / 100.0

        return H7151State(
            power=power,
            mode=_decode_mode(mode_reg, fan_speed),
            fan_speed=fan_speed,
            current_humidity=current_humidity,
            current_temp_c=current_temp_c,
            target_humidity=target_humidity,
        )

    async def set_power(self, on: bool) -> bool:
        """Turn device on (True) or off (False). Returns True on success."""
        resp = await self._send_cmd(_make_plain(0x33, 0x01, bytes([0x01 if on else 0x00])))
        return resp is not None and resp[0] == 0x33 and resp[1] == 0x01

    async def set_target_humidity(self, pct: float, timer_active: bool = False) -> bool:
        """Set target humidity (35–85%). Returns True on success."""
        pct = max(35.0, min(85.0, pct))
        raw = int(round(pct * 100))
        payload = bytes([0x03, 0x00, 0x01 if timer_active else 0x00,
                         (raw >> 8) & 0xFF, raw & 0xFF])
        resp = await self._send_cmd(_make_plain(0x3A, 0x05, payload))
        return resp is not None

    async def set_fan_speed(self, speed: int) -> bool:
        """Set fan speed (1=low, 2=med, 3=high). Returns True on success."""
        speed = max(1, min(3, speed))
        resp = await self._send_cmd(_make_plain(0x3A, 0x05, bytes([0x01, speed])))
        return resp is not None

    async def set_dryer(self) -> bool:
        """Activate Dryer mode. Returns True on success."""
        resp = await self._send_cmd(_make_plain(0x3A, 0x05, bytes([0x08, 0x01])))
        return resp is not None

    async def heartbeat(self):
        """Send a heartbeat (aa 01) and return the raw decrypted response."""
        return await self._send_cmd(_make_plain(0xAA, 0x01))


# ── CLI ───────────────────────────────────────────────────────────────────────

async def _main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"

    async with await H7151.connect() as dev:
        if cmd == "state":
            s = await dev.get_state()
            print(f"Power:            {'ON' if s.power else 'OFF'}")
            print(f"Mode:             {s.mode}")
            print(f"Fan speed:        {s.fan_speed}")
            print(f"Current humidity: {s.current_humidity:.1f}%")
            print(f"Current temp:     {s.current_temp_c:.1f}°C ({s.current_temp_c*9/5+32:.1f}°F)")
            print(f"Target humidity:  {s.target_humidity:.0f}%")

        elif cmd == "on":
            ok = await dev.set_power(True)
            print("Power ON:", "OK" if ok else "FAIL")

        elif cmd == "off":
            ok = await dev.set_power(False)
            print("Power OFF:", "OK" if ok else "FAIL")

        elif cmd == "fan" and len(sys.argv) > 2:
            speed = int(sys.argv[2])
            ok = await dev.set_fan_speed(speed)
            print(f"Fan speed {speed}:", "OK" if ok else "FAIL")

        elif cmd == "dryer":
            ok = await dev.set_dryer()
            print("Dryer mode:", "OK" if ok else "FAIL")

        elif cmd == "humidity" and len(sys.argv) > 2:
            pct = float(sys.argv[2])
            ok = await dev.set_target_humidity(pct)
            print(f"Target humidity {pct}%:", "OK" if ok else "FAIL")

        elif cmd == "raw" and len(sys.argv) > 2:
            # Send arbitrary packet: python h7151.py raw 3a 05 02 01
            data = bytes(int(x, 16) for x in sys.argv[2:])
            prefix, cmd_byte, payload = data[0], data[1], data[2:]
            plain = _make_plain(prefix, cmd_byte, payload)
            print(f"Sending: {plain.hex(' ')}")
            resp = await dev._send_cmd(plain)
            if resp is not None:
                print(f"Response: {resp.hex(' ')}")
            else:
                print("No response")

        elif cmd == "mode_test":
            modes = [
                ("Low (fan 1)",  lambda: dev.set_fan_speed(1)),
                ("Auto (50%)",   lambda: dev.set_target_humidity(50)),
                ("Medium (fan 2)", lambda: dev.set_fan_speed(2)),
                ("Auto (50%)-2", lambda: dev.set_target_humidity(50)),
                ("High (fan 3)", lambda: dev.set_fan_speed(3)),
                ("Auto (50%)-3", lambda: dev.set_target_humidity(50)),
                ("Dryer",        lambda: dev.set_dryer()),
            ]
            subs = [0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b]
            for name, action in modes:
                await action()
                await asyncio.sleep(1.5)
                results = []
                for sub in subs:
                    r = await dev._send_cmd(_make_plain(0xAA, 0x05, bytes([sub])))
                    val = r[3] if r else "??"
                    results.append(f"{sub:02x}={val:#04x}" if r else f"{sub:02x}=none")
                    await asyncio.sleep(0.1)
                print(f"  {name:16}  {' | '.join(results)}")
                await asyncio.sleep(0.3)

        elif cmd == "probe_dryer":
            # Try 3A 05 XX 01 for XX in 02..09 looking for Dryer mode command
            for sub in range(0x02, 0x0a):
                for val in [0x01, 0x02, 0x04, 0x05, 0x06, 0x07]:
                    payload = bytes([sub, val])
                    plain = _make_plain(0x3A, 0x05, payload)
                    resp = await dev._send_cmd(plain)
                    tag = resp.hex(' ') if resp else "(no response)"
                    print(f"  3A 05 {sub:02x} {val:02x}  -> {tag}")
                    await asyncio.sleep(0.3)

        elif cmd == "scan":
            # Probe read commands to find unknown state bytes
            cmds = [
                (0xAA, 0x01, b""),
                (0xAA, 0x02, b""),
                (0xAA, 0x03, b""),
                (0xAA, 0x04, b""),
                (0xAA, 0x05, bytes([0x01])),
                (0xAA, 0x05, bytes([0x02])),
                (0xAA, 0x05, bytes([0x03])),
                (0xAA, 0x05, bytes([0x04])),
                (0xAA, 0x05, bytes([0x05])),
                (0xAA, 0x06, b""),
                (0xAA, 0x07, b""),
                (0xAA, 0x08, b""),
                (0xAA, 0x09, b""),
                (0xAA, 0x10, b""),
                (0xAA, 0x11, b""),
                (0xAA, 0x12, b""),
            ]
            for prefix, cmd_byte, payload in cmds:
                label = f"AA {cmd_byte:02x}" + (f" {payload.hex()}" if payload else "")
                plain = _make_plain(prefix, cmd_byte, payload)
                resp = await dev._send_cmd(plain)
                if resp is not None:
                    print(f"  {label:12}  -> {resp[:12].hex(' ')} ...")
                else:
                    print(f"  {label:12}  -> (no response)")
                await asyncio.sleep(0.2)

        elif cmd == "listen":
            duration = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
            print(f"Listening for all BLE notifications for {duration:.0f}s — change mode on the device now.\n")
            deadline = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < deadline:
                try:
                    remaining = deadline - asyncio.get_event_loop().time()
                    raw_enc = await asyncio.wait_for(dev._notify_queue.get(), timeout=min(1.0, remaining))
                    plain = _decrypt_packet(raw_enc, dev._session_key)
                    print(f"  {plain.hex(' ')}")
                except asyncio.TimeoutError:
                    pass

        elif cmd == "watch":
            interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
            print(f"Watching device state every {interval}s — change settings in the app and observe.\n")
            print(f"{'':4}  00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19  |pw  b3    mode  th_hi th_md th_lo| decoded")
            print("-" * 110)
            prev_raw = None
            while True:
                try:
                    raw = await dev._send_cmd(_make_plain(0xAA, 0x01))
                    if raw is not None:
                        changed_bytes = []
                        if prev_raw is not None:
                            changed_bytes = [i for i in range(20) if raw[i] != prev_raw[i]]
                        hex_bytes = " ".join(
                            f"\033[1;33m{b:02x}\033[0m" if i in changed_bytes else f"{b:02x}"
                            for i, b in enumerate(raw)
                        )
                        marker = " <--" if changed_bytes else "    "
                        power  = raw[2]
                        b3     = raw[3]
                        mode   = raw[4]
                        th_raw = (raw[5] << 16) | (raw[6] << 8) | raw[7]
                        temp_c = (th_raw // 1000) / 10.0
                        hum    = (th_raw % 1000) / 10.0
                        changed_tag = f"  changed=[{','.join(str(i) for i in changed_bytes)}]" if changed_bytes else ""
                        print(f"{marker}  {hex_bytes}  |{power:02x}  {b3:#04x}  {mode:#04x}  {raw[5]:02x}    {raw[6]:02x}    {raw[7]:02x}| {temp_c:.1f}°C {hum:.1f}%{changed_tag}")
                        prev_raw = raw
                except Exception as e:
                    print(f"  [error] {e}")
                await asyncio.sleep(interval)

        else:
            print(f"Usage: {sys.argv[0]} [state|on|off|fan <1-3>|humidity <35-85>|dryer|watch [interval_s]]")


if __name__ == "__main__":
    asyncio.run(_main())
