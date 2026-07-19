# NanoKVM Service Examples

All service calls use the `nanokvm` domain.
The `host` field is optional when one NanoKVM is configured and required when
multiple NanoKVM devices are configured.

Response services return structured data. In automations or scripts, use
`response_variable` when you need to consume the returned data.

## `nanokvm.push_button`

Simulate pressing the physical power or reset button.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured
- `button_type`: `power` or `reset` (required)
- `duration`: `100-5000` milliseconds (optional, default `100`)

Example:

```yaml
service: nanokvm.push_button
data:
  host: "192.168.1.50"
  button_type: power
  duration: 200
```

## `nanokvm.paste_text`

Paste text through HID keyboard emulation.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured
- `text`: ASCII printable text (required)

Example:

```yaml
service: nanokvm.paste_text
data:
  host: "192.168.1.50"
  text: "sudo reboot"
```

## `nanokvm.reboot`

Reboot the NanoKVM device itself.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.reboot
data:
  host: "192.168.1.50"
```

## `nanokvm.reset_hdmi`

Reset the HDMI subsystem (primarily relevant for PCIe hardware).

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.reset_hdmi
data:
  host: "192.168.1.50"
```

## `nanokvm.reset_hid`

Reset the HID subsystem.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.reset_hid
data:
  host: "192.168.1.50"
```

## `nanokvm.wake_on_lan`

Send a Wake-on-LAN packet to a target MAC address.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured
- `mac`: target MAC address (required)

Example:

```yaml
service: nanokvm.wake_on_lan
data:
  host: "192.168.1.50"
  mac: "00:11:22:33:44:55"
```

## `nanokvm.set_mouse_jiggler`

Enable/disable mouse jiggler and choose mode.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured
- `enabled`: `true` or `false` (required)
- `mode`: `absolute` or `relative` (optional, default `absolute`)

Example:

```yaml
service: nanokvm.set_mouse_jiggler
data:
  host: "192.168.1.50"
  enabled: true
  mode: absolute
```

## `nanokvm.set_led_strip`

Configure NanoKVM Pro LED strip state.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured
- `on`: `true` or `false` (optional)
- `brightness`: brightness percentage from `0` to `100` (optional)
- `horizontal_count`: horizontal LED bead count, minimum `1` (optional)
- `vertical_count`: vertical LED bead count, minimum `1` (optional)

At least one of `on`, `brightness`, `horizontal_count`, or `vertical_count`
is required. Omitted values are preserved from the current LED strip state.
The bead counts must satisfy:

```text
horizontal_count + (2 * vertical_count) <= 150
```

Example:

```yaml
service: nanokvm.set_led_strip
data:
  host: "192.168.1.50"
  on: true
  brightness: 75
  horizontal_count: 10
  vertical_count: 70
```

## `nanokvm.scan_wifi`

Scan nearby Wi-Fi networks and return the NanoKVM Pro response.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.scan_wifi
data:
  host: "192.168.1.50"
response_variable: wifi_scan
```

## `nanokvm.list_images`

List images available on the NanoKVM.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.list_images
data:
  host: "192.168.1.50"
response_variable: images
```

## `nanokvm.is_image_download_enabled`

Return whether image downloading is enabled on the NanoKVM.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.is_image_download_enabled
data:
  host: "192.168.1.50"
response_variable: image_download_enabled
```

## `nanokvm.get_image_download_status`

Return the current NanoKVM image download status.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.get_image_download_status
data:
  host: "192.168.1.50"
response_variable: image_download_status
```

## `nanokvm.list_custom_edids`

List custom EDIDs available on a NanoKVM Pro.

Parameters:

- `host`: optional target host; required when multiple NanoKVM devices are configured

Example:

```yaml
service: nanokvm.list_custom_edids
data:
  host: "192.168.1.50"
response_variable: custom_edids
```

## `nanokvm.start_hdmi_recording`

Record the selected NanoKVM HDMI stream to an MP4 file on Home Assistant.
The call returns after the first video frame is being written. Only one recording
can run per NanoKVM device. The **HDMI Recording** switch uses the same recorder,
creates `/media/nanokvm/<device-key>/YYYYMMDDHHMMSS.mp4`, records video only,
and stops automatically after 30 minutes.

Parameters:

- target: exactly one NanoKVM camera entity (required)
- `filename`: `.mp4` path inside a Home Assistant allowed directory (required)
- `duration`: maximum duration from `1` to `7200` seconds (optional, default `3600`)
- `include_audio`: include HDMI audio (optional, default `false`; only NanoKVM Pro supports audio and it uses the higher-overhead WebRTC recorder)

Example:

```yaml
service: nanokvm.start_hdmi_recording
target:
  entity_id: camera.nanokvm_hdmi_stream
data:
  filename: "/config/www/nanokvm-capture.mp4"
  duration: 60
  include_audio: false
```

The recording is written to an extension-preserving temporary file and then
atomically moved to `filename`. A runtime failure after capture begins preserves
a non-empty partial MP4. Non-Pro video-only recording uses NanoKVM's direct
timestamped H.264 stream and avoids re-encoding. NanoKVM Pro uses the WebRTC
recorder for video-only and audio recordings so existing WebRTC viewers remain
active; this can noticeably increase Home Assistant CPU and memory usage.

NanoKVM Pro recording works only when the official WebUI video mode is set to
**H.264 WebRTC**. Concurrent WebRTC viewers are supported, but changing the
official WebUI to MJPEG, H.264 Direct, or H.265 mode during a recording stops
the WebRTC pipeline and finalizes the captured portion.

## `nanokvm.stop_hdmi_recording`

Stop and finalize the active recording early. Calling this service when the
camera is not recording is safe.

```yaml
service: nanokvm.stop_hdmi_recording
target:
  entity_id: camera.nanokvm_hdmi_stream
```
