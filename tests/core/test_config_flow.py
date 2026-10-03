"""Tests for NanoKVM config-flow behavior and transport validation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from nanokvm.client import (
    NanoKVMAuthenticationFailure,
    NanoKVMError,
)
import pytest
from yarl import URL

import custom_components.nanokvm.config_flow as config_flow_module
from custom_components.nanokvm.config_flow import (
    CannotConnect,
    InvalidAuth,
    NanoKVMConfigFlow,
    SSLCertificateChanged,
    async_prepare_ssh_host_key,
    validate_input,
)
from custom_components.nanokvm.const import (
    CONF_SSH_HOST_KEY,
    CONF_SSL_FINGERPRINT,
    CONF_TRUST_SSH_HOST_KEY,
    CONF_USE_STATIC_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    DOMAIN,
    INTEGRATION_TITLE,
)


@dataclass(slots=True)
class ClientScenario:
    """Script one fake NanoKVM client's authentication and info behavior."""

    auth_error: Exception | None = None
    info_error: Exception | None = None
    device_key: object = "device-key"
    required_fingerprint: str | None = None


class ScriptedClient:
    """Minimal async client whose instances consume configured scenarios."""

    scenarios: list[ClientScenario] = []
    instances: list[ScriptedClient] = []

    def __init__(
        self,
        url: str,
        *,
        ssl_fingerprint: str | None = None,
        **_: object,
    ) -> None:
        if not self.scenarios:
            raise AssertionError(f"No client scenario configured for {url}")
        self.scenario = self.scenarios.pop(0)
        self.url = URL(url)
        self.ssl_fingerprint = ssl_fingerprint
        self.authenticate_calls: list[tuple[str, str]] = []
        self.entered = False
        self.exited = False
        self.instances.append(self)

    async def __aenter__(self) -> ScriptedClient:
        """Enter the fake client context."""
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the fake client context."""
        self.exited = True

    async def authenticate(self, username: str, password: str) -> None:
        """Record credentials and raise the scripted authentication error."""
        if (
            self.scenario.required_fingerprint is not None
            and self.ssl_fingerprint != self.scenario.required_fingerprint
        ):
            raise aiohttp.ClientConnectorCertificateError(
                None, OSError("self-signed certificate")
            )
        self.authenticate_calls.append((username, password))
        if self.scenario.auth_error is not None:
            raise self.scenario.auth_error

    async def get_info(self) -> SimpleNamespace:
        """Return device identity or raise the scripted info error."""
        if self.scenario.info_error is not None:
            raise self.scenario.info_error
        return SimpleNamespace(device_key=self.scenario.device_key)


@pytest.fixture
def install_client(monkeypatch: pytest.MonkeyPatch):
    """Install a clean scripted client and return its scenario setter."""

    def install(*scenarios: ClientScenario) -> type[ScriptedClient]:
        ScriptedClient.scenarios = list(scenarios)
        ScriptedClient.instances = []
        monkeypatch.setattr(config_flow_module, "NanoKVMClient", ScriptedClient)
        return ScriptedClient

    return install


@pytest.fixture
def flow(hass_mock: MagicMock) -> NanoKVMConfigFlow:
    """Return a config flow attached to the shared Home Assistant boundary mock."""
    config_flow = NanoKVMConfigFlow()
    config_flow.hass = hass_mock
    # FlowManager sets a mutable per-flow context before invoking any step.
    config_flow.context = {"source": "user"}
    return config_flow


def _connection_data(host: str = "nanokvm.local") -> dict[str, Any]:
    """Return complete config-flow connection input."""
    return {
        CONF_HOST: host,
        CONF_USERNAME: "operator",
        CONF_PASSWORD: "secret",
    }


def _fingerprint_error() -> aiohttp.ServerFingerprintMismatch:
    """Return a realistic pinned-certificate mismatch."""
    return aiohttp.ServerFingerprintMismatch(b"expected", b"received", "nano", 443)


class SSHStateClient(ScriptedClient):
    """Scripted client exposing the optional SSH-state endpoint."""

    ssh_enabled = True

    async def get_ssh_state(self) -> SimpleNamespace:
        """Return the configured SSH service state."""
        return SimpleNamespace(enabled=self.ssh_enabled)


@pytest.mark.asyncio
async def test_prepare_ssh_host_key_probes_enabled_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled SSH service produces a key fingerprint for confirmation."""
    SSHStateClient.scenarios = [ClientScenario()]
    SSHStateClient.instances = []
    monkeypatch.setattr(config_flow_module, "NanoKVMClient", SSHStateClient)
    host_key = config_flow_module.SSHHostKey(
        host="nanokvm.local",
        known_hosts_line="nanokvm.local ssh-ed25519 ZHVtbXk=",
        fingerprint="SHA256:trusted",
    )
    probe = AsyncMock(return_value=host_key)
    monkeypatch.setattr(config_flow_module, "async_probe_host_key", probe)

    result = await async_prepare_ssh_host_key(_connection_data())

    assert result is host_key
    probe.assert_awaited_once_with("nanokvm.local")


@pytest.mark.asyncio
async def test_prepare_ssh_host_key_skips_clients_without_ssh_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test doubles and older clients without SSH state remain optional."""
    monkeypatch.setattr(config_flow_module, "NanoKVMClient", ScriptedClient)

    assert await async_prepare_ssh_host_key(_connection_data()) is None


@pytest.mark.asyncio
async def test_prepare_ssh_host_key_skips_disabled_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled SSH service does not trigger a TCP host-key probe."""
    SSHStateClient.scenarios = [ClientScenario()]
    SSHStateClient.instances = []
    SSHStateClient.ssh_enabled = False
    monkeypatch.setattr(config_flow_module, "NanoKVMClient", SSHStateClient)
    probe = AsyncMock()
    monkeypatch.setattr(config_flow_module, "async_probe_host_key", probe)

    try:
        assert await async_prepare_ssh_host_key(_connection_data()) is None
    finally:
        SSHStateClient.ssh_enabled = True
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_ssh_host_key_handles_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed optional probe leaves setup usable without trusting a key."""
    SSHStateClient.scenarios = [ClientScenario()]
    SSHStateClient.instances = []
    monkeypatch.setattr(config_flow_module, "NanoKVMClient", SSHStateClient)
    monkeypatch.setattr(
        config_flow_module,
        "async_probe_host_key",
        AsyncMock(side_effect=OSError("SSH unavailable")),
    )

    assert await async_prepare_ssh_host_key(_connection_data()) is None


@pytest.mark.asyncio
async def test_validate_input_returns_string_device_key_and_closes_client(
    install_client,
) -> None:
    """Successful validation authenticates once and returns a stable string ID."""
    client_type = install_client(ClientScenario(device_key=12345))

    assert await validate_input(_connection_data()) == "12345"
    assert len(client_type.instances) == 1
    client = client_type.instances[0]
    assert client.url == URL("http://nanokvm.local/api/")
    assert client.authenticate_calls == [("operator", "secret")]
    assert client.entered is True
    assert client.exited is True


@pytest.mark.asyncio
async def test_validate_input_uses_fingerprint_for_explicit_https(
    install_client,
) -> None:
    """Explicit HTTPS validation passes the stored pin to its only client."""
    client_type = install_client(ClientScenario())
    data = _connection_data("https://nanokvm.local")
    data[CONF_SSL_FINGERPRINT] = "AA11"

    assert await validate_input(data) == "device-key"
    assert len(client_type.instances) == 1
    assert client_type.instances[0].ssl_fingerprint == "AA11"


@pytest.mark.asyncio
async def test_validate_input_maps_authentication_failure_without_fallback(
    install_client,
) -> None:
    """Invalid credentials are terminal and map to the flow's auth error."""
    client_type = install_client(
        ClientScenario(auth_error=NanoKVMAuthenticationFailure("invalid")),
        ClientScenario(),
    )

    with pytest.raises(InvalidAuth):
        await validate_input(_connection_data())

    assert len(client_type.instances) == 1


