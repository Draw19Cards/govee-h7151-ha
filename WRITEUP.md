# GoveeLife H7151 BLE Protocol

This document describes the BLE communication protocol used by the GoveeLife H7151 Dehumidifier, reverse-engineered through packet capture, traffic analysis, and systematic empirical testing against a live device.

## BLE Service and Characteristics

**Service UUID**: `00010203-0405-0607-0809-0a0b0c0d1910`

| Role | UUID | Type |
|---|---|---|
| SEND | `00010203-0405-0607-0809-0a0b0c0d2b11` | Write without response |
| RECV | `00010203-0405-0607-0809-0a0b0c0d2b10` | Notify |

The device advertises as `ihoment_H7151_XXXX` or `H7151_XXXX`.

## Packet Format

All packets are exactly **20 bytes**, encrypted before transmission:

```
[0]    prefix
[1]    command byte
[2-18] payload (zero-padded)
[19]   XOR checksum (XOR of bytes 0–18)
```

**Prefixes**:
- `0xAA` — read query (device echoes the same prefix+cmd in its response)
- `0x33` — write command
- `0x3A` — write command with subcommand byte in payload

## Encryption

Every packet is encrypted with a two-layer scheme:

1. **AES/ECB/NoPadding** on each 16-byte block
2. **RC4** on any remaining bytes beyond the last full 16-byte block (only applies if `len(data) % 16 != 0`)

Since 20-byte packets aren't a multiple of 16, each packet is: 16 bytes AES-encrypted + 4 bytes RC4-encrypted.

```python
def safe_encrypt(data: bytes, key: bytes) -> bytes:
    out, i = bytearray(), 0
    while i + 16 <= len(data):
        out.extend(aes_ecb_encrypt(key, data[i:i+16]))
        i += 16
    if i < len(data):
        out.extend(rc4_xor(key, data[i:]))
    return bytes(out)
```

Decryption is the same function with AES decrypt (AES/ECB and RC4 are both symmetric in this context).

### Keys

Two keys are used:

- **Product key** `b"MakingLifeSmarte"` — used only during the key exchange handshake
- **Session key** — 16 bytes negotiated fresh on every BLE connection, used for all data packets

## Key Exchange

A session key must be established on every new connection before sending any data commands. The handshake is two round-trips, all encrypted with the product key.

### TX1 — Request session key

Write to SEND:
```
E7 01 [17 random bytes] [xor_checksum]
```

Device responds on RECV:
```
E7 01 [16-byte session key] [1 byte] [xor_checksum]
```

Bytes `[2:18]` of the decrypted response are the session key.

### TX2 — Confirm

Write to SEND:
```
E7 02 [17 random bytes] [xor_checksum]
```

Device ACKs on RECV:
```
E7 02 ...
```

All subsequent packets use the **session key**. The handshake adds ~300ms to the first command of each connection; we found it reliable within a 6-second timeout.

## State Queries

### `AA 01` — Main device state

**Request**: `AA 01 00 00 00 ... 00 [checksum]`

**Response layout**:

```
[0]  0xAA     prefix
[1]  0x01     cmd
[2]  power    0x01 = on, 0x00 = off
[3]  event    0x00 in normal poll responses; see Push Notifications
[4]  0x81     running status (constant when device is on)
[5]  th_hi    high byte of 24-bit packed temp/humidity value
[6]  th_mid   mid byte
[7]  th_lo    low byte
...
[19] checksum
```

### `AA 05 00` — Mode register

**Request**: `AA 05 00 00 ... [checksum]`

**Response byte `[3]`**:

| Value | Meaning |
|---|---|
| `0x01` | Fan speed mode (Low, Medium, or High) |
| `0x03` | Auto mode |
| `0x08` | Dryer mode |

Byte `[2]` echoes the subcommand (`0x00`).

### `AA 05 01` — Fan speed

**Request**: `AA 05 01 00 ... [checksum]`

