"""Timer-based BLE coordinator for the GoveeLife H7151 Dehumidifier.

This device advertises too infrequently for HA's passive scanner to deliver
reliable advertisement callbacks (verified on the target hardware: the device
is reachable for connection and appears under an active scan, but HA receives
no usable advertisement stream). An advertisement-driven coordinator therefore
never polls. We instead poll on a fixed interval, resolving the BLEDevice from
HA's connectable history, and connect on demand.

Robustness:
- establish_connection() (with retries) participates in habluetooth's slot
  accounting and rides through transient single-connection collisions.
- Every connect+exchange+disconnect is bounded by OP_TIMEOUT so a hung BLE
  call can never wedge the poll loop or hold the lock indefinitely.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MAX_HUMIDITY, MIN_HUMIDITY
from .device import (
    H7151Device,
    H7151State,
    make_dryer,
    make_fan,
    make_humidity,
    make_power,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
# Hard ceiling on a full connect + exchange + disconnect so a hung BLE call can
# never wedge the poll loop or hold the connection lock indefinitely. Kept below
# the poll interval so polls never pile up.
OP_TIMEOUT = 25


class H7151Coordinator(DataUpdateCoordinator[H7151State]):
    """Polls the H7151 over BLE on a fixed interval."""

    def __init__(
        self, hass: HomeAssistant, logger: logging.Logger, address: str, name: str
    ) -> None:
        super().__init__(
            hass,
            logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.address = address
        self.device_name = name
        self._device = H7151Device()
        self._lock = asyncio.Lock()

    def _ble_device(self):
        """Resolve a BLEDevice from HA's history, preferring a connectable one."""
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ) or bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=False
        )

    async def _async_update_data(self) -> H7151State:
        ble_device = self._ble_device()
        if ble_device is None:
            raise UpdateFailed(
                f"{self.address} not found — is it powered on and in range?"
            )
        try:
            async with self._lock:
                async with asyncio.timeout(OP_TIMEOUT):
                    return await self._device.async_poll(ble_device)
        except Exception as err:
            raise UpdateFailed(
                f"BLE error communicating with {self.address}: {err}"
            ) from err

    async def async_disconnect(self) -> None:
        """Release the held BLE connection (called on unload)."""
        await self._device.async_stop()

    async def _async_command(self, plain: bytes) -> None:
        ble_device = self._ble_device()
        if ble_device is None:
            raise HomeAssistantError(f"{self.device_name} is not in range")
        async with self._lock:
            async with asyncio.timeout(OP_TIMEOUT):
                state = await self._device.async_command(ble_device, plain)
        # Publish the post-command state immediately for snappy UI feedback.
        self.async_set_updated_data(state)

    # Public command API used by entities ---------------------------------------

    async def async_set_power(self, on: bool) -> None:
        await self._async_command(make_power(on))

    async def async_set_fan_speed(self, speed: int) -> None:
        await self._async_command(make_fan(speed))

    async def async_set_target_humidity(self, pct: float) -> None:
        pct = max(float(MIN_HUMIDITY), min(float(MAX_HUMIDITY), pct))
        await self._async_command(make_humidity(pct))

    async def async_set_dryer(self) -> None:
        await self._async_command(make_dryer())