@pytest.mark.asyncio
async def test_validate_input_http_certificate_error_falls_back_to_https(
    install_client,
) -> None:
    """A TLS error reached through HTTP redirection tries direct HTTPS next."""
    client_type = install_client(
        ClientScenario(auth_error=_fingerprint_error()),
        ClientScenario(device_key="https-device"),
    )

    assert await validate_input(_connection_data()) == "https-device"
    assert [client.url.scheme for client in client_type.instances] == ["http", "https"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "certificate_error",
    [
        _fingerprint_error(),
        aiohttp.ClientConnectorCertificateError(None, ValueError("certificate")),
    ],
)
async def test_validate_input_https_certificate_error_requests_confirmation(
    install_client,
    certificate_error: Exception,
) -> None:
    """A final HTTPS certificate failure maps to the SSL confirmation flow."""
    install_client(ClientScenario(auth_error=certificate_error))

    with pytest.raises(SSLCertificateChanged):
        await validate_input(_connection_data("https://nanokvm.local"))


@pytest.mark.asyncio
async def test_validate_input_exhausted_connection_candidates_cannot_connect(
    install_client,
) -> None:
    """Connection failures exhaust both transports before mapping cannot-connect."""
    client_type = install_client(
        ClientScenario(auth_error=aiohttp.ClientConnectionError("http unavailable")),
        ClientScenario(auth_error=aiohttp.ClientConnectionError("https unavailable")),
    )

    with pytest.raises(CannotConnect) as error:
        await validate_input(_connection_data())

    assert isinstance(error.value.__cause__, aiohttp.ClientConnectionError)
    assert [client.url.scheme for client in client_type.instances] == ["http", "https"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError(),
        aiohttp.ClientPayloadError("invalid payload"),
        NanoKVMError("api failure"),
    ],
)
async def test_validate_input_non_connection_errors_stop_transport_fallback(
    install_client,
    error: Exception,
) -> None:
    """Timeout, response, and library errors do not probe another transport."""
    client_type = install_client(ClientScenario(info_error=error), ClientScenario())

    with pytest.raises(CannotConnect) as flow_error:
        await validate_input(_connection_data())

    assert flow_error.value.__cause__ is error
    assert len(client_type.instances) == 1


def test_flow_initial_state_and_fingerprint_formatting(flow: NanoKVMConfigFlow) -> None:
    """A new flow has no staged data and formats certificate hashes for display."""
    assert flow.data == {}
    assert flow._discovered_fingerprint is None
    assert flow._ssl_return_step is None
    assert flow._format_fingerprint("AABB01") == "AA:BB:01"


