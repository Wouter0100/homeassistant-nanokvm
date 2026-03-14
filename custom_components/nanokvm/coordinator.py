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
from homeassistant.exceptions import ConfigEntryAuthFailed

from nanokvm.client import (
    NanoKVMApiError,
    NanoKVMAuthenticationFailure,
    NanoKVMClient,
    NanoKVMError,
)
from nanokvm.models import GetCdRomRsp, GetInfoRsp, GetMountedImageRsp, HidMode

from .const import (
    CONF_SSL_FINGERPRINT,
    CONF_USE_STATIC_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SIGNAL_NEW_MEDIA_ENTITIES,
    SIGNAL_NEW_SSH_SENSORS,
)
from .ssh_metrics import SSHMetricsCollector
from .utils import api_connection_options, extract_ssh_host

_LOGGER = logging.getLogger(__name__)

_UPDATE_MAX_ATTEMPTS = 3
_UPDATE_RETRY_DELAY_SECONDS = 1
_UPDATE_TIMEOUT_SECONDS = 10


def _is_auth_failure(error: Exception) -> bool:
    """Return whether the exception represents invalid credentials."""
    return isinstance(error, NanoKVMAuthenticationFailure) or (
        isinstance(error, aiohttp.ClientResponseError) and error.status == 401
    )


class NanoKVMDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching NanoKVM data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: NanoKVMClient,
        username: str,
        password: str,
        device_info: GetInfoRsp,
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
        self.media_entities_created = False
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

        for attempt in range(1, _UPDATE_MAX_ATTEMPTS + 1):
            try:
                return await self._async_fetch_once()
            except UpdateFailed as err:
                if attempt == _UPDATE_MAX_ATTEMPTS:
                    _LOGGER.debug(
                        "NanoKVM update attempt %s/%s failed for %s: %s. No retries left.",
                        attempt,
                        _UPDATE_MAX_ATTEMPTS,
                        current_host,
                        err,
                    )
                    raise
                _LOGGER.debug(
                    "NanoKVM update attempt %s/%s failed for %s: %s. Retrying in %ss",
                    attempt,
                    _UPDATE_MAX_ATTEMPTS,
                    current_host,
                    err,
                    _UPDATE_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_UPDATE_RETRY_DELAY_SECONDS)

        raise UpdateFailed("NanoKVM update failed after retry attempts")

    async def _async_fetch_once(self) -> dict[str, Any]:
        """Fetch data once, handling reauthentication when needed."""
        try:
            return await self._async_fetch_with_client()
        except (
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientConnectorCertificateError,
        ) as err:
            if self.client.url.scheme == "http" and await self._async_failover_client(err):
                return await self._async_fetch_with_client()
            raise ConfigEntryAuthFailed(
                "SSL certificate changed for NanoKVM"
            ) from err
        except aiohttp.ClientConnectorError as err:
            if await self._async_failover_client(err):
                return await self._async_fetch_with_client()
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err
        except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as err:
            if _is_auth_failure(err):
                await self._async_reauthenticate_client(err)
                try:
                    return await self._async_fetch_with_client()
                except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as reauth_err:
                    if _is_auth_failure(reauth_err):
                        raise ConfigEntryAuthFailed(
                            "Stored NanoKVM credentials are no longer valid"
                        ) from reauth_err
                    if isinstance(reauth_err, aiohttp.ClientResponseError):
                        raise UpdateFailed(f"HTTP error with NanoKVM: {reauth_err}") from reauth_err
                    raise UpdateFailed(f"Authentication failed: {reauth_err}") from reauth_err

            if isinstance(err, aiohttp.ClientResponseError):
                raise UpdateFailed(f"HTTP error with NanoKVM: {err}") from err
            raise UpdateFailed(f"Authentication failed: {err}") from err

        except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

    async def _async_fetch_with_client(self) -> dict[str, Any]:
        """Fetch data using the current client instance."""
        async with self.client, async_timeout.timeout(_UPDATE_TIMEOUT_SECONDS):
            if not self.client.token:
                await self.client.authenticate(self.username, self.password)

            await self._async_fetch_core_data()
            await self._async_fetch_storage_data()
            self._async_maybe_create_media_entities()
            await self._async_refresh_ssh_data()
            return self._build_update_data()

    async def _async_reauthenticate_client(self, original_error: Exception) -> None:
        """Reauthenticate and replace the client when token/auth fails."""
        options = api_connection_options(
            self.config_entry.data[CONF_HOST],
            self.config_entry.data.get(CONF_SSL_FINGERPRINT),
            preferred_url=str(self.client.url),
        )
        last_error: Exception | None = None

        for index, option in enumerate(options):
            new_client = NanoKVMClient(
                option.base_url,
                ssl_fingerprint=option.ssl_fingerprint,
            )
            try:
                async with new_client:
                    await new_client.authenticate(self.username, self.password)
                self.client = new_client
                return
            except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as auth_err:
                if _is_auth_failure(auth_err):
                    raise ConfigEntryAuthFailed(
                        "Stored NanoKVM credentials are no longer valid"
                    ) from auth_err
                if isinstance(auth_err, aiohttp.ClientResponseError):
                    raise UpdateFailed(f"Reauthentication failed: {auth_err}") from auth_err
                raise UpdateFailed(f"Authentication failed: {auth_err}") from auth_err
            except (
                aiohttp.ServerFingerprintMismatch,
                aiohttp.ClientConnectorCertificateError,
            ) as auth_err:
                if option.scheme == "http" and index < len(options) - 1:
                    last_error = auth_err
                    continue
                raise ConfigEntryAuthFailed(
                    "SSL certificate changed for NanoKVM"
                ) from auth_err
            except aiohttp.ClientConnectorError as auth_err:
                last_error = auth_err
                continue
            except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as auth_err:
                if isinstance(original_error, aiohttp.ClientResponseError):
                    raise UpdateFailed(f"Reauthentication failed: {auth_err}") from auth_err
                raise UpdateFailed(f"Authentication failed: {auth_err}") from auth_err

        if isinstance(original_error, aiohttp.ClientResponseError):
            assert last_error is not None
            raise UpdateFailed(f"Reauthentication failed: {last_error}") from last_error
        if last_error is not None:
            raise UpdateFailed(f"Authentication failed: {last_error}") from last_error

    async def _async_failover_client(self, original_error: Exception) -> bool:
        """Switch to an alternate API transport after a connection failure."""
        options = api_connection_options(
            self.config_entry.data[CONF_HOST],
            self.config_entry.data.get(CONF_SSL_FINGERPRINT),
            preferred_url=str(self.client.url),
        )
        fallback_options = tuple(
            option for option in options if option.base_url != str(self.client.url)
        )

        for option in fallback_options:
            new_client = NanoKVMClient(
                option.base_url,
                ssl_fingerprint=option.ssl_fingerprint,
            )
            try:
                async with new_client:
                    await new_client.authenticate(self.username, self.password)
                _LOGGER.debug(
                    "Switched NanoKVM API transport from %s to %s after connection failure",
                    self.client.url,
                    option.base_url,
                )
                self.client = new_client
                return True
            except NanoKVMAuthenticationFailure as err:
                raise ConfigEntryAuthFailed(
                    "Stored NanoKVM credentials are no longer valid"
                ) from err
            except (
                aiohttp.ServerFingerprintMismatch,
                aiohttp.ClientConnectorCertificateError,
            ) as err:
                raise ConfigEntryAuthFailed(
                    "SSL certificate changed for NanoKVM"
                ) from err
            except aiohttp.ClientConnectorError:
                continue
            except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

        _LOGGER.debug(
            "No alternate NanoKVM API transport succeeded after connection failure from %s: %s",
            self.client.url,
            original_error,
        )
        return False

    async def _async_fetch_core_data(self) -> None:
        """Fetch required API data used by entities."""
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
        self.application_version_info = await self.client.get_application_version()
        self.hdmi_state = await self.client.get_hdmi_state()
        self.mouse_jiggler_state = await self.client.get_mouse_jiggler_state()
        self.swap_size = await self.client.get_swap_size()
        self.tailscale_status = await self.client.get_tailscale_status()

    async def _async_fetch_storage_data(self) -> None:
        """Fetch storage-specific state (mounted image and CD-ROM mode)."""
        if self.hid_mode and self.hid_mode.mode == HidMode.NORMAL:
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

    async def _async_refresh_ssh_data(self) -> None:
        """Fetch or clear SSH metrics depending on SSH state."""
        if self.ssh_state and self.ssh_state.enabled:
            await self._async_update_ssh_data()
        else:
            await self._async_clear_ssh_data()

    def _build_update_data(self) -> dict[str, Any]:
        """Build coordinator data payload for entities."""
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

    def _clear_ssh_metrics(self) -> None:
        """Clear SSH-derived metrics from the coordinator."""
        self.uptime = None
        self.cpu_temperature = None
        self.memory_total = None
        self.memory_used_percent = None
        self.storage_total = None
        self.storage_used_percent = None

    def _async_maybe_create_media_entities(self) -> None:
        """Signal when media-backed entities should be created."""
        if self.media_entities_created:
            return

        if self.mounted_image and self.mounted_image.file != "":
            _LOGGER.debug("Mounted image present, signaling to create media entities")
            async_dispatcher_send(
                self.hass, SIGNAL_NEW_MEDIA_ENTITIES.format(self.config_entry.entry_id)
            )
            self.media_entities_created = True

    async def _async_update_ssh_data(self) -> None:
        """Fetch data via SSH."""
        if not self.ssh_metrics_collector:
            host = extract_ssh_host(self.config_entry.data[CONF_HOST])
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
            self._clear_ssh_metrics()
            if self.ssh_metrics_collector:
                await self.ssh_metrics_collector.disconnect()

    async def _async_clear_ssh_data(self) -> None:
        """Clear SSH data and disconnect client."""
        self._clear_ssh_metrics()
        if self.ssh_metrics_collector:
            await self.ssh_metrics_collector.disconnect()
            self.ssh_metrics_collector = None
