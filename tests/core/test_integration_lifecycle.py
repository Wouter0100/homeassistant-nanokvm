"""Tests for NanoKVM config-entry setup and unload lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from nanokvm.client import NanoKVMAuthenticationFailure, NanoKVMError
import pytest
from yarl import URL

import custom_components.nanokvm as nanokvm_module
from custom_components.nanokvm import PLATFORMS, async_setup_entry, async_unload_entry
from custom_components.nanokvm.const import (
    CONF_SSL_FINGERPRINT,
    CONF_USE_STATIC_HOST,
    DOMAIN,
)


@dataclass(slots=True)
class SetupScenario:
    """Script one setup client's authentication and device-info behavior."""

    auth_error: Exception | None = None
    info_error: Exception | None = None
    device_info: object | None = None


class SetupClient:
    """Minimal async NanoKVM client used at the integration boundary."""

    scenarios: list[SetupScenario] = []
    instances: list[SetupClient] = []

    def __init__(
        self,
        url: str,
        *,
        ssl_fingerprint: str | None = None,
        **_: object,
    ) -> None:
        if not self.scenarios:
            raise AssertionError(f"No setup scenario configured for {url}")
        self.scenario = self.scenarios.pop(0)
        self.url = URL(url)
        self.ssl_fingerprint = ssl_fingerprint
        self.authenticate_calls: list[tuple[str, str]] = []
        self.entered = False
        self.exited = False
        self.instances.append(self)

    async def __aenter__(self) -> SetupClient:
        """Enter the fake setup client context."""
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the fake setup client context."""
        self.exited = True

    async def authenticate(self, username: str, password: str) -> None:
        """Record credentials and raise any scripted authentication error."""
        self.authenticate_calls.append((username, password))
        if self.scenario.auth_error is not None:
            raise self.scenario.auth_error

    async def get_info(self) -> object:
        """Return complete initial device info or raise its scripted error."""
        if self.scenario.info_error is not None:
            raise self.scenario.info_error
        if self.scenario.device_info is not None:
            return self.scenario.device_info
        return SimpleNamespace(device_key="device-key", application="1.0.0")


@pytest.fixture
def install_setup_client(monkeypatch: pytest.MonkeyPatch):
    """Install a clean setup client and return its scenario setter."""

    def install(*scenarios: SetupScenario) -> type[SetupClient]:
        SetupClient.scenarios = list(scenarios)
        SetupClient.instances = []
        monkeypatch.setattr(nanokvm_module, "NanoKVMClient", SetupClient)
        return SetupClient

    return install


def _install_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refresh_error: Exception | None = None,
) -> tuple[MagicMock, SimpleNamespace]:
    """Install a coordinator factory with a scripted first refresh."""
    refresh = AsyncMock(side_effect=refresh_error)
    coordinator = SimpleNamespace(
        async_config_entry_first_refresh=refresh,
        is_pro_hardware=False,
    )
    factory = MagicMock(return_value=coordinator)
    monkeypatch.setattr(nanokvm_module, "NanoKVMDataUpdateCoordinator", factory)
    return factory, coordinator


def _prepare_hass(hass_mock: MagicMock) -> None:
    """Complete the Home Assistant config-entry async boundary."""
    hass_mock.config_entries.async_forward_entry_setups = AsyncMock()
    hass_mock.config_entries.async_unload_platforms = AsyncMock(return_value=True)


@pytest.mark.asyncio
async def test_setup_entry_success_initializes_coordinator_platforms_and_services(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful entry is refreshed, stored, forwarded, and service-enabled."""
    _prepare_hass(hass_mock)
    config_entry_mock.data[CONF_USE_STATIC_HOST] = True
    client_type = install_setup_client(SetupScenario())
    coordinator_factory, coordinator = _install_coordinator(monkeypatch)
    media = SimpleNamespace(async_shutdown=AsyncMock())
    media_factory = MagicMock(return_value=media)
    monkeypatch.setattr(nanokvm_module, "NanoKVMMediaRuntime", media_factory)
    register_services = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_register_services", register_services)

    assert await async_setup_entry(hass_mock, config_entry_mock) is True

    client = client_type.instances[0]
    assert client.url == URL("http://nanokvm.local/api/")
    assert client.ssl_fingerprint is None
    assert client.authenticate_calls == [("admin", "password")]
    assert client.entered is True
    assert client.exited is True
    coordinator_factory.assert_called_once()
    assert coordinator_factory.call_args.args == (hass_mock, config_entry_mock)
    assert coordinator_factory.call_args.kwargs == {
        "client": client,
        "username": "admin",
        "password": "password",
        "device_info": client.scenario.device_info
        or SimpleNamespace(device_key="device-key", application="1.0.0"),
    }
    coordinator.async_config_entry_first_refresh.assert_awaited_once_with()
    media_factory.assert_called_once_with(coordinator, logger=nanokvm_module._LOGGER)
    assert coordinator.media is media
    assert hass_mock.data[DOMAIN][config_entry_mock.entry_id] is coordinator
    hass_mock.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        config_entry_mock,
        PLATFORMS,
    )
    register_services.assert_called_once_with(hass_mock)


