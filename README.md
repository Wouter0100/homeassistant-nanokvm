# Sipeed NanoKVM Integration for Home Assistant

[![HACS][badge-hacs]][link-hacs]
[![GitHub Release][badge-release]][link-release]
[![GitHub Commit Activity][badge-commit-activity]][link-commits]
[![HACS Validation][badge-hacs-validation]][link-hacs-validation]
[![Hassfest][badge-hassfest]][link-hassfest]

Control and monitor a [Sipeed NanoKVM](https://github.com/sipeed/NanoKVM)
from Home Assistant.
This project started as an experiment built with an LLM (Google Gemini)
using Cline.

## What You Get

| Area | Capabilities |
| --- | --- |
| Power and hardware | Power/reset actions, status LEDs, HDMI toggle (PCI-E) |
| Device settings | SSH, mDNS, HID mode, OLED timeout, swap size |
| Virtual devices | Virtual network and virtual disk switches |
| Monitoring | Firmware/app version, mounted image, Tailscale, Wi-Fi |
| SSH diagnostics | Uptime, CPU temp, memory/storage usage when SSH is enabled |
| Camera | HDMI camera stream support |

## Installation

### HACS (recommended)

1. Ensure [HACS](https://hacs.xyz/) is installed.
2. In HACS, add this repository as a custom integration repository:
   - `https://github.com/Wouter0100/homeassistant-nanokvm`
3. Install **Sipeed NanoKVM** from HACS.
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
| Host | IP/hostname of NanoKVM |
| Username / Password | NanoKVM credentials (defaults may be prompted first) |
| Use static host only | Disables mDNS-based host updates after setup |

Notes:

- Zeroconf discovery is supported.
- Unique ID is based on NanoKVM `device_key`.
- HTTPS is currently not supported by this integration.
  Use `http://` (or plain IP/hostname).

## Entities

| Platform | Highlights |
| --- | --- |
| Binary sensor | Power LED, HDD LED, Wi-Fi Connected, Update Available |
| Button | Power/Reset, Reboot, Reset HID/HDMI, Update Application |
| Camera | HDMI stream camera |
| Select | HID mode, Mouse Jiggler, OLED timeout, Swap size |
| Sensor | Firmware, Mounted image, Tailscale, SSH diagnostics |
| Switch | Power, SSH, mDNS, Virtual network/disk, HDMI output |

Notes:

- HDMI controls are PCI-E only.
- HDD LED is Alpha-only.
- SSH diagnostics require SSH enabled on NanoKVM.

## Services

All services are under the `nanokvm` domain.

| Service | Parameters | Description |
| --- | --- | --- |
| `push_button` | `button_type`, `duration` | Simulate a button press |
| `paste_text` | `text` | Paste ASCII text via HID keyboard |
| `reboot` | - | Reboot NanoKVM |
| `reset_hdmi` | - | Reset HDMI subsystem |
| `reset_hid` | - | Reset HID subsystem |
| `wake_on_lan` | `mac` | Send Wake-on-LAN packet |
| `set_mouse_jiggler` | `enabled`, `mode` | Set mouse jiggler state |

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
| Cannot connect | Confirm host/IP, DNS, and network reachability from HA |
| Authentication fails | Verify NanoKVM credentials |
| Missing entities | Reload integration or restart HA |
| No SSH sensors | Enable SSH on NanoKVM |
| No HDMI controls | HDMI controls only appear on PCI-E hardware |

## Supported Languages

- English (`en`)
- French (`fr`)
- Portuguese (Brazil) (`pt-BR`)

## Links

- Documentation: <https://github.com/Wouter0100/homeassistant-nanokvm>
- Issues: <https://github.com/Wouter0100/homeassistant-nanokvm/issues>
- Python library (`python-nanokvm`):
  <https://github.com/puddly/python-nanokvm>
- License: MIT (`LICENSE`)

## Acknowledgements

- [Sipeed](https://sipeed.com/) for creating the NanoKVM device.
- [puddly](https://github.com/puddly) for creating
  [`python-nanokvm`](https://github.com/puddly/python-nanokvm).

[badge-hacs]: https://img.shields.io/badge/HACS-Custom-orange.svg
[badge-release]: https://img.shields.io/github/v/release/Wouter0100/homeassistant-nanokvm?display_name=tag&sort=semver
[badge-commit-activity]: https://img.shields.io/github/commit-activity/m/Wouter0100/homeassistant-nanokvm
[badge-hacs-validation]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hacs.yaml/badge.svg
[badge-hassfest]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hassfest.yaml/badge.svg
[link-hacs]: https://github.com/custom-components/hacs
[link-release]: https://github.com/Wouter0100/homeassistant-nanokvm/releases/latest
[link-commits]: https://github.com/Wouter0100/homeassistant-nanokvm/commits/main
[link-hacs-validation]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hacs.yaml
[link-hassfest]: https://github.com/Wouter0100/homeassistant-nanokvm/actions/workflows/hassfest.yaml
