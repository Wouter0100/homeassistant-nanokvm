"""Behavior tests for NanoKVM service registration and handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from nanokvm.models import (
    DownloadStatus,
    GetCustomEdidListRsp,
    GetImagesRsp,
    GetLedStripRsp,
    ImageEnabledRsp,
    ScanWifiRsp,
    StatusImageRsp,
    WiFiInfo,
)
import pytest
import voluptuous as vol

from custom_components.nanokvm import services as services_module
from custom_components.nanokvm.const import (
    ATTR_BRIGHTNESS,
    ATTR_BUTTON_TYPE,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_HORIZONTAL_COUNT,
    ATTR_MAC,
    ATTR_MODE,
    ATTR_ON,
    ATTR_TEXT,
    ATTR_VERTICAL_COUNT,
    BUTTON_TYPE_POWER,
    BUTTON_TYPE_RESET,
    CONF_HOST,
    DOMAIN,
    SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
    SERVICE_IMAGE_DOWNLOAD_ENABLED,
    SERVICE_LIST_CUSTOM_EDIDS,
    SERVICE_LIST_IMAGES,
    SERVICE_PASTE_TEXT,
    SERVICE_PUSH_BUTTON,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_SCAN_WIFI,
    SERVICE_SET_LED_STRIP,
    SERVICE_SET_MOUSE_JIGGLER,
    SERVICE_WAKE_ON_LAN,
)


@dataclass(frozen=True, slots=True)
class RegisteredService:
    """Service metadata captured at the Home Assistant registry boundary."""

    handler: Callable[[ServiceCall], Awaitable[Any]]
    schema: vol.Schema
    supports_response: SupportsResponse


class ModelResponse:
    """Minimal model-dump boundary used by response normalization tests."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.mode: str | None = None

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        """Record the requested serialization mode and return JSON-ready data."""
        self.mode = mode
        return self.response


def _client() -> SimpleNamespace:
    """Return the NanoKVM client methods exposed through service handlers."""
    return SimpleNamespace(
        push_button=AsyncMock(),
        paste_text=AsyncMock(),
        reboot_system=AsyncMock(),
        reset_hdmi=AsyncMock(),
        reset_hid=AsyncMock(),
        send_wake_on_lan=AsyncMock(),
        set_mouse_jiggler_state=AsyncMock(),
        set_led_strip=AsyncMock(),
        scan_wifi=AsyncMock(),
        get_images=AsyncMock(),
        is_image_download_enabled=AsyncMock(),
        get_image_download_status=AsyncMock(),
        get_custom_edid_list=AsyncMock(),
    )


def _coordinator(
    host: str | None = "nanokvm.local",
    *,
    client: SimpleNamespace | None = None,
    is_pro_hardware: bool = False,
    led_strip: GetLedStripRsp | None = None,
    context_error: Exception | None = None,
) -> SimpleNamespace:
    """Return a coordinator with a serialized client-access boundary."""
    service_client = client or _client()
    entry_data = {CONF_HOST: host} if host is not None else {}

    @asynccontextmanager
    async def async_client():
        if context_error is not None:
            raise context_error
        yield service_client

    return SimpleNamespace(
        config_entry=SimpleNamespace(data=entry_data),
        client=service_client,
        async_client=async_client,
        async_request_refresh=AsyncMock(),
        is_pro_hardware=is_pro_hardware,
        led_strip=led_strip,
    )


def _service_registry(hass: MagicMock) -> SimpleNamespace:
    """Install the runtime service-registry boundary missing from the HA class spec."""
    registry = SimpleNamespace(
        has_service=MagicMock(),
        async_register=MagicMock(),
        async_remove=MagicMock(),
    )
    hass.services = registry
    return registry


def _capture_registered_services(hass: MagicMock) -> dict[str, RegisteredService]:
    """Register services and return their captured handlers and schemas."""
    registry = _service_registry(hass)
    registry.has_service.return_value = False
    services_module.async_register_services(hass)

    registered: dict[str, RegisteredService] = {}
    for registration in registry.async_register.call_args_list:
        domain, service_name, handler = registration.args
        assert domain == DOMAIN
        registered[service_name] = RegisteredService(
            handler=handler,
            schema=registration.kwargs["schema"],
            supports_response=registration.kwargs.get(
                "supports_response", SupportsResponse.NONE
            ),
        )
    return registered


@pytest.fixture
def registered_services(hass_mock: MagicMock) -> dict[str, RegisteredService]:
    """Return all services registered against the Home Assistant boundary."""
    return _capture_registered_services(hass_mock)


