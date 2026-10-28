# Home Assistant Relay M-Bus Integration

This is a local Home Assistant integration for connecting to the Relay M-Bus datalogger
and retrieve thermal-energy meters data.

**Tested:** Relay DL6 firmware 3.3 with Sontex meters.

## Install with HACS

1. In **HACS → menu → Custom repositories**, add
   `https://github.com/r0bb10/HA-Relay-MBus` with category **Integration**.
2. Find **Relay M-Bus**, download a published release, and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Relay M-Bus**.

For manual installation, extract the release ZIP into
`<config>/custom_components/relay_mbus/`, then restart Home Assistant.

## Setup and use

1. Enter the logger's IP/hostname, TCP port (default **1001**), service passcode,
   and polling interval (default **15 minutes**).
2. The integration downloads the logger's configured meter inventory, choose the IDs you
   want to monitor. Each meter becomes a separate device with its supported sensors.

## Sensor meanings

- **Heating energy / cooling energy:** separate cumulative thermal-energy counters;
  the main register is heating and tariff 1 is cooling.
- **Circulated volume:** heating/cooling circuit water, not domestic water usage.
- **Thermal power / volume flow:** instantaneous circuit measurements.
- **Supply / return temperature:** circuit temperatures.
- **Hot-water / cold-water consumption:** cumulative domestic water volumes from
  external pulse inputs IN1 (module 1) and IN2 (module 2), respectively.

Failed or missing readings become unavailable, never fake zero values. Use
Home Assistant's Utility Meter helpers for daily/monthly consumption. Diagnostics
include status, last successful read, duration, and additional-record coverage.

## License

[MIT](LICENSE). Copyright (c) 2026 r0bb10. Release ZIPs include the license notice.
