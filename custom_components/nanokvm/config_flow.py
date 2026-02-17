"""Config flow for Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components import zeroconf

from nanokvm.client import NanoKVMClient, NanoKVMAuthenticationFailure, NanoKVMError

from .const import DEFAULT_USERNAME, DEFAULT_PASSWORD, DOMAIN, INTEGRATION_TITLE, CONF_USE_STATIC_HOST
from .utils import normalize_host, normalize_mdns

_LOGGER = logging.getLogger(__name__)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Validate the user input allows us to connect.
    """
    async with NanoKVMClient(normalize_host(data[CONF_HOST])) as client:
        try:
            await client.authenticate(data[CONF_USERNAME], data[CONF_PASSWORD])
            device_info = await client.get_info()
        except NanoKVMAuthenticationFailure as err:
            raise InvalidAuth from err
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError,
                aiohttp.ClientError, NanoKVMError) as err:
            raise CannotConnect from err

    return str(device_info.device_key)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sipeed NanoKVM."""

    VERSION = 1

    async def add_device(self, device_key: str, data: dict[str, Any]) -> FlowResult:
        _LOGGER.debug(
            "Adding device - key: %s, Host: %s, Static: %s",
            device_key,
            data[CONF_HOST],
            data.get(CONF_USE_STATIC_HOST, False)
        )
        await self.async_set_unique_id(device_key)
        self._abort_if_unique_id_configured()
        
        if CONF_USE_STATIC_HOST not in data:
            data[CONF_USE_STATIC_HOST] = False
        
        if data[CONF_USE_STATIC_HOST]:
            _LOGGER.debug(
                "Device configured to use static host %s (mDNS discovery disabled)",
                data[CONF_HOST]
            )
        else:
            _LOGGER.debug(
                "Device configured to allow mDNS discovery (host: %s, key: %s)",
                data[CONF_HOST],
                device_key
            )
        
        return self.async_create_entry(title=INTEGRATION_TITLE, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step (manual host entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_USERNAME: DEFAULT_USERNAME,
                CONF_PASSWORD: DEFAULT_PASSWORD,
            } | user_input

            try:
                await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug(
                    "Opened NanoKVM device at %s that still requires user credentials.",
                    user_input[CONF_HOST],
                )
                self.data = user_input
                return await self.async_step_auth()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.data = data            
                return await self.async_step_confirm()
            
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_USE_STATIC_HOST, default=False): bool,
            }),
            errors=errors,
        )
    
    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                device_key = await validate_input(self.hass, self.data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug(
                    "Opened NanoKVM device at %s that requires user credentials now.",
                    self.data[CONF_HOST],
                )
                return await self.async_step_auth()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self.add_device(device_key, self.data)
            
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={"name": self.data[CONF_HOST]},
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authentication step."""
        errors: dict[str, str] = {}
        
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        
        if user_input is not None:
            data = self.data | user_input

            try:
                device_key = await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self.add_device(device_key, data)

        return self.async_show_form(
            step_id="auth", 
            data_schema=schema, 
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        legacy_mdns_id = normalize_mdns(discovery_info.hostname)

        async with NanoKVMClient(normalize_host(discovery_info.hostname)) as client:
            try:
                await client.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD)
                device_info = await client.get_info()
                device_key = str(device_info.device_key)

                await self.async_set_unique_id(device_key)

                # Support both old (mDNS) and new (device_key) unique IDs.
                for entry in self._async_current_entries():
                    if entry.unique_id not in (device_key, legacy_mdns_id):
                        continue
                    if entry.data.get(CONF_USE_STATIC_HOST, False):
                        _LOGGER.debug(
                            "Device %s is configured with static host (%s), ignoring discovery",
                            discovery_info.hostname,
                            entry.data[CONF_HOST],
                        )
                    else:
                        _LOGGER.debug(
                            "Device %s is already configured, ignoring discovery",
                            discovery_info.hostname,
                        )
                    return self.async_abort(reason="already_configured")

                self._abort_if_unique_id_configured()

                _LOGGER.debug(
                    "Discovered NanoKVM device at %s that uses default credentials.",
                    discovery_info.hostname,
                )
            except NanoKVMAuthenticationFailure:
                # Fall back to legacy ID path when authentication blocks device_key retrieval.
                await self.async_set_unique_id(legacy_mdns_id)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(
                    "Discovered NanoKVM device at %s requires user credentials.",
                    discovery_info.hostname,
                )
                # If authentication fails, it's still a NanoKVM device, but we can't get device_info.
                # We'll let the flow continue to prompt for credentials.
            except (aiohttp.ClientError, NanoKVMError) as err:
                _LOGGER.debug("Failed to connect to %s during discovery: %s. Ignoring as most likely not a NanoKVM device.", discovery_info.hostname, err)
                return
        
        self.context["title_placeholders"] = {"name": discovery_info.hostname}

        self.data = {
            CONF_HOST: discovery_info.hostname
        }
        return await self.async_step_user(user_input=self.data)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
