"""Camera platform for Sipeed NanoKVM."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from aiohttp import BodyPartReader, MultipartReader
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.camera.webrtc import WebRTCSendMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from nanokvm.utils import obfuscate_password
from webrtc_models import RTCIceCandidateInit

from .camera_webrtc import NanoKVMWebRTCManager
from .coordinator import NanoKVMDataUpdateCoordinator
from .config_flow import normalize_host
from .const import DOMAIN, ICON_HDMI
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)

LOGIN_TIMEOUT_SECONDS = 15
WEBSOCKET_HEARTBEAT_SECONDS = 30.0
MAX_PENDING_ICE_CANDIDATES = 64
SNAPSHOT_TIMEOUT_SECONDS = 20


@dataclass(kw_only=True)
class NanoKVMCameraEntityDescription(EntityDescription):
    """Describes NanoKVM camera entity."""

    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True


CAMERAS: tuple[NanoKVMCameraEntityDescription, ...] = (
    NanoKVMCameraEntityDescription(
        key="hdmi",
        name="HDMI Stream",
        translation_key="hdmi",
        icon=ICON_HDMI,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM camera based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMCamera(
            coordinator=coordinator,
            description=description,
        )
        for description in CAMERAS
        if description.available_fn(coordinator)
    )


class NanoKVMCamera(NanoKVMEntity, Camera):
    """Defines a NanoKVM camera."""

    entity_description: NanoKVMCameraEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMCameraEntityDescription,
    ) -> None:
        """Initialize NanoKVM camera."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"camera_{description.key}",
        )
        Camera.__init__(self)
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_is_streaming = True
        self._webrtc = NanoKVMWebRTCManager(
            logger=_LOGGER,
            hass_provider=lambda: self.hass,
            connection_info_provider=self._stream_connection_info,
            authenticate_stream=self._authenticate_stream,
            login_timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            websocket_heartbeat_seconds=WEBSOCKET_HEARTBEAT_SECONDS,
            max_pending_ice_candidates=MAX_PENDING_ICE_CANDIDATES,
        )

    def _stream_connection_info(self) -> tuple[str, str, str] | None:
        """Return normalized base URL and credentials for stream auth/signaling."""
        config_entry = self.coordinator.config_entry
        if not config_entry or not config_entry.data:
            return None

        host = config_entry.data.get("host")
        username = config_entry.data.get("username")
        password = config_entry.data.get("password")

        if not host or not username or not password:
            return None

        return normalize_host(host), username, password

    async def _authenticate_stream(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
    ) -> str:
        """Authenticate directly against NanoKVM API and return JWT token."""
        async with session.post(
            f"{base_url}auth/login",
            json={
                "username": username,
                "password": obfuscate_password(password),
            },
            timeout=aiohttp.ClientTimeout(total=LOGIN_TIMEOUT_SECONDS),
            raise_for_status=True,
        ) as response:
            payload = await response.json(content_type=None)

        code = payload.get("code") if isinstance(payload, dict) else None
        token = payload.get("data", {}).get("token") if isinstance(payload, dict) else None
        if code != 0 or not token:
            msg = payload.get("msg", "unknown") if isinstance(payload, dict) else "unknown"
            raise RuntimeError(f"Stream authentication failed: code={code}, msg={msg}")
        return token

    async def _async_read_snapshot_frame(self) -> bytes | None:
        """Read one JPEG frame from NanoKVM MJPEG endpoint for snapshots."""
        conn = self._stream_connection_info()
        if conn is None:
            return None

        base_url, username, password = conn
        stream_url = f"{base_url}stream/mjpeg"

        async with aiohttp.ClientSession() as session:
            token = await self._authenticate_stream(session, base_url, username, password)
            async with session.get(
                stream_url,
                cookies={"nano-kvm-token": token},
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                raise_for_status=True,
            ) as upstream:
                reader = MultipartReader.from_response(upstream)

                while True:
                    async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                        part = await reader.next()

                    if part is None:
                        return None
                    if not isinstance(part, BodyPartReader):
                        continue

                    async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                        payload = await part.read()

                    if payload:
                        return payload

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        try:
            return await self._async_read_snapshot_frame()
        except Exception as err:
            _LOGGER.error("Error fetching still image: %s", err)
            return None

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle Home Assistant WebRTC offer using NanoKVM signaling."""
        await self._webrtc.async_handle_async_webrtc_offer(
            offer_sdp, session_id, send_message
        )

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward frontend ICE candidates to NanoKVM signaling websocket."""
        await self._webrtc.async_on_webrtc_candidate(session_id, candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session when frontend unsubscribes."""
        self._webrtc.close_webrtc_session(session_id)

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup camera resources when entity is removed."""
        await super().async_will_remove_from_hass()
        await self._webrtc.async_shutdown()
