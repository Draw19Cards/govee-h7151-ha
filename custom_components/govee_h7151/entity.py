"""Shared entity base for the Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import H7151Coordinator


class H7151Entity(CoordinatorEntity[H7151Coordinator]):
    """Base entity: shared device info and availability."""

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
        return super().available and self.coordinator.data is not None
