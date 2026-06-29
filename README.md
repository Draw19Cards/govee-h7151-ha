# Govee H7151 Dehumidifier — Home Assistant Integration

Local Bluetooth control of the GoveeLife H7151 Dehumidifier. No cloud, no Govee account — works entirely over BLE.

## Features

- **All 5 modes**: Low, Medium, High, Auto (target humidity), Dryer
- **Real-time sensors**: current temperature (°C) and relative humidity
- **Target humidity control**: 35–85% in Auto mode
- **Zero cloud dependency**: communicates directly with the device over Bluetooth

## Entities

| Entity | Type | Description |
|---|---|---|
| Govee H7151 Dehumidifier | Humidifier | Main control (power, mode, target humidity) |
| Temperature | Sensor | Current room temperature in °C |
| Humidity | Sensor | Current relative humidity |

## Requirements

- Home Assistant 2024.1 or later
- Bluetooth adapter accessible to your HA instance (HA OS built-in, Supervised with USB dongle, etc.)
- GoveeLife H7151 Dehumidifier powered on and within BLE range

## Installation via HACS

1. In HACS, go to **Integrations** → click the three-dot menu → **Custom repositories**
2. Paste `https://github.com/Draw19Cards/govee-h7151-ha` and select category **Integration**
3. Click **Add**, then search for "Govee H7151" and click **Download**
4. Restart Home Assistant

## Manual Installation

Copy the `custom_components/govee_h7151/` directory into your HA `config/custom_components/` directory, then restart Home Assistant.

## Configuration

The integration discovers the device automatically via Bluetooth. When your H7151 is powered on:

1. A notification appears in **Settings → Integrations**
2. Click **Configure** and confirm the device

To add manually: **Settings → Integrations → Add Integration → Govee H7151 Dehumidifier**, then enter the BLE address (MAC on Linux, UUID on macOS).

## Modes

| Mode | Behavior |
|---|---|
| `low` | Continuous, low fan speed |
| `medium` | Continuous, medium fan speed |
| `high` | Continuous, high fan speed |
| `auto` | Runs until target humidity reached, then pauses |
| `dryer` | High-output mode for drying clothes |

In `auto` mode, set the target humidity (35–85%) using the **Set Humidity** service or the HA humidifier card slider.

## Notes

- HA and the Govee app cannot control the device simultaneously — BLE is single-client. The integration connects on demand and disconnects after each poll.
- The device is polled every 30 seconds. Each connect/exchange is bounded by a timeout and retried a few times, so a transient connection collision (e.g. the Govee app briefly grabbing the link) doesn't wedge the poll loop — it recovers on the next cycle.
- Mode state is queried explicitly each poll via the mode register — it does not rely on tracking commands sent.
- Water-tank status is **not** exposed as an entity: the device only signals it via a one-shot push notification at the moment the tank changes, with no pollable register, so it cannot be reported reliably.

## Protocol

See [WRITEUP.md](WRITEUP.md) for a detailed writeup of the BLE protocol.
