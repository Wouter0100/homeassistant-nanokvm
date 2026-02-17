"""The Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from nanokvm.client import NanoKVMAuthenticationFailure, NanoKVMClient, NanoKVMError

from .config_flow import normalize_host
from .const import CONF_USE_STATIC_HOST, DOMAIN
from .coordinator import NanoKVMDataUpdateCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
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

    client = NanoKVMClient(normalize_host(host))

    device_info = None
    try:
        async with client:
            await client.authenticate(username, password)
            device_info = await client.get_info()
    except NanoKVMAuthenticationFailure as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except (aiohttp.ClientError, NanoKVMError, asyncio.TimeoutError):
        device_info = type(
            "DeviceInfo",
            (),
            {
                "device_key": f"{host}_{username}",
                "mdns": host,
                "application": "Unknown",
                "image": "Unknown",
            },
        )()

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
