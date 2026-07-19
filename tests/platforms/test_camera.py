"""Tests for the NanoKVM camera platform."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.camera import CameraEntityFeature, CameraState
from homeassistant.components.camera.const import StreamType
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError
import pytest
from webrtc_models import RTCIceCandidateInit
from yarl import URL

import custom_components.nanokvm.camera as camera_module
from custom_components.nanokvm.const import DOMAIN


def _coordinator(**overrides: object) -> SimpleNamespace:
    """Build the coordinator state consumed by the camera entity."""
    values: dict[str, object] = {
        "client": SimpleNamespace(
            url=URL("http://nanokvm.local/api/"),
            token="existing-token",
        ),
        "config_entry": SimpleNamespace(
            data={CONF_USERNAME: "admin", CONF_PASSWORD: "password"}
        ),
        "device_info": SimpleNamespace(device_key="test-device"),
        "is_pro_hardware": False,
        "last_update_success": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _camera(
    monkeypatch: pytest.MonkeyPatch,
    coordinator: SimpleNamespace | None = None,
) -> tuple[
    camera_module.NanoKVMCamera,
    MagicMock,
    MagicMock,
]:
    """Create a camera with its WebRTC manager isolated at the platform edge."""
    manager = MagicMock()
    manager.async_handle_async_webrtc_offer = AsyncMock()
    manager.async_on_webrtc_candidate = AsyncMock()
    manager.async_shutdown = AsyncMock()
    manager_factory = MagicMock(return_value=manager)
    monkeypatch.setattr(camera_module, "NanoKVMWebRTCManager", manager_factory)

    active_coordinator = coordinator or _coordinator()
    if getattr(active_coordinator, "media", None) is None:
        provider = SimpleNamespace(
            create_client=MagicMock(return_value=None),
            async_authenticate=AsyncMock(),
        )
        recorder = MagicMock()
        recorder.is_recording = False
        recorder.async_start = AsyncMock()
        recorder.async_stop = AsyncMock()
        recorder.async_add_state_listener.return_value = MagicMock()
        active_coordinator.media = SimpleNamespace(
            client_provider=provider,
            recording=recorder,
        )

    entity = camera_module.NanoKVMCamera(
        active_coordinator, camera_module.CAMERAS[0]
    )
    return entity, manager, manager_factory


def test_camera_description_inventory_and_default_availability() -> None:
    """The platform exposes one always-created HDMI stream description."""
    assert [description.key for description in camera_module.CAMERAS] == ["hdmi"]
    assert camera_module.CAMERAS[0].available_fn(_coordinator()) is True


def test_camera_requires_entry_scoped_media_runtime() -> None:
    """Platform setup fails explicitly if integration media ownership is missing."""
    with pytest.raises(RuntimeError, match="media runtime is not initialized"):
        camera_module.NanoKVMCamera(_coordinator(media=None), camera_module.CAMERAS[0])


@pytest.mark.asyncio
async def test_async_setup_entry_filters_unavailable_descriptions(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup eagerly adds only descriptions whose capability predicate passes."""
    coordinator = _coordinator()
    hass_mock.data = {DOMAIN: {config_entry_mock.entry_id: coordinator}}
    descriptions = (
        camera_module.NanoKVMCameraEntityDescription(
            key="available", available_fn=lambda _: True
        ),
        camera_module.NanoKVMCameraEntityDescription(
            key="unavailable", available_fn=lambda _: False
        ),
    )
    monkeypatch.setattr(camera_module, "CAMERAS", descriptions)
    entity_factory = MagicMock(side_effect=lambda **kwargs: kwargs["description"].key)
    monkeypatch.setattr(camera_module, "NanoKVMCamera", entity_factory)
    batches: list[list[object]] = []

    await camera_module.async_setup_entry(
        hass_mock,
        config_entry_mock,
        lambda entities: batches.append(list(entities)),
    )

    assert batches == [["available"]]
    entity_factory.assert_called_once_with(
        coordinator=coordinator,
        description=descriptions[0],
    )


