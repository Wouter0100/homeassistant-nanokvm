"""Constants for the Sipeed NanoKVM integration."""

DOMAIN = "nanokvm"
INTEGRATION_TITLE = "NanoKVM"

# Configuration
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_USE_STATIC_HOST = "use_static_host"
CONF_SSL_FINGERPRINT = "ssl_fingerprint"

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
SERVICE_SCAN_WIFI = "scan_wifi"
SERVICE_LIST_IMAGES = "list_images"
SERVICE_IMAGE_DOWNLOAD_ENABLED = "is_image_download_enabled"
SERVICE_GET_IMAGE_DOWNLOAD_STATUS = "get_image_download_status"
SERVICE_LIST_CUSTOM_EDIDS = "list_custom_edids"
SERVICE_SET_LED_STRIP = "set_led_strip"

# Service attributes
ATTR_BUTTON_TYPE = "button_type"
ATTR_DURATION = "duration"
ATTR_TEXT = "text"
ATTR_MAC = "mac"
ATTR_ENABLED = "enabled"
ATTR_MODE = "mode"
ATTR_ON = "on"
ATTR_BRIGHTNESS = "brightness"
ATTR_HORIZONTAL_COUNT = "horizontal_count"
ATTR_VERTICAL_COUNT = "vertical_count"

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
ICON_WATCHDOG = "mdi:shield-refresh"
ICON_LED_STRIP = "mdi:led-strip-variant"
ICON_CLOCK = "mdi:clock-outline"

# Signals
SIGNAL_NEW_SSH_SENSORS = "nanokvm_new_ssh_sensors_{}"
SIGNAL_NEW_SSH_SWITCHES = "nanokvm_new_ssh_switches_{}"
SIGNAL_NEW_MEDIA_ENTITIES = "nanokvm_new_media_entities_{}"
SIGNAL_NEW_NETWORK_ENTITIES = "nanokvm_new_network_entities_{}"

# NanoKVM Pro LED strip limits
LED_BRIGHTNESS_MIN = 0
LED_BRIGHTNESS_MAX = 100
LED_BEAD_MIN = 1
LED_BEAD_TOTAL_LIMIT = 150
