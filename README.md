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
| Power and hardware | Power/reset actions, status LEDs, HDMI output control (PCI-E), Pro HDMI capture/passthrough |
| Device settings | SSH, mDNS, HID mode, OLED timeout, swap size, mouse jiggler, watchdog, Pro low power, Pro LCD time format |
| Virtual devices | Virtual network, non-Pro virtual disk switch, Pro virtual mic, Pro virtual disk type |
| Pro LED strip | On/off, brightness, horizontal bead count, vertical bead count |
| Monitoring | Mounted image, CD-ROM mode, Tailscale, Wi-Fi, wired/wireless IP diagnostics, Pro time/static IP state |
| Updates | Application version reporting and install action |
| SSH diagnostics | Uptime, CPU temp, memory/storage usage when SSH is enabled |
| Camera | HDMI still snapshots on non-Pro models and native WebRTC streaming |

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
- NanoKVM Pro still snapshots are skipped to avoid interrupting WebRTC video mode.
- Feature-specific entities only appear when the device reports support for them.

## Entities

| Platform | Highlights |
| --- | --- |
| Binary sensor | Power LED, HDD LED, Wi-Fi/wired connected, CD-ROM mode, Pro static IP enabled, Pro time synchronized |
| Button | Power/Reset buttons, reboot, reset HID/HDMI, Pro sync time |
| Camera | HDMI stream camera with WebRTC and non-Pro still snapshots |
| Number | Pro LED brightness, horizontal beads, vertical beads |
| Select | HID mode, Mouse Jiggler, OLED timeout, Swap size, Pro LCD time format, Pro virtual disk type |
| Sensor | IP address, wired/wireless IP address, mounted image, Tailscale, SSH diagnostics |
| Switch | Power, SSH, mDNS, virtual network/disk, HDMI output, watchdog, Pro HDMI capture/passthrough, Pro low power, Pro LED strip, Pro virtual mic |
| Update | Application version and install action |

Notes:

- HDMI output controls are PCI-E only.
- HDD LED is Alpha-only.
- Wi-Fi entities only appear when the device reports Wi-Fi support.
- SSH diagnostics appear after SSH is enabled on the NanoKVM.
- The watchdog switch requires SSH and NanoKVM application version `2.2.2` or newer.
- Pro LED bead counts must satisfy `horizontal + (2 * vertical) <= 150`.

## Hardware Compatibility

| Feature | Availability |
| --- | --- |
| HDMI switch/button controls | PCI-E models |
| HDD LED binary sensor | Alpha models |
| SSH diagnostic sensors | Any model with SSH enabled |
| Swap size, CD-ROM mode, virtual disk switch | Non-Pro models |
| Virtual network switch | Non-Pro and Pro models |
| HDMI capture/passthrough, low power, LED strip, virtual mic, LCD time format, sync time | Pro models |
| Wired/wireless IP sensors | Created when that connection type is active |

### NanoKVM Pro

NanoKVM Pro devices are supported through the `nanokvm` Python library.
The integration exposes Pro controls for HDMI capture/passthrough, low power,
LED strip configuration, virtual network, virtual microphone, virtual disk
type, LCD time format, sync time, static IP state, and time synchronization
state.

Because the Pro firmware does not expose the same endpoints as non-Pro models,
these non-Pro entities are hidden on Pro:

- Swap size select
- HDMI output switch and Reset HDMI button
- CD-ROM Mode binary sensor
- Virtual Disk switch

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
| `set_led_strip` | `host`, `on`, `brightness`, `horizontal_count`, `vertical_count` | Set NanoKVM Pro LED strip state |
| `scan_wifi` | `host` | Scan Wi-Fi networks and return the Pro response |
| `list_images` | `host` | Return available NanoKVM images |
| `is_image_download_enabled` | `host` | Return whether image downloading is enabled |
| `get_image_download_status` | `host` | Return image download status |
| `list_custom_edids` | `host` | Return custom EDIDs available on NanoKVM Pro |

Notes:

- `push_button.duration` range is `100-5000` ms.
- `set_led_strip.brightness` range is `0-100`; LED beads must satisfy `horizontal + (2 * vertical) <= 150`.
- `host` is optional when one NanoKVM is configured and required when multiple devices are configured.
- Response services return structured data to callers that request a response.

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
| No Pro still image | Pro snapshots are intentionally skipped to avoid interrupting WebRTC video |
| Missing wired/wireless IP sensor | The sensor appears after the device reports an active address for that connection type |

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
