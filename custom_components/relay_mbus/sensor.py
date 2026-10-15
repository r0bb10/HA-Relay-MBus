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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RelayCoordinator
from .entity import RelayMeterEntity, meter_identity
from .models import MeterData, Record


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
    SensorEntityDescription(key="status", translation_key="status", entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="last_successful_read", translation_key="last_successful_read", device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="poll_duration", translation_key="poll_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="coverage", translation_key="coverage", device_class=SensorDeviceClass.ENUM, options=["single_telegram", "more_records_pending"], entity_category=EntityCategory.DIAGNOSTIC),
)


def _description(record: Record) -> RelaySensorDescription | None:
    """Reject unknown semantics or mismatched units rather than relabel values."""
    description = DESCRIPTIONS.get(record.quantity)
    if description is None or record.unit != description.native_unit_of_measurement:
        return None
    if record.function not in (0, 1, 2, 3):
        return None
    if record.quantity == "volume" and record.module:
        # Pulse-input plumbing is unknown; do not call these heating-circuit or
        # domestic-water counters merely because they report a volume.
        description = replace(description, translation_key="volume_channel")
    if record.storage != 0 or record.function != 0:
        return replace(description, state_class=None, entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False)
    return description


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
        qualifiers = []
        if record.module:
            qualifiers.append(f"module {record.module}")
        if record.tariff:
            qualifiers.append(f"tariff {record.tariff}")
        if record.storage:
            qualifiers.append(f"storage {record.storage}")
        if record.function:
            qualifiers.append({1: "maximum", 2: "minimum", 3: "error"}[record.function])
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
