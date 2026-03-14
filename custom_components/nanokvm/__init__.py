"""The Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from nanokvm.client import NanoKVMAuthenticationFailure, NanoKVMClient, NanoKVMError

from .const import CONF_SSL_FINGERPRINT, CONF_USE_STATIC_HOST, DOMAIN
from .coordinator import NanoKVMDataUpdateCoordinator
from .services import async_register_services, async_unregister_services
from .utils import api_connection_options

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sipeed NanoKVM from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    use_static_host = entry.data.get(CONF_USE_STATIC_HOST, False)

    _LOGGER.info(
        "Setting up NanoKVM integration for %s (static_host: %s)",
        host,
        use_static_host,
    )

    ssl_fingerprint = entry.data.get(CONF_SSL_FINGERPRINT)
    options = api_connection_options(host, ssl_fingerprint)
    last_error: Exception | None = None
    client: NanoKVMClient | None = None
    device_info = None

    for index, option in enumerate(options):
        candidate_client = NanoKVMClient(
            option.base_url,
            ssl_fingerprint=option.ssl_fingerprint,
        )
        try:
            async with candidate_client:
                await candidate_client.authenticate(username, password)
                device_info = await candidate_client.get_info()
            client = candidate_client
            break
        except NanoKVMAuthenticationFailure as err:
            raise ConfigEntryAuthFailed(
                f"Authentication failed for NanoKVM at {host}"
            ) from err
        except (
            aiohttp.ServerFingerprintMismatch,
            aiohttp.ClientConnectorCertificateError,
        ) as err:
            if option.scheme == "http" and index < len(options) - 1:
                last_error = err
                continue
            raise ConfigEntryAuthFailed(
                f"SSL certificate changed for NanoKVM at {host}"
            ) from err
        except aiohttp.ClientConnectorError as err:
            last_error = err
            if index < len(options) - 1:
                continue
            break
        except (aiohttp.ClientError, NanoKVMError, asyncio.TimeoutError) as err:
            last_error = err
            break

    if client is None or device_info is None:
        assert last_error is not None
        raise ConfigEntryNotReady(
            f"Failed to fetch initial device info: {last_error}"
        ) from last_error

    coordinator = NanoKVMDataUpdateCoordinator(
        hass,
        entry,
        client=client,
        username=username,
        password=password,
        device_info=device_info,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.pop(entry.entry_id, None)
    if coordinator and coordinator.ssh_metrics_collector:
        await coordinator.ssh_metrics_collector.disconnect()

    if not domain_data:
        async_unregister_services(hass)
        hass.data.pop(DOMAIN, None)

    return True