def test_get_reauth_entry_returns_context_entry(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """Reauth resolves its entry through the context entry ID."""
    flow.context = {"source": "reauth", "entry_id": config_entry_mock.entry_id}
    flow.hass.config_entries.async_get_entry.return_value = config_entry_mock

    assert flow._get_reauth_entry() is config_entry_mock


def test_get_reauth_entry_rejects_missing_entry(flow: NanoKVMConfigFlow) -> None:
    """A stale reauth context fails explicitly instead of using missing data."""
    flow.context = {"source": "reauth", "entry_id": "missing"}
    flow.hass.config_entries.async_get_entry.return_value = None

    with pytest.raises(RuntimeError, match="missing NanoKVM entry"):
        flow._get_reauth_entry()


def test_get_reconfigure_entry_rejects_missing_entry(flow: NanoKVMConfigFlow) -> None:
    """A stale reconfigure context fails explicitly."""
    flow.context = {"source": "reconfigure", "entry_id": "missing"}
    flow.hass.config_entries.async_get_entry.return_value = None

    with pytest.raises(RuntimeError, match="missing NanoKVM entry"):
        flow._get_reconfigure_entry()


def test_find_matching_entry_returns_first_match_or_none(
    flow: NanoKVMConfigFlow,
) -> None:
    """Legacy and device-key matching scans current entries in order."""
    first = SimpleNamespace(unique_id="first")
    match = SimpleNamespace(unique_id="device-key")
    flow._async_current_entries = MagicMock(return_value=[first, match])

    assert flow._async_find_matching_entry("legacy", "device-key") is match
    assert flow._async_find_matching_entry("absent") is None


def test_discovery_address_match_takes_precedence_over_hostname_hint(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale or repeated hostname cannot hide an exact configured-address match."""
    hint = SimpleNamespace(unique_id="other-device", data={CONF_HOST: "192.0.2.99"})
    exact = SimpleNamespace(unique_id="target-device", data={CONF_HOST: "192.0.2.20"})
    flow._async_current_entries = MagicMock(return_value=[hint, exact])
    registry = SimpleNamespace(
        async_get_device=lambda **kwargs: SimpleNamespace(name="nano-kvm")
    )
    monkeypatch.setattr(
        "homeassistant.helpers.device_registry.async_get", lambda hass: registry
    )

    assert flow._async_find_entry_by_discovery_host(
        "192.0.2.20", "nano-kvm.local."
    ) is exact


@pytest.mark.asyncio
async def test_existing_static_entry_aborts_without_host_update(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """Static entries ignore discovery and keep their configured host."""
    config_entry_mock.data[CONF_USE_STATIC_HOST] = True

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key="new-key",
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("current_unique_id", "device_key", "expected_unique_id"),
    [
        ("legacy.local.", "device-key", "device-key"),
        ("device-key", "device-key", None),
        ("legacy.local.", None, None),
    ],
)
@pytest.mark.asyncio
async def test_existing_dynamic_entry_updates_discovered_host_and_optional_unique_id(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    current_unique_id: str,
    device_key: str | None,
    expected_unique_id: str | None,
) -> None:
    """Verified dynamic discovery refreshes the host and migrates legacy IDs."""
    config_entry_mock.unique_id = current_unique_id
    config_entry_mock.data[CONF_USE_STATIC_HOST] = False
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key=device_key,
    )

    assert result == {"updated": True}
    kwargs = flow.async_update_reload_and_abort.call_args.kwargs
    assert kwargs["data_updates"] == {CONF_HOST: "192.0.2.20"}
    assert kwargs["reason"] == "already_configured"
    assert kwargs["reload_even_if_entry_is_unchanged"] is False
    assert kwargs.get("unique_id") == expected_unique_id


@pytest.mark.asyncio
async def test_existing_dynamic_entry_with_unchanged_host_still_uses_reload_helper(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """Unchanged discovery uses the helper's no-reload-if-unchanged contract."""
    config_entry_mock.data[CONF_USE_STATIC_HOST] = False
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    assert await flow._async_handle_existing_entry(
        config_entry_mock,
        config_entry_mock.data[CONF_HOST],
    ) == {"updated": True}
    assert (
        flow.async_update_reload_and_abort.call_args.kwargs[
            "reload_even_if_entry_is_unchanged"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_existing_entry_rebinds_ssh_key_after_verified_host_change(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """A matching device identity allows a trusted key to follow its host."""
    config_entry_mock.unique_id = "device-key"
    config_entry_mock.data.update(
        {
            CONF_HOST: "nanokvm.local",
            CONF_SSH_HOST_KEY: "nanokvm.local ssh-ed25519 ZHVtbXk=",
            CONF_USE_STATIC_HOST: False,
        }
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key="device-key",
    )

    assert result == {"updated": True}
    assert flow.async_update_reload_and_abort.call_args.kwargs["data_updates"] == {
        CONF_HOST: "192.0.2.20",
        CONF_SSH_HOST_KEY: "192.0.2.20 ssh-ed25519 ZHVtbXk=",
    }


@pytest.mark.asyncio
async def test_existing_entry_with_unchanged_ssh_host_keeps_trusted_key(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """An unchanged discovery host does not need a new SSH trust prompt."""
    config_entry_mock.unique_id = "device-key"
    config_entry_mock.data.update(
        {
            CONF_HOST: "nanokvm.local",
            CONF_SSH_HOST_KEY: "nanokvm.local ssh-ed25519 ZHVtbXk=",
            CONF_USE_STATIC_HOST: False,
        }
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "nanokvm.local",
        device_key="device-key",
    )

    assert result == {"updated": True}
    assert flow.async_update_reload_and_abort.call_args.kwargs["data_updates"] == {
        CONF_HOST: "nanokvm.local",
    }


@pytest.mark.asyncio
async def test_existing_entry_repairs_stale_ssh_key_alias_for_current_host(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """A prior host update cannot leave the approved key pinned to an old alias."""
    config_entry_mock.unique_id = "device-key"
    config_entry_mock.data.update(
        {
            CONF_HOST: "192.0.2.20",
            CONF_SSH_HOST_KEY: "nanokvm.local ssh-ed25519 ZHVtbXk=",
            CONF_USE_STATIC_HOST: False,
        }
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key="device-key",
    )

    assert result == {"updated": True}
    assert flow.async_update_reload_and_abort.call_args.kwargs["data_updates"] == {
        CONF_HOST: "192.0.2.20",
        CONF_SSH_HOST_KEY: "192.0.2.20 ssh-ed25519 ZHVtbXk=",
    }


@pytest.mark.asyncio
async def test_existing_entry_requests_new_ssh_trust_without_identity_match(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host change without a matching device key requires fresh user trust."""
    config_entry_mock.unique_id = "legacy.local."
    config_entry_mock.data.update(
        {
            CONF_HOST: "legacy.local",
            CONF_PASSWORD: DEFAULT_PASSWORD,
            CONF_SSH_HOST_KEY: "legacy.local ssh-ed25519 b2xkLWtleQ==",
            CONF_USE_STATIC_HOST: False,
        }
    )
    fresh_key = config_flow_module.SSHHostKey(
        host="192.0.2.20",
        known_hosts_line="192.0.2.20 ssh-ed25519 bmV3LWtleQ==",
        fingerprint="SHA256:new",
    )
    prepare_key = AsyncMock(return_value=fresh_key)
    monkeypatch.setattr(config_flow_module, "async_prepare_ssh_host_key", prepare_key)
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key="new-device-key",
    )

    assert result["step_id"] == "ssh_host_key"
    prepare_key.assert_awaited_once_with(
        config_entry_mock.data | {CONF_HOST: "192.0.2.20"}
    )
    assert await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: True}) == {
        "updated": True
    }
    flow.async_update_reload_and_abort.assert_called_once_with(
        config_entry_mock,
        data_updates={
            CONF_HOST: "192.0.2.20",
            CONF_SSH_HOST_KEY: "192.0.2.20 ssh-ed25519 bmV3LWtleQ==",
        },
        reason="reconfigure_successful",
        unique_id="new-device-key",
    )


@pytest.mark.asyncio
async def test_existing_entry_with_invalid_ssh_key_aborts_if_probe_fails(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid stored key is not reused when a replacement cannot be probed."""
    config_entry_mock.unique_id = "legacy.local."
    config_entry_mock.data.update(
        {
            CONF_HOST: "legacy.local",
            CONF_SSH_HOST_KEY: "invalid-known-hosts-line",
            CONF_USE_STATIC_HOST: False,
        }
    )
    prepare_key = AsyncMock(return_value=None)
    monkeypatch.setattr(config_flow_module, "async_prepare_ssh_host_key", prepare_key)
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow._async_handle_existing_entry(
        config_entry_mock,
        "192.0.2.20",
        device_key="new-device-key",
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ssh_host_key_unavailable"
    prepare_key.assert_awaited_once_with(
        config_entry_mock.data | {CONF_HOST: "192.0.2.20"}
    )
    flow.async_update_reload_and_abort.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("static_host", [None, False, True])
async def test_add_device_sets_unique_id_defaults_and_creates_entry(
    flow: NanoKVMConfigFlow,
    static_host: bool | None,
) -> None:
    """Adding a device records identity and persists an explicit discovery policy."""
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    data = _connection_data()
    if static_host is not None:
        data[CONF_USE_STATIC_HOST] = static_host

    result = await flow.add_device("device-key", data)

    flow.async_set_unique_id.assert_awaited_once_with("device-key")
    flow._abort_if_unique_id_configured.assert_called_once_with()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == INTEGRATION_TITLE
    assert result["data"][CONF_USE_STATIC_HOST] is bool(static_host)
    assert result["data"]["preferred_host"] == "nanokvm.local"


@pytest.mark.asyncio
async def test_add_device_with_existing_ssh_key_skips_new_probe(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries carrying a trusted key do not repeat the initial probe."""
    data = _connection_data()
    data[CONF_SSH_HOST_KEY] = "nanokvm.local ssh-ed25519 ZHVtbXk="
    flow.add_device = AsyncMock(return_value={"created": True})
    probe = AsyncMock(side_effect=AssertionError("probe should not run"))
    monkeypatch.setattr(config_flow_module, "async_prepare_ssh_host_key", probe)

    assert await flow._async_add_device_with_ssh_key("device-key", data) == {
        "created": True
    }
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_ssl_fingerprint_routes_new_and_changed_certificates(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprint probing chooses first-trust or changed-certificate confirmation."""
    fetch = AsyncMock(side_effect=["AABB", "CCDD"])
    monkeypatch.setattr(config_flow_module, "async_fetch_remote_fingerprint", fetch)
    flow.data = {CONF_HOST: "nanokvm.local"}

    first = await flow._async_fetch_and_redirect_ssl("confirm")
    assert first["step_id"] == "ssl_fingerprint"
    assert first["description_placeholders"]["fingerprint"] == "AA:BB"

    flow.data[CONF_SSL_FINGERPRINT] = "AABB"
    changed = await flow._async_fetch_and_redirect_ssl("reauth_finish")
    assert changed["step_id"] == "ssl_fingerprint_changed"
    assert changed["description_placeholders"] == {
        "host": "nanokvm.local",
        "old_fingerprint": "AA:BB",
        "new_fingerprint": "CC:DD",
    }
    assert fetch.await_args_list[0].args == ("https://nanokvm.local/api/",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("return_step", "method_name"),
    [
        ("confirm", "async_step_confirm"),
        ("auth", "async_step_auth"),
        ("reauth_finish", "async_step_reauth_finish"),
    ],
)
async def test_accepting_new_fingerprint_resumes_saved_step(
    flow: NanoKVMConfigFlow,
    return_step: str,
    method_name: str,
) -> None:
    """Certificate acceptance stores the pin and resumes the interrupted step."""
    flow.data = {CONF_HOST: "nanokvm.local"}
    flow._discovered_fingerprint = "AABB"
    flow._ssl_return_step = return_step
    resumed = AsyncMock(return_value={"resumed": return_step})
    setattr(flow, method_name, resumed)

    result = await flow.async_step_ssl_fingerprint({})

    assert result == {"resumed": return_step}
    assert flow.data[CONF_SSL_FINGERPRINT] == "AABB"
    assert flow._ssl_return_step is None
    resumed.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_accepting_fingerprint_without_saved_step_returns_form(
    flow: NanoKVMConfigFlow,
) -> None:
    """A defensive missing return target leaves the flow on certificate confirmation."""
    flow.data = {CONF_HOST: "nanokvm.local"}
    flow._discovered_fingerprint = "AABB"
    flow._ssl_return_step = None

    result = await flow.async_step_ssl_fingerprint({})

    assert result["step_id"] == "ssl_fingerprint"
    assert flow.data[CONF_SSL_FINGERPRINT] == "AABB"


@pytest.mark.asyncio
async def test_accepting_changed_fingerprint_finishes_reauth(
    flow: NanoKVMConfigFlow,
) -> None:
    """Changed-certificate acceptance replaces the pin before reauth validation."""
    flow.data = {CONF_HOST: "nanokvm.local", CONF_SSL_FINGERPRINT: "OLD"}
    flow._discovered_fingerprint = "NEW"
    flow.async_step_reauth_finish = AsyncMock(return_value={"reauth": True})

    result = await flow.async_step_ssl_fingerprint_changed({})

    assert result == {"reauth": True}
    assert flow.data[CONF_SSL_FINGERPRINT] == "NEW"
    flow.async_step_reauth_finish.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_user_step_shows_schema_without_input(flow: NanoKVMConfigFlow) -> None:
    """The first form requests host and defaults dynamic discovery on."""
    result = await flow.async_step_user()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["data_schema"]({CONF_HOST: "nano"}) == {
        CONF_HOST: "nano",
        CONF_USE_STATIC_HOST: False,
    }


@pytest.mark.asyncio
async def test_user_step_success_stages_defaults_and_requests_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default credentials that work stage complete data before confirmation."""
    validation = AsyncMock(return_value="device-key")
    monkeypatch.setattr(config_flow_module, "validate_input", validation)

    result = await flow.async_step_user(
        {CONF_HOST: "nanokvm.local", CONF_USE_STATIC_HOST: True}
    )

    assert result["step_id"] == "confirm"
    assert flow.data == {
        CONF_HOST: "nanokvm.local",
        CONF_USE_STATIC_HOST: True,
        CONF_USERNAME: DEFAULT_USERNAME,
        CONF_PASSWORD: DEFAULT_PASSWORD,
    }
    validation.assert_awaited_once_with(flow.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (CannotConnect(), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_step_connection_and_unknown_errors_stay_on_form(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_error: str,
) -> None:
    """Recoverable setup failures are rendered on the initial form."""
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=error),
    )

    result = await flow.async_step_user({CONF_HOST: "nanokvm.local"})

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}


@pytest.mark.asyncio
async def test_user_step_invalid_default_credentials_routes_to_auth(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detected device with changed credentials asks the user to authenticate."""
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=InvalidAuth()),
    )

    result = await flow.async_step_user({CONF_HOST: "nanokvm.local"})

    assert result["step_id"] == "auth"
    assert flow.data == {CONF_HOST: "nanokvm.local"}


@pytest.mark.asyncio
async def test_user_step_certificate_error_routes_to_ssl_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-signed certificate is fingerprinted before device confirmation."""
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=SSLCertificateChanged()),
    )
    fetch = AsyncMock(return_value="AABB")
    monkeypatch.setattr(config_flow_module, "async_fetch_remote_fingerprint", fetch)

    result = await flow.async_step_user({CONF_HOST: "nanokvm.local"})

    assert result["step_id"] == "ssl_fingerprint"
    assert flow._ssl_return_step == "confirm"
    assert flow.data[CONF_USERNAME] == DEFAULT_USERNAME


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_step", "expected_error"),
    [
        (CannotConnect(), "confirm", "cannot_connect"),
        (RuntimeError("unexpected"), "confirm", "unknown"),
    ],
)
async def test_confirm_step_errors_remain_on_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_step: str,
    expected_error: str,
) -> None:
    """Confirmation revalidation reports connection and unexpected failures."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=error),
    )

    result = await flow.async_step_confirm({})

    assert result["step_id"] == expected_step
    assert result["errors"] == {"base": expected_error}


@pytest.mark.asyncio
async def test_confirm_step_auth_change_routes_to_auth(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials changed during confirmation return the user to auth."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=InvalidAuth()),
    )

    assert (await flow.async_step_confirm({}))["step_id"] == "auth"


@pytest.mark.asyncio
async def test_confirm_step_certificate_change_repeats_ssl_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A certificate change during confirmation refreshes the displayed pin."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=SSLCertificateChanged()),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_fetch_remote_fingerprint",
        AsyncMock(return_value="AABB"),
    )

    result = await flow.async_step_confirm({})

    assert result["step_id"] == "ssl_fingerprint"
    assert flow._ssl_return_step == "confirm"


@pytest.mark.asyncio
async def test_confirm_step_success_adds_staged_device(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful confirmation delegates final entry creation with staged data."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="device-key"),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(return_value=None),
    )
    flow.add_device = AsyncMock(return_value={"created": True})

    assert await flow.async_step_confirm({}) == {"created": True}
    flow.add_device.assert_awaited_once_with("device-key", flow.data)


@pytest.mark.asyncio
async def test_confirm_step_requests_ssh_host_key_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled SSH service pauses setup for explicit host-key trust."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="device-key"),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(
            return_value=config_flow_module.SSHHostKey(
                host="nanokvm.local",
                known_hosts_line="nanokvm.local ssh-ed25519 ZHVtbXk=",
                fingerprint="SHA256:trusted",
            )
        ),
    )
    flow.add_device = AsyncMock(return_value={"created": True})

    result = await flow.async_step_confirm({})

    assert result["step_id"] == "ssh_host_key"
    assert result["description_placeholders"] == {
        "host": "nanokvm.local",
        "fingerprint": "SHA256:trusted",
    }
    assert CONF_TRUST_SSH_HOST_KEY in result["data_schema"]({})

    assert await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: True}) == {
        "created": True
    }
    flow.add_device.assert_awaited_once_with(
        "device-key",
        flow.data,
    )
    assert flow.data[CONF_SSH_HOST_KEY] == "nanokvm.local ssh-ed25519 ZHVtbXk="


@pytest.mark.asyncio
async def test_declining_ssh_host_key_trust_still_adds_initial_device(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining optional SSH trust keeps the non-SSH integration usable."""
    flow.data = _connection_data()
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="device-key"),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(
            return_value=config_flow_module.SSHHostKey(
                host="nanokvm.local",
                known_hosts_line="nanokvm.local ssh-ed25519 ZHVtbXk=",
                fingerprint="SHA256:trusted",
            )
        ),
    )
    flow.add_device = AsyncMock(return_value={"created": True})

    assert (await flow.async_step_confirm({}))["step_id"] == "ssh_host_key"
    assert await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: False}) == {
        "created": True
    }
    flow.add_device.assert_awaited_once_with("device-key", flow.data)
    assert CONF_SSH_HOST_KEY not in flow.data


@pytest.mark.asyncio
async def test_reconfigure_step_replaces_ssh_host_key(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure exposes a new fingerprint and persists it after trust."""
    flow.context = {"source": "reconfigure", "entry_id": config_entry_mock.entry_id}
    flow.hass.config_entries.async_get_entry.return_value = config_entry_mock
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(
            return_value=config_flow_module.SSHHostKey(
                host="nanokvm.local",
                known_hosts_line="nanokvm.local ssh-ed25519 bmV3LWtleQ==",
                fingerprint="SHA256:new",
            )
        ),
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow.async_step_reconfigure()

    assert result["step_id"] == "ssh_host_key"
    assert await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: True}) == {
        "updated": True
    }
    flow.async_update_reload_and_abort.assert_called_once_with(
        config_entry_mock,
        data_updates={CONF_SSH_HOST_KEY: "nanokvm.local ssh-ed25519 bmV3LWtleQ=="},
        reason="reconfigure_successful",
    )


@pytest.mark.asyncio
async def test_reconfigure_aborts_when_ssh_host_key_probe_fails(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfiguration stops without replacing trust if the key probe fails."""
    flow.context = {"source": "reconfigure", "entry_id": config_entry_mock.entry_id}
    flow.hass.config_entries.async_get_entry.return_value = config_entry_mock
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(return_value=None),
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ssh_host_key_unavailable"
    assert flow._reconfigure_entry is None
    flow.async_update_reload_and_abort.assert_not_called()


@pytest.mark.asyncio
async def test_declining_ssh_host_key_during_reconfigure_keeps_entry_unchanged(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """Declining a replacement host key leaves reconfiguration untouched."""
    flow._reconfigure_entry = config_entry_mock
    flow._pending_ssh_host_key = config_flow_module.SSHHostKey(
        host="nanokvm.local",
        known_hosts_line="nanokvm.local ssh-ed25519 bmV3LWtleQ==",
        fingerprint="SHA256:new",
    )
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    result = await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: False})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ssh_host_key_not_trusted"
    flow.async_update_reload_and_abort.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (CannotConnect(), "cannot_connect"),
        (InvalidAuth(), "invalid_auth"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_auth_step_reports_validation_errors(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_error: str,
) -> None:
    """Credential validation maps its supported errors onto the auth form."""
    flow.data = {CONF_HOST: "nanokvm.local"}
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=error),
    )

    result = await flow.async_step_auth(
        {CONF_USERNAME: "operator", CONF_PASSWORD: "secret"}
    )

    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": expected_error}


@pytest.mark.asyncio
async def test_auth_step_success_merges_credentials_and_adds_device(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful authentication preserves host data and stores entered credentials."""
    flow.data = {CONF_HOST: "nanokvm.local", CONF_USE_STATIC_HOST: True}
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="device-key"),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(return_value=None),
    )
    flow.add_device = AsyncMock(return_value={"created": True})
    credentials = {CONF_USERNAME: "operator", CONF_PASSWORD: "secret"}

    assert await flow.async_step_auth(credentials) == {"created": True}
    flow.add_device.assert_awaited_once_with(
        "device-key",
        flow.data | credentials,
    )


