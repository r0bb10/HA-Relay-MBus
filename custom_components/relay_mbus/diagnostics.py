"""Privacy-preserving diagnostics; never serialize protocol objects wholesale."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL

from . import RelayConfigEntry
from .const import CONF_METER_IDS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RelayConfigEntry
) -> dict:
    """Export only explicitly safe operational metadata.

    Identity strings, exceptions, records, raw frames, config entry titles and
    arbitrary nested config are deliberately excluded rather than heuristically
    redacted. Meter dictionary keys are replaced with local ordinal labels.
    """
    coordinator = entry.runtime_data
    result = coordinator.data
    return {
        "config": {
            key: "**REDACTED**"
            for key in (CONF_HOST, CONF_PORT, CONF_PASSWORD, CONF_METER_IDS, CONF_SCAN_INTERVAL)
            if key in entry.data
        },
        "options": {
            key: "**REDACTED**"
            for key in (CONF_METER_IDS, CONF_SCAN_INTERVAL)
            if key in entry.options
        },
        "selected_meter_count": len(
            entry.options.get(CONF_METER_IDS, entry.data[CONF_METER_IDS])
        ),
        "last_update_success": coordinator.last_update_success,
        "poll": None if result is None else {
            "polled_at": result.polled_at.isoformat(),
            "duration": result.duration,
            "successful_meter_count": len(result.meters.keys() - result.errors.keys()),
            "failed_meter_count": len(result.errors),
            "meters": [
                {
                    "status": meter.status,
                    "record_count": len(meter.records),
                    "more_records": meter.more_records,
                }
                for meter in result.meters.values()
            ],
        },
    }
