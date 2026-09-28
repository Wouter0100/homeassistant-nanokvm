"""Config flow for Sipeed NanoKVM integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from nanokvm.client import NanoKVMClient, NanoKVMAuthenticationFailure, NanoKVMError
from nanokvm.utils import async_fetch_remote_fingerprint

from .const import (
    CONF_SSH_HOST_KEY,
    CONF_SSL_FINGERPRINT,
    CONF_TRUST_SSH_HOST_KEY,
    CONF_USE_STATIC_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    DOMAIN,
    INTEGRATION_TITLE,
)
from .ssh_host_keys import (
    SSHHostKey,
    async_probe_host_key,
    retarget_known_hosts_line,
)
from .utils import (
    api_connection_options,
    extract_ssh_host,
    https_probe_url,
    normalize_host,
    normalize_mdns,
)

_LOGGER = logging.getLogger(__name__)

async def validate_input(data: dict[str, Any]) -> str:
    """Validate the user input allows us to connect."""
    options = api_connection_options(
        data[CONF_HOST],
        data.get(CONF_SSL_FINGERPRINT),
    )
    last_error: Exception | None = None

    for index, option in enumerate(options):
        async with NanoKVMClient(
            option.base_url,
            ssl_fingerprint=option.ssl_fingerprint,
        ) as client:
            try:
                await client.authenticate(data[CONF_USERNAME], data[CONF_PASSWORD])
                device_info = await client.get_info()
                return str(device_info.device_key)
            except NanoKVMAuthenticationFailure as err:
                raise InvalidAuth from err
            except (
                aiohttp.ClientConnectorCertificateError,
                aiohttp.ServerFingerprintMismatch,
            ) as err:
                if option.scheme == "http" and index < len(options) - 1:
                    last_error = err
                    continue
                raise SSLCertificateChanged from err
            except aiohttp.ClientConnectionError as err:
                last_error = err
                if index < len(options) - 1:
                    continue
            except (asyncio.TimeoutError, aiohttp.ClientError, NanoKVMError) as err:
                last_error = err
                break

    assert last_error is not None
    raise CannotConnect from last_error


async def async_prepare_ssh_host_key(data: dict[str, Any]) -> SSHHostKey | None:
    """Probe the SSH host key when the device reports SSH as enabled.

    SSH is optional on NanoKVM devices.  A missing or unavailable SSH service
    therefore leaves the integration usable while an enabled service gets a
    strict, user-confirmed host key before metrics collection can authenticate.
    """
    if not hasattr(NanoKVMClient, "get_ssh_state"):
        return None

    options = api_connection_options(
        data[CONF_HOST],
        data.get(CONF_SSL_FINGERPRINT),
    )
    for option in options:
        try:
            async with NanoKVMClient(
                option.base_url,
                ssl_fingerprint=option.ssl_fingerprint,
            ) as client:
                await client.authenticate(data[CONF_USERNAME], data[CONF_PASSWORD])
                ssh_state = await client.get_ssh_state()
        except (asyncio.TimeoutError, aiohttp.ClientError, NanoKVMError):
            continue

        if not getattr(ssh_state, "enabled", False):
            return None

        try:
            return await async_probe_host_key(extract_ssh_host(data[CONF_HOST]))
        except Exception as err:
            _LOGGER.warning(
                "Unable to verify the SSH host key for NanoKVM at %s: %s",
                data[CONF_HOST],
                err,
            )
            return None

    return None


class NanoKVMConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sipeed NanoKVM."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self.data: dict[str, Any] = {}
        self._discovered_fingerprint: str | None = None
        self._ssl_return_step: str | None = None
        self._pending_device_key: str | None = None
        self._pending_ssh_host_key: SSHHostKey | None = None
        self._reconfigure_entry: ConfigEntry | None = None

    def _get_reauth_entry(self) -> ConfigEntry:
        """Return the config entry currently undergoing reauthentication."""
        entry_id = self.context.get("entry_id")
        if entry_id is None:
            raise RuntimeError("Reauth flow started without a NanoKVM entry ID")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise RuntimeError("Reauth flow started for a missing NanoKVM entry")
        return entry

    def _get_reconfigure_entry(self) -> ConfigEntry:
        """Return the config entry currently undergoing reconfiguration."""
        entry_id = self.context.get("entry_id")
        if entry_id is None:
            raise RuntimeError("Reconfigure flow started without a NanoKVM entry ID")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise RuntimeError("Reconfigure flow started for a missing NanoKVM entry")
        return entry

    async def _async_add_device_with_ssh_key(
        self,
        device_key: str,
        data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Create a device entry after optional SSH host-key confirmation."""
        self.data = data
        if data.get(CONF_SSH_HOST_KEY):
            return await self.add_device(device_key, data)

        self._pending_device_key = device_key
        host_key = await async_prepare_ssh_host_key(data)
        if host_key is None:
            self._pending_device_key = None
            return await self.add_device(device_key, data)

        self._pending_ssh_host_key = host_key
        return await self.async_step_ssh_host_key()

    def _async_find_matching_entry(self, *unique_ids: str) -> ConfigEntry | None:
        """Find an existing config entry by any of the provided unique IDs."""
        for entry in self._async_current_entries():
            if entry.unique_id in unique_ids:
                return entry
        return None

    def _async_find_entry_by_discovery_host(
        self, *discovery_hosts: str
    ) -> ConfigEntry | None:
        """Find an existing config entry by its configured host."""
        normalized_hosts = {
            host.rstrip(".").casefold() for host in discovery_hosts
        }
        for entry in self._async_current_entries():
            configured_host = entry.data.get(CONF_HOST)
            if not isinstance(configured_host, str):
                continue
            try:
                configured_host = extract_ssh_host(configured_host)
            except ValueError:
                continue
            if configured_host.rstrip(".").casefold() in normalized_hosts:
                return entry
        return None

    async def _async_handle_existing_entry(
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

        data_updates: dict[str, Any] = {CONF_HOST: discovery_host}
        trusted_ssh_key = entry.data.get(CONF_SSH_HOST_KEY)
        if isinstance(trusted_ssh_key, str) and trusted_ssh_key:
            try:
                updated_ssh_key = retarget_known_hosts_line(
                    trusted_ssh_key,
                    extract_ssh_host(discovery_host),
                )
            except ValueError:
                return await self._async_reconfirm_ssh_host_key(
                    entry,
                    discovery_host,
                    device_key,
                )
            if updated_ssh_key != trusted_ssh_key:
                if device_key == entry.unique_id:
                    data_updates[CONF_SSH_HOST_KEY] = updated_ssh_key
                else:
                    return await self._async_reconfirm_ssh_host_key(
                        entry,
                        discovery_host,
                        device_key,
                    )

        update_kwargs: dict[str, Any] = {
            "data_updates": data_updates,
            "reason": "already_configured",
            "reload_even_if_entry_is_unchanged": False,
        }
        if device_key is not None and entry.unique_id != device_key:
            update_kwargs["unique_id"] = device_key

        return self.async_update_reload_and_abort(entry, **update_kwargs)

    async def _async_reconfirm_ssh_host_key(
        self,
        entry: ConfigEntry,
        discovery_host: str,
        device_key: str | None,
    ) -> ConfigFlowResult:
        """Require fresh user trust when discovery cannot verify key identity."""
        data = dict(entry.data)
        data[CONF_HOST] = discovery_host
        host_key = await async_prepare_ssh_host_key(data)
        if host_key is None:
            return self.async_abort(reason="ssh_host_key_unavailable")

        self._reconfigure_entry = entry
        self._pending_ssh_host_key = host_key
        self._pending_device_key = device_key
        self.data = data
        return await self.async_step_ssh_host_key()

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
            https_probe_url(self.data[CONF_HOST])
        )

        if return_step == "reauth_finish" and self.data.get(CONF_SSL_FINGERPRINT):
            return await self.async_step_ssl_fingerprint_changed()

        return await self.async_step_ssl_fingerprint()

    async def async_step_ssl_fingerprint(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to trust a new SSL certificate (first-time setup)."""
        fingerprint = self._discovered_fingerprint
        if fingerprint is None:
            return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            self.data[CONF_SSL_FINGERPRINT] = fingerprint
            return_step = self._ssl_return_step
            self._ssl_return_step = None

            if return_step == "confirm":
                return await self.async_step_confirm()
            if return_step == "auth":
                return await self.async_step_auth()
            if return_step == "zeroconf":
                return await self.async_step_user(user_input=self.data)
            if return_step == "reauth_finish":
                return await self.async_step_reauth_finish()

        return self.async_show_form(
            step_id="ssl_fingerprint",
            description_placeholders={
                "host": self.data[CONF_HOST],
                "fingerprint": self._format_fingerprint(fingerprint),
            },
        )

    async def async_step_ssl_fingerprint_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm a changed SSL certificate (reauth)."""
        fingerprint = self._discovered_fingerprint
        if fingerprint is None:
            return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            self.data[CONF_SSL_FINGERPRINT] = fingerprint
            return await self.async_step_reauth_finish()

        old_fingerprint = self.data.get(CONF_SSL_FINGERPRINT) or ""

        return self.async_show_form(
            step_id="ssl_fingerprint_changed",
            description_placeholders={
                "host": self.data[CONF_HOST],
                "old_fingerprint": self._format_fingerprint(old_fingerprint),
                "new_fingerprint": self._format_fingerprint(fingerprint),
            },
        )

    async def async_step_ssh_host_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to trust the SSH host key before storing it."""
        host_key = self._pending_ssh_host_key
        if user_input is None:
            if host_key is None:
                return self.async_abort(reason="ssh_host_key_unavailable")
            return self.async_show_form(
                step_id="ssh_host_key",
                data_schema=vol.Schema(
                    {vol.Required(CONF_TRUST_SSH_HOST_KEY, default=False): bool}
                ),
                description_placeholders={
                    "host": host_key.host,
                    "fingerprint": host_key.fingerprint,
                },
            )

        if not user_input.get(CONF_TRUST_SSH_HOST_KEY, False):
            if self._reconfigure_entry is None:
                device_key = self._pending_device_key
                self._pending_device_key = None
                self._pending_ssh_host_key = None
                self.data.pop(CONF_SSH_HOST_KEY, None)
                if device_key is not None:
                    return await self.add_device(device_key, self.data)
            return self.async_abort(reason="ssh_host_key_not_trusted")
        if host_key is None:
            return self.async_abort(reason="ssh_host_key_unavailable")

        self.data[CONF_SSH_HOST_KEY] = host_key.known_hosts_line
        self._pending_ssh_host_key = None

        if self._reconfigure_entry is not None:
            entry = self._reconfigure_entry
            self._reconfigure_entry = None
            data_updates = {CONF_SSH_HOST_KEY: host_key.known_hosts_line}
            if self.data.get(CONF_HOST) != entry.data.get(CONF_HOST):
                data_updates[CONF_HOST] = self.data[CONF_HOST]
            update_kwargs: dict[str, Any] = {
                "data_updates": data_updates,
                "reason": "reconfigure_successful",
            }
            device_key = self._pending_device_key
            self._pending_device_key = None
            self._pending_ssh_host_key = None
            if device_key is not None and entry.unique_id != device_key:
                update_kwargs["unique_id"] = device_key
            return self.async_update_reload_and_abort(
                entry,
                **update_kwargs,
            )

        device_key = self._pending_device_key
        self._pending_device_key = None
        if device_key is None:
            return self.async_abort(reason="ssh_host_key_unavailable")
        return await self.add_device(device_key, self.data)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the approved SSH host key for an existing entry."""
        del user_input
        entry = self._get_reconfigure_entry()
        if self._reconfigure_entry is None:
            self._reconfigure_entry = entry
            self.data = dict(entry.data)
            self._pending_ssh_host_key = await async_prepare_ssh_host_key(self.data)
            if self._pending_ssh_host_key is None:
                self._reconfigure_entry = None
                return self.async_abort(reason="ssh_host_key_unavailable")

        return await self.async_step_ssh_host_key()

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
            _LOGGER.error(
                "Reauth failed after SSL confirmation: %s: %s",
                type(err).__name__,
                err,
            )
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
                return await self._async_add_device_with_ssh_key(
                    device_key,
                    self.data,
                )

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
                return await self._async_add_device_with_ssh_key(device_key, data)

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
        is_ssh_service = discovery_info.type == "_ssh._tcp.local."
        existing_entry = (
            self._async_find_entry_by_discovery_host(
                discovery_host,
                discovery_hostname,
            )
            if is_ssh_service
            else None
        )
        if existing_entry and existing_entry.data.get(CONF_USE_STATIC_HOST, False):
            return self.async_abort(reason="already_configured")

        ssl_fingerprint = (
            existing_entry.data.get(CONF_SSL_FINGERPRINT)
            if existing_entry
            else None
        )
        self.context["title_placeholders"] = {
            "name": discovery_info.hostname.rstrip(".")
        }
        host_for_flow = (
            str(URL(https_probe_url(discovery_host)).origin())
            if is_ssh_service
            else discovery_host
        )
        api_url = (
            https_probe_url(discovery_host)
            if is_ssh_service
            else normalize_host(discovery_host)
        )
        self.data = {CONF_HOST: host_for_flow}
        if existing_entry:
            self.data[CONF_USERNAME] = existing_entry.data.get(
                CONF_USERNAME, DEFAULT_USERNAME
            )
            self.data[CONF_PASSWORD] = existing_entry.data.get(
                CONF_PASSWORD, DEFAULT_PASSWORD
            )
        if ssl_fingerprint:
            self.data[CONF_SSL_FINGERPRINT] = ssl_fingerprint

        async with NanoKVMClient(
            api_url,
            ssl_fingerprint=ssl_fingerprint,
        ) as client:
            try:
                await client.authenticate(
                    self.data.get(CONF_USERNAME, DEFAULT_USERNAME),
                    self.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                )
                device_info = await client.get_info()
                device_key = str(device_info.device_key)

                await self.async_set_unique_id(device_key)

                # Support both old (mDNS) and new (device_key) unique IDs.
                if entry := self._async_find_matching_entry(
                    device_key, discovery_hostname
                ):
                    return await self._async_handle_existing_entry(
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
                    return await self._async_handle_existing_entry(
                        entry,
                        discovery_host,
                    )

                await self.async_set_unique_id(discovery_hostname)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(
                    "Discovered NanoKVM device at %s (%s) requires user credentials.",
                    discovery_hostname,
                    discovery_host,
                )
                # If authentication fails, it's still a NanoKVM device, but we can't get device_info.
                # We'll let the flow continue to prompt for credentials.
                if existing_entry:
                    return await self._async_handle_existing_entry(
                        existing_entry,
                        discovery_host,
                    )
            except aiohttp.ClientConnectorCertificateError:
                _LOGGER.debug(
                    "NanoKVM discovered at %s (%s) uses an untrusted TLS certificate.",
                    discovery_hostname,
                    discovery_host,
                )
                # Reserve the hostname before waiting for TLS trust so repeated
                # zeroconf updates cannot create duplicate discovery flows.
                await self.async_set_unique_id(discovery_hostname)
                return await self._async_fetch_and_redirect_ssl("zeroconf")
            except (aiohttp.ClientError, asyncio.TimeoutError, NanoKVMError) as err:
                _LOGGER.debug(
                    "Failed to connect to %s (%s) during discovery: %s. Ignoring as most likely not a NanoKVM device.",
                    discovery_hostname,
                    discovery_host,
                    err,
                )
                return self.async_abort(reason="cannot_connect")

        return await self.async_step_user(user_input=self.data)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class SSLCertificateChanged(HomeAssistantError):
    """Error to indicate the SSL certificate has changed."""
