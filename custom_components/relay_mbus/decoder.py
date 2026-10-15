"""Conservative, standalone M-Bus variable-data decoder (CI 72 only).

Only standard, unextended measurement VIFs are assigned quantities. Vendor
extensions, dates and identification records remain available as raw records.
No additional telegram is requested when the meter advertises more data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
import re
import struct

from .models import MeterData, Record


def _bcd(data: bytes) -> int:
    value = 0
    for byte in reversed(data):
        if byte >> 4 > 9 or byte & 15 > 9:
            raise ValueError("Invalid packed BCD")
        value = value * 100 + (byte >> 4) * 10 + (byte & 15)
    return value


def _measurement(vif: int) -> tuple[str | None, str | None, float]:
    if 0x00 <= vif <= 0x07:
        return "energy", "kWh", 10 ** ((vif & 7) - 6)
    if 0x08 <= vif <= 0x0F:
        return "energy", "kWh", 10 ** (vif & 7) / 3_600_000
    if 0x10 <= vif <= 0x17:
        return "volume", "m³", 10 ** ((vif & 7) - 6)
    if 0x20 <= vif <= 0x23:
        return "operating_time", "h", (1 / 3600, 1 / 60, 1, 24)[vif & 3]
    if 0x28 <= vif <= 0x2F:
        return "power", "W", 10 ** ((vif & 7) - 3)
    if 0x30 <= vif <= 0x37:
        return "power", "W", 10 ** (vif & 7) / 3600
    if 0x38 <= vif <= 0x3F:
        return "volume_flow_rate", "m³/h", 10 ** ((vif & 7) - 6)
    if 0x40 <= vif <= 0x47:
        return "volume_flow_rate", "m³/h", 60 * 10 ** ((vif & 7) - 7)
    if 0x48 <= vif <= 0x4F:
        return "volume_flow_rate", "m³/h", 3600 * 10 ** ((vif & 7) - 9)
    for start, quantity, unit in (
        (0x58, "temperature_flow", "°C"),
        (0x5C, "temperature_return", "°C"),
        (0x60, "temperature_difference", "K"),
    ):
        if start <= vif <= start + 3:
            return quantity, unit, 10 ** ((vif & 3) - 3)
    return None, None, 1


def decode_telegram(frame: bytes, expected_meter_id: str) -> MeterData:
    """Validate a complete long frame and decode supported standard records.

    Malformed/truncated records fail the telegram, rather than publish partial
    measurements. Explicit manufacturer tails are retained, without interpreting
    their contents as more standard records. Duplicate semantic keys are retained
    as raw-only records so no ambiguous measurement can become an entity.
    """
    if not re.fullmatch(r"[0-9]{8}", expected_meter_id):
        raise ValueError("Expected meter ID must contain eight ASCII digits")
    if (len(frame) < 21 or frame[0] != 0x68 or frame[3] != 0x68
            or frame[1] != frame[2] or len(frame) != frame[1] + 6
            or frame[-1] != 0x16):
        raise ValueError("Invalid M-Bus long-frame envelope")
    if sum(frame[4:-2]) & 255 != frame[-2]:
        raise ValueError("Invalid M-Bus checksum")
    if frame[6] != 0x72:
        raise ValueError("Unsupported M-Bus CI or byte order")
    meter_id = f"{_bcd(frame[7:11]):08d}"
    if meter_id != expected_meter_id:
        raise ValueError("M-Bus meter identity mismatch")
    manufacturer_code = int.from_bytes(frame[11:13], "little")
    letters = [(manufacturer_code >> shift) & 31 for shift in (10, 5, 0)]
    manufacturer = "".join(chr(64 + x) for x in letters) if all(1 <= x <= 26 for x in letters) else "unknown"
    # Only the zero signature was observed. Nonzero signatures are unsupported;
    # do not assume their payload has the same encoding as these fixtures.
    if frame[17:19] != b"\x00\x00":
        raise ValueError("Unsupported M-Bus signature")
    data = frame[19:-2]
    pos = 0
    records: list[Record] = []
    more_records = False

    def take(count: int) -> bytes:
        nonlocal pos
        if pos + count > len(data):
            raise ValueError("Truncated M-Bus record")
        result = data[pos:pos + count]
        pos += count
        return result

    def extensions(first: int) -> tuple[int, ...]:
        result = []
        current = first
        while current & 0x80:
            if len(result) >= 10:
                raise ValueError("Excessive M-Bus extensions")
            current = take(1)[0]
            result.append(current)
        return tuple(result)

    while pos < len(data):
        start = pos
        dif = take(1)[0]
        if dif == 0x2F:  # idle filler
            continue
        if dif in (0x0F, 0x1F):
            more_records = dif == 0x1F
            records.append(Record(key=f"special:{dif:02x}", quantity=None, value=None,
                                  unit=None, function=0, storage=0, tariff=0, module=0,
                                  dif=dif, vif=0x7F, raw=data[start:]))
            break
        if dif & 15 == 15:
            raise ValueError("Unsupported special DIF")
        difes = extensions(dif)
        storage, tariff, module = (dif >> 6) & 1, 0, 0
        for i, dife in enumerate(difes):
            storage |= (dife & 15) << (1 + 4 * i)
            tariff |= ((dife >> 4) & 3) << (2 * i)
            module |= ((dife >> 6) & 1) << i
        vif = take(1)[0]
        # Plain-text unit VIF: retain label bytes as raw, without assigning units.
        custom_unit = b""
        if vif & 0x7F == 0x7C:
            custom_unit = take(take(1)[0])
        vifes = extensions(vif)
        kind = dif & 15
        lengths = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 4, 6: 6, 7: 8,
                   8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 14: 6}
        variable = None
        if kind == 13:
            variable = take(1)[0]
            if variable <= 0xBF:
                length = variable
            elif variable <= 0xEF:
                length = variable & 15
            elif variable <= 0xFA:
                length = variable - 0xF0
            else:
                raise ValueError("Unsupported M-Bus variable length")
        else:
            length = lengths[kind]
        raw = take(length)
        numeric = None
        if kind in (1, 2, 3, 4, 6, 7):
            numeric = int.from_bytes(raw, "little", signed=True)
        elif kind == 5:
            numeric = struct.unpack("<f", raw)[0]
            if not math.isfinite(numeric):
                raise ValueError("Non-finite M-Bus float")
        elif kind in (9, 10, 11, 12, 14):
            numeric = _bcd(raw)
        elif variable is not None:
            if 0xC0 <= variable <= 0xDF:
                numeric = _bcd(raw) * (-1 if variable >= 0xD0 else 1)
            elif 0xE0 <= variable <= 0xEF:
                numeric = int.from_bytes(raw, "little", signed=True)
        quantity, unit, scale = _measurement(vif)
        if vifes or custom_unit or numeric is None:
            quantity = unit = None
        value = numeric * scale if quantity is not None else None
        function = (dif >> 4) & 3
        semantic = f"{vif:02x}" + "".join(f":{x:02x}" for x in vifes)
        if custom_unit:
            semantic += ":" + custom_unit.hex()
        key = f"f{function}:s{storage}:t{tariff}:m{module}:v{semantic}"
        records.append(Record(key=key, quantity=quantity, value=value, unit=unit,
                              function=function, storage=storage, tariff=tariff, module=module,
                              dif=dif, vif=vif, difes=difes, vifes=vifes, raw=raw))
    counts = Counter(record.key for record in records)
    records = [replace(record, quantity=None, value=None, unit=None)
               if counts[record.key] > 1 else record for record in records]
    return MeterData(meter_id=meter_id, manufacturer=manufacturer, version=frame[13],
                     medium=frame[14], access_number=frame[15], status=frame[16],
                     records=tuple(records), more_records=more_records, raw_frame=frame)
