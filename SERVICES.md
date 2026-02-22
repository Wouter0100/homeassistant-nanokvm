# NanoKVM Service Examples

All service calls use the `nanokvm` domain.

## `nanokvm.push_button`

Simulate pressing the physical power or reset button.

Parameters:

- `button_type`: `power` or `reset` (required)
- `duration`: `100-5000` milliseconds (optional, default `100`)

Example:

```yaml
service: nanokvm.push_button
data:
  button_type: power
  duration: 200
```

## `nanokvm.paste_text`

Paste text through HID keyboard emulation.

Parameters:

- `text`: ASCII printable text (required)

Example:

```yaml
service: nanokvm.paste_text
data:
  text: "sudo reboot"
```

## `nanokvm.reboot`

Reboot the NanoKVM device itself.

Parameters:

- none

Example:

```yaml
service: nanokvm.reboot
data: {}
```

## `nanokvm.reset_hdmi`

Reset the HDMI subsystem (primarily relevant for PCIe hardware).

Parameters:

- none

Example:

```yaml
service: nanokvm.reset_hdmi
data: {}
```

## `nanokvm.reset_hid`

Reset the HID subsystem.

Parameters:

- none

Example:

```yaml
service: nanokvm.reset_hid
data: {}
```

## `nanokvm.wake_on_lan`

Send a Wake-on-LAN packet to a target MAC address.

Parameters:

- `mac`: target MAC address (required)

Example:

```yaml
service: nanokvm.wake_on_lan
data:
  mac: "00:11:22:33:44:55"
```

## `nanokvm.set_mouse_jiggler`

Enable/disable mouse jiggler and choose mode.

Parameters:

- `enabled`: `true` or `false` (required)
- `mode`: `absolute` or `relative` (optional, default `absolute`)

Example:

```yaml
service: nanokvm.set_mouse_jiggler
data:
  enabled: true
  mode: absolute
```
