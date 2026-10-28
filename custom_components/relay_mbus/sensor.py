"""Semantically typed measurements from validated M-Bus records.

Current cumulative energy/volume counters provisionally use total_increasing.
Validate reset, rollover, and bidirectional semantics for additional meter models:
net counters must use total instead. Never infer a reset or manufacture a zero.
Stored snapshots and non-current functions deliberately have no statistics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
import math

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .coordinator import RelayCoordinator
from .entity import RelayMeterEntity, meter_identity
from .models import MeterData, Record
from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class RelaySensorDescription(SensorEntityDescription):
    """A supported physical quantity and its canonical decoder unit."""


DESCRIPTIONS = {
    quantity: RelaySensorDescription(
        key=quantity,
        translation_key=quantity,
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=state_class,
        entity_category=EntityCategory.DIAGNOSTIC if quantity == "operating_time" else None,
        entity_registry_enabled_default=quantity != "operating_time",
    )
    for quantity, unit, device_class, state_class in (
        ("energy", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
        ("volume", "m³", SensorDeviceClass.VOLUME, SensorStateClass.TOTAL_INCREASING),
        ("power", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
        ("volume_flow_rate", "m³/h", SensorDeviceClass.VOLUME_FLOW_RATE, SensorStateClass.MEASUREMENT),
        ("temperature_flow", "°C", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
        ("temperature_return", "°C", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
        # Older HA versions cannot distinguish temperature intervals from absolute
        # temperatures. A generic K sensor is safer than applying offset conversion.
        ("temperature_difference", "K", getattr(SensorDeviceClass, "TEMPERATURE_DELTA", None), SensorStateClass.MEASUREMENT),
        ("operating_time", "h", SensorDeviceClass.DURATION, None),
    )
}

DIAGNOSTICS = (
    # Raw header status is not the Sontex detailed-error register. Keep opt-in
    # until a model/firmware-specific mapping can be established without guesses.
    SensorEntityDescription(key="status", translation_key="status", entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False),
    SensorEntityDescription(key="last_successful_read", translation_key="last_successful_read", device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="poll_duration", translation_key="poll_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False),
    SensorEntityDescription(key="coverage", translation_key="coverage", device_class=SensorDeviceClass.ENUM, options=["single_telegram", "more_records_pending"], entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False),
)


def _description(record: Record) -> RelaySensorDescription | None:
    """Reject unknown semantics or mismatched units rather than relabel values."""
    description = DESCRIPTIONS.get(record.quantity)
    if description is None or record.unit != description.native_unit_of_measurement:
        return None
    if record.function not in (0, 1, 2, 3):
        return None
    # Default installation semantics: combined heating/cooling meters, with
    # domestic hot water on IN1 and cold water on IN2. These are not inferred
    # from generic M-Bus metadata; see the installation assumptions in README.
    if record.quantity == "energy" and record.module == 0 and record.tariff in (0, 1):
        description = replace(
            description,
            translation_key="heating_energy" if record.tariff == 0 else "cooling_energy",
        )
    if record.quantity == "volume" and record.module:
        if record.module in (1, 2) and record.tariff == 0:
            description = replace(
                description,
                translation_key="hot_water" if record.module == 1 else "cold_water",
                device_class=SensorDeviceClass.WATER,
            )
        else:
            description = replace(description, translation_key="volume_channel")
    if record.storage != 0 or record.function != 0:
        return replace(description, state_class=None, entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False)
    return description


def _qualifiers(record: Record, translation_key: str) -> list[str]:
    """Keep addressing metadata in names unless the label already explains it."""
    qualifiers = []
    if record.module and translation_key not in ("hot_water", "cold_water"):
        qualifiers.append(f"module {record.module}")
    if record.tariff and translation_key != "cooling_energy":
        qualifiers.append(f"tariff {record.tariff}")
    if record.storage:
        qualifiers.append(f"storage {record.storage}")
    if record.function:
        qualifiers.append({1: "maximum", 2: "minimum", 3: "error"}[record.function])
    return qualifiers


@callback
def _async_align_entity_id(
    hass: HomeAssistant, entry: ConfigEntry, meter: MeterData, record: Record,
    description: RelaySensorDescription,
) -> None:
    """Rename recognized legacy default IDs without replacing registry entries.

    Custom IDs are left alone. Registry APIs handle name collisions; unique IDs,
    disabled state, user names and device associations remain unchanged.
    """
    names = {
        "heating_energy": ("Thermal energy", "Heating energy"),
        "cooling_energy": ("Thermal energy", "Cooling energy"),
        "hot_water": ("Volume", "Hot-water consumption"),
        "cold_water": ("Volume", "Cold-water consumption"),
    }
    if description.translation_key not in names:
        return
    registry = er.async_get(hass)
    unique_id = f"{meter_identity(meter)}_record_{record.key}"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id is None:
        return
    existing = registry.async_get(entity_id)
    if existing is None or existing.config_entry_id != entry.entry_id:
        return
    old_name, new_name = names[description.translation_key]
    old_suffix = slugify(" ".join([old_name, *_qualifiers(record, "legacy")]))
    object_id = entity_id.removeprefix("sensor.")
    # Only the original device-derived defaults, not arbitrary custom IDs.
    prefix = slugify(f"M-Bus {meter.meter_id}") + "_"
    if object_id != prefix + old_suffix:
        return
    new_suffix = slugify(" ".join([new_name, *_qualifiers(record, description.translation_key)]))
    # The available-ID API was renamed after the minimum supported HA version.
    available_id = getattr(registry, "async_get_available_entity_id", None)
    if available_id is None:
        available_id = registry.async_generate_entity_id
    new_id = available_id("sensor", prefix + new_suffix)
    registry.async_update_entity(entity_id, new_entity_id=new_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover records initially and on later successful meter reads."""
    coordinator: RelayCoordinator = entry.runtime_data
    known: set[tuple[str, str, str]] = set()

    @callback
    def discover() -> None:
        if coordinator.data is None:
            return
        entities: list[SensorEntity] = []
        for meter_id, meter in coordinator.data.meters.items():
            if meter_id in coordinator.data.errors:
                continue
            identity = meter_identity(meter)
            for description in DIAGNOSTICS:
                key = (identity, "diagnostic", description.key)
                if key not in known:
                    known.add(key)
                    entities.append(RelayDiagnosticSensor(coordinator, meter, description))
            counts = Counter(record.key for record in meter.records)
            for record in meter.records:
                key = (identity, "record", record.key)
                description = _description(record)
                if counts[record.key] == 1 and description is not None and key not in known:
                    known.add(key)
                    _async_align_entity_id(hass, entry, meter, record, description)
                    entities.append(RelayRecordSensor(coordinator, meter, record, description))
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class RelayRecordSensor(RelayMeterEntity, SensorEntity):
    """A single stable function/storage/tariff/module/VIF record."""

    entity_description: RelaySensorDescription

    def __init__(self, coordinator: RelayCoordinator, meter: MeterData, record: Record, description: RelaySensorDescription) -> None:
        super().__init__(coordinator, meter)
        self.entity_description = description
        self._record_key = record.key
        self._quantity = record.quantity
        self._unit = record.unit
        self._address = (record.function, record.storage, record.tariff, record.module)
        self._attr_unique_id = f"{self._meter_identity}_record_{record.key}"
        qualifiers = _qualifiers(record, description.translation_key)
        self._attr_translation_placeholders = {"qualifier": f" ({', '.join(qualifiers)})" if qualifiers else ""}
        self._attr_extra_state_attributes = {
            "function": record.function,
            "storage": record.storage,
            "tariff": record.tariff,
            "module": record.module,
        }

    @property
    def record(self) -> Record | None:
        """Ambiguous duplicates and disappearing records are not fresh values."""
        if (meter := self.meter) is None:
            return None
        matches = [record for record in meter.records if record.key == self._record_key]
        if len(matches) != 1:
            return None
        record = matches[0]
        if record.quantity != self._quantity or record.unit != self._unit:
            return None
        if (record.function, record.storage, record.tariff, record.module) != self._address:
            return None
        value = record.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return record

    @property
    def available(self) -> bool:
        return super().available and self.record is not None

    @property
    def native_value(self) -> int | float | None:
        return record.value if (record := self.record) is not None else None


class RelayDiagnosticSensor(RelayMeterEntity, SensorEntity):
    """Meter status, observed coverage and gateway-session timing."""

    def __init__(self, coordinator: RelayCoordinator, meter: MeterData, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, meter)
        self.entity_description = description
        self._attr_unique_id = f"{self._meter_identity}_diagnostic_{description.key}"
        self._last_success: datetime | None = coordinator.data.polled_at

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.meter is not None and self.coordinator.last_update_success:
            self._last_success = self.coordinator.data.polled_at
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        if self.entity_description.key in ("last_successful_read", "poll_duration"):
            return self.coordinator.last_update_success and self.coordinator.data is not None
        return super().available

    @property
    def native_value(self) -> int | float | str | datetime | None:
        key = self.entity_description.key
        if key == "last_successful_read":
            if self.meter is not None and self.coordinator.last_update_success:
                self._last_success = self.coordinator.data.polled_at
            return self._last_success
        if key == "poll_duration":
            return self.coordinator.data.duration if self.coordinator.data is not None else None
        if (meter := self.meter) is None:
            return None
        if key == "status":
            return meter.status
        # A single reply is not a claim that all possible telegrams were read.
        return "more_records_pending" if meter.more_records else "single_telegram"
