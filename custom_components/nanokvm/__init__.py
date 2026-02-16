"""The Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import datetime # Added for timedelta
import logging
from typing import Any

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from nanokvm.client import (
    NanoKVMApiError,
    NanoKVMAuthenticationFailure,
    NanoKVMClient,
    NanoKVMError,
)
from nanokvm.models import (
    GetCdRomRsp,
    GetMountedImageRsp,
    GpioType,
    MouseJigglerMode,
)

from .config_flow import normalize_host
from .const import (
    ATTR_BUTTON_TYPE,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_MAC,
    ATTR_MODE,
    ATTR_TEXT,
    BUTTON_TYPE_POWER,
    BUTTON_TYPE_RESET,
    CONF_USE_STATIC_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTEGRATION_TITLE,
    SERVICE_PASTE_TEXT,
    SERVICE_PUSH_BUTTON,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_SET_MOUSE_JIGGLER,
    SERVICE_WAKE_ON_LAN,
    SIGNAL_NEW_SSH_SENSORS,
)
from .ssh_metrics import SSHMetricsCollector

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Service schemas
PUSH_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BUTTON_TYPE): vol.In([BUTTON_TYPE_POWER, BUTTON_TYPE_RESET]),
        vol.Optional(ATTR_DURATION, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=100, max=5000)
        ),
    }
)

PASTE_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TEXT): str,
    }
)

WAKE_ON_LAN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MAC): str,
    }
)

SET_MOUSE_JIGGLER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): bool,
        vol.Optional(ATTR_MODE, default="absolute"): vol.In(["absolute", "relative"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sipeed NanoKVM from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    use_static_host = entry.data.get(CONF_USE_STATIC_HOST, False)

    _LOGGER.info(
        "Setting up NanoKVM integration for %s (static_host: %s)",
        host,
        use_static_host
    )

    client = NanoKVMClient(normalize_host(host))

    device_info = None
    try:
        async with client:
            await client.authenticate(username, password)
            device_info = await client.get_info() # Fetch device_info immediately
    except NanoKVMAuthenticationFailure as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except (aiohttp.ClientError, NanoKVMError, asyncio.TimeoutError):
        device_info = type('DeviceInfo', (), {'device_key': f"{host}_{username}", 'mdns': host, 'application': 'Unknown', 'image': 'Unknown'})()

    coordinator = NanoKVMDataUpdateCoordinator(
        hass,
        entry, # Pass the config entry
        client=client,
        username=username,
        password=password,
        device_info=device_info,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def _execute_service(service_name: str, handler) -> None:
        """Execute a service on all configured NanoKVM devices."""
        for coordinator in hass.data[DOMAIN].values():
            client = coordinator.client
            try:
                async with client:
                    await handler(client)
            except Exception as err:
                _LOGGER.error("Error executing %s service for %s: %s", service_name, client.host, err)

    async def handle_push_button(call: ServiceCall) -> None:
        """Handle the push button service."""
        button_type = call.data[ATTR_BUTTON_TYPE]
        duration = call.data[ATTR_DURATION]
        gpio_type = GpioType.POWER if button_type == BUTTON_TYPE_POWER else GpioType.RESET

        async def service_logic(client: NanoKVMClient):
            await client.push_button(gpio_type, duration)
            _LOGGER.debug("Button %s pushed for %d ms on %s", button_type, duration, client.host)

        await _execute_service(SERVICE_PUSH_BUTTON, service_logic)

    async def handle_paste_text(call: ServiceCall) -> None:
        """Handle the paste text service."""
        text = call.data[ATTR_TEXT]

        async def service_logic(client: NanoKVMClient):
            await client.paste_text(text)
            _LOGGER.debug("Text pasted on %s", client.host)

        await _execute_service(SERVICE_PASTE_TEXT, service_logic)

    async def handle_reboot(call: ServiceCall) -> None:
        """Handle the reboot service."""
        async def service_logic(client: NanoKVMClient):
            await client.reboot_system()
            _LOGGER.debug("System reboot initiated on %s", client.host)

        await _execute_service(SERVICE_REBOOT, service_logic)

    async def handle_reset_hdmi(call: ServiceCall) -> None:
        """Handle the reset HDMI service."""
        async def service_logic(client: NanoKVMClient):
            await client.reset_hdmi()
            _LOGGER.debug("HDMI reset initiated on %s", client.host)

        await _execute_service(SERVICE_RESET_HDMI, service_logic)

    async def handle_reset_hid(call: ServiceCall) -> None:
        """Handle the reset HID service."""
        async def service_logic(client: NanoKVMClient):
            await client.reset_hid()
            _LOGGER.debug("HID reset initiated on %s", client.host)

        await _execute_service(SERVICE_RESET_HID, service_logic)

    async def handle_wake_on_lan(call: ServiceCall) -> None:
        """Handle the wake on LAN service."""
        mac = call.data[ATTR_MAC]

        async def service_logic(client: NanoKVMClient):
            await client.send_wake_on_lan(mac)
            _LOGGER.debug("Wake on LAN packet sent to %s via %s", mac, client.host)

        await _execute_service(SERVICE_WAKE_ON_LAN, service_logic)

    async def handle_set_mouse_jiggler(call: ServiceCall) -> None:
        """Handle the set mouse jiggler service."""
        enabled = call.data[ATTR_ENABLED]
        mode_str = call.data[ATTR_MODE]
        mode = MouseJigglerMode.ABSOLUTE if mode_str == "absolute" else MouseJigglerMode.RELATIVE

        async def service_logic(client: NanoKVMClient):
            await client.set_mouse_jiggler_state(enabled, mode)
            _LOGGER.debug("Mouse jiggler on %s set to %s with mode %s", client.host, enabled, mode_str)

        await _execute_service(SERVICE_SET_MOUSE_JIGGLER, service_logic)

    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_BUTTON, handle_push_button, schema=PUSH_BUTTON_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PASTE_TEXT, handle_paste_text, schema=PASTE_TEXT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REBOOT, handle_reboot
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HDMI, handle_reset_hdmi
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HID, handle_reset_hid
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WAKE_ON_LAN, handle_wake_on_lan, schema=WAKE_ON_LAN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MOUSE_JIGGLER, handle_set_mouse_jiggler, schema=SET_MOUSE_JIGGLER_SCHEMA
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator.ssh_metrics_collector:
            await coordinator.ssh_metrics_collector.disconnect()

    return unload_ok


class NanoKVMDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching NanoKVM data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry, # Pass config_entry
        client: NanoKVMClient,
        username: str,
        password: str,
        device_info: Any,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry # Store config_entry
        self.client = client
        self.username = username
        self.password = password
        self.device_info = device_info
        self.hardware_info = None
        self.gpio_info = None
        self.virtual_device_info = None
        self.ssh_state = None
        self.mdns_state = None
        self.hid_mode = None
        self.oled_info = None
        self.wifi_status = None
        self.application_version_info = None
        self.mounted_image = None
        self.cdrom_status = None
        self.mouse_jiggler_state = None
        self.hdmi_state = None
        self.swap_size = None
        self.tailscale_status = None
        self.uptime = None
        self.cpu_temperature = None
        self.memory_total = None
        self.memory_used_percent = None
        self.storage_total = None
        self.storage_used_percent = None
        self.ssh_sensors_created = False
        self.ssh_metrics_collector = None
        self.hostname_info = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from NanoKVM."""
        use_static_host = self.config_entry.data.get(CONF_USE_STATIC_HOST, False)
        current_host = self.config_entry.data[CONF_HOST]
        
        _LOGGER.debug(
            "Fetching data from NanoKVM at %s (static_host: %s)",
            current_host,
            use_static_host
        )
        
        try:
            async with self.client, async_timeout.timeout(10):
                # Re-authenticate if needed
                if not self.client.token:
                    await self.client.authenticate(self.username, self.password)

                # Fetch all the data we need
                self.device_info = await self.client.get_info()
                self.hostname_info = await self.client.get_hostname()
                self.hardware_info = await self.client.get_hardware()
                self.gpio_info = await self.client.get_gpio()
                self.virtual_device_info = await self.client.get_virtual_device_status()
                self.ssh_state = await self.client.get_ssh_state()
                self.mdns_state = await self.client.get_mdns_state()
                self.hid_mode = await self.client.get_hid_mode()
                self.oled_info = await self.client.get_oled_info()
                self.wifi_status = await self.client.get_wifi_status()
                try:
                    self.application_version_info = await self.client.get_application_version()
                except (NanoKVMApiError, aiohttp.ClientResponseError):
                    self.application_version_info = None
                self.hdmi_state = await self.client.get_hdmi_state()
                self.mouse_jiggler_state = await self.client.get_mouse_jiggler_state()
                self.swap_size = await self.client.get_swap_size()
                try:
                    self.tailscale_status = await self.client.get_tailscale_status()
                except (NanoKVMApiError, aiohttp.ClientResponseError):
                    self.tailscale_status = None

                # Only fetch mounted image and CD-ROM status if HID mode is NORMAL
                # When HID mode is HID_ONLY, these features are automatically disabled
                if self.hid_mode.mode == "normal":
                    try:
                        self.mounted_image = await self.client.get_mounted_image()
                    except NanoKVMApiError as err:
                        _LOGGER.debug(
                            "Failed to get mounted image, retrieving default value: %s", err
                        )
                        self.mounted_image = GetMountedImageRsp(file="")

                    try:
                        self.cdrom_status = await self.client.get_cdrom_status()
                    except NanoKVMApiError as err:
                        _LOGGER.debug(
                            "Failed to get CD-ROM status, retrieving default value: %s", err
                        )
                        self.cdrom_status = GetCdRomRsp(cdrom=0)
                else:
                    # Set default values when HID mode is enabled
                    self.mounted_image = GetMountedImageRsp(file="")
                    self.cdrom_status = GetCdRomRsp(cdrom=0)

                # Fetch SSH data if enabled
                if self.ssh_state and self.ssh_state.enabled:
                    await self._async_update_ssh_data()
                else:
                    await self._async_clear_ssh_data()

                return {
                    "device_info": self.device_info,
                    "hardware_info": self.hardware_info,
                    "gpio_info": self.gpio_info,
                    "virtual_device_info": self.virtual_device_info,
                    "ssh_state": self.ssh_state,
                    "mdns_state": self.mdns_state,
                    "hid_mode": self.hid_mode,
                    "oled_info": self.oled_info,
                    "wifi_status": self.wifi_status,
                    "application_version_info": self.application_version_info,
                    "mounted_image": self.mounted_image,
                    "cdrom_status": self.cdrom_status,
                    "mouse_jiggler_state": self.mouse_jiggler_state,
                    "hdmi_state": self.hdmi_state,
                    "swap_size": self.swap_size,
                    "tailscale_status": self.tailscale_status,
                    "hostname_info": self.hostname_info,
                }
        except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as err:
            if ((isinstance(err, NanoKVMAuthenticationFailure) or
                 (isinstance(err, aiohttp.ClientResponseError) and err.status == 401)) and
                hasattr(self.device_info, 'application') and self.device_info.application != 'Unknown'):
                host = normalize_host(self.config_entry.data[CONF_HOST])
                new_client = NanoKVMClient(host)
                try:
                    async with new_client:
                        await new_client.authenticate(self.username, self.password)
                    self.client = new_client
                    return await self._async_update_data()
                except Exception as auth_err:
                    if isinstance(err, aiohttp.ClientResponseError):
                        raise UpdateFailed(f"Reauthentication failed: {auth_err}") from auth_err
                    else:
                        raise UpdateFailed(f"Authentication failed: {auth_err}") from auth_err

            if isinstance(err, aiohttp.ClientResponseError):
                raise UpdateFailed(f"HTTP error with NanoKVM: {err}") from err
            else:
                raise UpdateFailed(f"Authentication failed: {err}") from err

        except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

    async def _async_update_ssh_data(self) -> None:
        """Fetch data via SSH."""
        if not self.ssh_metrics_collector:
            host = self.config_entry.data[CONF_HOST].replace("/api/", "").replace("http://", "").replace("https://", "")
            self.ssh_metrics_collector = SSHMetricsCollector(host=host, password=self.password)

        try:
            metrics = await self.ssh_metrics_collector.collect()
            self.uptime = metrics.uptime
            self.cpu_temperature = metrics.cpu_temperature
            self.memory_total = metrics.memory_total
            self.memory_used_percent = metrics.memory_used_percent
            self.storage_total = metrics.storage_total
            self.storage_used_percent = metrics.storage_used_percent
            _LOGGER.debug(
                "SSH coordinator metrics updated: uptime=%s cpu_temperature=%s memory_used_percent=%s storage_used_percent=%s",
                self.uptime,
                self.cpu_temperature,
                self.memory_used_percent,
                self.storage_used_percent,
            )

            if not self.ssh_sensors_created:
                _LOGGER.debug("SSH enabled, signaling to create SSH sensors")
                async_dispatcher_send(self.hass, SIGNAL_NEW_SSH_SENSORS.format(self.config_entry.entry_id))
                self.ssh_sensors_created = True

        except Exception as err:
            _LOGGER.debug("Failed to fetch data via SSH: %s", err)
            self.uptime = None
            self.cpu_temperature = None
            if self.ssh_metrics_collector:
                await self.ssh_metrics_collector.disconnect()

    async def _async_clear_ssh_data(self) -> None:
        """Clear SSH data and disconnect client."""
        self.uptime = None
        self.cpu_temperature = None
        self.memory_total = None
        self.memory_used_percent = None
        self.storage_total = None
        self.storage_used_percent = None
        self.ssh_sensors_created = False
        if self.ssh_metrics_collector:
            await self.ssh_metrics_collector.disconnect()
            self.ssh_metrics_collector = None


class NanoKVMEntity(CoordinatorEntity):
    """Base class for NanoKVM entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        unique_id_suffix: str,
        name: str | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        if name is not None:
            self._attr_name = name
        self._attr_unique_id = f"{coordinator.device_info.device_key}_{unique_id_suffix}"
        _LOGGER.debug("Created entity %s with unique_id: %s", unique_id_suffix, self._attr_unique_id)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information about this NanoKVM device."""
        sw_version = self.coordinator.device_info.application
        if hasattr(self.coordinator.device_info, "image") and self.coordinator.device_info.image:
            sw_version += f" (Image: {self.coordinator.device_info.image})"

        return {
            "identifiers": {(DOMAIN, self.coordinator.device_info.device_key)},
            "name": self.coordinator.hostname_info.hostname,
            "manufacturer": "Sipeed",
            "model": f"{INTEGRATION_TITLE} {self.coordinator.hardware_info.version.value}",
            "sw_version": sw_version,
            "hw_version": self.coordinator.hardware_info.version.value,
        }
