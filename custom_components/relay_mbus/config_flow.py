"""Discover configured inventory, then explicitly opt in to meter reads."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .client import RelayAuthError, RelayClient, RelayError
from .const import (
    CONF_METER_IDS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    endpoint_id,
    endpoint_lock,
    normalize_host,
)


def _meter_ids(value: Any) -> list[str]:
    """Validate explicit ASCII IDs and remove duplicates without losing order."""
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value.strip())
    if not isinstance(value, list) or not value:
        raise vol.Invalid("invalid_meter_ids")
    ids = [item.strip() if isinstance(item, str) else "" for item in value]
    if not all(re.fullmatch(r"[0-9]{8}", item) for item in ids):
        raise vol.Invalid("invalid_meter_ids")
    return list(dict.fromkeys(ids))


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    if CONF_HOST in data:
        host = normalize_host(data[CONF_HOST])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in host.split(".")
            ):
                raise vol.Invalid("invalid_host") from None
        data[CONF_HOST] = host
    if CONF_PASSWORD in data and not re.fullmatch(r"[0-9]{8}", data[CONF_PASSWORD]):
        raise vol.Invalid("invalid_password")
    if CONF_METER_IDS in data:
        data[CONF_METER_IDS] = _meter_ids(data[CONF_METER_IDS])
    if CONF_PORT in data:
        data[CONF_PORT] = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))(data[CONF_PORT])
    if CONF_SCAN_INTERVAL in data:
        data[CONF_SCAN_INTERVAL] = vol.All(
            vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
        )(data[CONF_SCAN_INTERVAL])
    return data


def _schema(
    defaults: dict[str, Any], *, endpoint: bool, inventory: tuple[str, ...] | None = None,
    retained: tuple[str, ...] = (), interval: bool = True,
) -> vol.Schema:
    fields: dict = {}
    if endpoint:
        fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = str
        fields[vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT))] = vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        )
        fields[vol.Required(CONF_PASSWORD)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    if inventory is not None:
        options = [selector.SelectOptionDict(value=mid, label=mid) for mid in inventory]
        options.extend(
            selector.SelectOptionDict(value=mid, label=f"{mid} (unavailable)")
            for mid in retained if mid not in inventory
        )
        fields[vol.Required(CONF_METER_IDS, default=defaults.get(CONF_METER_IDS, []))] = (
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, multiple=True, mode=selector.SelectSelectorMode.LIST,
            ))
        )
    if interval:
        fields[vol.Required(
            CONF_SCAN_INTERVAL,
            default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )] = vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))
    return vol.Schema(fields)


async def _discover_meters(hass: HomeAssistant, data: dict[str, Any]) -> tuple[str, ...]:
    """Fetch configured IDs without requesting measurements; always close."""
    client = RelayClient(
        data[CONF_HOST], data[CONF_PORT], data[CONF_PASSWORD],
        lock=endpoint_lock(hass, data[CONF_HOST], data[CONF_PORT]),
    )
    try:
        return await client.discover_meters()
    finally:
        await client.async_close()


def _selection(value: Any, allowed: tuple[str, ...]) -> list[str]:
    """Do not allow manual IDs or IDs outside this flow's inventory."""
    if not isinstance(value, list):
        raise vol.Invalid("invalid_meter_ids")
    ids = _meter_ids(value)
    if not set(ids).issubset(allowed):
        raise vol.Invalid("invalid_meter_ids")
    return ids