@pytest.mark.asyncio
async def test_auth_step_certificate_error_resumes_auth_after_confirmation(
    flow: NanoKVMConfigFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TLS confirmation during auth retains the credentials being validated."""
    flow.data = {CONF_HOST: "nanokvm.local"}
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=SSLCertificateChanged()),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_fetch_remote_fingerprint",
        AsyncMock(return_value="AABB"),
    )

    result = await flow.async_step_auth(
        {CONF_USERNAME: "operator", CONF_PASSWORD: "secret"}
    )

    assert result["step_id"] == "ssl_fingerprint"
    assert flow._ssl_return_step == "auth"
    assert flow.data[CONF_USERNAME] == "operator"


def _prepare_reauth(
    flow: NanoKVMConfigFlow,
    entry: MagicMock,
) -> None:
    """Attach a config entry to a flow's reauthentication context."""
    entry.title = "NanoKVM"
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.hass.config_entries.async_get_entry.return_value = entry


@pytest.mark.asyncio
async def test_reauth_probe_routes_to_credential_form(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal reauth probe proceeds to the credential form with stored data."""
    _prepare_reauth(flow, config_entry_mock)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=InvalidAuth()),
    )

    result = await flow.async_step_reauth(dict(config_entry_mock.data))

    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["name"] == "nanokvm.local"
    assert flow.data == config_entry_mock.data


@pytest.mark.asyncio
async def test_reauth_probe_certificate_change_routes_to_changed_confirmation(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth detects a changed stored certificate before asking for credentials."""
    config_entry_mock.data[CONF_SSL_FINGERPRINT] = "OLD"
    _prepare_reauth(flow, config_entry_mock)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=SSLCertificateChanged()),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_fetch_remote_fingerprint",
        AsyncMock(return_value="NEW"),
    )

    result = await flow.async_step_reauth({})

    assert result["step_id"] == "ssl_fingerprint_changed"
    assert flow._ssl_return_step == "reauth_finish"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (CannotConnect(), "cannot_connect"),
        (InvalidAuth(), "invalid_auth"),
        (SSLCertificateChanged(), "ssl_certificate_changed"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_reauth_confirm_maps_validation_errors(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_error: str,
) -> None:
    """Updated credentials report each supported validation failure."""
    _prepare_reauth(flow, config_entry_mock)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=error),
    )

    result = await flow.async_step_reauth_confirm(
        {CONF_USERNAME: "new-user", CONF_PASSWORD: "new-password"}
    )

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": expected_error}
    assert flow.data[CONF_USERNAME] == "new-user"


