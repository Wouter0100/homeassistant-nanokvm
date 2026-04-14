# Sipeed NanoKVM Integration for Home Assistant

[![HACS][badge-hacs]][link-hacs]
[![GitHub Release][badge-release]][link-release]
[![GitHub Commit Activity][badge-commit-activity]][link-commits]
[![HACS Validation][badge-hacs-validation]][link-hacs-validation]
[![Hassfest][badge-hassfest]][link-hassfest]

Control and monitor a [Sipeed NanoKVM](https://github.com/sipeed/NanoKVM)
from Home Assistant.
The integration connects to the NanoKVM API and exposes power controls,
device settings, diagnostics, services, and camera streaming in Home Assistant.

## What You Get

| Area | Capabilities |
| --- | --- |
| Power and hardware | Power/reset actions, status LEDs, HDMI output control (PCI-E) |
| Device settings | SSH, mDNS, HID mode, OLED timeout, swap size, mouse jiggler, watchdog |
| Virtual devices | Virtual network and virtual disk switches |
| Monitoring | Mounted image, CD-ROM mode, Tailscale, Wi-Fi |
| Updates | Application version reporting and install action |
| SSH diagnostics | Uptime, CPU temp, memory/storage usage when SSH is enabled |
| Camera | HDMI still snapshots and native WebRTC streaming |

## Installation

### HACS (recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed.
2. Open this repository directly in your HACS instance:

   [![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wouter0100&repository=homeassistant-nanokvm)

3. Click **Download** in HACS.
4. Restart Home Assistant.

### Manual

1. Download the latest release from
   [releases](https://github.com/Wouter0100/homeassistant-nanokvm/releases).
2. Copy the `nanokvm` folder to
   `<config>/custom_components/nanokvm`.
3. Restart Home Assistant.

## Configuration

Add the integration from **Settings -> Devices & Services -> Add Integration**.

| Option | Description |
| --- | --- |
| Host / API URL | IP, hostname, or full NanoKVM URL |
| Username / Password | NanoKVM credentials (defaults may be prompted first) |
| Use static host only | Disables mDNS-based host updates after setup |

Notes:

- Zeroconf discovery is supported.
- Unique ID is based on NanoKVM `device_key`.
- If no scheme is provided, the integration tries `http` first and then `https`.
- Self-signed HTTPS certificates are supported by confirming the presented fingerprint during setup or reauthentication.
- When **Use static host only** is disabled, zeroconf rediscovery can refresh the stored host/IP.
- Coordinator polling interval is 30 seconds.

## Known Limitations

- Host updates from discovery are disabled when **Use static host only** is enabled.
- Camera stream availability depends on HDMI input/source status.
- Feature-specific entities only appear when the device reports support for them.

## Entities

| Platform | Highlights |
| --- | --- |
| Binary sensor | Power LED, HDD LED, Wi-Fi connected, CD-ROM mode |
| Button | Power/Reset buttons, reboot, reset HID/HDMI |
| Camera | HDMI stream camera with still snapshots and WebRTC |
| Select | HID mode, Mouse Jiggler, OLED timeout, Swap size |
| Sensor | Mounted image, Tailscale, SSH diagnostics |
| Switch | Power, SSH, mDNS, Virtual network/disk, HDMI output, watchdog |
| Update | Application version and install action |

Notes:

- HDMI output controls are PCI-E only.
- HDD LED is Alpha-only.
- Wi-Fi entities only appear when the device reports Wi-Fi support.
- SSH diagnostics appear after SSH is enabled on the NanoKVM.
- The watchdog switch requires SSH and NanoKVM application version `2.2.2` or newer.

## Hardware Compatibility

| Feature | Availability |
| --- | --- |
| HDMI switch/button controls | PCI-E models |
| HDD LED binary sensor | Alpha models |
| SSH diagnostic sensors | Any model with SSH enabled |
| Swap size, CD-ROM mode, virtual network/disk switches | Non-Pro models |

### NanoKVM Pro

NanoKVM Pro devices are supported. Because the Pro firmware no longer
exposes the `/vm/swap`, `/vm/hdmi`, and `/storage/cdrom` endpoints and
uses a different schema on `/vm/device/virtual`, the following entities
are hidden on Pro:

- Swap size select
- HDMI output switch and Reset HDMI button
- CD-ROM Mode binary sensor
- Virtual Network and Virtual Disk switches

All other entities work. See
[`docs/plans/2026-04-14-nanokvm-pro-followups.md`](docs/plans/2026-04-14-nanokvm-pro-followups.md)
for planned Pro-specific additions.

## Services

All services are under the `nanokvm` domain.
For full call examples, see [`SERVICES.md`](SERVICES.md).

| Service | Parameters | Description |
| --- | --- | --- |
| `push_button` | `host`, `button_type`, `duration` | Simulate a button press |
| `paste_text` | `host`, `text` | Paste text via HID keyboard (ASCII printable only) |
| `reboot` | `host` | Reboot NanoKVM |
| `reset_hdmi` | `host` | Reset HDMI subsystem |
| `reset_hid` | `host` | Reset HID subsystem |
| `wake_on_lan` | `host`, `mac` | Send Wake-on-LAN packet |
| `set_mouse_jiggler` | `host`, `enabled`, `mode` | Set mouse jiggler state |

Notes:

- `push_button.duration` range is `100-5000` ms.
- `host` is optional when one NanoKVM is configured and required when multiple devices are configured.

## Example Automation

```yaml
automation:
  - alias: "NanoKVM power button on HA start"
    trigger:
      - platform: homeassistant
        event: start
    action:
      - service: nanokvm.push_button
        data:
          button_type: power
          duration: 100
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Cannot connect | Confirm host/API URL, DNS, network reachability, and expected HTTP/HTTPS scheme |
| Authentication fails | Verify NanoKVM credentials |
| Missing entities | Some entities only appear after the related feature is available (for example SSH enabled or media mounted) |
| No SSH sensors | Enable SSH on NanoKVM |
| No HDMI controls | HDMI controls only appear on PCI-E hardware |

## Supported Languages

- English (`en`)
- French (`fr`)
- Portuguese (Brazil) (`pt-BR`)

## Links

- Documentation: <https://github.com/Wouter0100/homeassistant-nanokvm>
- Issues: <https://github.com/Wouter0100/homeassistant-nanokvm/issues>
- Service examples: [`SERVICES.md`](SERVICES.md)
- Contributing guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Agent/dev notes: [`AGENTS.md`](AGENTS.md)
- Python library (`python-nanokvm`):
  <https://github.com/puddly/python-nanokvm>
- License: MIT (`LICENSE`)

## Acknowledgements

- This project started as an experiment built with an LLM (Google Gemini) using Cline.
- [Sipeed](https://sipeed.com/) for creating the NanoKVM device.
- [puddly](https://github.com/puddly) for creating
  [`python-nanokvm`](https://github.com/puddly/python-nanokvm).

[badge-hacs]: https://img.shields.io/badge/HACS-Default-41BDF5.svg
[badge-release]: https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.github.com%2Frepos%2FWouter0100%2Fhomeassistant-nanokvm%2Freleases%2Flatest&query=%24.tag_name&label=release
[badge-commit-activity]: https://img.shields.io/github/commit-activity/m/Wouter0100/homeassistant-nanokvm
[badge-hacs-validation]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hacs.yaml/badge.svg
[badge-hassfest]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hassfest.yaml/badge.svg
[link-hacs]: https://github.com/custom-components/hacs
[link-release]: https://github.com/Wouter0100/homeassistant-nanokvm/releases/latest
[link-commits]: https://github.com/Wouter0100/homeassistant-nanokvm/commits/main
[link-hacs-validation]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hacs.yaml
[link-hassfest]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hassfest.yaml
