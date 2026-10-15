"""Home Assistant independent models for the Relay M-Bus protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class Record:
    """An M-Bus record, retaining its addressing and original bytes."""

    key: str
    quantity: str | None
    value: int | float | None
    unit: str | None
    function: int
    storage: int
    tariff: int
    module: int
    dif: int
    vif: int
    difes: tuple[int, ...] = ()
    vifes: tuple[int, ...] = ()
    raw: bytes = field(default=b"", repr=False)


@dataclass(frozen=True, kw_only=True)
class MeterData:
    """Validated data from one physical meter."""

    meter_id: str
    manufacturer: str
    version: int
    medium: int
    status: int
    access_number: int
    records: tuple[Record, ...]
    more_records: bool
    raw_frame: bytes = field(default=b"", repr=False)


@dataclass(frozen=True, kw_only=True)
class PollResult:
    """One bounded, sequential gateway session."""

    identity: str
    meters: dict[str, MeterData]
    errors: dict[str, str]
    polled_at: datetime
    duration: float
