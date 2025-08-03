"""Config flow for Sipeed NanoKVM integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components import zeroconf

from nanokvm.client import NanoKVMClient, NanoKVMAuthenticationFailure, NanoKVMError

from .const import DEFAULT_USERNAME, DEFAULT_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

def normalize_host(host: str) -> str:
    # Ensure the host has a scheme
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    
    # Ensure the host ends with /api/
    if not host.endswith("/api/"):
        host = f"{host}api/" if host.endswith("/") else f"{host}/api/"
    
    return host

def normalize_mdns(mdns: str) -> str:
    # Ensure all mDNS host names end with a dot
    if not mdns.endswith("."):
        mdns = f"{mdns}."

    return mdns

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Validate the user input allows us to connect.
    """
    session = async_get_clientsession(hass)

    client = NanoKVMClient(normalize_host(data[CONF_HOST]), session)

    try:
        await client.authenticate(data[CONF_USERNAME], data[CONF_PASSWORD])
        device_info = await client.get_info()
    except NanoKVMAuthenticationFailure as err:
        raise InvalidAuth from err
    except (aiohttp.ClientError, NanoKVMError) as err:
        raise CannotConnect from err

    return normalize_mdns(device_info.mdns)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sipeed NanoKVM."""

    VERSION = 1

    async def add_device(self, mdns, data) -> FlowResult:
        _LOGGER.debug(
            "Adding device with mDNS name %s as unique_id",
            mdns
        )
        await self.async_set_unique_id(mdns)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(title=f"NanoKVM ({mdns})", data=data)

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
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )
    
    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                mdns = await validate_input(self.hass, self.data)
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
                return await self.add_device(mdns, self.data)
            
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
                mdns = await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self.add_device(mdns, data)

        return self.async_show_form(
            step_id="auth", 
            data_schema=schema, 
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        
        await self.async_set_unique_id(normalize_mdns(discovery_info.hostname))
        self._abort_if_unique_id_configured()

        session = async_get_clientsession(self.hass)
        client = NanoKVMClient(normalize_host(discovery_info.hostname), session)

        try:
            await client.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD)
            await client.get_info()

            _LOGGER.debug(
                "Discovered NanoKVM device at %s that uses default credentials.",
                discovery_info.hostname,
            )
        except NanoKVMAuthenticationFailure:
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