@pytest.mark.asyncio
async def test_camera_initializes_native_webrtc_stream(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entity metadata and manager providers describe a native WebRTC camera."""
    coordinator = _coordinator(is_pro_hardware=True)
    entity, _manager, manager_factory = _camera(monkeypatch, coordinator)
    entity.hass = hass_mock

    assert entity.unique_id == "test-device_camera_hdmi"
    assert entity.supported_features == CameraEntityFeature.STREAM
    assert entity.is_streaming is False
    assert entity.state == CameraState.IDLE
    assert entity.available is True
    coordinator.last_update_success = False
    assert entity.available is False
    assert await entity.stream_source() is None
    assert entity.camera_capabilities.frontend_stream_types == {StreamType.WEB_RTC}

    manager_factory.assert_called_once()
    manager_options = manager_factory.call_args.kwargs
    assert manager_options["logger"] is camera_module._LOGGER
    assert manager_options["hass_provider"]() is hass_mock
    assert manager_options["client_factory"] == coordinator.media.client_provider.create_client
    assert (
        manager_options["authenticate_client"]
        == coordinator.media.client_provider.async_authenticate
    )
    assert manager_options["is_pro_hardware"]() is True
    assert manager_options["session_state_callback"] == entity._handle_streaming_state
    assert manager_options["login_timeout_seconds"] == 15
    assert manager_options["websocket_heartbeat_seconds"] == 30.0
    assert manager_options["max_pending_ice_candidates"] == 64

    assert entity._recorder is coordinator.media.recording
    entity._recorder.async_add_state_listener.assert_called_once_with(
        entity._handle_recording_state
    )


def test_camera_streaming_state_tracks_native_webrtc_sessions(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The camera is streaming only while its WebRTC manager has a session."""
    entity, _manager, manager_factory = _camera(monkeypatch)
    entity.hass = hass_mock
    write_state = MagicMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write_state)
    state_callback = manager_factory.call_args.kwargs["session_state_callback"]

    state_callback(True)

    assert entity.is_streaming is True
    assert entity.state == CameraState.STREAMING

    state_callback(False)

    assert entity.is_streaming is False
    assert entity.state == CameraState.IDLE
    assert write_state.call_count == 2


