"""Humidifier entity for Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.components.humidifier import (
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MAX_HUMIDITY,
    MIN_HUMIDITY,
    MODE_AUTO,
    MODE_DRYER,
    MODE_HIGH,
    MODE_LOW,
    MODE_MEDIUM,
    MODES,
)
from .coordinator import H7151Coordinator
from .entity import H7151Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: H7151Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([H7151HumidifierEntity(coordinator, entry.entry_id)])


class H7151HumidifierEntity(H7151Entity, HumidifierEntity):
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_supported_features = HumidifierEntityFeature.MODES
    _attr_available_modes = MODES
    _attr_min_humidity = MIN_HUMIDITY
    _attr_max_humidity = MAX_HUMIDITY
    _attr_name = None  # use the device name as the entity name

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return data.power if data else None

    @property
    def mode(self) -> str | None:
        data = self.coordinator.data
        return data.mode if data else None

    @property
    def target_humidity(self) -> int | None:
        data = self.coordinator.data
        return int(data.target_humidity) if data else None

    @property
    def current_humidity(self) -> float | None:
        data = self.coordinator.data
        return data.current_humidity if data else None

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
            data = self.coordinator.data
            target = (data.target_humidity if data else 0) or 50.0
            await self.coordinator.async_set_target_humidity(target)
        elif mode == MODE_DRYER:
            await self.coordinator.async_set_dryer()

    async def async_set_humidity(self, humidity: int) -> None:
        await self.coordinator.async_set_target_humidity(float(humidity))