def _install_coordinators(hass: MagicMock, *coordinators: SimpleNamespace) -> None:
    """Install coordinators into Home Assistant integration storage."""
    hass.data[DOMAIN] = {
        f"entry-{index}": coordinator
        for index, coordinator in enumerate(coordinators, start=1)
    }


async def _call_service(
    hass: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    service_name: str,
    data: dict[str, Any] | None = None,
) -> Any:
    """Validate service data and invoke the captured Home Assistant handler."""
    service = registered_services[service_name]
    validated_data = service.schema(data or {})
    service_call = ServiceCall(
        hass,
        DOMAIN,
        service_name,
        validated_data,
        return_response=service.supports_response is SupportsResponse.ONLY,
    )
    return await service.handler(service_call)


def test_model_to_response_serializes_models_in_json_mode() -> None:
    """Model responses must request JSON-safe values for Home Assistant."""
    model = ModelResponse({"status": "ready"})

    assert services_module._model_to_response(model) == {"status": "ready"}
    assert model.mode == "json"


def test_model_to_response_preserves_mapping_responses() -> None:
    """Already-normalized mapping responses must be returned unchanged."""
    response = {"items": ["one", "two"]}

    assert services_module._model_to_response(response) is response


@pytest.mark.parametrize("value", [None, True, "ready", ["one"]])
def test_model_to_response_wraps_non_mapping_values(value: object) -> None:
    """Scalar and sequence responses must receive a stable response key."""
    assert services_module._model_to_response(value) == {"value": value}


def test_pro_gate_accepts_pro_hardware() -> None:
    """Pro services must accept coordinators reporting Pro hardware."""
    services_module._ensure_pro(
        SimpleNamespace(is_pro_hardware=True), SERVICE_SCAN_WIFI
    )


def test_pro_gate_rejects_other_hardware() -> None:
    """Pro services must expose a user-facing hardware error."""
    with pytest.raises(
        HomeAssistantError,
        match="The scan_wifi service is only available for NanoKVM Pro devices",
    ):
        services_module._ensure_pro(
            SimpleNamespace(is_pro_hardware=False), SERVICE_SCAN_WIFI
        )


def test_service_schemas_apply_defaults_and_coercion() -> None:
    """Service schemas must apply documented defaults before handler execution."""
    assert services_module.PUSH_BUTTON_SCHEMA(
        {ATTR_BUTTON_TYPE: BUTTON_TYPE_POWER}
    ) == {
        ATTR_BUTTON_TYPE: BUTTON_TYPE_POWER,
        ATTR_DURATION: 100,
    }
    assert (
        services_module.PUSH_BUTTON_SCHEMA(
            {ATTR_BUTTON_TYPE: BUTTON_TYPE_RESET, ATTR_DURATION: "250"}
        )[ATTR_DURATION]
        == 250
    )
    assert services_module.SET_MOUSE_JIGGLER_SCHEMA({ATTR_ENABLED: True}) == {
        ATTR_ENABLED: True,
        ATTR_MODE: "absolute",
    }


@pytest.mark.parametrize(
    ("schema", "data"),
    [
        (
            services_module.PUSH_BUTTON_SCHEMA,
            {ATTR_BUTTON_TYPE: BUTTON_TYPE_POWER, ATTR_DURATION: 99},
        ),
        (services_module.HOST_ONLY_SCHEMA, {CONF_HOST: ""}),
        (
            services_module.SET_MOUSE_JIGGLER_SCHEMA,
            {ATTR_ENABLED: True, ATTR_MODE: "bad"},
        ),
        (services_module.SET_LED_STRIP_SCHEMA, {ATTR_BRIGHTNESS: 101}),
    ],
)
def test_service_schemas_reject_invalid_fields(
    schema: vol.Schema, data: dict[str, Any]
) -> None:
    """Invalid user input must fail before reaching a device handler."""
    with pytest.raises(vol.Invalid):
        schema(data)


def test_register_services_registers_complete_surface_with_response_contracts(
    hass_mock: MagicMock,
) -> None:
    """Registration must expose every implementation service exactly once."""
    registered = _capture_registered_services(hass_mock)

    assert tuple(registered) == services_module._SERVICE_NAMES
    assert {
        name
        for name, service in registered.items()
        if service.supports_response is SupportsResponse.ONLY
    } == {
        SERVICE_SCAN_WIFI,
        SERVICE_LIST_IMAGES,
        SERVICE_IMAGE_DOWNLOAD_ENABLED,
        SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
        SERVICE_LIST_CUSTOM_EDIDS,
    }
    assert all(service.schema is not None for service in registered.values())


