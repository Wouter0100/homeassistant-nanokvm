"""Config flow for Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from nanokvm.client import NanoKVMClient, NanoKVMAuthenticationFailure, NanoKVMError
from nanokvm.utils import async_fetch_remote_fingerprint

from .const import (
    CONF_SSL_FINGERPRINT,
    CONF_USE_STATIC_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    DOMAIN,
    INTEGRATION_TITLE,
)
from .utils import normalize_host, normalize_mdns

_LOGGER = logging.getLogger(__name__)

async def validate_input(data: dict[str, Any]) -> str:
    """Validate the user input allows us to connect."""
    async with NanoKVMClient(
        normalize_host(data[CONF_HOST]),
        ssl_fingerprint=data.get(CONF_SSL_FINGERPRINT),
    ) as client:
        try:
            await client.authenticate(data[CONF_USERNAME], data[CONF_PASSWORD])
            device_info = await client.get_info()
        except NanoKVMAuthenticationFailure as err:
            raise InvalidAuth from err
        except (aiohttp.ClientConnectorCertificateError,
                aiohttp.ServerFingerprintMismatch) as err:
            raise SSLCertificateChanged from err
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError,
                aiohttp.ClientError, NanoKVMError) as err:
            raise CannotConnect from err

    return str(device_info.device_key)


class NanoKVMConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sipeed NanoKVM."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self.data: dict[str, Any] = {}
        self._discovered_fingerprint: str | None = None
        self._ssl_return_step: str | None = None

    def _get_reauth_entry(self) -> ConfigEntry:
        """Return the config entry currently undergoing reauthentication."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            raise RuntimeError("Reauth flow started for a missing NanoKVM entry")
        return entry

    def _async_find_matching_entry(self, *unique_ids: str) -> ConfigEntry | None:
        """Find an existing config entry by any of the provided unique IDs."""
        for entry in self._async_current_entries():
            if entry.unique_id in unique_ids:
                return entry
        return None

    def _async_handle_existing_entry(
        self,
        entry: ConfigEntry,
        discovery_host: str,
        *,
        device_key: str | None = None,
    ) -> ConfigFlowResult:
        """Handle discovery for an already-configured device."""
        current_host = entry.data[CONF_HOST]
        use_static_host = entry.data.get(CONF_USE_STATIC_HOST, False)

        if use_static_host:
            _LOGGER.debug(
                "Device discovered at %s is configured with static host %s, ignoring discovery",
                discovery_host,
                current_host,
            )
            return self.async_abort(reason="already_configured")

        if current_host != discovery_host:
            _LOGGER.debug(
                "Updating discovered host for NanoKVM from %s to %s",
                current_host,
                discovery_host,
            )
        else:
            _LOGGER.debug(
                "Device %s is already configured with the current host, ignoring discovery",
                discovery_host,
            )

        update_kwargs: dict[str, Any] = {
            "data_updates": {CONF_HOST: discovery_host},
            "reason": "already_configured",
            "reload_even_if_entry_is_unchanged": False,
        }
        if device_key is not None and entry.unique_id != device_key:
            update_kwargs["unique_id"] = device_key

        return self.async_update_reload_and_abort(entry, **update_kwargs)

    async def add_device(
        self, device_key: str, data: dict[str, Any]
    ) -> ConfigFlowResult:
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

    def _format_fingerprint(self, fingerprint: str) -> str:
        """Format a hex fingerprint with colons for display."""
        return ":".join(
            fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)
        )

    async def _async_fetch_and_redirect_ssl(
        self, return_step: str
    ) -> ConfigFlowResult:
        """Fetch the remote fingerprint and redirect to the SSL confirmation step."""
        self._ssl_return_step = return_step
        self._discovered_fingerprint = await async_fetch_remote_fingerprint(
            normalize_host(self.data[CONF_HOST])
        )

        if return_step == "reauth_finish" and self.data.get(CONF_SSL_FINGERPRINT):
            return await self.async_step_ssl_fingerprint_changed()

        return await self.async_step_ssl_fingerprint()

    async def async_step_ssl_fingerprint(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to trust a new SSL certificate (first-time setup)."""
        if user_input is not None:
            self.data[CONF_SSL_FINGERPRINT] = self._discovered_fingerprint
            return_step = self._ssl_return_step
            self._ssl_return_step = None

            if return_step == "confirm":
                return await self.async_step_confirm()
            if return_step == "auth":
                return await self.async_step_auth()
            if return_step == "reauth_finish":
                return await self.async_step_reauth_finish()

        return self.async_show_form(
            step_id="ssl_fingerprint",
            description_placeholders={
                "host": self.data[CONF_HOST],
                "fingerprint": self._format_fingerprint(self._discovered_fingerprint),
            },
        )

    async def async_step_ssl_fingerprint_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm a changed SSL certificate (reauth)."""
        if user_input is not None:
            self.data[CONF_SSL_FINGERPRINT] = self._discovered_fingerprint
            return await self.async_step_reauth_finish()

        old_fingerprint = self.data.get(CONF_SSL_FINGERPRINT) or ""

        return self.async_show_form(
            step_id="ssl_fingerprint_changed",
            description_placeholders={
                "host": self.data[CONF_HOST],
                "old_fingerprint": self._format_fingerprint(old_fingerprint),
                "new_fingerprint": self._format_fingerprint(
                    self._discovered_fingerprint
                ),
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start a reauthentication flow for an existing entry.

        Probes the device to determine whether the failure is a credential
        problem or a certificate change, then routes to the appropriate step.
        """
        del entry_data
        entry = self._get_reauth_entry()
        self.data = dict(entry.data)

        try:
            await validate_input(self.data)
        except SSLCertificateChanged:
            return await self._async_fetch_and_redirect_ssl("reauth_finish")
        except (InvalidAuth, CannotConnect, Exception):
            pass

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update stored credentials for an existing NanoKVM entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        if user_input is not None:
            self.data = dict(entry.data) | user_input

            try:
                device_key = await validate_input(self.data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except SSLCertificateChanged:
                errors["base"] = "ssl_certificate_changed"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self._async_finish_reauth(entry, device_key)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": entry.data.get(CONF_HOST, INTEGRATION_TITLE),
            },
        )

    async def async_step_reauth_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish reauth after the user confirmed a new SSL fingerprint."""
        entry = self._get_reauth_entry()

        try:
            device_key = await validate_input(self.data)
        except InvalidAuth:
            return await self.async_step_reauth_confirm()
        except (CannotConnect, SSLCertificateChanged, Exception) as err:
            _LOGGER.error("Reauth failed after SSL confirmation: %s", err)
            return self.async_abort(reason="cannot_connect")

        return await self._async_finish_reauth(entry, device_key)

    async def _async_finish_reauth(
        self, entry: ConfigEntry, device_key: str
    ) -> ConfigFlowResult:
        """Complete reauth by updating the config entry."""
        await self.async_set_unique_id(device_key)

        existing_entry = self._async_find_matching_entry(device_key)
        if (
            existing_entry is not None
            and existing_entry.entry_id != entry.entry_id
        ):
            return self.async_abort(reason="already_configured")

        update_kwargs: dict[str, Any] = {
            "data_updates": {
                CONF_USERNAME: self.data[CONF_USERNAME],
                CONF_PASSWORD: self.data[CONF_PASSWORD],
                CONF_SSL_FINGERPRINT: self.data.get(CONF_SSL_FINGERPRINT),
            },
        }
        if entry.unique_id != device_key:
            update_kwargs["unique_id"] = device_key

        return self.async_update_reload_and_abort(
            entry,
            reason="reauth_successful",
            **update_kwargs,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step (manual host entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_USERNAME: DEFAULT_USERNAME,
                CONF_PASSWORD: DEFAULT_PASSWORD,
            } | user_input

            try:
                await validate_input(data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug(
                    "Opened NanoKVM device at %s that still requires user credentials.",
                    user_input[CONF_HOST],
                )
                self.data = user_input
                return await self.async_step_auth()
            except SSLCertificateChanged:
                self.data = data
                return await self._async_fetch_and_redirect_ssl("confirm")
            except Exception:
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
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                device_key = await validate_input(self.data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug(
                    "Opened NanoKVM device at %s that requires user credentials now.",
                    self.data[CONF_HOST],
                )
                return await self.async_step_auth()
            except SSLCertificateChanged:
                return await self._async_fetch_and_redirect_ssl("confirm")
            except Exception:
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
    ) -> ConfigFlowResult:
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
                device_key = await validate_input(data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except SSLCertificateChanged:
                self.data = data
                return await self._async_fetch_and_redirect_ssl("auth")
            except Exception:
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
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        discovery_hostname = normalize_mdns(discovery_info.hostname)
        discovery_host = discovery_info.host

        async with NanoKVMClient(normalize_host(discovery_host)) as client:
            try:
                await client.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD)
                device_info = await client.get_info()
                device_key = str(device_info.device_key)

                await self.async_set_unique_id(device_key)

                # Support both old (mDNS) and new (device_key) unique IDs.
                if entry := self._async_find_matching_entry(device_key, discovery_hostname):
                    return self._async_handle_existing_entry(
                        entry,
                        discovery_host,
                        device_key=device_key,
                    )

                self._abort_if_unique_id_configured()

                _LOGGER.debug(
                    "Discovered NanoKVM device at %s (%s) that uses default credentials.",
                    discovery_hostname,
                    discovery_host,
                )
            except NanoKVMAuthenticationFailure:
                # Fall back to legacy ID path when authentication blocks device_key retrieval.
                if entry := self._async_find_matching_entry(discovery_hostname):
                    return self._async_handle_existing_entry(entry, discovery_host)

                await self.async_set_unique_id(discovery_hostname)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(
                    "Discovered NanoKVM device at %s (%s) requires user credentials.",
                    discovery_hostname,
                    discovery_host,
                )
                # If authentication fails, it's still a NanoKVM device, but we can't get device_info.
                # We'll let the flow continue to prompt for credentials.
            except (aiohttp.ClientError, asyncio.TimeoutError, NanoKVMError) as err:
                _LOGGER.debug(
                    "Failed to connect to %s (%s) during discovery: %s. Ignoring as most likely not a NanoKVM device.",
                    discovery_hostname,
                    discovery_host,
                    err,
                )
                return self.async_abort(reason="cannot_connect")

        self.context["title_placeholders"] = {"name": discovery_info.hostname.rstrip(".")}

        self.data = {
            CONF_HOST: discovery_host
        }
        return await self.async_step_user(user_input=self.data)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class SSLCertificateChanged(HomeAssistantError):
    """Error to indicate the SSL certificate has changed."""
