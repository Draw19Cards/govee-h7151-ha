"""Humidifier entity for Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_HUMIDITY, MIN_HUMIDITY, MODES, MODE_AUTO, MODE_DRYER, MODE_HIGH, MODE_LOW, MODE_MEDIUM
from .coordinator import H7151Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: H7151Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([H7151HumidifierEntity(coordinator, entry)])


class H7151HumidifierEntity(CoordinatorEntity[H7151Coordinator], HumidifierEntity):
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_supported_features = HumidifierEntityFeature.MODES
    _attr_available_modes = MODES
    _attr_min_humidity = MIN_HUMIDITY
    _attr_max_humidity = MAX_HUMIDITY
    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name

    def __init__(self, coordinator: H7151Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Govee H7151 Dehumidifier",
            manufacturer="GoveeLife",
            model="H7151",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.power

    @property
    def mode(self) -> str:
        return self.coordinator.data.mode

    @property
    def target_humidity(self) -> int:
        return int(self.coordinator.data.target_humidity)

    @property
    def current_humidity(self) -> float:
        return self.coordinator.data.current_humidity

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_power(False)

    async def async_set_mode(self, mode: str) -> None:
        if mode == MODE_LOW:
            await self.coordinator.async_set_fan_speed(1)
        elif mode == MODE_MEDIUM:
            await self.coordinator.async_set_fan_speed(2)
        elif mode == MODE_HIGH:
            await self.coordinator.async_set_fan_speed(3)
        elif mode == MODE_AUTO:
            target = self.coordinator.data.target_humidity or 50.0
            await self.coordinator.async_set_target_humidity(target)
        elif mode == MODE_DRYER:
            await self.coordinator.async_set_dryer()

    async def async_set_humidity(self, humidity: int) -> None:
        await self.coordinator.async_set_target_humidity(float(humidity))
