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
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from nanokvm.client import (
    NanoKVMClient,
    NanoKVMError,
    NanoKVMAuthenticationFailure,
)
from nanokvm.models import GpioType, HidMode, GetMountedImageRsp, GetCdRomRsp

from .const import (
    ATTR_BUTTON_TYPE,
    ATTR_DURATION,
    ATTR_MAC,
    ATTR_TEXT,
    BUTTON_TYPE_POWER,
    BUTTON_TYPE_RESET,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_PASTE_TEXT,
    SERVICE_PUSH_BUTTON,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_WAKE_ON_LAN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sipeed NanoKVM from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Ensure the host has a scheme
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    
    # Ensure the host ends with /api/
    if not host.endswith("/api/"):
        host = f"{host}/api/" if host.endswith("/") else f"{host}/api/"

    session = async_get_clientsession(hass)
    client = NanoKVMClient(host, session)

    try:
        await client.authenticate(username, password)
        device_info = await client.get_info() # Fetch device_info immediately
    except NanoKVMAuthenticationFailure as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except (aiohttp.ClientError, NanoKVMError) as err:
        _LOGGER.error("Failed to connect: %s", err)
        raise ConfigEntryNotReady from err

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
    async def handle_push_button(call: ServiceCall) -> None:
        """Handle the push button service."""
        button_type = call.data[ATTR_BUTTON_TYPE]
        duration = call.data[ATTR_DURATION]
        
        gpio_type = GpioType.POWER if button_type == BUTTON_TYPE_POWER else GpioType.RESET
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.push_button(gpio_type, duration)
                _LOGGER.debug("Button %s pushed for %d ms", button_type, duration)
            except Exception as err:
                _LOGGER.error("Failed to push button: %s", err)

    async def handle_paste_text(call: ServiceCall) -> None:
        """Handle the paste text service."""
        text = call.data[ATTR_TEXT]
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.paste_text(text)
                _LOGGER.debug("Text pasted: %s", text)
            except Exception as err:
                _LOGGER.error("Failed to paste text: %s", err)

    async def handle_reboot(call: ServiceCall) -> None:
        """Handle the reboot service."""
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.reboot_system()
                _LOGGER.debug("System reboot initiated")
            except Exception as err:
                _LOGGER.error("Failed to reboot system: %s", err)

    async def handle_reset_hdmi(call: ServiceCall) -> None:
        """Handle the reset HDMI service."""
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.reset_hdmi()
                _LOGGER.debug("HDMI reset initiated")
            except Exception as err:
                _LOGGER.error("Failed to reset HDMI: %s", err)

    async def handle_reset_hid(call: ServiceCall) -> None:
        """Handle the reset HID service."""
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.reset_hid()
                _LOGGER.debug("HID reset initiated")
            except Exception as err:
                _LOGGER.error("Failed to reset HID: %s", err)

    async def handle_wake_on_lan(call: ServiceCall) -> None:
        """Handle the wake on LAN service."""
        mac = call.data[ATTR_MAC]
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            client = coordinator.client
            try:
                await client.send_wake_on_lan(mac)
                _LOGGER.debug("Wake on LAN packet sent to %s", mac)
            except Exception as err:
                _LOGGER.error("Failed to send Wake on LAN: %s", err)

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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

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
        self.mounted_image = None
        self.cdrom_status = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from NanoKVM."""
        try:
            # Re-authenticate if needed
            if not self.client.token:
                await self.client.authenticate(self.username, self.password)

            async with async_timeout.timeout(10):
                # Fetch all the data we need
                self.device_info = await self.client.get_info()
                self.hardware_info = await self.client.get_hardware()
                self.gpio_info = await self.client.get_gpio()
                self.virtual_device_info = await self.client.get_virtual_device_status()
                self.ssh_state = await self.client.get_ssh_state()
                self.mdns_state = await self.client.get_mdns_state()
                self.hid_mode = await self.client.get_hid_mode()
                self.oled_info = await self.client.get_oled_info()
                self.wifi_status = await self.client.get_wifi_status()

                if self.hid_mode.mode == HidMode.HID_ONLY:  # Failsafe to the API throwing errors.
                    self.mounted_image = GetMountedImageRsp(file="")
                    self.cdrom_status = GetCdRomRsp(cdrom=0)
                else:
                    self.mounted_image = await self.client.get_mounted_image()
                    self.cdrom_status = await self.client.get_cdrom_status()

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
                    "mounted_image": self.mounted_image,
                    "cdrom_status": self.cdrom_status,
                }
        except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            # If we get an authentication error, try to re-authenticate
            if isinstance(err, NanoKVMAuthenticationFailure):
                try:
                    await self.client.authenticate(self.username, self.password)
                    # Try the update again
                    return await self._async_update_data()
                except Exception as auth_err:
                    raise UpdateFailed(f"Authentication failed: {auth_err}") from auth_err
            
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err


class NanoKVMEntity(CoordinatorEntity):
    """Base class for NanoKVM entities."""

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        name: str,
        unique_id_suffix: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.device_info.device_key}_{unique_id_suffix}"
        _LOGGER.debug("Setting unique_id for %s: %s", self._attr_name, self._attr_unique_id)
        
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information about this NanoKVM device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.device_info.device_key)},
            "name": f"NanoKVM ({self.coordinator.device_info.mdns}.)",
            "manufacturer": "Sipeed",
            "model": f"NanoKVM {self.coordinator.hardware_info.version.value}",
            "sw_version": self.coordinator.device_info.application,
        }
