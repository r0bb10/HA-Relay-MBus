"""Shared physical-meter identity and availability for Relay entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RelayCoordinator
from .models import MeterData


def meter_identity(meter: MeterData) -> str:
    """Identify a meter independently of gateway address and record ordering."""
    return f"{meter.manufacturer}_{meter.meter_id}"


class RelayMeterEntity(CoordinatorEntity[RelayCoordinator]):
    """An entity belonging to a physical M-Bus meter, not its TCP gateway."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RelayCoordinator, meter: MeterData) -> None:
        """Initialize stable identity without inventing a gateway serial number."""
        super().__init__(coordinator)
        self.meter_id = meter.meter_id
        self._meter_identity = meter_identity(meter)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._meter_identity)},
            name=f"M-Bus {meter.meter_id}",
            manufacturer=meter.manufacturer,
            serial_number=meter.meter_id,
        )

    @property
    def meter(self) -> MeterData | None:
        """Return only this poll's successful, matching physical meter."""
        data = self.coordinator.data
        if data is None or self.meter_id in data.errors:
            return None
        meter = data.meters.get(self.meter_id)
        if meter is None or meter_identity(meter) != self._meter_identity:
            return None
        return meter

    @property
    def available(self) -> bool:
        """A successful gateway poll does not imply every meter succeeded."""
        return super().available and self.meter is not None