def test_camera_reuses_entry_scoped_recording_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entity never creates a second recorder for the same config entry."""
    coordinator = _coordinator(is_pro_hardware=False)
    entity, _manager, _manager_factory = _camera(monkeypatch, coordinator)

    assert entity._recorder is coordinator.media.recording


@pytest.mark.asyncio
async def test_hdmi_recording_allows_video_only_on_non_pro_hardware(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video-only direct recording is available on non-Pro hardware."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    entity.hass = hass_mock
    hass_mock.config = SimpleNamespace(is_allowed_path=MagicMock())
    hass_mock.async_add_executor_job = AsyncMock(return_value=True)

    await entity.async_start_hdmi_recording(
        filename="/config/www/capture.mp4",
        duration=60,
        include_audio=False,
    )

    entity._recorder.async_start.assert_awaited_once_with(
        filename="/config/www/capture.mp4",
        duration=60,
        include_audio=False,
    )


@pytest.mark.asyncio
async def test_hdmi_recording_rejects_audio_on_non_pro_hardware(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Pro devices reject audio because the direct stream is video-only."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    entity.hass = hass_mock

    with pytest.raises(HomeAssistantError, match="audio recording is only available"):
        await entity.async_start_hdmi_recording(
            filename="/config/www/capture.mp4",
            duration=60,
            include_audio=True,
        )

    entity._recorder.async_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_hdmi_recording_requires_mp4_and_allowed_path(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entity rejects unsupported containers and paths outside HA's allowlist."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(is_pro_hardware=True)
    )
    entity.hass = hass_mock
    is_allowed_path = MagicMock()
    hass_mock.config = SimpleNamespace(is_allowed_path=is_allowed_path)
    hass_mock.async_add_executor_job = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="must use the .mp4 extension"):
        await entity.async_start_hdmi_recording(
            filename="/config/www/capture.mkv",
            duration=60,
            include_audio=True,
        )

    with pytest.raises(HomeAssistantError, match="not in an allowed directory"):
        await entity.async_start_hdmi_recording(
            filename="/tmp/capture.mp4",
            duration=60,
            include_audio=True,
        )

    entity._recorder.async_start.assert_not_awaited()
    hass_mock.async_add_executor_job.assert_awaited_once_with(
        is_allowed_path, "/tmp/capture.mp4"
    )
    is_allowed_path.assert_not_called()


@pytest.mark.asyncio
async def test_hdmi_recording_delegates_and_updates_camera_state(
    hass_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validated recording actions delegate and recorder state reaches Home Assistant."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(is_pro_hardware=True)
    )
    entity.hass = hass_mock
    is_allowed_path = MagicMock()
    hass_mock.config = SimpleNamespace(is_allowed_path=is_allowed_path)
    hass_mock.async_add_executor_job = AsyncMock(return_value=True)
    write_state = MagicMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write_state)

    await entity.async_start_hdmi_recording(
        filename="/config/www/capture.mp4",
        duration=60,
        include_audio=False,
    )
    await entity.async_stop_hdmi_recording()

    entity._recorder.async_start.assert_awaited_once_with(
        filename="/config/www/capture.mp4",
        duration=60,
        include_audio=False,
    )
    entity._recorder.async_stop.assert_awaited_once_with()
    hass_mock.async_add_executor_job.assert_awaited_once_with(
        is_allowed_path, "/config/www/capture.mp4"
    )
    is_allowed_path.assert_not_called()

    state_callback = entity._recorder.async_add_state_listener.call_args.args[0]
    state_callback(True)
    assert entity.is_recording is True
    assert entity.state == CameraState.RECORDING
    state_callback(False)
    assert entity.is_recording is False
    assert entity.state == CameraState.IDLE
    assert write_state.call_count == 2



@pytest.mark.asyncio
async def test_snapshot_is_suppressed_on_pro_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pro snapshot never opens MJPEG because it would interrupt WebRTC."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(is_pro_hardware=True)
    )
    create_client = entity._media.client_provider.create_client

    assert await entity._async_read_snapshot_frame() is None
    create_client.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_returns_none_when_client_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot reads stop cleanly when the config cannot build a client."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    entity._media.client_provider.create_client.return_value = None

    assert await entity._async_read_snapshot_frame() is None


class _RequestContext(AbstractAsyncContextManager[object]):
    """Async request context used by snapshot reader tests."""

    def __init__(self, upstream: object) -> None:
        self.upstream = upstream
        self.exited = False

    async def __aenter__(self) -> object:
        return self.upstream

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True


class _StreamClient(AbstractAsyncContextManager["_StreamClient"]):
    """Authenticated-client test double retaining request observations."""

    def __init__(self, upstream: object) -> None:
        self.request_context = _RequestContext(upstream)
        self.requests: list[tuple[str, str]] = []
        self.exited = False

    async def __aenter__(self) -> _StreamClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True

    def _request(self, method: str, path: str) -> _RequestContext:
        self.requests.append((method, path))
        return self.request_context


class _Reader:
    """Multipart reader returning a prescribed sequence of parts."""

    def __init__(self, parts: list[object | None]) -> None:
        self.parts = iter(parts)

    async def next(self) -> object | None:
        return next(self.parts)


class _BodyPart:
    """Body part returning one prescribed payload."""

    def __init__(self, payload: bytes) -> None:
        self.read = AsyncMock(return_value=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ([None], None),
        ([object(), _BodyPart(b"jpeg")], b"jpeg"),
        ([_BodyPart(b""), None], None),
    ],
)
async def test_snapshot_scans_multipart_response_for_nonempty_frame(
    monkeypatch: pytest.MonkeyPatch,
    parts: list[object | None],
    expected: bytes | None,
) -> None:
    """Multipart metadata and empty frames are skipped until JPEG data or EOF."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    upstream = object()
    client = _StreamClient(upstream)
    authenticate = AsyncMock()
    reader = _Reader(parts)
    entity._media.client_provider.create_client.return_value = client
    entity._media.client_provider.async_authenticate = authenticate
    monkeypatch.setattr(camera_module, "BodyPartReader", _BodyPart)
    from_response = MagicMock(return_value=reader)
    monkeypatch.setattr(camera_module.MultipartReader, "from_response", from_response)

    assert await entity._async_read_snapshot_frame() == expected
    assert client.requests == [("GET", "/stream/mjpeg")]
    assert client.exited is True
    assert client.request_context.exited is True
    authenticate.assert_awaited_once_with(client)
    from_response.assert_called_once_with(upstream)


