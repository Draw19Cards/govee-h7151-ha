"""Sensor entities for Govee H7151 Dehumidifier."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import H7151Coordinator
from .device import H7151State
from .entity import H7151Entity


@dataclass(frozen=True, kw_only=True)
class H7151SensorDescription(SensorEntityDescription):
    value_fn: Callable[[H7151State], float]


SENSORS: tuple[H7151SensorDescription, ...] = (
    H7151SensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        name="Temperature",
        value_fn=lambda data: data.current_temp_c,
    ),
    H7151SensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        name="Humidity",
        value_fn=lambda data: data.current_humidity,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: H7151Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        H7151Sensor(coordinator, entry.entry_id, desc) for desc in SENSORS
    )


class H7151Sensor(H7151Entity, SensorEntity):
    entity_description: H7151SensorDescription

    def __init__(
        self,
        coordinator: H7151Coordinator,
        entry_id: str,
        description: H7151SensorDescription,
    ) -> None:
        super().__init__(coordinator, f"{entry_id}_{description.key}")
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return self.entity_description.value_fn(data) if data else None
