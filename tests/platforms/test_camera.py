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
import pytest
from webrtc_models import RTCIceCandidateInit
from yarl import URL

import custom_components.nanokvm.camera as camera_module
from custom_components.nanokvm.const import CONF_SSL_FINGERPRINT, DOMAIN


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

    entity = camera_module.NanoKVMCamera(
        coordinator or _coordinator(), camera_module.CAMERAS[0]
    )
    return entity, manager, manager_factory


def test_camera_description_inventory_and_default_availability() -> None:
    """The platform exposes one always-created HDMI stream description."""
    assert [description.key for description in camera_module.CAMERAS] == ["hdmi"]
    assert camera_module.CAMERAS[0].available_fn(_coordinator()) is True


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
    assert entity.is_streaming is True
    assert entity.state == CameraState.STREAMING
    assert entity.available is True
    coordinator.last_update_success = False
    assert entity.available is False
    assert await entity.stream_source() is None
    assert entity.camera_capabilities.frontend_stream_types == {StreamType.WEB_RTC}

    manager_factory.assert_called_once()
    manager_options = manager_factory.call_args.kwargs
    assert manager_options["logger"] is camera_module._LOGGER
    assert manager_options["hass_provider"]() is hass_mock
    assert manager_options["client_factory"] == entity._create_stream_client
    assert manager_options["authenticate_client"] == entity._authenticate_stream_client
    assert manager_options["is_pro_hardware"]() is True
    assert manager_options["login_timeout_seconds"] == 15
    assert manager_options["websocket_heartbeat_seconds"] == 30.0
    assert manager_options["max_pending_ice_candidates"] == 64


@pytest.mark.parametrize(
    ("config_entry", "expected"),
    [
        (None, None),
        (SimpleNamespace(data={}), None),
        (SimpleNamespace(data={CONF_PASSWORD: "password"}), None),
        (SimpleNamespace(data={CONF_USERNAME: "admin"}), None),
        (
            SimpleNamespace(data={CONF_USERNAME: "admin", CONF_PASSWORD: "password"}),
            ("admin", "password"),
        ),
    ],
)
def test_stream_credentials_require_complete_config(
    monkeypatch: pytest.MonkeyPatch,
    config_entry: SimpleNamespace | None,
    expected: tuple[str, str] | None,
) -> None:
    """Stream credentials are usable only when both values are configured."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(config_entry=config_entry)
    )

    assert entity._stream_credentials() == expected


@pytest.mark.parametrize("config_entry", [None, SimpleNamespace(data={})])
def test_create_stream_client_requires_config_data(
    monkeypatch: pytest.MonkeyPatch,
    config_entry: SimpleNamespace | None,
) -> None:
    """No standalone stream client is created without entry data."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(config_entry=config_entry)
    )
    client_factory = MagicMock()
    monkeypatch.setattr(camera_module, "NanoKVMClient", client_factory)

    assert entity._create_stream_client() is None
    client_factory.assert_not_called()


@pytest.mark.parametrize(
    ("url", "expected_fingerprint"),
    [
        (URL("http://nanokvm.local/api/"), None),
        (URL("https://nanokvm.local/api/"), "sha256:fingerprint"),
    ],
)
def test_create_stream_client_reuses_active_transport(
    monkeypatch: pytest.MonkeyPatch,
    url: URL,
    expected_fingerprint: str | None,
) -> None:
    """Stream clients inherit the resolved URL, token, and HTTPS fingerprint."""
    config_entry = SimpleNamespace(
        data={
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "password",
            CONF_SSL_FINGERPRINT: "sha256:fingerprint",
        }
    )
    entity, _manager, _manager_factory = _camera(
        monkeypatch,
        _coordinator(
            client=SimpleNamespace(url=url, token="existing-token"),
            config_entry=config_entry,
        ),
    )
    stream_client = object()
    client_factory = MagicMock(return_value=stream_client)
    monkeypatch.setattr(camera_module, "NanoKVMClient", client_factory)

    assert entity._create_stream_client() is stream_client
    client_factory.assert_called_once_with(
        str(url),
        token="existing-token",
        ssl_fingerprint=expected_fingerprint,
    )


@pytest.mark.asyncio
async def test_authenticate_stream_client_rejects_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing credentials fail before an authentication request is attempted."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(config_entry=None)
    )
    client = SimpleNamespace(token=None, authenticate=AsyncMock())

    with pytest.raises(RuntimeError, match="Missing NanoKVM stream credentials"):
        await entity._authenticate_stream_client(client)

    client.authenticate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["existing-token", None])
async def test_authenticate_stream_client_uses_existing_or_configured_auth(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    """Existing tokens bypass login while empty clients authenticate once."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    client = SimpleNamespace(token=token, authenticate=AsyncMock())

    await entity._authenticate_stream_client(client)

    if token:
        client.authenticate.assert_not_awaited()
    else:
        client.authenticate.assert_awaited_once_with("admin", "password")


@pytest.mark.asyncio
async def test_snapshot_is_suppressed_on_pro_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pro snapshot never opens MJPEG because it would interrupt WebRTC."""
    entity, _manager, _manager_factory = _camera(
        monkeypatch, _coordinator(is_pro_hardware=True)
    )
    create_client = MagicMock()
    monkeypatch.setattr(entity, "_create_stream_client", create_client)

    assert await entity._async_read_snapshot_frame() is None
    create_client.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_returns_none_when_client_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot reads stop cleanly when the config cannot build a client."""
    entity, _manager, _manager_factory = _camera(monkeypatch)
    monkeypatch.setattr(entity, "_create_stream_client", MagicMock(return_value=None))

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
    monkeypatch.setattr(entity, "_create_stream_client", MagicMock(return_value=client))
    monkeypatch.setattr(entity, "_authenticate_stream_client", authenticate)
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
async def test_camera_removal_runs_entity_and_webrtc_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal executes inherited entity cleanup before manager shutdown."""
    calls: list[str] = []

    async def super_cleanup(_entity: object) -> None:
        calls.append("entity")

    entity, manager, _manager_factory = _camera(monkeypatch)
    manager.async_shutdown.side_effect = lambda: calls.append("webrtc")
    monkeypatch.setattr(
        camera_module.NanoKVMEntity,
        "async_will_remove_from_hass",
        super_cleanup,
    )

    await entity.async_will_remove_from_hass()

    assert calls == ["entity", "webrtc"]
    manager.async_shutdown.assert_awaited_once_with()