@pytest.mark.asyncio
async def test_async_camera_image_returns_frame_and_handles_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Still-image requests return bytes, but ordinary fetch errors become no image."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    read_frame = AsyncMock(return_value=b"jpeg")
    monkeypatch.setattr(entity, "_async_read_snapshot_frame", read_frame)

    assert await entity.async_camera_image(width=640, height=480) == b"jpeg"

    read_frame.side_effect = TimeoutError("snapshot timed out")
    with caplog.at_level(logging.ERROR, logger=camera_module.__name__):
        assert await entity.async_camera_image() is None

    assert "Error fetching still image: snapshot timed out" in caplog.text


@pytest.mark.asyncio
async def test_async_camera_image_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task cancellation is not converted into an ordinary missing snapshot."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    monkeypatch.setattr(
        entity,
        "_async_read_snapshot_frame",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await entity.async_camera_image()


@pytest.mark.asyncio
async def test_webrtc_calls_delegate_to_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offers, ICE candidates, and close notifications retain their arguments."""
    entity, manager, _manager_factory = _camera(monkeypatch)
    send_message: Callable[[Any], None] = MagicMock()
    candidate = RTCIceCandidateInit(
        candidate="candidate:1",
        sdp_mid="0",
        sdp_m_line_index=0,
        user_fragment=None,
    )

    await entity.async_handle_async_webrtc_offer(
        "offer-sdp", "offer-session", send_message
    )
    await entity.async_on_webrtc_candidate("candidate-session", candidate)
    entity.close_webrtc_session("closed-session")

    manager.async_handle_async_webrtc_offer.assert_awaited_once_with(
        "offer-sdp", "offer-session", send_message
    )
    manager.async_on_webrtc_candidate.assert_awaited_once_with(
        "candidate-session", candidate
    )
    manager.close_webrtc_session.assert_called_once_with("closed-session")


@pytest.mark.asyncio
async def test_camera_removal_unsubscribes_recording_and_stops_webrtc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entity removal drops its listener without stopping the shared recorder."""
    calls: list[str] = []

    async def super_cleanup(_entity: object) -> None:
        calls.append("entity")

    entity, manager, _manager_factory = _camera(monkeypatch)
    remove_listener = entity._remove_recording_listener
    remove_listener.side_effect = lambda: calls.append("listener")
    manager.async_shutdown.side_effect = lambda: calls.append("webrtc")
    monkeypatch.setattr(
        camera_module.NanoKVMEntity,
        "async_will_remove_from_hass",
        super_cleanup,
    )

    await entity.async_will_remove_from_hass()

    assert calls == ["entity", "listener", "webrtc"]
    remove_listener.assert_called_once_with()
    manager.async_shutdown.assert_awaited_once_with()