**Response byte `[3]`**: `1` = Low, `2` = Medium, `3` = High

Used only to disambiguate Low/Medium/High when `AA 05 00` returns `0x01`. Ignored when mode is Auto or Dryer.

### `AA 05 03` — Target humidity

**Request**: `AA 05 03 00 ... [checksum]`

**Response bytes `[5:7]`**: big-endian uint16, divide by 100 for percentage.

Example: `0x1388` = 5000 → 50.0%

## Temperature / Humidity Encoding

Bytes `[5:8]` of the `AA 01` response encode a **24-bit big-endian packed value** combining temperature and humidity:

```
raw = (byte[5] << 16) | (byte[6] << 8) | byte[7]

temp_C    = floor(raw / 1000) / 10.0
humidity% = (raw % 1000) / 10.0
```

Example: `[0x03, 0x2E, 0xEE]` → 208622 → **20.8 °C / 62.2% RH**

The same encoding appears in the `AA 10` thermometer query (alternate source; not used in this driver).

## Mode Decode Logic

Poll both `AA 05 00` and `AA 05 01`, then combine:

| `AA 05 00` byte[3] | `AA 05 01` byte[3] | Mode |
|---|---|---|
| `0x01` | `1` | Low |
| `0x01` | `2` | Medium |
| `0x01` | `3` | High |
| `0x03` | any | Auto |
| `0x08` | any | Dryer |

## Write Commands

### Power on / off

```
33 01 01   (power on)
33 01 00   (power off)
```

### Fan speed / manual mode (Low / Medium / High)

```
3A 05 01 [speed]
```

`speed`: `01` = Low, `02` = Medium, `03` = High. Sets both the mode register and fan speed.

### Target humidity (enters Auto mode)

```
3A 05 03 00 00 [hi] [lo]
```

`[hi][lo]` is target percentage × 100 as big-endian uint16. Setting any target humidity automatically puts the device into Auto mode.

Example: 50% → `3A 05 03 00 00 13 88`

### Dryer mode

```
3A 05 08 01
```

There is no dedicated "exit dryer" command — send any other mode command to change out of Dryer.

## Push Notifications

The device sends spontaneous (unsolicited) `AA 01` notifications when state changes via the physical control panel. These carry the current device state in bytes `[2]` and `[5:8]`, and are distinguished from poll responses by **byte `[3]`**:

| byte[3] | Event |
|---|---|
| `0x00` | Normal poll response |
| `0x01` | Device powered on |
| `0x05` | Mode or fan speed changed via physical button |
| `0x17` | Water tank removed or full |

These are **transient**: the device emits the event once at the instant the state changes and does not repeat it. The current state (power, temp/humidity, mode) is always available from the poll queries, so the integration does not depend on catching pushes.

Note on tank status: the `0x17` tank event is push-only. Byte-for-byte comparison of every poll response (`AA 01` and `AA 05 00`–`09`) with the tank in error vs. OK shows **no difference** other than the temperature/humidity reading — power stays `0x01`, running status stays `0x81`, and no register reflects the tank. Because the device exposes no pollable tank register and the push fires only on change, tank status cannot be reported reliably over a poll-based connection, so no tank entity is provided.

## Discovery Notes

The mode register (`AA 05 00`) was the key discovery that unlocked reliable mode state reporting. `AA 01` byte `[4]` (the "running status" byte, always `0x81` when on) was initially suspected to be mode, but turned out to be a fixed status indicator. The actual mode register was identified by sending `AA 05 00` while cycling through all five modes via the physical panel and observing the byte `[3]` values change.

The Dryer mode write command (`3A 05 08 01`) was discovered by systematically probing `3A 05 00`–`3A 05 09` with values `00`–`09`, watching for the dryer indicator to activate on the device's display.

The water tank event code (`0x17` in byte `[3]`) was captured by pulling the water tank while a BLE notification listener was active.
