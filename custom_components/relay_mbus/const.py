"""Constants and shared endpoint ownership for Relay M-Bus."""

from __future__ import annotations

import asyncio
import ipaddress

from homeassistant.core import HomeAssistant, callback

DOMAIN = "relay_mbus"
CONF_METER_IDS = "meter_ids"
DEFAULT_PORT = 1001
DEFAULT_SCAN_INTERVAL = 15
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 1440
PLATFORMS = ["sensor"]


def normalize_host(host: str) -> str:
    """Normalize equivalent endpoint spellings without resolving DNS."""
    host = host.strip().rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host


def endpoint_id(host: str, port: int) -> str:
    """Return a collision-free normalized endpoint identity."""
    return f"[{normalize_host(host)}]:{port}"


@callback
def endpoint_lock(hass: HomeAssistant, host: str, port: int) -> asyncio.Lock:
    """Share locks between config probes and all entry clients.

    Keep locks for the lifetime of HA: removing one while a probe is waiting
    would allow a second independent connection owner for the same endpoint.
    """
    locks = hass.data.setdefault(DOMAIN, {}).setdefault("endpoint_locks", {})
    return locks.setdefault(endpoint_id(host, port), asyncio.Lock())