@pytest.mark.asyncio
async def test_setup_entry_explicit_https_passes_stored_fingerprint(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit HTTPS entry uses its persisted certificate pin."""
    _prepare_hass(hass_mock)
    config_entry_mock.data["host"] = "https://nanokvm.local"
    config_entry_mock.data[CONF_SSL_FINGERPRINT] = "AABB"
    client_type = install_setup_client(SetupScenario())
    _install_coordinator(monkeypatch)
    monkeypatch.setattr(nanokvm_module, "async_register_services", MagicMock())

    assert await async_setup_entry(hass_mock, config_entry_mock) is True
    assert len(client_type.instances) == 1
    assert client_type.instances[0].url.scheme == "https"
    assert client_type.instances[0].ssl_fingerprint == "AABB"


@pytest.mark.asyncio
async def test_setup_entry_authentication_failure_requests_reauth(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid stored credentials map to Home Assistant's reauth exception."""
    _prepare_hass(hass_mock)
    client_type = install_setup_client(
        SetupScenario(auth_error=NanoKVMAuthenticationFailure("invalid")),
        SetupScenario(),
    )
    coordinator_factory, _ = _install_coordinator(monkeypatch)

    with pytest.raises(ConfigEntryAuthFailed, match="Authentication failed"):
        await async_setup_entry(hass_mock, config_entry_mock)

    assert len(client_type.instances) == 1
    coordinator_factory.assert_not_called()
    assert DOMAIN not in hass_mock.data


@pytest.mark.asyncio
async def test_setup_entry_http_certificate_error_falls_back_to_https(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect certificate failure on HTTP retries the direct HTTPS candidate."""
    _prepare_hass(hass_mock)
    mismatch = aiohttp.ServerFingerprintMismatch(
        b"expected", b"received", "nanokvm.local", 443
    )
    client_type = install_setup_client(
        SetupScenario(auth_error=mismatch),
        SetupScenario(),
    )
    coordinator_factory, _ = _install_coordinator(monkeypatch)
    monkeypatch.setattr(nanokvm_module, "async_register_services", MagicMock())

    assert await async_setup_entry(hass_mock, config_entry_mock) is True
    assert [client.url.scheme for client in client_type.instances] == ["http", "https"]
    assert coordinator_factory.call_args.kwargs["client"] is client_type.instances[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "certificate_error",
    [
        aiohttp.ServerFingerprintMismatch(
            b"expected", b"received", "nanokvm.local", 443
        ),
        aiohttp.ClientConnectorCertificateError(None, ValueError("certificate")),
    ],
)
async def test_setup_entry_final_certificate_error_requests_reauth(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
    certificate_error: Exception,
) -> None:
    """A certificate failure on the final transport maps to auth recovery."""
    _prepare_hass(hass_mock)
    config_entry_mock.data["host"] = "https://nanokvm.local"
    install_setup_client(SetupScenario(auth_error=certificate_error))
    coordinator_factory, _ = _install_coordinator(monkeypatch)

    with pytest.raises(ConfigEntryAuthFailed, match="SSL certificate changed"):
        await async_setup_entry(hass_mock, config_entry_mock)

    coordinator_factory.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_exhausted_connection_candidates_is_not_ready(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both unavailable transports defer entry setup with the final error."""
    _prepare_hass(hass_mock)
    first = aiohttp.ClientConnectionError("http unavailable")
    last = aiohttp.ClientConnectionError("https unavailable")
    client_type = install_setup_client(
        SetupScenario(auth_error=first),
        SetupScenario(auth_error=last),
    )
    coordinator_factory, _ = _install_coordinator(monkeypatch)

    with pytest.raises(ConfigEntryNotReady) as error:
        await async_setup_entry(hass_mock, config_entry_mock)

    assert error.value.__cause__ is last
    assert [client.url.scheme for client in client_type.instances] == ["http", "https"]
    coordinator_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError(),
        aiohttp.ClientPayloadError("invalid response"),
        NanoKVMError("api failure"),
    ],
)
async def test_setup_entry_non_connection_errors_stop_fallback(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Timeout, response, and API errors defer setup without probing HTTPS."""
    _prepare_hass(hass_mock)
    client_type = install_setup_client(
        SetupScenario(info_error=error),
        SetupScenario(),
    )
    coordinator_factory, _ = _install_coordinator(monkeypatch)

    with pytest.raises(ConfigEntryNotReady) as setup_error:
        await async_setup_entry(hass_mock, config_entry_mock)

    assert setup_error.value.__cause__ is error
    assert len(client_type.instances) == 1
    coordinator_factory.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_first_refresh_failure_stops_platform_forwarding(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator readiness must be established before storage and forwarding."""
    _prepare_hass(hass_mock)
    install_setup_client(SetupScenario())
    refresh_error = ConfigEntryNotReady("poll failed")
    _install_coordinator(monkeypatch, refresh_error=refresh_error)
    register_services = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_register_services", register_services)

    with pytest.raises(ConfigEntryNotReady, match="poll failed"):
        await async_setup_entry(hass_mock, config_entry_mock)

    assert DOMAIN not in hass_mock.data
    hass_mock.config_entries.async_forward_entry_setups.assert_not_awaited()
    register_services.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_preserves_existing_domain_coordinators(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    install_setup_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a second entry does not replace already configured coordinators."""
    _prepare_hass(hass_mock)
    existing = object()
    hass_mock.data = {DOMAIN: {"existing-entry": existing}}
    install_setup_client(SetupScenario())
    _, coordinator = _install_coordinator(monkeypatch)
    monkeypatch.setattr(nanokvm_module, "async_register_services", MagicMock())

    assert await async_setup_entry(hass_mock, config_entry_mock) is True
    assert hass_mock.data[DOMAIN] == {
        "existing-entry": existing,
        config_entry_mock.entry_id: coordinator,
    }


@pytest.mark.asyncio
async def test_unload_failure_keeps_entry_and_services(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed platform unload leaves integration ownership untouched."""
    _prepare_hass(hass_mock)
    coordinator = object()
    hass_mock.data = {DOMAIN: {config_entry_mock.entry_id: coordinator}}
    hass_mock.config_entries.async_unload_platforms.return_value = False
    unregister = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_unregister_services", unregister)

    assert await async_unload_entry(hass_mock, config_entry_mock) is False
    assert hass_mock.data[DOMAIN][config_entry_mock.entry_id] is coordinator
    unregister.assert_not_called()


@pytest.mark.asyncio
async def test_unload_one_of_multiple_entries_keeps_global_services(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global services remain registered while another NanoKVM entry exists."""
    _prepare_hass(hass_mock)
    remaining = object()
    hass_mock.data = {
        DOMAIN: {
            config_entry_mock.entry_id: object(),
            "remaining-entry": remaining,
        }
    }
    unregister = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_unregister_services", unregister)

    assert await async_unload_entry(hass_mock, config_entry_mock) is True
    hass_mock.config_entries.async_unload_platforms.assert_awaited_once_with(
        config_entry_mock,
        PLATFORMS,
    )
    assert hass_mock.data[DOMAIN] == {"remaining-entry": remaining}
    unregister.assert_not_called()


@pytest.mark.asyncio
async def test_unload_last_entry_unregisters_services_and_domain(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final unload removes global services and the integration data bucket."""
    _prepare_hass(hass_mock)
    hass_mock.data = {DOMAIN: {config_entry_mock.entry_id: object()}}
    unregister = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_unregister_services", unregister)

    assert await async_unload_entry(hass_mock, config_entry_mock) is True
    assert DOMAIN not in hass_mock.data
    unregister.assert_called_once_with(hass_mock)


@pytest.mark.asyncio
async def test_unload_missing_domain_is_idempotent(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated unload safely applies the empty-domain cleanup path."""
    _prepare_hass(hass_mock)
    hass_mock.data = {}
    unregister = MagicMock()
    monkeypatch.setattr(nanokvm_module, "async_unregister_services", unregister)

    assert await async_unload_entry(hass_mock, config_entry_mock) is True
    unregister.assert_called_once_with(hass_mock)
    assert DOMAIN not in hass_mock.data
