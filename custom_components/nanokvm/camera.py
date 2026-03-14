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
from nanokvm.client import NanoKVMClient
from webrtc_models import RTCIceCandidateInit

from .camera_webrtc import NanoKVMWebRTCManager
from .coordinator import NanoKVMDataUpdateCoordinator
from .const import CONF_SSL_FINGERPRINT, DOMAIN, ICON_HDMI
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)

LOGIN_TIMEOUT_SECONDS = 15
WEBSOCKET_HEARTBEAT_SECONDS = 30.0
MAX_PENDING_ICE_CANDIDATES = 64
SNAPSHOT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, kw_only=True)
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
            client_factory=self._create_stream_client,
            authenticate_client=self._authenticate_stream_client,
            login_timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            websocket_heartbeat_seconds=WEBSOCKET_HEARTBEAT_SECONDS,
            max_pending_ice_candidates=MAX_PENDING_ICE_CANDIDATES,
        )

    def _stream_credentials(self) -> tuple[str, str] | None:
        """Return configured stream credentials."""
        config_entry = self.coordinator.config_entry
        if not config_entry or not config_entry.data:
            return None

        username = config_entry.data.get("username")
        password = config_entry.data.get("password")

        if not username or not password:
            return None

        return username, password

    def _create_stream_client(self) -> NanoKVMClient | None:
        """Create a NanoKVM client using the integration's resolved transport."""
        config_entry = self.coordinator.config_entry
        if not config_entry or not config_entry.data:
            return None

        active_url = str(self.coordinator.client.url)
        ssl_fingerprint = (
            config_entry.data.get(CONF_SSL_FINGERPRINT)
            if self.coordinator.client.url.scheme == "https"
            else None
        )
        return NanoKVMClient(
            active_url,
            token=self.coordinator.client.token,
            ssl_fingerprint=ssl_fingerprint,
        )

    async def _authenticate_stream_client(self, client: NanoKVMClient) -> None:
        """Authenticate a stream client using the configured credentials."""
        credentials = self._stream_credentials()
        if credentials is None:
            raise RuntimeError("Missing NanoKVM stream credentials")

        if client.token:
            return

        username, password = credentials
        await client.authenticate(username, password)

    async def _async_read_snapshot_frame(self) -> bytes | None:
        """Read one JPEG frame from NanoKVM MJPEG endpoint for snapshots."""
        client = self._create_stream_client()
        if client is None:
            return None

        async with client:
            await self._authenticate_stream_client(client)
            # Reuse NanoKVMClient's authenticated session and SSL config for MJPEG.
            async with client._request(
                aiohttp.hdrs.METH_GET,
                "/stream/mjpeg",
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