async def _probe_identity(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Validate a passcode without requiring any selected meter to be online."""
    client = RelayClient(
        data[CONF_HOST], data[CONF_PORT], data[CONF_PASSWORD],
        lock=endpoint_lock(hass, data[CONF_HOST], data[CONF_PORT]),
    )
    try:
        return await client.probe_identity()
    finally:
        await client.async_close()


def _entry_snapshot(entry: config_entries.ConfigEntry) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Bind a displayed inventory to the immutable configuration it came from."""
    return entry.data, entry.options


class RelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Manage the single entry owning each normalized endpoint."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._inventory: tuple[str, ...] = ()
        self._retained: tuple[str, ...] = ()
        self._snapshot: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> RelayOptionsFlow:
        return RelayOptionsFlow()

    def _duplicate(self, data: dict[str, Any], exclude: str | None = None) -> bool:
        return any(
            entry.entry_id != exclude and endpoint_id(
                entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT)
            ) == endpoint_id(data[CONF_HOST], data[CONF_PORT])
            for entry in self._async_current_entries()
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                data = _validate({key: user_input[key] for key in (
                    CONF_HOST, CONF_PORT, CONF_PASSWORD, CONF_SCAN_INTERVAL
                )})
                if self._duplicate(data):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(endpoint_id(data[CONF_HOST], data[CONF_PORT]))
                self._abort_if_unique_id_configured()
                inventory = await _discover_meters(self.hass, data)
            except vol.Invalid as err:
                errors["base"] = str(err) if str(err) in {
                    "invalid_host", "invalid_password", "invalid_meter_ids"
                } else "invalid_input"
            except (RelayError, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if inventory:
                    self._pending, self._inventory = data, inventory
                    return await self.async_step_meters()
                errors["base"] = "no_meters"
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}, endpoint=True),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        snapshot = _entry_snapshot(entry)
        errors = {}
        if user_input is not None:
            try:
                data = _validate({
                    CONF_PORT: DEFAULT_PORT, CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    **entry.data, **entry.options, **user_input,
                })
                if self._duplicate(data, entry.entry_id):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(endpoint_id(data[CONF_HOST], data[CONF_PORT]))
                inventory = await _discover_meters(self.hass, data)
            except vol.Invalid:
                errors["base"] = "invalid_input"
            except (RelayError, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if _entry_snapshot(entry) != snapshot:
                    return self.async_abort(reason="configuration_changed")
                if inventory:
                    self._pending, self._inventory = data, inventory
                    self._retained = tuple(data.get(CONF_METER_IDS, ()))
                    self._snapshot = snapshot
                    return await self.async_step_meters()
                errors["base"] = "no_meters"
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(
                {**entry.data, **entry.options, **(user_input or {})}, endpoint=True
            ),
            errors=errors,
        )

    async def async_step_meters(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if not self._pending:
            return self.async_abort(reason="no_meters")
        if user_input is not None:
            try:
                ids = _selection(user_input.get(CONF_METER_IDS), self._inventory + self._retained)
            except vol.Invalid:
                errors["base"] = "invalid_meter_ids"
            else:
                data = {**self._pending, CONF_METER_IDS: ids}
                entry = (
                    self._get_reconfigure_entry()
                    if self.source == config_entries.SOURCE_RECONFIGURE else None
                )
                if self._duplicate(data, entry.entry_id if entry else None):
                    return self.async_abort(reason="already_configured")
                if entry is not None:
                    if _entry_snapshot(entry) != self._snapshot:
                        return self.async_abort(reason="configuration_changed")
                    options = {
                        **entry.options, CONF_METER_IDS: ids,
                        CONF_SCAN_INTERVAL: data[CONF_SCAN_INTERVAL],
                    }
                    return self.async_update_reload_and_abort(
                        entry, data_updates=data, options=options,
                        unique_id=endpoint_id(data[CONF_HOST], data[CONF_PORT]),
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Relay M-Bus", data=data)
        return self.async_show_form(
            step_id="meters", data_schema=_schema(
                {CONF_METER_IDS: list(self._retained)}, endpoint=False,
                inventory=self._inventory, retained=self._retained, interval=False,
            ), errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            try:
                data = _validate({
                    CONF_HOST: entry.data[CONF_HOST],
                    CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                })
                await _probe_identity(self.hass, data)
            except vol.Invalid:
                errors["base"] = "invalid_password"
            except RelayAuthError:
                errors["base"] = "invalid_auth"
            except (RelayError, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: data[CONF_PASSWORD]})
        schema = vol.Schema({vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)


class RelayOptionsFlow(config_entries.OptionsFlowWithReload):
    """Allow changing only explicitly selected IDs and polling frequency."""

    def __init__(self) -> None:
        self._inventory: tuple[str, ...] | None = None
        self._retained: tuple[str, ...] = ()
        self._snapshot: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        defaults = {**self.config_entry.data, **self.config_entry.options}
        snapshot = _entry_snapshot(self.config_entry)
        errors = {}
        if self._snapshot is not None and self._snapshot != snapshot:
            return self.async_abort(reason="configuration_changed")
        if self._inventory is None:
            try:
                inventory = await _discover_meters(self.hass, {
                    CONF_PORT: DEFAULT_PORT, **defaults,
                })
            except (RelayError, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if _entry_snapshot(self.config_entry) != snapshot:
                    return self.async_abort(reason="configuration_changed")
                if inventory:
                    self._inventory = inventory
                    self._retained = tuple(defaults.get(CONF_METER_IDS, ()))
                    self._snapshot = snapshot
                else:
                    errors["base"] = "no_meters"
            if errors:
                return self.async_show_form(step_id="init", data_schema=vol.Schema({}), errors=errors)
            # A retry button submission only refreshes discovery, never saves.
            user_input = None
        if user_input is not None:
            try:
                data = _validate({
                    CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, defaults.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL,
                    )),
                })
                data[CONF_METER_IDS] = _selection(
                    user_input.get(CONF_METER_IDS), self._inventory + self._retained,
                )
            except vol.Invalid:
                errors["base"] = "invalid_input"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, **data},
                )
                return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="init", data_schema=_schema(
                defaults, endpoint=False, inventory=self._inventory, retained=self._retained,
            ), errors=errors
        )
