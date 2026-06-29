"""Shared entity base for the Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import H7151Coordinator


class H7151Entity(PassiveBluetoothCoordinatorEntity[H7151Coordinator]):
    """Base entity: device info + availability tied to the BLE coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: H7151Coordinator, unique_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Govee H7151 Dehumidifier",
            manufacturer="GoveeLife",
            model="H7151",
        )

    @property
    def available(self) -> bool:
        # Available only when the device is being seen AND we have data.
        return super().available and self.coordinator.data is not None