def test_register_services_is_idempotent_when_already_registered(
    hass_mock: MagicMock,
) -> None:
    """Repeated setup must not replace existing service handlers."""
    registry = _service_registry(hass_mock)
    registry.has_service.return_value = True

    services_module.async_register_services(hass_mock)

    registry.async_register.assert_not_called()


async def test_target_resolution_rejects_empty_configuration(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """A service call must fail clearly when no device is configured."""
    with pytest.raises(HomeAssistantError, match="No NanoKVM devices are configured"):
        await _call_service(hass_mock, registered_services, SERVICE_REBOOT)


async def test_target_resolution_uses_only_device_when_host_is_omitted(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """A single configured device must not require an explicit host."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(hass_mock, registered_services, SERVICE_REBOOT)

    coordinator.client.reboot_system.assert_awaited_once_with()


async def test_target_resolution_requires_host_for_multiple_devices(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Ambiguous multi-device calls must ask for an explicit target."""
    _install_coordinators(
        hass_mock,
        _coordinator("first.local"),
        _coordinator("second.local"),
    )

    with pytest.raises(
        HomeAssistantError,
        match="Multiple NanoKVM devices are configured; specify the host field",
    ):
        await _call_service(hass_mock, registered_services, SERVICE_REBOOT)


async def test_target_resolution_matches_normalized_host_forms(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Equivalent bare, HTTP, and HTTPS host forms must target one device."""
    first = _coordinator("first.local")
    selected = _coordinator("http://nanokvm.local/api/")
    _install_coordinators(hass_mock, first, selected)

    await _call_service(
        hass_mock,
        registered_services,
        SERVICE_REBOOT,
        {CONF_HOST: "https://nanokvm.local"},
    )

    first.client.reboot_system.assert_not_awaited()
    selected.client.reboot_system.assert_awaited_once_with()


async def test_target_resolution_rejects_unknown_host(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """An unknown explicit host must not fall back to another device."""
    _install_coordinators(hass_mock, _coordinator("configured.local"))

    with pytest.raises(
        HomeAssistantError,
        match="No NanoKVM device is configured for host missing.local",
    ):
        await _call_service(
            hass_mock,
            registered_services,
            SERVICE_REBOOT,
            {CONF_HOST: "missing.local"},
        )


async def test_target_resolution_rejects_duplicate_normalized_hosts(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Duplicate normalized host entries must remain explicitly ambiguous."""
    _install_coordinators(
        hass_mock,
        _coordinator("http://duplicate.local"),
        _coordinator("https://duplicate.local/api/"),
    )

    with pytest.raises(
        HomeAssistantError,
        match="Multiple NanoKVM devices match host duplicate.local",
    ):
        await _call_service(
            hass_mock,
            registered_services,
            SERVICE_REBOOT,
            {CONF_HOST: "duplicate.local"},
        )


@pytest.mark.parametrize(
    ("button_type", "expected_gpio"),
    [
        (BUTTON_TYPE_POWER, services_module.GpioType.POWER),
        (BUTTON_TYPE_RESET, services_module.GpioType.RESET),
    ],
)
async def test_push_button_maps_service_type_and_duration(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    button_type: str,
    expected_gpio: services_module.GpioType,
) -> None:
    """Push-button calls must map UI values to library enum arguments."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(
        hass_mock,
        registered_services,
        SERVICE_PUSH_BUTTON,
        {ATTR_BUTTON_TYPE: button_type, ATTR_DURATION: 750},
    )

    coordinator.client.push_button.assert_awaited_once_with(expected_gpio, 750)


async def test_paste_text_forwards_exact_text(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Paste service text must reach the NanoKVM client unchanged."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(
        hass_mock,
        registered_services,
        SERVICE_PASTE_TEXT,
        {ATTR_TEXT: "Hello, NanoKVM!"},
    )

    coordinator.client.paste_text.assert_awaited_once_with("Hello, NanoKVM!")


@pytest.mark.parametrize(
    ("service_name", "client_method"),
    [
        (SERVICE_REBOOT, "reboot_system"),
        (SERVICE_RESET_HDMI, "reset_hdmi"),
        (SERVICE_RESET_HID, "reset_hid"),
    ],
)
async def test_no_argument_services_invoke_expected_client_method(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    service_name: str,
    client_method: str,
) -> None:
    """Simple service handlers must invoke their corresponding API method."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(hass_mock, registered_services, service_name)

    getattr(coordinator.client, client_method).assert_awaited_once_with()


async def test_wake_on_lan_forwards_mac_address(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Wake-on-LAN must forward the requested MAC address unchanged."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(
        hass_mock,
        registered_services,
        SERVICE_WAKE_ON_LAN,
        {ATTR_MAC: "00:11:22:33:44:55"},
    )

    coordinator.client.send_wake_on_lan.assert_awaited_once_with("00:11:22:33:44:55")


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    [
        ("absolute", services_module.MouseJigglerMode.ABSOLUTE),
        ("relative", services_module.MouseJigglerMode.RELATIVE),
    ],
)
async def test_mouse_jiggler_maps_mode_enum(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    mode: str,
    expected_mode: services_module.MouseJigglerMode,
) -> None:
    """Mouse-jiggler mode strings must map to library enums."""
    coordinator = _coordinator()
    _install_coordinators(hass_mock, coordinator)

    await _call_service(
        hass_mock,
        registered_services,
        SERVICE_SET_MOUSE_JIGGLER,
        {ATTR_ENABLED: False, ATTR_MODE: mode},
    )

    coordinator.client.set_mouse_jiggler_state.assert_awaited_once_with(
        False, expected_mode
    )


async def test_led_strip_requires_at_least_one_update_field(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """An empty LED update must fail before resolving or contacting a device."""
    _install_coordinators(hass_mock, _coordinator(is_pro_hardware=True))

    with pytest.raises(
        HomeAssistantError, match="At least one LED strip field is required"
    ):
        await _call_service(hass_mock, registered_services, SERVICE_SET_LED_STRIP)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {ATTR_ON: False},
            {
                "on": False,
                "brightness": 75,
                "horizontal_count": 30,
                "vertical_count": 20,
            },
        ),
        (
            {ATTR_VERTICAL_COUNT: 25},
            {
                "on": True,
                "brightness": 75,
                "horizontal_count": 30,
                "vertical_count": 25,
            },
        ),
    ],
)
async def test_led_strip_merges_partial_updates_and_refreshes(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    data: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """LED updates must preserve unspecified device state and refresh afterward."""
    coordinator = _coordinator(
        is_pro_hardware=True,
        led_strip=GetLedStripRsp(on=True, hor=30, ver=20, brightness=75),
    )
    _install_coordinators(hass_mock, coordinator)

    await _call_service(hass_mock, registered_services, SERVICE_SET_LED_STRIP, data)

    coordinator.client.set_led_strip.assert_awaited_once_with(**expected)
    coordinator.async_request_refresh.assert_awaited_once_with()


async def test_led_strip_translates_invalid_merged_config(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Invalid aggregate bead counts must become a user-facing HA error."""
    coordinator = _coordinator(
        is_pro_hardware=True,
        led_strip=GetLedStripRsp(on=True, hor=30, ver=20, brightness=75),
    )
    _install_coordinators(hass_mock, coordinator)

    with pytest.raises(
        HomeAssistantError,
        match=r"horizontal_count \+ \(2 \* vertical_count\) <= 150",
    ):
        await _call_service(
            hass_mock,
            registered_services,
            SERVICE_SET_LED_STRIP,
            {ATTR_HORIZONTAL_COUNT: 120},
        )

    coordinator.client.set_led_strip.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "service_name",
    [SERVICE_SET_LED_STRIP, SERVICE_SCAN_WIFI, SERVICE_LIST_CUSTOM_EDIDS],
)
async def test_pro_services_reject_non_pro_devices(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    service_name: str,
) -> None:
    """Every Pro-only service must apply the same hardware gate."""
    coordinator = _coordinator(
        is_pro_hardware=False,
        led_strip=GetLedStripRsp(on=True, hor=30, ver=20, brightness=75),
    )
    _install_coordinators(hass_mock, coordinator)
    data = {ATTR_BRIGHTNESS: 50} if service_name == SERVICE_SET_LED_STRIP else {}

    with pytest.raises(
        HomeAssistantError,
        match=rf"The {service_name} service is only available for NanoKVM Pro devices",
    ):
        await _call_service(hass_mock, registered_services, service_name, data)


@pytest.mark.parametrize(
    ("service_name", "client_method", "response", "expected"),
    [
        (
            SERVICE_SCAN_WIFI,
            "scan_wifi",
            ScanWifiRsp(
                wifiList=[
                    WiFiInfo(
                        ssid="Lab",
                        bssid="00:11:22:33:44:55",
                        signal=-40,
                        frequency=2412,
                        security="WPA2",
                    )
                ]
            ),
            {
                "wifi_list": [
                    {
                        "ssid": "Lab",
                        "bssid": "00:11:22:33:44:55",
                        "signal": -40,
                        "frequency": 2412,
                        "security": "WPA2",
                    }
                ]
            },
        ),
        (
            SERVICE_LIST_IMAGES,
            "get_images",
            GetImagesRsp(files=["installer.iso"]),
            {"files": ["installer.iso"]},
        ),
        (
            SERVICE_IMAGE_DOWNLOAD_ENABLED,
            "is_image_download_enabled",
            ImageEnabledRsp(enabled=True),
            {"enabled": True},
        ),
        (
            SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
            "get_image_download_status",
            StatusImageRsp(
                status=DownloadStatus.IN_PROGRESS,
                file="installer.iso",
                percentage="25",
            ),
            {
                "status": "in_progress",
                "file": "installer.iso",
                "percentage": "25",
            },
        ),
        (
            SERVICE_LIST_CUSTOM_EDIDS,
            "get_custom_edid_list",
            GetCustomEdidListRsp(edidList=["display.bin"]),
            {"edid_list": ["display.bin"]},
        ),
    ],
)
async def test_response_services_return_json_ready_models(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
    service_name: str,
    client_method: str,
    response: object,
    expected: dict[str, Any],
) -> None:
    """Response handlers must normalize library models for HA service callers."""
    coordinator = _coordinator(is_pro_hardware=True)
    getattr(coordinator.client, client_method).return_value = response
    _install_coordinators(hass_mock, coordinator)

    result = await _call_service(hass_mock, registered_services, service_name)

    assert result == expected
    getattr(coordinator.client, client_method).assert_awaited_once_with()


async def test_non_response_handler_preserves_home_assistant_errors(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Already-user-facing errors must not be wrapped a second time."""
    coordinator = _coordinator()
    original_error = HomeAssistantError("friendly device error")
    coordinator.client.paste_text.side_effect = original_error
    _install_coordinators(hass_mock, coordinator)

    with pytest.raises(HomeAssistantError) as captured:
        await _call_service(
            hass_mock,
            registered_services,
            SERVICE_PASTE_TEXT,
            {ATTR_TEXT: "text"},
        )

    assert captured.value is original_error


async def test_non_response_handler_translates_unexpected_errors(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Unexpected client failures must include service and host context."""
    coordinator = _coordinator()
    coordinator.client.paste_text.side_effect = RuntimeError("offline")
    _install_coordinators(hass_mock, coordinator)

    with pytest.raises(
        HomeAssistantError,
        match="Failed to execute paste_text for nanokvm.local: offline",
    ) as captured:
        await _call_service(
            hass_mock,
            registered_services,
            SERVICE_PASTE_TEXT,
            {ATTR_TEXT: "text"},
        )

    assert isinstance(captured.value.__cause__, RuntimeError)


async def test_client_context_failures_are_translated_with_unknown_host_fallback(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Client-access failures must use a stable host fallback for malformed entries."""
    coordinator = _coordinator(
        host=None,
        context_error=RuntimeError("session failed"),
    )
    _install_coordinators(hass_mock, coordinator)

    with pytest.raises(
        HomeAssistantError,
        match=r"Failed to execute reboot for <unknown>: session failed",
    ):
        await _call_service(hass_mock, registered_services, SERVICE_REBOOT)


async def test_response_handler_translates_unexpected_errors(
    hass_mock: MagicMock,
    registered_services: Mapping[str, RegisteredService],
) -> None:
    """Response-service failures must use the same user-facing error contract."""
    coordinator = _coordinator()
    coordinator.client.get_images.side_effect = RuntimeError("storage unavailable")
    _install_coordinators(hass_mock, coordinator)

    with pytest.raises(
        HomeAssistantError,
        match="Failed to execute list_images for nanokvm.local: storage unavailable",
    ) as captured:
        await _call_service(hass_mock, registered_services, SERVICE_LIST_IMAGES)

    assert isinstance(captured.value.__cause__, RuntimeError)


def test_unregister_services_removes_only_registered_services(
    hass_mock: MagicMock,
) -> None:
    """Unload must remove every currently registered integration service."""
    registered_names = {SERVICE_REBOOT, SERVICE_SCAN_WIFI, SERVICE_LIST_IMAGES}
    registry = _service_registry(hass_mock)
    registry.has_service.side_effect = lambda domain, service_name: (
        domain == DOMAIN and service_name in registered_names
    )

    services_module.async_unregister_services(hass_mock)

    assert registry.async_remove.call_args_list == [
        call(DOMAIN, service_name)
        for service_name in services_module._SERVICE_NAMES
        if service_name in registered_names
    ]


def test_unregister_services_is_noop_when_none_are_registered(
    hass_mock: MagicMock,
) -> None:
    """Unload must tolerate an already-empty service registry."""
    registry = _service_registry(hass_mock)
    registry.has_service.return_value = False

    services_module.async_unregister_services(hass_mock)

    registry.async_remove.assert_not_called()