@pytest.mark.asyncio
async def test_reauth_confirm_success_finishes_entry_update(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid replacement credentials delegate the final reauth update."""
    _prepare_reauth(flow, config_entry_mock)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="new-device-key"),
    )
    flow._async_finish_reauth = AsyncMock(return_value={"updated": True})

    assert await flow.async_step_reauth_confirm(
        {CONF_USERNAME: "new-user", CONF_PASSWORD: "new-password"}
    ) == {"updated": True}
    flow._async_finish_reauth.assert_awaited_once_with(
        config_entry_mock,
        "new-device-key",
    )


@pytest.mark.asyncio
async def test_reauth_finish_invalid_auth_returns_to_credentials(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted certificate does not hide an independent credential failure."""
    _prepare_reauth(flow, config_entry_mock)
    flow.data = dict(config_entry_mock.data)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=InvalidAuth()),
    )
    flow.async_step_reauth_confirm = AsyncMock(return_value={"credentials": True})

    assert await flow.async_step_reauth_finish() == {"credentials": True}
    flow.async_step_reauth_confirm.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CannotConnect(), SSLCertificateChanged()])
async def test_reauth_finish_connection_failures_abort(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Failure after certificate acceptance terminates reauth cleanly."""
    _prepare_reauth(flow, config_entry_mock)
    flow.data = dict(config_entry_mock.data)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(side_effect=error),
    )

    result = await flow.async_step_reauth_finish()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_reauth_finish_success_updates_entry(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful post-certificate validation completes the reauth update."""
    _prepare_reauth(flow, config_entry_mock)
    flow.data = dict(config_entry_mock.data)
    monkeypatch.setattr(
        config_flow_module,
        "validate_input",
        AsyncMock(return_value="device-key"),
    )
    flow._async_finish_reauth = AsyncMock(return_value={"updated": True})

    assert await flow.async_step_reauth_finish() == {"updated": True}
    flow._async_finish_reauth.assert_awaited_once_with(
        config_entry_mock,
        "device-key",
    )


@pytest.mark.asyncio
async def test_finish_reauth_rejects_device_owned_by_another_entry(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
) -> None:
    """Reauth cannot migrate an entry onto another configured device identity."""
    other_entry = SimpleNamespace(entry_id="other-entry", unique_id="device-key")
    flow.data = dict(config_entry_mock.data)
    flow.async_set_unique_id = AsyncMock()
    flow._async_find_matching_entry = MagicMock(return_value=other_entry)

    result = await flow._async_finish_reauth(config_entry_mock, "device-key")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("device_key", ["test-device", "new-device-key"])
async def test_finish_reauth_updates_credentials_pin_and_identity_when_needed(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    device_key: str,
) -> None:
    """Successful reauth updates credentials and migrates a changed device key."""
    flow.data = {
        **config_entry_mock.data,
        CONF_USERNAME: "new-user",
        CONF_PASSWORD: "new-password",
        CONF_SSL_FINGERPRINT: "AABB",
    }
    flow.async_set_unique_id = AsyncMock()
    flow._async_find_matching_entry = MagicMock(return_value=config_entry_mock)
    flow.async_update_reload_and_abort = MagicMock(return_value={"updated": True})

    assert await flow._async_finish_reauth(config_entry_mock, device_key) == {
        "updated": True
    }
    kwargs = flow.async_update_reload_and_abort.call_args.kwargs
    assert kwargs["reason"] == "reauth_successful"
    assert kwargs["data_updates"] == {
        CONF_USERNAME: "new-user",
        CONF_PASSWORD: "new-password",
        CONF_SSL_FINGERPRINT: "AABB",
    }
    assert kwargs.get("unique_id") == (
        None if device_key == config_entry_mock.unique_id else device_key
    )


def _discovery_info(
    service_type: str = "_workstation._tcp.local.",
    *,
    host: str = "192.0.2.20",
    addresses: tuple[str, ...] | None = None,
) -> ZeroconfServiceInfo:
    """Return a zeroconf discovery payload."""
    return ZeroconfServiceInfo(
        hostname="nano-kvm.local.",
        ip_address=ip_address(host),
        ip_addresses=[ip_address(address) for address in addresses or (host,)],
        port=22 if service_type == "_ssh._tcp.local." else 9,
        type=service_type,
        name=f"nano-kvm.{service_type}",
        properties={},
    )


def _prepare_new_discovery(flow: NanoKVMConfigFlow) -> None:
    """Install Home Assistant boundary mocks used by new discovery flows."""
    flow.context = {"source": "zeroconf"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._async_find_matching_entry = MagicMock(return_value=None)
    flow.async_step_user = AsyncMock(return_value={"user": True})


@pytest.mark.asyncio
async def test_zeroconf_verified_existing_entry_updates_by_device_key(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
) -> None:
    """Authenticated discovery updates an existing device using verified identity."""
    install_client(ClientScenario(device_key="verified-key"))
    flow.async_set_unique_id = AsyncMock()
    flow._async_find_matching_entry = MagicMock(return_value=config_entry_mock)
    flow._async_handle_existing_entry = AsyncMock(return_value={"updated": True})

    assert await flow.async_step_zeroconf(_discovery_info()) == {"updated": True}
    flow._async_find_matching_entry.assert_called_once_with(
        "verified-key",
        "nano-kvm.local.",
    )
    flow._async_handle_existing_entry.assert_called_once_with(
        config_entry_mock,
        "192.0.2.20",
        device_key="verified-key",
    )


@pytest.mark.asyncio
async def test_zeroconf_new_default_device_routes_to_user_confirmation(
    flow: NanoKVMConfigFlow,
    install_client,
) -> None:
    """A new default-credential device stages its discovered host for setup."""
    client_type = install_client(ClientScenario(device_key="verified-key"))
    _prepare_new_discovery(flow)

    assert await flow.async_step_zeroconf(_discovery_info()) == {"user": True}
    flow.async_set_unique_id.assert_awaited_once_with("verified-key")
    flow._abort_if_unique_id_configured.assert_called_once_with()
    flow.async_step_user.assert_awaited_once_with(user_input={CONF_HOST: "192.0.2.20"})
    assert flow.context["title_placeholders"] == {"name": "nano-kvm.local"}
    assert client_type.instances[0].authenticate_calls == [
        (DEFAULT_USERNAME, DEFAULT_PASSWORD)
    ]


@pytest.mark.asyncio
async def test_zeroconf_auth_required_uses_legacy_identity_for_new_flow(
    flow: NanoKVMConfigFlow,
    install_client,
) -> None:
    """Auth-required discovery uses normalized mDNS identity until login succeeds."""
    install_client(
        ClientScenario(auth_error=NanoKVMAuthenticationFailure("credentials changed"))
    )
    _prepare_new_discovery(flow)

    assert await flow.async_step_zeroconf(_discovery_info()) == {"user": True}
    flow.async_set_unique_id.assert_awaited_once_with("nano-kvm.local.")
    flow.async_step_user.assert_awaited_once_with(user_input={CONF_HOST: "192.0.2.20"})


@pytest.mark.asyncio
async def test_zeroconf_self_signed_certificate_prompts_and_resumes_with_pin(
    flow: NanoKVMConfigFlow,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pro discovery confirms its certificate before sending credentials."""
    client_type = install_client(
        ClientScenario(
            auth_error=aiohttp.ClientConnectorCertificateError(
                None, OSError("self-signed certificate")
            )
        ),
        ClientScenario(device_key="verified-key"),
    )
    flow.context = {"source": "zeroconf"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._async_find_matching_entry = MagicMock(return_value=None)
    flow.async_step_confirm = AsyncMock(return_value={"confirm": True})
    fetch = AsyncMock(return_value="AABB")
    monkeypatch.setattr(config_flow_module, "async_fetch_remote_fingerprint", fetch)

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.")
    )

    assert result["step_id"] == "ssl_fingerprint"
    assert result["description_placeholders"] == {
        "host": "https://192.0.2.20",
        "fingerprint": "AA:BB",
    }
    assert flow._ssl_return_step == "zeroconf"
    fetch.assert_awaited_once_with("https://192.0.2.20/api/")
    assert client_type.instances[0].url == URL("https://192.0.2.20/api/")
    assert client_type.instances[0].ssl_fingerprint is None

    resumed = await flow.async_step_ssl_fingerprint({})

    assert resumed == {"confirm": True}
    assert client_type.instances[1].url == URL("https://192.0.2.20/api/")
    assert client_type.instances[1].ssl_fingerprint == "AABB"
    assert client_type.instances[1].authenticate_calls == [
        (DEFAULT_USERNAME, DEFAULT_PASSWORD)
    ]
    assert flow.data[CONF_SSL_FINGERPRINT] == "AABB"


@pytest.mark.asyncio
async def test_zeroconf_pro_reuses_saved_pin_and_credentials_for_same_host(
    flow: NanoKVMConfigFlow,
    install_client,
) -> None:
    """An already-configured Pro reuses its trusted TLS identity on discovery."""
    client_type = install_client(ClientScenario(device_key="verified-key"))
    entry = SimpleNamespace(
        unique_id="verified-key",
        data={
            CONF_HOST: "192.0.2.20",
            CONF_USERNAME: "operator",
            CONF_PASSWORD: "saved-password",
            CONF_SSL_FINGERPRINT: "AABB",
            CONF_USE_STATIC_HOST: False,
        },
    )
    flow.context = {"source": "zeroconf"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._async_current_entries = MagicMock(return_value=[entry])
    flow._async_find_matching_entry = MagicMock(return_value=entry)
    flow._async_handle_existing_entry = AsyncMock(return_value={"updated": True})

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.")
    )

    assert result == {"updated": True}
    assert client_type.instances[0].url == URL("https://192.0.2.20/api/")
    assert client_type.instances[0].ssl_fingerprint == "AABB"
    assert client_type.instances[0].authenticate_calls == [("operator", "saved-password")]
    flow._async_handle_existing_entry.assert_called_once_with(
        entry,
        "192.0.2.20",
        device_key="verified-key",
    )


def _prepare_configured_pro_discovery(
    flow: NanoKVMConfigFlow,
    entry: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hostname: str | None = "nano-kvm",
    host: str = "192.0.2.20",
) -> None:
    """Expose one configured Pro and its API-reported device-registry name."""
    entry.unique_id = "verified-key"
    entry.data = {
        CONF_HOST: host,
        CONF_USERNAME: "operator",
        CONF_PASSWORD: "saved-password",
        CONF_SSL_FINGERPRINT: "AABB",
        CONF_SSH_HOST_KEY: "192.0.2.20 ssh-ed25519 ZHVtbXk=",
        CONF_USE_STATIC_HOST: False,
    }
    flow.context = {"source": "zeroconf"}
    flow._async_current_entries = MagicMock(return_value=[entry])
    flow.async_set_unique_id = AsyncMock()
    registry = MagicMock()
    registry.async_get_device.return_value = SimpleNamespace(
        name=hostname,
        name_by_user="My renamed NanoKVM",
    )
    monkeypatch.setattr(
        "homeassistant.helpers.device_registry.async_get", lambda hass: registry
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_fetch_remote_fingerprint",
        AsyncMock(return_value="AABB"),
    )


@pytest.mark.parametrize("configured_host", ["192.0.2.20", "https://192.0.2.20"])
@pytest.mark.parametrize(
    ("hostname", "addresses"),
    [
        ("nano-kvm", ("192.0.2.21",)),
        ("NANO-KVM.local.", ("192.0.2.21",)),
        (None, ("192.0.2.21", "192.0.2.20")),
    ],
)
@pytest.mark.asyncio
async def test_zeroconf_pro_wifi_keeps_working_ethernet_entry(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
    configured_host: str,
    hostname: str | None,
    addresses: tuple[str, ...],
) -> None:
    """A second interface neither prompts for TLS trust nor replaces working Ethernet."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, hostname=hostname, host=configured_host
    )
    client_type = install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21", addresses=addresses)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry_mock.data[CONF_HOST] == configured_host
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()
    assert [str(client.url) for client in client_type.instances] == [
        "https://192.0.2.21/api/", "https://192.0.2.20/api/"
    ]
    assert all(
        client.authenticate_calls == [("operator", "saved-password")]
        for client in client_type.instances
    )


@pytest.mark.asyncio
async def test_zeroconf_pro_wifi_respects_static_ethernet_entry(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static-host opt-out also applies to another interface of the same Pro."""
    _prepare_configured_pro_discovery(flow, config_entry_mock, monkeypatch)
    config_entry_mock.data[CONF_USE_STATIC_HOST] = True
    client_type = install_client()

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )

    assert result["reason"] == "already_configured"
    assert client_type.instances == []
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize(
    "current_host_error",
    [
        asyncio.TimeoutError(),
        NanoKVMAuthenticationFailure("expired credentials"),
        _fingerprint_error(),
        None,
    ],
)
@pytest.mark.asyncio
async def test_zeroconf_pro_wifi_replaces_unusable_ethernet_with_verified_identity(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
    current_host_error: Exception | None,
) -> None:
    """Verified alternate-address discovery still recovers an unusable saved host."""
    _prepare_configured_pro_discovery(flow, config_entry_mock, monkeypatch)
    install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        ClientScenario(auth_error=current_host_error, device_key="other-device"),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )

    assert result["reason"] == "already_configured"
    updated_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data[CONF_HOST] == "192.0.2.21"
    assert updated_data[CONF_SSL_FINGERPRINT] == "AABB"
    assert updated_data[CONF_SSH_HOST_KEY] == "192.0.2.21 ssh-ed25519 ZHVtbXk="
    assert updated_data["preferred_host"] == "192.0.2.20"


@pytest.mark.parametrize("announcement_host", ["192.0.2.20", "192.0.2.21"])
@pytest.mark.asyncio
async def test_zeroconf_pro_returns_to_preferred_ethernet_while_wifi_works(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
    announcement_host: str,
) -> None:
    """Recovery of the preferred address takes priority over working fallback Wi-Fi."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, host="192.0.2.21"
    )
    config_entry_mock.data.update({
        "preferred_host": "https://192.0.2.20",
        CONF_SSH_HOST_KEY: "192.0.2.21 ssh-ed25519 ZHVtbXk=",
    })
    install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host=announcement_host)
    )

    assert result["reason"] == "already_configured"
    updated_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data[CONF_HOST] == "https://192.0.2.20"
    assert updated_data["preferred_host"] == "https://192.0.2.20"
    assert updated_data[CONF_SSH_HOST_KEY] == "192.0.2.20 ssh-ed25519 ZHVtbXk="
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
        config_entry_mock.entry_id
    )


