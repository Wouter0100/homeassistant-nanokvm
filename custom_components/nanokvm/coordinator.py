"""Data update coordinator for the Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
import datetime
import logging
from time import monotonic
from typing import Any, TypeVar

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from nanokvm.client import (
    NanoKVMApiError,
    NanoKVMAuthenticationFailure,
    NanoKVMClient,
    NanoKVMError,
    NanoKVMNotSupportedError,
)
from nanokvm.ssh_client import NanoKVMSSHConnectionError
from nanokvm.models import (
    GetCdRomRsp,
    GetGpioRsp,
    GetHardwareRsp,
    GetHdmiCaptureRsp,
    GetHdmiPassthroughRsp,
    GetHdmiStateRsp,
    GetHostnameRsp,
    GetHidModeRsp,
    GetInfoRsp,
    GetLedStripRsp,
    GetLcdTimeFormatRsp,
    GetLowPowerRsp,
    GetMdnsStateRsp,
    GetMountedImageRsp,
    GetMouseJigglerRsp,
    GetOLEDRsp,
    GetSSHStateRsp,
    GetStaticIPRsp,
    GetTimeStatusRsp,
    GetTailscaleStatusRsp,
    GetVersionRsp,
    GetVirtualDeviceRsp,
    GetWifiRsp,
    HidMode,
    HWVersion,
)

from .const import (
    CONF_PREFERRED_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSL_FINGERPRINT,
    CONF_USE_STATIC_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTEGRATION_TITLE,
    PREFERRED_HOST_CHECK_INTERVAL_SECONDS,
    PREFERRED_HOST_MAX_CHECK_INTERVAL_SECONDS,
    PREFERRED_HOST_TIMEOUT_SECONDS,
    SIGNAL_NEW_MEDIA_ENTITIES,
    SIGNAL_NEW_NETWORK_ENTITIES,
    SIGNAL_NEW_SSH_SENSORS,
    SIGNAL_NEW_SSH_SWITCHES,
)
from .led import LedBrightnessRequest, note_reported_led_strip
from .ssh_metrics import SSHMetricsCollector
from .ssh_host_keys import retarget_known_hosts_line
from .utils import (
    api_connection_options,
    device_sw_version,
    extract_ssh_host,
    verification_url,
)

_LOGGER = logging.getLogger(__name__)

_UPDATE_MAX_ATTEMPTS = 3
_UPDATE_RETRY_DELAY_SECONDS = 1
_UPDATE_TIMEOUT_SECONDS = 10
_REQUEST_REFRESH_COOLDOWN_SECONDS = 1.5
_SSH_METRICS_TIMEOUT_SECONDS = 15
_APP_VERSION_REQUEST_TIMEOUT_SECONDS = 45
_APP_VERSION_CACHE_SECONDS = 300
_APP_VERSION_FAILURE_CACHE_SECONDS = 60
_WATCHDOG_MIN_VERSION = AwesomeVersion("2.2.2")
_MAX_CONCURRENT_REQUESTS = 4
_GATED_CORE_ATTRIBUTES = (
    "virtual_device_info",
    "hdmi_state",
    "swap_size",
    "hdmi_capture",
    "hdmi_passthrough",
    "low_power",
    "led_strip",
    "lcd_time_format",
    "time_status",
    "static_ip",
)
_INVALID_FILE_CONTENT_CODE = -2
_INVALID_FILE_CONTENT_MESSAGE = "invalid file content"
_ResponseT = TypeVar("_ResponseT")


def _is_auth_failure(error: Exception) -> bool:
    """Return whether the exception represents invalid credentials."""
    return isinstance(error, NanoKVMAuthenticationFailure) or (
        isinstance(error, aiohttp.ClientResponseError) and error.status == 401
    )


def _is_invalid_file_content_error(error: NanoKVMApiError) -> bool:
    """Return whether the API error is NanoKVM's optional-file missing response."""
    return (
        error.code == _INVALID_FILE_CONTENT_CODE
        and error.msg == _INVALID_FILE_CONTENT_MESSAGE
    )


def _format_timeout_error(action: str) -> str:
    """Return a stable timeout message without relying on an empty exception."""
    return f"Timed out {action} after {_UPDATE_TIMEOUT_SECONDS} seconds"


class NanoKVMDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching NanoKVM data."""

    config_entry: ConfigEntry
    device_info: GetInfoRsp
    hostname_info: GetHostnameRsp | None
    hardware_info: GetHardwareRsp | None
    gpio_info: GetGpioRsp | None
    virtual_device_info: GetVirtualDeviceRsp | None
    ssh_state: GetSSHStateRsp | None
    mdns_state: GetMdnsStateRsp | None
    hid_mode: GetHidModeRsp | None
    oled_info: GetOLEDRsp | None
    wifi_status: GetWifiRsp | None
    application_version_info: GetVersionRsp | None
    mounted_image: GetMountedImageRsp | None
    cdrom_status: GetCdRomRsp | None
    mouse_jiggler_state: GetMouseJigglerRsp | None
    hdmi_state: GetHdmiStateRsp | None
    hdmi_capture: GetHdmiCaptureRsp | None
    hdmi_passthrough: GetHdmiPassthroughRsp | None
    low_power: GetLowPowerRsp | None
    led_strip: GetLedStripRsp | None
    lcd_time_format: GetLcdTimeFormatRsp | None
    time_status: GetTimeStatusRsp | None
    static_ip: GetStaticIPRsp | None
    swap_size: int | None
    tailscale_status: GetTailscaleStatusRsp | None
    uptime: datetime.datetime | None
    cpu_temperature: float | None
    memory_total: float | None
    memory_used_percent: float | None
    storage_total: float | None
    storage_used_percent: float | None
    watchdog_enabled: bool | None
    ssh_metrics_collector: SSHMetricsCollector | None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: NanoKVMClient,
        username: str,
        password: str,
        device_info: GetInfoRsp,
        ssh_known_hosts: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.username = username
        self.password = password
        self.device_info = device_info
        self.ssh_known_hosts = ssh_known_hosts
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
        self.hdmi_capture = None
        self.hdmi_passthrough = None
        self.low_power = None
        self.led_strip = None
        self.led_brightness_request: LedBrightnessRequest | None = None
        self.lcd_time_format = None
        self.time_status = None
        self.static_ip = None
        self.swap_size = None
        self.tailscale_status = None
        self.uptime = None
        self.cpu_temperature = None
        self.memory_total = None
        self.memory_used_percent = None
        self.storage_total = None
        self.storage_used_percent = None
        self.media_entities_created = False
        self.network_entities_created: set[str] = set()
        self.ssh_sensors_created = False
        self.ssh_switches_created = False
        self.ssh_metrics_collector = None
        self._ssh_connection_warning_logged = False
        self.hostname_info = None
        self.watchdog_enabled = None
        self._app_version_last_fetched: datetime.datetime | None = None
        self._app_version_fetch_task: asyncio.Task[None] | None = None
        self._client_lock = asyncio.Lock()
        self._preferred_host_last_checked: float | None = None
        self._registered_device_details: tuple[str, str] | None = None
        self._preferred_host_check_interval = PREFERRED_HOST_CHECK_INTERVAL_SECONDS

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            # The default ten second cooldown left entities stale after the
            # second of two actions; polling this device is cheap.
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=_REQUEST_REFRESH_COOLDOWN_SECONDS,
                immediate=True,
            ),
        )

    @contextlib.asynccontextmanager
    async def async_client(self) -> AsyncIterator[NanoKVMClient]:
        """Yield the shared API client to one caller at a time."""
        async with self._client_lock:
            async with self.client as client:
                yield client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from NanoKVM."""
        if await self._async_restore_preferred_host():
            # The entry will reload with the verified preferred endpoint.
            # Avoid polling its superseded client while that reload is pending.
            return self._build_update_data()
        use_static_host = self.config_entry.data.get(CONF_USE_STATIC_HOST, False)
        current_host = self.config_entry.data[CONF_HOST]

        _LOGGER.debug(
            "Fetching data from NanoKVM at %s (static_host: %s)",
            current_host,
            use_static_host,
        )

        for attempt in range(1, _UPDATE_MAX_ATTEMPTS):
            try:
                return await self._async_fetch_once()
            except UpdateFailed as err:
                _LOGGER.debug(
                    "NanoKVM update attempt %s/%s failed for %s: %s. Retrying in %ss",
                    attempt,
                    _UPDATE_MAX_ATTEMPTS,
                    current_host,
                    err,
                    _UPDATE_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_UPDATE_RETRY_DELAY_SECONDS)

        return await self._async_fetch_once()

    async def _async_restore_preferred_host(self) -> bool:
        """Restore a verified preferred Pro endpoint without requiring another mDNS event."""
        data = dict(self.config_entry.data)
        preferred_host = data.get(CONF_PREFERRED_HOST)
        if (
            not self.is_pro_hardware
            or data.get(CONF_USE_STATIC_HOST, False)
            or not isinstance(preferred_host, str)
            or preferred_host == data[CONF_HOST]
        ):
            return False

        now = monotonic()
        if (
            self._preferred_host_last_checked is not None
            and now - self._preferred_host_last_checked
            < self._preferred_host_check_interval
        ):
            return False
        self._preferred_host_last_checked = now

        # A scheme-less preferred host is reached like the working connection,
        # unless a saved pin lets the probe verify the host before logging in.
        probe_url = verification_url(
            preferred_host,
            "https" if data.get(CONF_SSL_FINGERPRINT) else self.client.url.scheme,
        )
        try:
            async with asyncio.timeout(PREFERRED_HOST_TIMEOUT_SECONDS):
                async with NanoKVMClient(
                    probe_url,
                    ssl_fingerprint=(
                        data.get(CONF_SSL_FINGERPRINT)
                        if probe_url.startswith("https://")
                        else None
                    ),
                    request_timeout=PREFERRED_HOST_TIMEOUT_SECONDS,
                ) as client:
                    await client.authenticate(self.username, self.password)
                    info = await client.get_info()
        except (aiohttp.ClientError, NanoKVMError, asyncio.TimeoutError):
            # Back off so a long-gone address is not probed every minute.
            self._preferred_host_check_interval = min(
                self._preferred_host_check_interval * 2,
                PREFERRED_HOST_MAX_CHECK_INTERVAL_SECONDS,
            )
            _LOGGER.debug(
                "Preferred NanoKVM host %s is not available, next check in %ss",
                preferred_host,
                self._preferred_host_check_interval,
            )
            return False
        self._preferred_host_check_interval = PREFERRED_HOST_CHECK_INTERVAL_SECONDS

        if self.config_entry.data != data:
            return False

        if str(info.device_key) != self.config_entry.unique_id:
            # The address now belongs to another NanoKVM. Forget it so stored
            # credentials are not sent there again.
            _LOGGER.debug(
                "Preferred NanoKVM host %s now serves another device, keeping %s",
                preferred_host,
                data[CONF_HOST],
            )
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=data | {CONF_PREFERRED_HOST: data[CONF_HOST]},
            )
            return False

        updated_data = data | {CONF_HOST: preferred_host}
        if trusted_key := data.get(CONF_SSH_HOST_KEY):
            try:
                updated_data[CONF_SSH_HOST_KEY] = retarget_known_hosts_line(
                    trusted_key, extract_ssh_host(preferred_host)
                )
            except ValueError:
                _LOGGER.debug(
                    "Cannot restore preferred NanoKVM host %s with an invalid SSH key",
                    preferred_host,
                )
                return False

        if self.hass.config_entries.async_update_entry(
            self.config_entry, data=updated_data
        ):
            _LOGGER.debug(
                "Restoring preferred NanoKVM host from %s to %s",
                data[CONF_HOST], preferred_host,
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return True
        return False

    async def _async_fetch_once(self) -> dict[str, Any]:
        """Fetch data once, handling reauthentication when needed."""
        try:
            return await self._async_fetch_with_client()
        except (
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientConnectorCertificateError,
        ) as err:
            if self.client.url.scheme == "http" and await self._async_failover_client(err):
                return await self._async_fetch_with_error_mapping()
            raise ConfigEntryAuthFailed(
                "SSL certificate changed for NanoKVM"
            ) from err
        except aiohttp.ClientConnectionError as err:
            if await self._async_failover_client(err):
                return await self._async_fetch_with_error_mapping()
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err
        except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as err:
            if _is_auth_failure(err):
                await self._async_reauthenticate_client(err)
                return await self._async_fetch_with_error_mapping()

            if isinstance(err, aiohttp.ClientResponseError):
                raise UpdateFailed(f"HTTP error with NanoKVM: {err}") from err
            raise UpdateFailed(f"Authentication failed: {err}") from err

        except asyncio.TimeoutError:
            raise UpdateFailed(
                _format_timeout_error("communicating with NanoKVM")
            ) from None
        except (NanoKVMError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

    async def _async_fetch_with_error_mapping(self) -> dict[str, Any]:
        """Retry a fetch while mapping failures to Home Assistant exceptions."""
        try:
            return await self._async_fetch_with_client()
        except (
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientConnectorCertificateError,
        ) as err:
            raise ConfigEntryAuthFailed(
                "SSL certificate changed for NanoKVM"
            ) from err
        except asyncio.TimeoutError:
            raise UpdateFailed(
                _format_timeout_error("communicating with NanoKVM")
            ) from None
        except (aiohttp.ClientResponseError, NanoKVMAuthenticationFailure) as err:
            if _is_auth_failure(err):
                raise ConfigEntryAuthFailed(
                    "Stored NanoKVM credentials are no longer valid"
                ) from err
            if isinstance(err, aiohttp.ClientResponseError):
                raise UpdateFailed(f"HTTP error with NanoKVM: {err}") from err
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except (NanoKVMError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

    async def _async_fetch_with_client(self) -> dict[str, Any]:
        """Fetch data using the current client instance."""
        async with self.async_client() as client:
            async with asyncio.timeout(_UPDATE_TIMEOUT_SECONDS):
                if not client.token:
                    await client.authenticate(self.username, self.password)

                await self._async_fetch_core_data()
                self._async_maybe_create_network_entities()
                await self._async_fetch_storage_data()
                self._async_maybe_create_media_entities()

        self._async_sync_device_registry()
        await self._async_refresh_ssh_data()
        self._async_schedule_app_version_refresh()
        return self._build_update_data()

    def _async_sync_device_registry(self) -> None:
        """Keep the registered device name and firmware current between reloads."""
        details = (
            self.hostname_info.hostname if self.hostname_info else INTEGRATION_TITLE,
            device_sw_version(
                self.device_info.application,
                getattr(self.device_info, "image", None),
            ),
        )
        if self._registered_device_details in (None, details):
            # Entities register the device with these values themselves.
            self._registered_device_details = details
            return

        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, self.device_info.device_key)}
        )
        if device is None:
            return
        registry.async_update_device(
            device.id, name=details[0], sw_version=details[1]
        )
        self._registered_device_details = details

    async def _async_reauthenticate_client(self, original_error: Exception) -> None:
        """Reauthenticate and replace the client when token/auth fails."""
        async with self._client_lock:
            await self._async_reauthenticate_client_locked(original_error)

    async def _async_reauthenticate_client_locked(
        self, original_error: Exception
    ) -> None:
        """Reauthenticate and replace the client while access is serialized."""
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
            except aiohttp.ClientConnectionError as auth_err:
                last_error = auth_err
                continue
            except asyncio.TimeoutError:
                if isinstance(original_error, aiohttp.ClientResponseError):
                    raise UpdateFailed(
                        _format_timeout_error("reauthenticating with NanoKVM")
                    ) from None
                raise UpdateFailed(
                    _format_timeout_error("authenticating with NanoKVM")
                ) from None
            except (NanoKVMError, aiohttp.ClientError) as auth_err:
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
        async with self._client_lock:
            return await self._async_failover_client_locked(original_error)

    async def _async_failover_client_locked(self, original_error: Exception) -> bool:
        """Switch transports while access to the shared client is serialized."""
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
            except asyncio.TimeoutError:
                raise UpdateFailed(
                    _format_timeout_error("checking alternate NanoKVM API transport")
                ) from None
            except (NanoKVMError, aiohttp.ClientError) as err:
                raise UpdateFailed(f"Error communicating with NanoKVM: {err}") from err

        _LOGGER.debug(
            "No alternate NanoKVM API transport succeeded after connection failure from %s: %s",
            self.client.url,
            original_error,
        )
        return False

    async def _fetch_optional(
        self,
        endpoint: str,
        call: Callable[[], Awaitable[_ResponseT]],
    ) -> _ResponseT | None:
        """Run an optional endpoint call; return None when the device lacks it."""
        try:
            return await call()
        except NanoKVMNotSupportedError as err:
            _LOGGER.debug(
                "NanoKVM endpoint %s is not supported on this device: %s",
                endpoint,
                err,
            )
            return None
        except NanoKVMApiError as err:
            _LOGGER.debug(
                "NanoKVM optional endpoint %s returned an API error: %s",
                endpoint,
                err,
            )
            return None
        except aiohttp.ClientResponseError as err:
            if err.status != 404:
                raise
            _LOGGER.debug("NanoKVM endpoint %s is not available on this device", endpoint)
            return None

    async def _fetch_oled_info(self) -> GetOLEDRsp | None:
        """Fetch OLED state, treating NanoKVM Pro's missing OLED file as unavailable."""
        try:
            return await self.client.get_oled_info()
        except NanoKVMApiError as err:
            if not _is_invalid_file_content_error(err):
                raise
            _LOGGER.debug(
                "NanoKVM endpoint /vm/oled returned %r; treating OLED as unavailable",
                _INVALID_FILE_CONTENT_MESSAGE,
            )
            return None

    async def _async_fetch_concurrently(
        self, calls: dict[str, Callable[[], Awaitable[Any]]]
    ) -> dict[str, Any]:
        """Run independent endpoint calls a few at a time and return all results."""
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)

        async def run(call: Callable[[], Awaitable[Any]]) -> Any:
            async with semaphore:
                return await call()

        tasks = [asyncio.ensure_future(run(call)) for call in calls.values()]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return dict(zip(calls, results))

    async def _async_fetch_core_data(self) -> None:
        """Fetch required API data used by entities."""
        client = self.client
        # Hardware identity gates the endpoints below and never changes per device.
        self.hardware_info = await client.get_hardware()

        def optional(
            endpoint: str, call: Callable[[], Awaitable[Any]]
        ) -> Callable[[], Awaitable[Any]]:
            return lambda: self._fetch_optional(endpoint, call)

        calls: dict[str, Callable[[], Awaitable[Any]]] = {
            "device_info": client.get_info,
            "hostname_info": client.get_hostname,
            "gpio_info": client.get_gpio,
            "ssh_state": client.get_ssh_state,
            "mdns_state": client.get_mdns_state,
            "hid_mode": client.get_hid_mode,
            "oled_info": self._fetch_oled_info,
            "wifi_status": client.get_wifi_status,
            "mouse_jiggler_state": client.get_mouse_jiggler_state,
            "tailscale_status": client.get_tailscale_status,
        }
        if self.hardware_info is not None:
            calls["virtual_device_info"] = optional(
                "/vm/device/virtual", client.get_virtual_device_status
            )
        if self.supports_hdmi_endpoint:
            calls["hdmi_state"] = optional("/vm/hdmi", client.get_hdmi_state)
        if self.is_non_pro_hardware:
            calls["swap_size"] = optional("/vm/swap", client.get_swap_size)
        if self.is_pro_hardware:
            calls |= {
                "hdmi_capture": optional("/vm/hdmi/capture", client.get_hdmi_capture),
                "hdmi_passthrough": optional(
                    "/vm/hdmi/passthrough", client.get_hdmi_passthrough
                ),
                "low_power": optional("/vm/low-power", client.get_low_power),
                "led_strip": optional("/vm/ledstrip/get", client.get_led_strip),
                "lcd_time_format": optional(
                    "/vm/lcd/time/format", client.get_lcd_time_format
                ),
                "time_status": optional("/vm/time/status", client.get_time_status),
                "static_ip": optional("/network/static-ip", client.get_static_ip),
            }

        results = await self._async_fetch_concurrently(calls)

        # Publish everything together so a failed poll leaves the last state intact.
        for attribute in _GATED_CORE_ATTRIBUTES:
            setattr(self, attribute, None)
        for attribute, value in results.items():
            setattr(self, attribute, value)
        note_reported_led_strip(self, self.led_strip)

    def _async_schedule_app_version_refresh(self) -> None:
        """Refresh application version info outside the critical poll path."""
        if (
            self._app_version_fetch_task is not None
            and not self._app_version_fetch_task.done()
        ):
            return

        now = datetime.datetime.now(datetime.UTC)
        cache_ttl = (
            _APP_VERSION_CACHE_SECONDS
            if self.application_version_info is not None
            else _APP_VERSION_FAILURE_CACHE_SECONDS
        )
        if (
            self._app_version_last_fetched is not None
            and (now - self._app_version_last_fetched).total_seconds() < cache_ttl
        ):
            return

        self._app_version_fetch_task = self.hass.async_create_task(
            self._async_refresh_app_version()
        )

    async def _async_refresh_app_version(self) -> None:
        """Fetch and cache application version info without blocking coordinator refreshes."""
        try:
            self.application_version_info = await self._async_fetch_app_version()
            self._app_version_last_fetched = datetime.datetime.now(datetime.UTC)
            self.async_update_listeners()
        finally:
            self._app_version_fetch_task = None

    async def _async_fetch_app_version(self) -> GetVersionRsp | None:
        """Fetch application version using a dedicated client and timeout."""
        version_client = NanoKVMClient(
            str(self.client.url),
            token=self.client.token,
            ssl_fingerprint=self.config_entry.data.get(CONF_SSL_FINGERPRINT),
            request_timeout=_APP_VERSION_REQUEST_TIMEOUT_SECONDS,
        )
        try:
            async with version_client:
                return await version_client.get_application_version()
        except (NanoKVMError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug(
                "Failed to fetch application version from NanoKVM: %s "
                "(device may have no internet access)",
                err,
            )
            return None

    async def _async_fetch_storage_data(self) -> None:
        """Fetch storage-specific state (mounted image and CD-ROM mode)."""
        mounted_image = GetMountedImageRsp(file="", cdrom=False, readOnly=False)
        cdrom_status = GetCdRomRsp(cdrom=0) if self.is_non_pro_hardware else None
        if self.hid_mode and self.hid_mode.mode == HidMode.NORMAL:
            try:
                mounted_image = await self.client.get_mounted_image()
            except NanoKVMApiError as err:
                _LOGGER.debug(
                    "Failed to get mounted image, retrieving default value: %s", err
                )

            if self.is_non_pro_hardware:
                cdrom_status = await self._fetch_optional(
                    "/storage/cdrom", self.client.get_cdrom_status
                )

        self.mounted_image = mounted_image
        self.cdrom_status = cdrom_status

    async def _async_refresh_ssh_data(self) -> None:
        """Fetch or clear SSH metrics depending on SSH state."""
        if self.ssh_state and self.ssh_state.enabled:
            try:
                async with asyncio.timeout(_SSH_METRICS_TIMEOUT_SECONDS):
                    await self._async_update_ssh_data()
            except TimeoutError:
                _LOGGER.debug(
                    "Timed out fetching optional SSH metrics after %s seconds",
                    _SSH_METRICS_TIMEOUT_SECONDS,
                )
                await self._async_clear_ssh_data()
            except asyncio.CancelledError:
                await self._async_clear_ssh_data()
                raise
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
            "hdmi_capture": self.hdmi_capture,
            "hdmi_passthrough": self.hdmi_passthrough,
            "low_power": self.low_power,
            "led_strip": self.led_strip,
            "lcd_time_format": self.lcd_time_format,
            "time_status": self.time_status,
            "static_ip": self.static_ip,
            "swap_size": self.swap_size,
            "tailscale_status": self.tailscale_status,
            "hostname_info": self.hostname_info,
            "watchdog_enabled": self.watchdog_enabled,
        }

    def _clear_ssh_runtime_state(self) -> None:
        """Clear SSH-derived runtime state from the coordinator."""
        self.uptime = None
        self.cpu_temperature = None
        self.memory_total = None
        self.memory_used_percent = None
        self.storage_total = None
        self.storage_used_percent = None
        self.watchdog_enabled = None

    @property
    def supports_watchdog(self) -> bool:
        """Return whether NanoKVM watchdog support is available."""
        application_version = self.device_info.application.strip()
        if not application_version:
            return False

        try:
            return AwesomeVersion(application_version) >= _WATCHDOG_MIN_VERSION
        except AwesomeVersionException:
            _LOGGER.debug(
                "Unable to determine watchdog support from NanoKVM application version %r",
                application_version,
            )
            return False

    @property
    def is_pro_hardware(self) -> bool:
        """Return whether the detected NanoKVM hardware is the Pro model."""
        return bool(
            self.hardware_info and self.hardware_info.version == HWVersion.PRO
        )

    @property
    def is_non_pro_hardware(self) -> bool:
        """Return whether non-Pro endpoints and controls apply to this device."""
        return self.hardware_info is not None and not self.is_pro_hardware

    @property
    def supports_hdmi_endpoint(self) -> bool:
        """Return whether the non-Pro HDMI endpoint should be queried."""
        return bool(
            self.hardware_info and self.hardware_info.version == HWVersion.PCIE
        )

    def _active_network_connection_types(self) -> set[str]:
        """Return active network connection types reported by the NanoKVM."""
        return {
            address.type.casefold()
            for address in self.device_info.ips
            if address.addr
        }

    async def async_ensure_ssh_metrics_collector(self) -> SSHMetricsCollector:
        """Return the active SSH collector, creating it when needed."""
        if not self.ssh_metrics_collector:
            self.ssh_metrics_collector = SSHMetricsCollector(
                host=extract_ssh_host(self.config_entry.data[CONF_HOST]),
                password=self.password,
                known_hosts=self.ssh_known_hosts,
            )
        return self.ssh_metrics_collector

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

    def _async_maybe_create_network_entities(self) -> None:
        """Signal when per-connection network entities should be created."""
        for connection_type in self._active_network_connection_types():
            if connection_type in self.network_entities_created:
                continue

            _LOGGER.debug(
                "Network connection type %s present, signaling to create entities",
                connection_type,
            )
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_NETWORK_ENTITIES.format(self.config_entry.entry_id),
                connection_type,
            )
            self.network_entities_created.add(connection_type)

    async def _async_update_ssh_data(self) -> None:
        """Fetch data via SSH."""
        try:
            collector = await self.async_ensure_ssh_metrics_collector()
            metrics = await collector.collect(include_watchdog=self.supports_watchdog)
            self._ssh_connection_warning_logged = False
            self.uptime = metrics.uptime
            self.cpu_temperature = metrics.cpu_temperature
            self.memory_total = metrics.memory_total
            self.memory_used_percent = metrics.memory_used_percent
            self.storage_total = metrics.storage_total
            self.storage_used_percent = metrics.storage_used_percent
            self.watchdog_enabled = metrics.watchdog_enabled
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

            if self.supports_watchdog and not self.ssh_switches_created:
                _LOGGER.debug("Watchdog supported, signaling to create SSH-backed switches")
                async_dispatcher_send(
                    self.hass, SIGNAL_NEW_SSH_SWITCHES.format(self.config_entry.entry_id)
                )
                self.ssh_switches_created = True

        except NanoKVMSSHConnectionError as err:
            if self._ssh_connection_warning_logged:
                _LOGGER.debug(
                    "SSH metrics remain unavailable for %s: %s",
                    self.config_entry.data[CONF_HOST],
                    err,
                )
            else:
                _LOGGER.warning(
                    "SSH metrics unavailable for %s because host-key verification or "
                    "the SSH connection failed: %s. Reconfigure the NanoKVM entry "
                    "after verifying the device fingerprint if its host key changed.",
                    self.config_entry.data[CONF_HOST],
                    err,
                )
                self._ssh_connection_warning_logged = True
            self._clear_ssh_runtime_state()
            if self.ssh_metrics_collector:
                await self.ssh_metrics_collector.disconnect()
        except Exception as err:
            _LOGGER.debug("Failed to fetch data via SSH: %s", err)
            self._clear_ssh_runtime_state()
            if self.ssh_metrics_collector:
                await self.ssh_metrics_collector.disconnect()

    async def _async_clear_ssh_data(self) -> None:
        """Clear SSH data and disconnect client."""
        self._clear_ssh_runtime_state()
        if self.ssh_metrics_collector:
            await self.ssh_metrics_collector.disconnect()
            self.ssh_metrics_collector = None

    async def async_shutdown(self) -> None:
        """Release any background tasks and live connections owned by the coordinator."""
        try:
            await super().async_shutdown()
        finally:
            try:
                if self._app_version_fetch_task is not None:
                    self._app_version_fetch_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._app_version_fetch_task
                    self._app_version_fetch_task = None
            finally:
                if self.ssh_metrics_collector:
                    await self.ssh_metrics_collector.disconnect()
                    self.ssh_metrics_collector = None
