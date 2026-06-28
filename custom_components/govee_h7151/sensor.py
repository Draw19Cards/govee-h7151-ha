"""Sensor entities for Govee H7151 Dehumidifier."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
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
    async_add_entities([
        H7151TemperatureSensor(coordinator, entry),
        H7151HumiditySensor(coordinator, entry),
    ])


class _H7151Sensor(CoordinatorEntity[H7151Coordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: H7151Coordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})


class H7151TemperatureSensor(_H7151Sensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_name = "Temperature"

    def __init__(self, coordinator: H7151Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "temperature")

    @property
    def native_value(self) -> float:
        return self.coordinator.data.current_temp_c


class H7151HumiditySensor(_H7151Sensor):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_name = "Humidity"

    def __init__(self, coordinator: H7151Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "humidity")

    @property
    def native_value(self) -> float:
        return self.coordinator.data.current_humidity
