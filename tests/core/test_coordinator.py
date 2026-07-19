"""Behavior tests for the NanoKVM data update coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import pytest
from yarl import URL

from homeassistant.const import CONF_HOST
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from nanokvm.client import (
    NanoKVMApiError,
    NanoKVMAuthenticationFailure,
    NanoKVMClient,
    NanoKVMError,
    NanoKVMNotSupportedError,
)
from nanokvm.models import (
    GetCdRomRsp,
    GetGpioRsp,
    GetHardwareRsp,
    GetHdmiCaptureRsp,
    GetHdmiPassthroughRsp,
    GetHdmiStateRsp,
    GetHidModeRsp,
    GetHostnameRsp,
    GetInfoRsp,
    GetLcdTimeFormatRsp,
    GetLedStripRsp,
    GetLowPowerRsp,
    GetMdnsStateRsp,
    GetMountedImageRsp,
    GetMouseJigglerRsp,
    GetOLEDRsp,
    GetSSHStateRsp,
    GetStaticIPRsp,
    GetTailscaleStatusRsp,
    GetTimeStatusRsp,
    GetVersionRsp,
    GetVirtualDeviceRsp,
    GetWifiRsp,
    HidMode,
    HWVersion,
    IPInfo,
    LcdTimeFormat,
    MouseJigglerMode,
    TailscaleState,
)

import custom_components.nanokvm.coordinator as coordinator_module
from custom_components.nanokvm.const import (
    CONF_SSL_FINGERPRINT,
    SIGNAL_NEW_MEDIA_ENTITIES,
    SIGNAL_NEW_NETWORK_ENTITIES,
    SIGNAL_NEW_SSH_SENSORS,
    SIGNAL_NEW_SSH_SWITCHES,
)
from custom_components.nanokvm.coordinator import (
    NanoKVMDataUpdateCoordinator,
    _format_timeout_error,
    _is_auth_failure,
    _is_invalid_file_content_error,
)
from custom_components.nanokvm.ssh_metrics import SSHMetricsSnapshot


@pytest.fixture
def coordinator(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
) -> NanoKVMDataUpdateCoordinator:
    """Return a coordinator with a mocked device boundary."""
    client = MagicMock(spec=NanoKVMClient)
    client.url = URL("http://nanokvm.local/api/")
    client.token = "token"
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return NanoKVMDataUpdateCoordinator(
        hass_mock,
        config_entry_mock,
        client=client,
        username="admin",
        password="password",
        device_info=_device_info(),
    )


def _device_info(
    *,
    application: str = "2.2.2",
    ips: list[IPInfo] | None = None,
) -> GetInfoRsp:
    """Build a complete NanoKVM device-info response."""
    return GetInfoRsp(
        ips=[] if ips is None else ips,
        mdns="nanokvm.local",
        image="2026-07-12",
        application=application,
        deviceKey="test-device",
    )


def _response_error(status: int) -> aiohttp.ClientResponseError:
    """Build an aiohttp response error with a printable request URL."""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url=URL("http://nanokvm.local/api/vm/info")),
        history=(),
        status=status,
        message="response failure",
    )


def _api_error(
    *,
    code: int = -1,
    message: str = "device error",
) -> NanoKVMApiError:
    """Build a NanoKVM API-level error."""
    return NanoKVMApiError(message, code=code, msg=message)


def _candidate_client(
    url: str,
    *,
    authenticate_error: Exception | None = None,
) -> MagicMock:
    """Build a candidate transport client."""
    candidate = MagicMock(spec=NanoKVMClient)
    candidate.url = URL(url)
    candidate.token = "candidate-token"
    candidate.__aenter__ = AsyncMock(return_value=candidate)
    candidate.__aexit__ = AsyncMock(return_value=None)
    candidate.authenticate = AsyncMock(side_effect=authenticate_error)
    return candidate


def _configure_required_client_responses(
    client: MagicMock,
    *,
    hardware: HWVersion | None,
) -> dict[str, object]:
    """Configure complete core endpoint responses for one hardware model."""
    responses: dict[str, object] = {
        "device_info": _device_info(),
        "hostname_info": GetHostnameRsp(hostname="nano"),
        "hardware_info": None if hardware is None else GetHardwareRsp(version=hardware),
        "gpio_info": GetGpioRsp(pwr=True, hdd=False),
        "virtual_device_info": GetVirtualDeviceRsp(network=True, disk=True),
        "ssh_state": GetSSHStateRsp(enabled=True),
        "mdns_state": GetMdnsStateRsp(enabled=True),
        "hid_mode": GetHidModeRsp(mode=HidMode.NORMAL),
        "oled_info": GetOLEDRsp(exist=True, sleep=60),
        "wifi_status": GetWifiRsp(supported=True, connected=True, ssid="lab"),
        "hdmi_state": GetHdmiStateRsp(enabled=True),
        "mouse_jiggler_state": GetMouseJigglerRsp(
            enabled=True,
            mode=MouseJigglerMode.ABSOLUTE,
        ),
        "swap_size": 256,
        "tailscale_status": GetTailscaleStatusRsp(
            state=TailscaleState.RUNNING,
            name="nano",
            ip="100.64.0.10",
            account="owner@example.com",
        ),
    }
    client.get_info.return_value = responses["device_info"]
    client.get_hostname.return_value = responses["hostname_info"]
    client.get_hardware.return_value = responses["hardware_info"]
    client.get_gpio.return_value = responses["gpio_info"]
    client.get_virtual_device_status.return_value = responses["virtual_device_info"]
    client.get_ssh_state.return_value = responses["ssh_state"]
    client.get_mdns_state.return_value = responses["mdns_state"]
    client.get_hid_mode.return_value = responses["hid_mode"]
    client.get_oled_info.return_value = responses["oled_info"]
    client.get_wifi_status.return_value = responses["wifi_status"]
    client.get_hdmi_state.return_value = responses["hdmi_state"]
    client.get_mouse_jiggler_state.return_value = responses["mouse_jiggler_state"]
    client.get_swap_size.return_value = responses["swap_size"]
    client.get_tailscale_status.return_value = responses["tailscale_status"]
    return responses


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NanoKVMAuthenticationFailure("invalid"), True),
        (_response_error(401), True),
        (_response_error(403), False),
        (NanoKVMError("other"), False),
    ],
)
def test_auth_failure_classification(error: Exception, expected: bool) -> None:
    """Only explicit authentication failures and HTTP 401 require reauth."""
    assert _is_auth_failure(error) is expected


def test_invalid_file_content_error_requires_matching_code_and_message() -> None:
    """The optional OLED-file policy matches the complete API error signature."""
    matching = _api_error(code=-2, message="invalid file content")
    wrong_code = _api_error(code=-1, message="invalid file content")
    wrong_message = _api_error(code=-2, message="other")

    assert _is_invalid_file_content_error(matching) is True
    assert _is_invalid_file_content_error(wrong_code) is False
    assert _is_invalid_file_content_error(wrong_message) is False
    assert _format_timeout_error("testing") == "Timed out testing after 10 seconds"


async def test_update_data_retries_then_returns_success(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient update failures are retried with the configured delay."""
    expected = {"ready": True}
    coordinator._async_fetch_once = AsyncMock(
        side_effect=[UpdateFailed("first"), UpdateFailed("second"), expected]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", sleep)

    assert await coordinator._async_update_data() == expected
    assert coordinator._async_fetch_once.await_count == 3
    assert sleep.await_args_list == [call(1), call(1)]


async def test_update_data_reraises_final_failure(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third update failure is returned to Home Assistant."""
    final_error = UpdateFailed("final")
    coordinator._async_fetch_once = AsyncMock(
        side_effect=[UpdateFailed("first"), UpdateFailed("second"), final_error]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", sleep)

    with pytest.raises(UpdateFailed, match="final") as captured:
        await coordinator._async_update_data()

    assert captured.value is final_error
    assert sleep.await_count == 2


async def test_fetch_once_http_fingerprint_error_can_fail_over(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An HTTP probe redirected to mismatched TLS may switch transports."""
    mismatch = aiohttp.ServerFingerprintMismatch(b"expected", b"actual", "nano", 443)
    expected = {"transport": "https"}
    coordinator._async_fetch_with_client = AsyncMock(side_effect=[mismatch, expected])
    coordinator._async_failover_client = AsyncMock(return_value=True)

    assert await coordinator._async_fetch_once() == expected
    coordinator._async_failover_client.assert_awaited_once_with(mismatch)


async def test_fetch_once_https_fingerprint_error_starts_reauthentication(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """A fingerprint change on active HTTPS is an entry-auth failure."""
    coordinator.client.url = URL("https://nanokvm.local/api/")
    mismatch = aiohttp.ServerFingerprintMismatch(b"expected", b"actual", "nano", 443)
    coordinator._async_fetch_with_client = AsyncMock(side_effect=mismatch)
    coordinator._async_failover_client = AsyncMock()

    with pytest.raises(ConfigEntryAuthFailed, match="SSL certificate changed"):
        await coordinator._async_fetch_once()

    coordinator._async_failover_client.assert_not_awaited()


async def test_fetch_once_connection_failure_without_fallback_is_update_failed(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An exhausted connection failover remains a retryable update failure."""
    disconnected = aiohttp.ServerDisconnectedError("gone")
    coordinator._async_fetch_with_client = AsyncMock(side_effect=disconnected)
    coordinator._async_failover_client = AsyncMock(return_value=False)

    with pytest.raises(UpdateFailed, match="Error communicating with NanoKVM"):
        await coordinator._async_fetch_once()


async def test_fetch_once_reauthenticates_then_returns_data(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An expired token is replaced before retrying the fetch once."""
    expected = {"reauthenticated": True}
    expired = NanoKVMAuthenticationFailure("expired")
    coordinator._async_fetch_with_client = AsyncMock(side_effect=[expired, expected])
    coordinator._async_reauthenticate_client = AsyncMock()

    assert await coordinator._async_fetch_once() == expected
    coordinator._async_reauthenticate_client.assert_awaited_once_with(expired)


@pytest.mark.parametrize(
    ("second_error", "expected_exception", "message"),
    [
        (
            _response_error(401),
            ConfigEntryAuthFailed,
            "Stored NanoKVM credentials are no longer valid",
        ),
        (_response_error(500), UpdateFailed, "HTTP error with NanoKVM"),
    ],
)
async def test_fetch_once_maps_error_after_reauthentication(
    coordinator: NanoKVMDataUpdateCoordinator,
    second_error: Exception,
    expected_exception: type[Exception],
    message: str,
) -> None:
    """The retry after reauthentication preserves auth and HTTP contracts."""
    coordinator._async_fetch_with_client = AsyncMock(
        side_effect=[NanoKVMAuthenticationFailure("expired"), second_error]
    )
    coordinator._async_reauthenticate_client = AsyncMock()

    with pytest.raises(expected_exception, match=message):
        await coordinator._async_fetch_once()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (_response_error(500), "HTTP error with NanoKVM"),
        (asyncio.TimeoutError(), "Timed out communicating with NanoKVM"),
        (NanoKVMError("broken response"), "Error communicating with NanoKVM"),
    ],
)
async def test_fetch_once_maps_non_authentication_failures(
    coordinator: NanoKVMDataUpdateCoordinator,
    error: Exception,
    message: str,
) -> None:
    """HTTP, timeout, and library errors retain stable update messages."""
    coordinator._async_fetch_with_client = AsyncMock(side_effect=error)

    with pytest.raises(UpdateFailed, match=message):
        await coordinator._async_fetch_once()


async def test_fetch_with_client_authenticates_and_runs_fetch_groups(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An unauthenticated poll runs core, storage, optional, and snapshot stages."""
    coordinator.client.token = None
    coordinator._async_fetch_core_data = AsyncMock()
    coordinator._async_fetch_storage_data = AsyncMock()
    coordinator._async_refresh_ssh_data = AsyncMock()
    coordinator._async_maybe_create_network_entities = MagicMock()
    coordinator._async_maybe_create_media_entities = MagicMock()
    coordinator._async_schedule_app_version_refresh = MagicMock()
    coordinator._build_update_data = MagicMock(return_value={"ready": True})

    assert await coordinator._async_fetch_with_client() == {"ready": True}
    coordinator.client.authenticate.assert_awaited_once_with("admin", "password")
    coordinator._async_fetch_core_data.assert_awaited_once_with()
    coordinator._async_fetch_storage_data.assert_awaited_once_with()
    coordinator._async_refresh_ssh_data.assert_awaited_once_with()
    coordinator._async_maybe_create_network_entities.assert_called_once_with()
    coordinator._async_maybe_create_media_entities.assert_called_once_with()
    coordinator._async_schedule_app_version_refresh.assert_called_once_with()


async def test_reauthentication_rejects_invalid_stored_credentials(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate authentication failure starts the HA reauth flow."""
    candidate = _candidate_client(
        "http://nanokvm.local/api/",
        authenticate_error=NanoKVMAuthenticationFailure("invalid"),
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    with pytest.raises(ConfigEntryAuthFailed, match="Stored NanoKVM credentials"):
        await coordinator._async_reauthenticate_client_locked(_response_error(401))


async def test_reauthentication_maps_non_auth_http_error(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-auth HTTP response from authentication is retryable."""
    candidate = _candidate_client(
        "http://nanokvm.local/api/",
        authenticate_error=_response_error(500),
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    with pytest.raises(UpdateFailed, match="Reauthentication failed"):
        await coordinator._async_reauthenticate_client_locked(_response_error(401))


async def test_reauthentication_fingerprint_redirect_tries_https(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint mismatch reached through HTTP continues to HTTPS."""
    mismatch = aiohttp.ServerFingerprintMismatch(b"expected", b"actual", "nano", 443)
    http_candidate = _candidate_client(
        "http://nanokvm.local/api/",
        authenticate_error=mismatch,
    )
    https_candidate = _candidate_client("https://nanokvm.local/api/")
    factory = MagicMock(side_effect=[http_candidate, https_candidate])
    monkeypatch.setattr(coordinator_module, "NanoKVMClient", factory)

    await coordinator._async_reauthenticate_client_locked(_response_error(401))

    assert coordinator.client is https_candidate
    assert factory.call_count == 2


async def test_reauthentication_https_fingerprint_failure_is_auth_failed(
    coordinator: NanoKVMDataUpdateCoordinator,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint mismatch on an explicit HTTPS target requires confirmation."""
    config_entry_mock.data[CONF_HOST] = "https://nanokvm.local"
    coordinator.client.url = URL("https://nanokvm.local/api/")
    mismatch = aiohttp.ServerFingerprintMismatch(b"expected", b"actual", "nano", 443)
    candidate = _candidate_client(
        "https://nanokvm.local/api/",
        authenticate_error=mismatch,
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    with pytest.raises(ConfigEntryAuthFailed, match="SSL certificate changed"):
        await coordinator._async_reauthenticate_client_locked(_response_error(401))


@pytest.mark.parametrize(
    ("original_error", "candidate_error", "message"),
    [
        (_response_error(401), asyncio.TimeoutError(), "Timed out reauthenticating"),
        (RuntimeError("token"), asyncio.TimeoutError(), "Timed out authenticating"),
        (_response_error(401), NanoKVMError("broken"), "Reauthentication failed"),
        (RuntimeError("token"), NanoKVMError("broken"), "Authentication failed"),
    ],
)
async def test_reauthentication_maps_timeout_and_library_errors(
    coordinator: NanoKVMDataUpdateCoordinator,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    original_error: Exception,
    candidate_error: Exception,
    message: str,
) -> None:
    """Reauthentication messages distinguish refresh from initial authentication."""
    config_entry_mock.data[CONF_HOST] = "http://nanokvm.local"
    candidate = _candidate_client(
        "http://nanokvm.local/api/",
        authenticate_error=candidate_error,
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    with pytest.raises(UpdateFailed, match=message):
        await coordinator._async_reauthenticate_client_locked(original_error)


@pytest.mark.parametrize(
    ("original_error", "message"),
    [
        (_response_error(401), "Reauthentication failed"),
        (RuntimeError("token"), "Authentication failed"),
    ],
)
async def test_reauthentication_exhausts_connection_candidates(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    original_error: Exception,
    message: str,
) -> None:
    """Connection failures across both transports retain the correct context."""
    candidates = [
        _candidate_client(
            "http://nanokvm.local/api/",
            authenticate_error=aiohttp.ServerDisconnectedError("http"),
        ),
        _candidate_client(
            "https://nanokvm.local/api/",
            authenticate_error=aiohttp.ServerDisconnectedError("https"),
        ),
    ]
    monkeypatch.setattr(
        coordinator_module,
        "NanoKVMClient",
        MagicMock(side_effect=candidates),
    )

    with pytest.raises(UpdateFailed, match=message):
        await coordinator._async_reauthenticate_client_locked(original_error)


async def test_failover_replaces_client_after_authentication(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A working alternate transport becomes the shared client."""
    candidate = _candidate_client("https://nanokvm.local/api/")
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    assert (
        await coordinator._async_failover_client_locked(RuntimeError("disconnect"))
        is True
    )
    assert coordinator.client is candidate
    candidate.authenticate.assert_awaited_once_with("admin", "password")


async def test_failover_wrapper_delegates_to_serialized_implementation(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Public failover access delegates while owning the client lock."""
    original_error = RuntimeError("disconnect")
    coordinator._async_failover_client_locked = AsyncMock(return_value=True)

    assert await coordinator._async_failover_client(original_error) is True
    coordinator._async_failover_client_locked.assert_awaited_once_with(original_error)


@pytest.mark.parametrize(
    ("candidate_error", "expected_exception", "message"),
    [
        (
            NanoKVMAuthenticationFailure("invalid"),
            ConfigEntryAuthFailed,
            "Stored NanoKVM credentials",
        ),
        (
            aiohttp.ServerFingerprintMismatch(b"expected", b"actual", "nano", 443),
            ConfigEntryAuthFailed,
            "SSL certificate changed",
        ),
        (
            asyncio.TimeoutError(),
            UpdateFailed,
            "Timed out checking alternate NanoKVM API transport",
        ),
        (NanoKVMError("broken"), UpdateFailed, "Error communicating with NanoKVM"),
    ],
)
async def test_failover_maps_candidate_errors(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    candidate_error: Exception,
    expected_exception: type[Exception],
    message: str,
) -> None:
    """Alternate transport failures preserve auth, certificate, and retry contracts."""
    candidate = _candidate_client(
        "https://nanokvm.local/api/",
        authenticate_error=candidate_error,
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    with pytest.raises(expected_exception, match=message):
        await coordinator._async_failover_client_locked(RuntimeError("disconnect"))


async def test_failover_returns_false_when_connector_cannot_reach_alternate(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable alternate transport leaves the current client unchanged."""
    current = coordinator.client
    candidate = _candidate_client(
        "https://nanokvm.local/api/",
        authenticate_error=aiohttp.ClientConnectorError(None, OSError("refused")),
    )
    monkeypatch.setattr(
        coordinator_module, "NanoKVMClient", MagicMock(return_value=candidate)
    )

    assert (
        await coordinator._async_failover_client_locked(RuntimeError("disconnect"))
        is False
    )
    assert coordinator.client is current


async def test_failover_explicit_scheme_has_no_alternate(
    coordinator: NanoKVMDataUpdateCoordinator,
    config_entry_mock: MagicMock,
) -> None:
    """An explicitly configured transport has no implicit alternate."""
    config_entry_mock.data[CONF_HOST] = "http://nanokvm.local"

    assert (
        await coordinator._async_failover_client_locked(RuntimeError("disconnect"))
        is False
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NanoKVMNotSupportedError("unsupported"), None),
        (_api_error(), None),
        (_response_error(404), None),
    ],
)
async def test_optional_endpoint_suppresses_unavailable_responses(
    coordinator: NanoKVMDataUpdateCoordinator,
    error: Exception,
    expected: None,
) -> None:
    """Known optional-endpoint failures produce unavailable state."""
    endpoint = AsyncMock(side_effect=error)

    assert await coordinator._fetch_optional("/optional", endpoint) is expected


async def test_optional_endpoint_returns_value_and_reraises_other_http_errors(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Optional calls return success and do not hide non-404 HTTP failures."""
    endpoint = AsyncMock(return_value={"enabled": True})
    assert await coordinator._fetch_optional("/optional", endpoint) == {"enabled": True}

    endpoint.side_effect = _response_error(500)
    with pytest.raises(aiohttp.ClientResponseError):
        await coordinator._fetch_optional("/optional", endpoint)


async def test_oled_fetch_handles_only_missing_optional_file(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """The OLED helper suppresses only the known missing-file response."""
    oled = GetOLEDRsp(exist=True, sleep=60)
    coordinator.client.get_oled_info.return_value = oled
    assert await coordinator._fetch_oled_info() is oled

    coordinator.client.get_oled_info.side_effect = _api_error(
        code=-2,
        message="invalid file content",
    )
    assert await coordinator._fetch_oled_info() is None

    coordinator.client.get_oled_info.side_effect = _api_error()
    with pytest.raises(NanoKVMApiError):
        await coordinator._fetch_oled_info()


async def test_core_fetch_populates_pcie_state(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """PCIe polling assigns required and non-Pro optional endpoint state."""
    responses = _configure_required_client_responses(
        coordinator.client,
        hardware=HWVersion.PCIE,
    )

    await coordinator._async_fetch_core_data()

    for attribute in (
        "device_info",
        "hostname_info",
        "hardware_info",
        "gpio_info",
        "virtual_device_info",
        "ssh_state",
        "mdns_state",
        "hid_mode",
        "oled_info",
        "wifi_status",
        "hdmi_state",
        "mouse_jiggler_state",
        "swap_size",
        "tailscale_status",
    ):
        assert getattr(coordinator, attribute) == responses[attribute]
    assert coordinator.hdmi_capture is None
    assert coordinator.static_ip is None


async def test_core_fetch_skips_hardware_gated_endpoints_without_hardware(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Missing hardware identity clears all hardware-gated state."""
    _configure_required_client_responses(coordinator.client, hardware=None)
    coordinator.virtual_device_info = object()
    coordinator.hdmi_state = object()
    coordinator.swap_size = 256

    await coordinator._async_fetch_core_data()

    assert coordinator.virtual_device_info is None
    assert coordinator.hdmi_state is None
    assert coordinator.swap_size is None
    coordinator.client.get_virtual_device_status.assert_not_awaited()
    coordinator.client.get_hdmi_state.assert_not_awaited()
    coordinator.client.get_swap_size.assert_not_awaited()


async def test_core_fetch_populates_pro_optional_state(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Pro polling stores every supported Pro response."""
    _configure_required_client_responses(coordinator.client, hardware=HWVersion.PRO)
    expected = {
        "hdmi_capture": GetHdmiCaptureRsp(enabled=True),
        "hdmi_passthrough": GetHdmiPassthroughRsp(enabled=False),
        "low_power": GetLowPowerRsp(enabled=True),
        "led_strip": GetLedStripRsp(on=True, hor=20, ver=30, brightness=80),
        "lcd_time_format": GetLcdTimeFormatRsp(format=LcdTimeFormat.TWENTY_FOUR_HOUR),
        "time_status": GetTimeStatusRsp(isSynchronized=True, lastSyncTime=1234),
        "static_ip": GetStaticIPRsp(enabled=True, ip="192.0.2.50"),
    }
    coordinator.client.get_hdmi_capture.return_value = expected["hdmi_capture"]
    coordinator.client.get_hdmi_passthrough.return_value = expected["hdmi_passthrough"]
    coordinator.client.get_low_power.return_value = expected["low_power"]
    coordinator.client.get_led_strip.return_value = expected["led_strip"]
    coordinator.client.get_lcd_time_format.return_value = expected["lcd_time_format"]
    coordinator.client.get_time_status.return_value = expected["time_status"]
    coordinator.client.get_static_ip.return_value = expected["static_ip"]

    await coordinator._async_fetch_core_data()

    for attribute, value in expected.items():
        assert getattr(coordinator, attribute) == value
    assert coordinator.hdmi_state is None
    assert coordinator.swap_size is None


async def test_storage_fetch_reads_mounted_image_and_cdrom(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Normal HID mode reads mounted-image and supported CD-ROM state."""
    coordinator.hid_mode = GetHidModeRsp(mode=HidMode.NORMAL)
    coordinator.hardware_info = GetHardwareRsp(version=HWVersion.PCIE)
    mounted = GetMountedImageRsp(file="images/rescue.iso", cdrom=True, readOnly=True)
    cdrom = GetCdRomRsp(cdrom=1)
    coordinator.client.get_mounted_image.return_value = mounted
    coordinator.client.get_cdrom_status.return_value = cdrom

    await coordinator._async_fetch_storage_data()

    assert coordinator.mounted_image == mounted
    assert coordinator.cdrom_status == cdrom


async def test_storage_fetch_uses_default_when_mounted_image_fails(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """A mounted-image API error produces the documented empty state."""
    coordinator.hid_mode = GetHidModeRsp(mode=HidMode.NORMAL)
    coordinator.hardware_info = GetHardwareRsp(version=HWVersion.PCIE)
    coordinator.client.get_mounted_image.side_effect = _api_error()
    coordinator.client.get_cdrom_status.side_effect = NanoKVMNotSupportedError()

    await coordinator._async_fetch_storage_data()

    assert coordinator.mounted_image == GetMountedImageRsp(
        file="",
        cdrom=False,
        readOnly=False,
    )
    assert coordinator.cdrom_status is None


async def test_storage_fetch_skips_cdrom_for_pro_in_normal_hid_mode(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Normal HID on Pro reads media but skips the non-Pro CD-ROM endpoint."""
    coordinator.hid_mode = GetHidModeRsp(mode=HidMode.NORMAL)
    coordinator.hardware_info = GetHardwareRsp(version=HWVersion.PRO)
    mounted = GetMountedImageRsp(file="images/rescue.iso")
    coordinator.client.get_mounted_image.return_value = mounted

    await coordinator._async_fetch_storage_data()

    assert coordinator.mounted_image == mounted
    assert coordinator.cdrom_status is None
    coordinator.client.get_cdrom_status.assert_not_awaited()


@pytest.mark.parametrize(
    ("hardware", "expected_cdrom"),
    [
        (HWVersion.PCIE, GetCdRomRsp(cdrom=0)),
        (HWVersion.PRO, None),
    ],
)
async def test_storage_fetch_uses_defaults_outside_normal_hid_mode(
    coordinator: NanoKVMDataUpdateCoordinator,
    hardware: HWVersion,
    expected_cdrom: GetCdRomRsp | None,
) -> None:
    """Storage endpoints are skipped when HID mode cannot expose media."""
    coordinator.hid_mode = GetHidModeRsp(mode=HidMode.HID_ONLY)
    coordinator.hardware_info = GetHardwareRsp(version=hardware)

    await coordinator._async_fetch_storage_data()

    assert coordinator.mounted_image == GetMountedImageRsp(
        file="",
        cdrom=False,
        readOnly=False,
    )
    assert coordinator.cdrom_status == expected_cdrom
    coordinator.client.get_mounted_image.assert_not_awaited()


def test_build_update_data_returns_complete_state_snapshot(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """The coordinator snapshot includes every entity-facing API field."""
    keys = (
        "device_info",
        "hardware_info",
        "gpio_info",
        "virtual_device_info",
        "ssh_state",
        "mdns_state",
        "hid_mode",
        "oled_info",
        "wifi_status",
        "application_version_info",
        "mounted_image",
        "cdrom_status",
        "mouse_jiggler_state",
        "hdmi_state",
        "hdmi_capture",
        "hdmi_passthrough",
        "low_power",
        "led_strip",
        "lcd_time_format",
        "time_status",
        "static_ip",
        "swap_size",
        "tailscale_status",
        "hostname_info",
        "watchdog_enabled",
    )
    expected = {key: object() for key in keys}
    for key, value in expected.items():
        setattr(coordinator, key, value)

    assert coordinator._build_update_data() == expected


@pytest.mark.parametrize(
    ("application", "expected"),
    [
        ("", False),
        (" ", False),
        ("2.2.1", False),
        ("2.2.2", True),
        ("2.3.0", True),
        ("not a version", False),
    ],
)
def test_watchdog_capability_uses_application_version(
    coordinator: NanoKVMDataUpdateCoordinator,
    application: str,
    expected: bool,
) -> None:
    """Watchdog support begins at firmware 2.2.2 and rejects invalid versions."""
    coordinator.device_info = _device_info(application=application)

    assert coordinator.supports_watchdog is expected


@pytest.mark.parametrize(
    (
        "hardware",
        "is_pro",
        "non_pro_virtual",
        "hdmi",
        "swap",
        "cdrom",
    ),
    [
        (None, False, False, False, False, False),
        (HWVersion.ALPHA, False, True, False, True, True),
        (HWVersion.PCIE, False, True, True, True, True),
        (HWVersion.PRO, True, False, False, False, False),
    ],
)
def test_hardware_capability_properties(
    coordinator: NanoKVMDataUpdateCoordinator,
    hardware: HWVersion | None,
    is_pro: bool,
    non_pro_virtual: bool,
    hdmi: bool,
    swap: bool,
    cdrom: bool,
) -> None:
    """Hardware gates match the endpoints exposed for each product family."""
    coordinator.hardware_info = (
        None if hardware is None else GetHardwareRsp(version=hardware)
    )
    coordinator.virtual_device_info = (
        GetVirtualDeviceRsp(network=True)
        if hardware in (HWVersion.ALPHA, HWVersion.PCIE)
        else None
    )

    assert coordinator.is_pro_hardware is is_pro
    assert coordinator.supports_non_pro_virtual_device_controls is non_pro_virtual
    assert coordinator.supports_hdmi_endpoint is hdmi
    assert coordinator.supports_swap_size is swap
    assert coordinator.supports_cdrom_endpoint is cdrom


def test_active_network_connection_types_are_normalized_and_deduplicated(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Only addressed connection types contribute dynamic entities."""
    coordinator.device_info = _device_info(
        ips=[
            IPInfo(name="eth0", addr="192.0.2.10", version="IPv4", type="WiReD"),
            IPInfo(name="eth1", addr="192.0.2.11", version="IPv4", type="wired"),
            IPInfo(name="wlan0", addr="", version="IPv4", type="wireless"),
        ]
    )

    assert coordinator._active_network_connection_types() == {"wired"}


async def test_ensure_ssh_collector_creates_once_and_reuses(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH collector creation uses the API host and configured password once."""
    collector = object()
    factory = MagicMock(return_value=collector)
    monkeypatch.setattr(coordinator_module, "SSHMetricsCollector", factory)

    assert await coordinator.async_ensure_ssh_metrics_collector() is collector
    assert await coordinator.async_ensure_ssh_metrics_collector() is collector
    factory.assert_called_once_with(host="nanokvm.local", password="password")


def test_media_entity_signal_is_sent_once_after_mount(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first mounted image emits one entry-scoped media signal."""
    dispatcher = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", dispatcher)

    coordinator.mounted_image = None
    coordinator._async_maybe_create_media_entities()
    coordinator.mounted_image = GetMountedImageRsp(file="images/rescue.iso")
    coordinator._async_maybe_create_media_entities()
    coordinator._async_maybe_create_media_entities()

    dispatcher.assert_called_once_with(
        coordinator.hass,
        SIGNAL_NEW_MEDIA_ENTITIES.format(coordinator.config_entry.entry_id),
    )
    assert coordinator.media_entities_created is True


def test_network_entity_signals_are_sent_once_per_connection_type(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic network signals are normalized and idempotent by type."""
    dispatcher = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", dispatcher)
    coordinator.device_info = _device_info(
        ips=[
            IPInfo(name="eth0", addr="192.0.2.10", version="IPv4", type="wired"),
            IPInfo(
                name="wlan0",
                addr="192.0.2.20",
                version="IPv4",
                type="WIRELESS",
            ),
        ]
    )

    coordinator._async_maybe_create_network_entities()
    coordinator._async_maybe_create_network_entities()

    assert dispatcher.call_count == 2
    signal = SIGNAL_NEW_NETWORK_ENTITIES.format(coordinator.config_entry.entry_id)
    assert {(args.args[1], args.args[2]) for args in dispatcher.call_args_list} == {
        (signal, "wired"),
        (signal, "wireless"),
    }
    assert coordinator.network_entities_created == {"wired", "wireless"}


async def test_successful_ssh_update_assigns_metrics_and_signals_once(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful SSH collection stores metrics and creates supported entities."""
    metrics = SSHMetricsSnapshot(
        uptime=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        cpu_temperature=51.5,
        memory_total=512.0,
        memory_used_percent=25.0,
        storage_total=1024.0,
        storage_used_percent=50.0,
        watchdog_enabled=False,
    )
    collector = SimpleNamespace(
        collect=AsyncMock(return_value=metrics),
        disconnect=AsyncMock(),
    )
    coordinator.ssh_metrics_collector = collector
    coordinator.device_info = _device_info(application="2.2.2")
    dispatcher = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", dispatcher)

    await coordinator._async_update_ssh_data()
    await coordinator._async_update_ssh_data()

    assert coordinator.uptime == metrics.uptime
    assert coordinator.cpu_temperature == metrics.cpu_temperature
    assert coordinator.memory_total == metrics.memory_total
    assert coordinator.memory_used_percent == metrics.memory_used_percent
    assert coordinator.storage_total == metrics.storage_total
    assert coordinator.storage_used_percent == metrics.storage_used_percent
    assert coordinator.watchdog_enabled is False
    assert dispatcher.call_args_list == [
        call(
            coordinator.hass,
            SIGNAL_NEW_SSH_SENSORS.format(coordinator.config_entry.entry_id),
        ),
        call(
            coordinator.hass,
            SIGNAL_NEW_SSH_SWITCHES.format(coordinator.config_entry.entry_id),
        ),
    ]
    assert collector.collect.await_count == 2


async def test_failed_ssh_update_clears_metrics_and_disconnects(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An SSH collection failure clears stale metrics without failing the poll."""
    collector = SimpleNamespace(
        collect=AsyncMock(side_effect=RuntimeError("ssh failed")),
        disconnect=AsyncMock(),
    )
    coordinator.ssh_metrics_collector = collector
    coordinator.uptime = datetime.datetime.now(datetime.UTC)
    coordinator.watchdog_enabled = True

    await coordinator._async_update_ssh_data()

    assert coordinator.uptime is None
    assert coordinator.watchdog_enabled is None
    collector.disconnect.assert_awaited_once_with()


async def test_disabled_ssh_refresh_clears_collector_and_metrics(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Disabling SSH disconnects and removes its collector."""
    collector = SimpleNamespace(disconnect=AsyncMock())
    coordinator.ssh_metrics_collector = collector
    coordinator.ssh_state = GetSSHStateRsp(enabled=False)
    coordinator.cpu_temperature = 50.0

    await coordinator._async_refresh_ssh_data()

    assert coordinator.cpu_temperature is None
    assert coordinator.ssh_metrics_collector is None
    collector.disconnect.assert_awaited_once_with()


async def test_app_version_schedule_refreshes_state_and_listeners(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """A stale version cache schedules one background refresh."""
    version = GetVersionRsp(current="2.2.2", latest="2.3.0")
    coordinator._async_fetch_app_version = AsyncMock(return_value=version)
    coordinator.async_update_listeners = MagicMock()
    coordinator.hass.async_create_task = asyncio.create_task

    coordinator._async_schedule_app_version_refresh()
    task = coordinator._app_version_fetch_task
    assert task is not None
    await task

    assert coordinator.application_version_info == version
    assert coordinator._app_version_last_fetched is not None
    assert coordinator._app_version_fetch_task is None
    coordinator.async_update_listeners.assert_called_once_with()


async def test_app_version_schedule_skips_active_and_fresh_cache(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """An active task or fresh success/failure cache suppresses duplicate work."""
    active_task = asyncio.create_task(asyncio.Event().wait())
    coordinator._app_version_fetch_task = active_task
    coordinator.hass.async_create_task = MagicMock()
    coordinator._async_schedule_app_version_refresh()
    coordinator.hass.async_create_task.assert_not_called()
    active_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active_task

    coordinator._app_version_fetch_task = None
    coordinator._app_version_last_fetched = datetime.datetime.now(datetime.UTC)
    coordinator.application_version_info = GetVersionRsp(
        current="2.2.2", latest="2.3.0"
    )
    coordinator._async_schedule_app_version_refresh()
    coordinator.hass.async_create_task.assert_not_called()

    coordinator.application_version_info = None
    coordinator._async_schedule_app_version_refresh()
    coordinator.hass.async_create_task.assert_not_called()


async def test_app_version_refresh_cancellation_clears_task_reference(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> None:
    """Cancelling a version refresh leaves no stale task reference."""
    started = asyncio.Event()

    async def wait_for_cancellation() -> None:
        started.set()
        await asyncio.Event().wait()

    coordinator._async_fetch_app_version = wait_for_cancellation
    task = asyncio.create_task(coordinator._async_refresh_app_version())
    coordinator._app_version_fetch_task = task
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert coordinator._app_version_fetch_task is None


async def test_app_version_client_returns_value_and_suppresses_device_failure(
    coordinator: NanoKVMDataUpdateCoordinator,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated version client copies transport auth and isolates failures."""
    fingerprint = "AA" * 32
    config_entry_mock.data[CONF_SSL_FINGERPRINT] = fingerprint
    version = GetVersionRsp(current="2.2.2", latest="2.3.0")
    success = _candidate_client("http://nanokvm.local/api/")
    success.get_application_version.return_value = version
    failure = _candidate_client("http://nanokvm.local/api/")
    failure.get_application_version.side_effect = NanoKVMError("offline")
    factory = MagicMock(side_effect=[success, failure])
    monkeypatch.setattr(coordinator_module, "NanoKVMClient", factory)

    assert await coordinator._async_fetch_app_version() == version
    assert await coordinator._async_fetch_app_version() is None
    assert factory.call_args_list == [
        call(
            "http://nanokvm.local/api/",
            token="token",
            ssl_fingerprint=fingerprint,
            request_timeout=45,
        ),
        call(
            "http://nanokvm.local/api/",
            token="token",
            ssl_fingerprint=fingerprint,
            request_timeout=45,
        ),
    ]


async def test_shutdown_cleans_owned_resources_when_base_shutdown_fails(
    coordinator: NanoKVMDataUpdateCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator-owned tasks and SSH are released even if base cleanup fails."""
    task = asyncio.create_task(asyncio.Event().wait())
    collector = SimpleNamespace(disconnect=AsyncMock())
    media = SimpleNamespace(async_shutdown=AsyncMock())
    coordinator._app_version_fetch_task = task
    coordinator.ssh_metrics_collector = collector
    coordinator.media = media
    base_shutdown = AsyncMock(side_effect=RuntimeError("base failed"))
    monkeypatch.setattr(DataUpdateCoordinator, "async_shutdown", base_shutdown)

    with pytest.raises(RuntimeError, match="base failed"):
        await coordinator.async_shutdown()

    assert task.cancelled()
    assert coordinator._app_version_fetch_task is None
    collector.disconnect.assert_awaited_once_with()
    assert coordinator.ssh_metrics_collector is None
    media.async_shutdown.assert_awaited_once_with()
