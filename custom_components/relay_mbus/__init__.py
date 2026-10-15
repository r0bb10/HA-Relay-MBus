"""Read-only Relay M-Bus datalogger integration."""

from __future__ import annotations

from collections.abc import Iterable
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .client import RelayClient
from .const import (
    CONF_METER_IDS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    endpoint_id,
    endpoint_lock,
)
from .coordinator import RelayCoordinator

type RelayConfigEntry = ConfigEntry[RelayCoordinator]

_METER_IDENTITY = re.compile(r"(?:[A-Z]{3}|unknown)_([0-9]{8})")
_ENTITY_IDENTITY = re.compile(
    r"(?:[A-Z]{3}|unknown)_([0-9]{8})_(?:record|diagnostic)_[a-zA-Z0-9_:]+"
)


def _device_meter_id(device: dr.DeviceEntry) -> str | None:
    """Recognize only unambiguous integration meter identities, never a gateway."""
    identities = [value for domain, value in device.identifiers if domain == DOMAIN]
    if not identities:
        return None
    matches = [_METER_IDENTITY.fullmatch(value) for value in identities]
    if any(match is None for match in matches):
        return None
    meter_ids = {match[1] for match in matches if match is not None}
    if len(meter_ids) != 1:
        return None
    meter_id = meter_ids.pop()
    # A conflicting serial is ambiguous; do not guess which identity is current.
    if device.serial_number is not None and device.serial_number != meter_id:
        return None
    return meter_id


@callback
def _async_cleanup_meter_registry(
    hass: HomeAssistant, entry: RelayConfigEntry, meter_ids: Iterable[str]
) -> None:
    """Forget deselected meters, without touching selected or foreign entries.

    Run after the old platforms unload and before any network-dependent refresh.
    Registry enumeration includes disabled snapshots and diagnostics as well as
    currently loaded entities. Unknown/ambiguous identities are left untouched.
    """
    selected = set(meter_ids)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        if entity.device_id is not None:
            device = devices.async_get(entity.device_id)
            meter_id = _device_meter_id(device) if device is not None else None
        else:
            match = _ENTITY_IDENTITY.fullmatch(entity.unique_id)
            meter_id = match[1] if match is not None else None
        if meter_id is not None and meter_id not in selected:
            entities.async_remove(entity.entity_id)

    # In current HA these are entry-owned underlying devices, not composite
    # views of shared identities. Removing ours preserves other entries' devices.
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        meter_id = _device_meter_id(device)
        if (
            meter_id is not None
            and meter_id not in selected
            and not er.async_entries_for_device(
                entities, device.id, include_disabled_entities=True
            )
        ):
            devices.async_remove_device(device.id)


async def async_setup_entry(hass: HomeAssistant, entry: RelayConfigEntry) -> bool:
    """Set up one connection owner and perform HA's retryable first refresh."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and endpoint_id(
            other.data[CONF_HOST], other.data.get(CONF_PORT, DEFAULT_PORT)
        ) == endpoint_id(host, port):
            raise ConfigEntryError("This gateway endpoint already has an entry")

    meter_ids = entry.options.get(CONF_METER_IDS, entry.data[CONF_METER_IDS])
    _async_cleanup_meter_registry(hass, entry, meter_ids)
    client = RelayClient(
        host, port, entry.data[CONF_PASSWORD], lock=endpoint_lock(hass, host, port)
    )
    coordinator = RelayCoordinator(
        hass,
        entry,
        client,
        meter_ids,
        entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        ),
    )
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RelayConfigEntry) -> bool:
    """Release sockets and scheduled polling after entity unload succeeds."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded
