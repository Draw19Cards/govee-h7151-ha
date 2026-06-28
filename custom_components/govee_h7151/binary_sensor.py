"""Binary sensor entities for Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import H7151Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: H7151Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([H7151TankSensor(coordinator, entry)])


class H7151TankSensor(CoordinatorEntity[H7151Coordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_has_entity_name = True
    _attr_name = "Water Tank"

    def __init__(self, coordinator: H7151Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tank"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.tank_problem
