"""Conservative inventory decoding for the observed DL6 firmware only.

Configuration data can contain credentials: never retain or include it in errors.
"""

SUPPORTED_IDENTITY = "DL6_3.3_FA½M"
CONFIGURATION_SIZE = 2048
MAX_METERS = 60


def parse_inventory(data: bytes, identity: str) -> tuple[str, ...]:
    """Extract forward-BCD IDs from the verified layout, not meter readings."""
    if identity != SUPPORTED_IDENTITY:
        raise ValueError("Unsupported logger inventory firmware")
    if len(data) != CONFIGURATION_SIZE:
        raise ValueError("Invalid configuration size")
    count = data[0]
    if count > MAX_METERS:
        raise ValueError("Invalid configured meter count")
    ids = []
    for slot in range(MAX_METERS):
        record = data[32 + slot * 8:40 + slot * 8]
        if slot >= count:
            if record != b"\xff" * 8:
                raise ValueError("Unrecognized unused inventory slot")
            continue
        packed = record[1:5]
        if any(byte & 15 > 9 or byte >> 4 > 9 for byte in packed):
            raise ValueError("Invalid configured meter BCD")
        meter_id = packed.hex()
        if meter_id == "00000000" or meter_id in ids:
            raise ValueError("Empty or duplicate configured meter ID")
        ids.append(meter_id)
    return tuple(ids)
