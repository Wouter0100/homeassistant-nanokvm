# NanoKVM Service Examples

All service calls use the `nanokvm` domain.

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
