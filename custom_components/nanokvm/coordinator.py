"""Data update coordinator for the Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from nanokvm.client import (
    NanoKVMApiError,
    NanoKVMAuthenticationFailure,
    NanoKVMClient,
    NanoKVMError,
)
from nanokvm.models import GetCdRomRsp, GetMountedImageRsp

from .config_flow import normalize_host
from .const import CONF_USE_STATIC_HOST, DEFAULT_SCAN_INTERVAL, DOMAIN, SIGNAL_NEW_SSH_SENSORS
from .ssh_metrics import SSHMetricsCollector

_LOGGER = logging.getLogger(__name__)


class NanoKVMDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching NanoKVM data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: NanoKVMClient,
        username: str,
        password: str,
        device_info: Any,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
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
            use_static_host,
        )

        try:
            async with self.client, async_timeout.timeout(10):
                if not self.client.token:
                    await self.client.authenticate(self.username, self.password)

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
                    self.mounted_image = GetMountedImageRsp(file="")
                    self.cdrom_status = GetCdRomRsp(cdrom=0)

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
            if (
                (
                    isinstance(err, NanoKVMAuthenticationFailure)
                    or (
                        isinstance(err, aiohttp.ClientResponseError)
                        and err.status == 401
                    )
                )
                and hasattr(self.device_info, "application")
                and self.device_info.application != "Unknown"
            ):
                host = normalize_host(self.config_entry.data[CONF_HOST])
                new_client = NanoKVMClient(host)
                try:
                    async with new_client:
                        await new_client.authenticate(self.username, self.password)
                    self.client = new_client
                    return await self._async_update_data()
                except Exception as auth_err:
                    if isinstance(err, aiohttp.ClientResponseError):
                        raise UpdateFailed(
                            f"Reauthentication failed: {auth_err}"
                        ) from auth_err
                    raise UpdateFailed(f"Authentication failed: {auth_err}") from auth_err

            if isinstance(err, aiohttp.ClientResponseError):
                raise UpdateFailed(f"HTTP error with NanoKVM: {err}") from err
            raise UpdateFailed(f"Authentication failed: {err}") from err

        except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

    async def _async_update_ssh_data(self) -> None:
        """Fetch data via SSH."""
        if not self.ssh_metrics_collector:
            host = (
                self.config_entry.data[CONF_HOST]
                .replace("/api/", "")
                .replace("http://", "")
                .replace("https://", "")
            )
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
                async_dispatcher_send(
                    self.hass, SIGNAL_NEW_SSH_SENSORS.format(self.config_entry.entry_id)
                )
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
