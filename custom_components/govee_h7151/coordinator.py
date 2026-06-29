"""Advertisement-driven BLE coordinator for the GoveeLife H7151 Dehumidifier.

Built on ActiveBluetoothDataUpdateCoordinator: HA's Bluetooth stack notifies us
of the device's advertisements, and we only connect/poll when the device is
actually present and connectable. This avoids blindly attempting a connection
every N seconds (which, against a single-connection device that drops the link
readily, produced timeouts and stuck "unavailable" states). Availability tracks
advertisements, so the device only goes unavailable when it truly stops being
seen — not on a single failed poll.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import MAX_HUMIDITY, MIN_HUMIDITY
from .device import (
    H7151Device,
    H7151State,
    make_dryer,
    make_fan,
    make_humidity,
    make_power,
)

_LOGGER = logging.getLogger(__name__)

# Minimum spacing between connection-based polls. The device is polled when it
# advertises and at least this long has passed since the last attempt.
POLL_INTERVAL_SECONDS = 30
# Hard ceiling on a full connect + exchange + disconnect so a hung BLE call can
# never wedge the poll loop or hold the connection lock indefinitely.
OP_TIMEOUT = 25


class H7151Coordinator(ActiveBluetoothDataUpdateCoordinator[H7151State]):
    """Polls the H7151 over BLE in response to its advertisements."""

    def __init__(
        self, hass: HomeAssistant, logger: logging.Logger, address: str, name: str
    ) -> None:
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            mode=BluetoothScanningMode.ACTIVE,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll,
            connectable=True,
        )
        self.device_name = name
        self.data: H7151State | None = None
        self._device = H7151Device()
        self._lock = asyncio.Lock()

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        return (
            self.hass.state is CoreState.running
            and (
                seconds_since_last_poll is None
                or seconds_since_last_poll >= POLL_INTERVAL_SECONDS
            )
            and bool(
                async_ble_device_from_address(self.hass, self.address, connectable=True)
            )
        )

    async def _async_poll(self, service_info: BluetoothServiceInfoBleak) -> H7151State:
        ble_device = (
            async_ble_device_from_address(self.hass, self.address, connectable=True)
            or service_info.device
        )
        async with self._lock:
            async with asyncio.timeout(OP_TIMEOUT):
                return await self._device.async_poll(ble_device)

    async def _async_command(self, plain: bytes) -> None:
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(f"{self.device_name} is not in range")
        async with self._lock:
            async with asyncio.timeout(OP_TIMEOUT):
                state = await self._device.async_command(ble_device, plain)
        # Publish the post-command state immediately for snappy UI feedback.
        self.data = state
        self.async_update_listeners()

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