@pytest.mark.parametrize(
    "preferred_scenario",
    [ClientScenario(auth_error=asyncio.TimeoutError()), ClientScenario(device_key="other-device")],
)
@pytest.mark.asyncio
async def test_zeroconf_pro_keeps_wifi_if_preferred_host_is_unverified(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
    preferred_scenario: ClientScenario,
) -> None:
    """Failed or wrong-device probes cannot interrupt working fallback Wi-Fi."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, host="192.0.2.21"
    )
    config_entry_mock.data["preferred_host"] = "https://192.0.2.20"
    config_entry_mock.data[CONF_SSH_HOST_KEY] = "192.0.2.21 ssh-ed25519 ZHVtbXk="
    client_type = install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        preferred_scenario,
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.22")
    )

    assert result["reason"] == "already_configured"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    assert [str(client.url) for client in client_type.instances] == [
        "https://192.0.2.22/api/", "https://192.0.2.20/api/", "https://192.0.2.21/api/"
    ]


@pytest.mark.asyncio
async def test_zeroconf_pro_fallback_preserves_previously_saved_preferred_host(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later fallback address must not become the new preferred address."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, host="192.0.2.21"
    )
    config_entry_mock.data["preferred_host"] = "https://192.0.2.20"
    install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        ClientScenario(auth_error=asyncio.TimeoutError()),
        ClientScenario(auth_error=asyncio.TimeoutError()),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.22")
    )

    assert result["reason"] == "already_configured"
    updated_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data[CONF_HOST] == "192.0.2.22"
    assert updated_data["preferred_host"] == "https://192.0.2.20"


@pytest.mark.asyncio
async def test_zeroconf_pro_matches_saved_preferred_host_without_registry_hostname(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saved preferred IP also lets recovery reuse TLS trust without a name hint."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, hostname=None, host="192.0.2.21"
    )
    config_entry_mock.data["preferred_host"] = "https://192.0.2.20"
    install_client(ClientScenario(device_key="verified-key", required_fingerprint="AABB"))

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.20")
    )

    assert result["reason"] == "already_configured"
    updated = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated[CONF_HOST] == "https://192.0.2.20"
    assert updated[CONF_SSL_FINGERPRINT] == "AABB"


@pytest.mark.parametrize("trust", [False, True])
@pytest.mark.asyncio
async def test_zeroconf_pro_fallback_ssh_confirmation_preserves_preferred_host(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
    trust: bool,
) -> None:
    """A damaged SSH key requires approval before changing either saved address."""
    _prepare_configured_pro_discovery(flow, config_entry_mock, monkeypatch)
    config_entry_mock.data[CONF_SSH_HOST_KEY] = "invalid-known-hosts-line"
    install_client(
        ClientScenario(device_key="verified-key", required_fingerprint="AABB"),
        ClientScenario(auth_error=asyncio.TimeoutError()),
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_prepare_ssh_host_key",
        AsyncMock(return_value=config_flow_module.SSHHostKey(
            host="192.0.2.21",
            known_hosts_line="192.0.2.21 ssh-ed25519 ZHVtbXk=",
            fingerprint="SHA256:trusted",
        )),
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )
    assert result["step_id"] == "ssh_host_key"
    assert flow.data["preferred_host"] == "192.0.2.20"
    flow.hass.config_entries.async_update_entry.assert_not_called()

    result = await flow.async_step_ssh_host_key({CONF_TRUST_SSH_HOST_KEY: trust})
    if trust:
        assert result["reason"] == "reconfigure_successful"
        updated = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated[CONF_HOST] == "192.0.2.21"
        assert updated["preferred_host"] == "192.0.2.20"
    else:
        assert result["reason"] == "ssh_host_key_not_trusted"
        assert config_entry_mock.data[CONF_HOST] == "192.0.2.20"
        assert "preferred_host" not in config_entry_mock.data
        flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_pro_hostname_match_cannot_replace_another_device(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname hint cannot override a different authenticated device identity."""
    _prepare_configured_pro_discovery(flow, config_entry_mock, monkeypatch)
    install_client(
        ClientScenario(device_key="other-device", required_fingerprint="AABB")
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_pro_failed_auth_cannot_change_ethernet_host(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored credentials failing on Wi-Fi do not authorize a host update."""
    _prepare_configured_pro_discovery(flow, config_entry_mock, monkeypatch)
    install_client(
        ClientScenario(
            auth_error=NanoKVMAuthenticationFailure("credentials rejected"),
            required_fingerprint="AABB",
        )
    )

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )

    assert result["reason"] == "already_configured"
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_pro_unrelated_hostname_requires_its_own_tls_trust(
    flow: NanoKVMConfigFlow,
    config_entry_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated SSH announcement cannot borrow an existing Pro's credentials."""
    _prepare_configured_pro_discovery(
        flow, config_entry_mock, monkeypatch, hostname="other-nanokvm"
    )
    client_type = install_client(ClientScenario(required_fingerprint="AABB"))

    result = await flow.async_step_zeroconf(
        _discovery_info("_ssh._tcp.local.", host="192.0.2.21")
    )

    assert result["step_id"] == "ssl_fingerprint"
    assert client_type.instances[0].ssl_fingerprint is None
    assert client_type.instances[0].authenticate_calls == []
    assert CONF_PASSWORD not in flow.data
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_duplicate_pro_certificate_prompt_is_aborted(
    flow: NanoKVMConfigFlow,
    hass_mock: MagicMock,
    install_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated SSH announcement does not create another certificate flow."""
    install_client(
        ClientScenario(
            auth_error=aiohttp.ClientConnectorCertificateError(
                None, OSError("self-signed certificate")
            )
        )
    )
    flow.context = {"source": "zeroconf"}
    flow.handler = DOMAIN
    flow.flow_id = "duplicate-flow"
    hass_mock.config_entries.flow.async_progress_by_handler.return_value = [
        {
            "flow_id": "first-flow",
            "context": {
                "source": "zeroconf",
                "unique_id": "nano-kvm.local.",
            },
        }
    ]
    fetch = AsyncMock(return_value="AABB")
    monkeypatch.setattr(config_flow_module, "async_fetch_remote_fingerprint", fetch)

    with pytest.raises(AbortFlow, match="already_in_progress"):
        await flow.async_step_zeroconf(_discovery_info("_ssh._tcp.local."))

    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("unreachable"),
        asyncio.TimeoutError(),
        NanoKVMError("not a NanoKVM"),
    ],
)
async def test_zeroconf_connection_errors_abort_discovery(
    flow: NanoKVMConfigFlow,
    install_client,
    error: Exception,
) -> None:
    """Unverified network and API failures are ignored as invalid discovery."""
    install_client(ClientScenario(auth_error=error))

    result = await flow.async_step_zeroconf(_discovery_info())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_zeroconf_duplicate_identity_propagates_framework_abort(
    flow: NanoKVMConfigFlow,
    install_client,
) -> None:
    """The Home Assistant duplicate guard terminates a new discovery flow."""
    install_client(ClientScenario(device_key="duplicate-key"))
    _prepare_new_discovery(flow)
    flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")

    with pytest.raises(AbortFlow, match="already_configured"):
        await flow.async_step_zeroconf(_discovery_info())
