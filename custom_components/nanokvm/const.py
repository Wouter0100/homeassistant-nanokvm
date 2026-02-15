"""Constants for the Sipeed NanoKVM integration."""

DOMAIN = "nanokvm"
INTEGRATION_TITLE = "NanoKVM"

# Configuration
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_USE_STATIC_HOST = "use_static_host"

# Default values
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_SCAN_INTERVAL = 30

# Services
SERVICE_PUSH_BUTTON = "push_button"
SERVICE_PASTE_TEXT = "paste_text"
SERVICE_REBOOT = "reboot"
SERVICE_RESET_HDMI = "reset_hdmi"
SERVICE_RESET_HID = "reset_hid"
SERVICE_WAKE_ON_LAN = "wake_on_lan"
SERVICE_SET_MOUSE_JIGGLER = "set_mouse_jiggler"

# Service attributes
ATTR_BUTTON_TYPE = "button_type"
ATTR_DURATION = "duration"
ATTR_TEXT = "text"
ATTR_MAC = "mac"
ATTR_ENABLED = "enabled"
ATTR_MODE = "mode"

# Button types
BUTTON_TYPE_POWER = "power"
BUTTON_TYPE_RESET = "reset"

# Entity categories
ENTITY_CATEGORY_CONFIG = "config"
ENTITY_CATEGORY_DIAGNOSTIC = "diagnostic"

# Icons
ICON_KVM = "mdi:keyboard-variant"
ICON_POWER = "mdi:power"
ICON_RESET = "mdi:restart"
ICON_HID = "mdi:keyboard"
ICON_NETWORK = "mdi:ethernet"
ICON_DISK = "mdi:harddisk"
ICON_SSH = "mdi:console"
ICON_MDNS = "mdi:dns"
ICON_OLED = "mdi:monitor-small"
ICON_WIFI = "mdi:wifi"
ICON_IMAGE = "mdi:disc"
ICON_CDROM = "mdi:disc"
ICON_MOUSE_JIGGLER = "mdi:mouse"
ICON_HDMI = "mdi:video-input-hdmi"

# Signals
SIGNAL_NEW_SSH_SENSORS = "nanokvm_new_ssh_sensors_{}"
