"""Tests for NanoKVM HDMI recording service contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
import pytest
import voluptuous as vol

from custom_components.nanokvm import services as services_module
from custom_components.nanokvm.const import DOMAIN


def _service_call(hass: MagicMock, service: str, data: dict[str, object]) -> ServiceCall:
    """Create a service call for direct batched-handler tests."""
    return ServiceCall(hass, DOMAIN, service, data)


def test_start_recording_schema_applies_defaults_and_coercion() -> None:
    """Start recording defaults to a one-hour video-only MP4."""
    schema = services_module.START_HDMI_RECORDING_SCHEMA

    validated = schema({"filename": "/config/www/session.mp4"})

    assert validated == {
        "filename": "/config/www/session.mp4",
        "duration": 3600,
        "include_audio": False,
    }
    assert schema(
        {
            "filename": "/config/www/session.mp4",
            "duration": "7200",
            "include_audio": False,
        }
    )["duration"] == 7200


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"filename": ""},
        {"filename": "/config/www/session.mp4", "duration": 0},
        {"filename": "/config/www/session.mp4", "duration": 7201},
    ],
)
def test_start_recording_schema_rejects_invalid_fields(data: dict[str, object]) -> None:
    """Missing paths and out-of-range durations fail before entity dispatch."""
    with pytest.raises(vol.Invalid):
        services_module.START_HDMI_RECORDING_SCHEMA(data)


@pytest.mark.asyncio
async def test_start_recording_requires_exactly_one_camera(hass_mock: MagicMock) -> None:
    """A single output path must never be dispatched to zero or many cameras."""
    call = _service_call(
        hass_mock,
        "start_hdmi_recording",
        {
            "filename": "/config/www/session.mp4",
            "duration": 3600,
            "include_audio": True,
        },
    )

    for entities in ([], [SimpleNamespace(), SimpleNamespace()]):
        with pytest.raises(HomeAssistantError, match="exactly one NanoKVM camera"):
            await services_module._async_start_hdmi_recording(entities, call)


@pytest.mark.asyncio
async def test_start_recording_forwards_validated_options(hass_mock: MagicMock) -> None:
    """The entity-target service forwards the requested output and media options."""
    camera = SimpleNamespace(async_start_hdmi_recording=AsyncMock())
    call = _service_call(
        hass_mock,
        "start_hdmi_recording",
        {
            "filename": "/config/www/session.mp4",
            "duration": 90,
            "include_audio": False,
        },
    )

    await services_module._async_start_hdmi_recording([camera], call)

    camera.async_start_hdmi_recording.assert_awaited_once_with(
        filename="/config/www/session.mp4",
        duration=90,
        include_audio=False,
    )


@pytest.mark.asyncio
async def test_stop_recording_requires_one_camera_and_delegates(
    hass_mock: MagicMock,
) -> None:
    """Stop is entity-targeted and delegates idempotence to the camera recorder."""
    call = _service_call(hass_mock, "stop_hdmi_recording", {})

    for entities in ([], [SimpleNamespace(), SimpleNamespace()]):
        with pytest.raises(HomeAssistantError, match="exactly one NanoKVM camera"):
            await services_module._async_stop_hdmi_recording(entities, call)

    camera = SimpleNamespace(async_stop_hdmi_recording=AsyncMock())
    await services_module._async_stop_hdmi_recording([camera], call)

    camera.async_stop_hdmi_recording.assert_awaited_once_with()


def test_register_services_adds_camera_target_recording_actions(
    hass_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording actions register as batched NanoKVM camera entity services."""
    hass_mock.services = SimpleNamespace(
        has_service=MagicMock(return_value=False),
        async_register=MagicMock(),
        async_remove=MagicMock(),
    )
    register = MagicMock()
    monkeypatch.setattr(
        services_module.service,
        "async_register_batched_platform_entity_service",
        register,
    )

    services_module.async_register_services(hass_mock)

    assert [call.kwargs["service_name"] for call in register.call_args_list] == [
        "start_hdmi_recording",
        "stop_hdmi_recording",
    ]
    assert all(call.kwargs["service_domain"] == DOMAIN for call in register.call_args_list)
    assert all(call.kwargs["entity_domain"] == "camera" for call in register.call_args_list)
    assert register.call_args_list[0].kwargs["func"] is services_module._async_start_hdmi_recording
    assert register.call_args_list[0].kwargs["schema"] == services_module.START_HDMI_RECORDING_FIELDS
    assert register.call_args_list[1].kwargs["func"] is services_module._async_stop_hdmi_recording
    assert register.call_args_list[1].kwargs["schema"] is None
