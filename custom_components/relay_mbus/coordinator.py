"""One sequential, bounded polling session per Relay gateway."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import RelayAuthError, RelayClient, RelayError
from .models import PollResult

_LOGGER = logging.getLogger(__name__)


class RelayCoordinator(DataUpdateCoordinator[PollResult]):
    """Keep meter failures distinct from fresh successful measurements."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: RelayClient,
        meter_ids: list[str],
        interval_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Relay M-Bus",
            update_interval=timedelta(minutes=interval_minutes),
        )
        self.client = client
        self.meter_ids = tuple(meter_ids)
        self._normal_interval = timedelta(minutes=interval_minutes)
        self._failures = 0
        self._closed = False

    async def _async_update_data(self) -> PollResult:
        try:
            result = await self.client.poll(self.meter_ids)
            if not any(
                meter_id in result.meters and meter_id not in result.errors
                for meter_id in self.meter_ids
            ):
                raise UpdateFailed("No selected meters could be read")
        except RelayAuthError as err:
            # Only explicit client authentication errors qualify. In particular,
            # an unexpected control-03 response is not evidence of a bad secret.
            raise ConfigEntryAuthFailed("Gateway authentication failed") from err
        except (RelayError, OSError, TimeoutError, UpdateFailed) as err:
            self._failures += 1
            self.update_interval = self._normal_interval * min(2**min(self._failures, 4), 16)
            raise UpdateFailed("Gateway read failed; retrying with backoff") from err
        self._failures = 0
        self.update_interval = self._normal_interval
        return result

    async def async_shutdown(self) -> None:
        """Cancel active client work and close sockets when unloading."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.client.async_close()
        finally:
            await super().async_shutdown()
