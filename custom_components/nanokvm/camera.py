"""Camera platform for Sipeed NanoKVM."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from aiohttp import BodyPartReader, MultipartReader
from homeassistant.components.camera import (
    Camera,
    CameraEntityDescription,
    CameraEntityFeature,
)
from homeassistant.components.camera.webrtc import WebRTCSendMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from webrtc_models import RTCIceCandidateInit

from .coordinator import NanoKVMDataUpdateCoordinator
from .const import DOMAIN, ICON_HDMI
from .entity import NanoKVMEntity
from .media.client import NanoKVMStreamClientProvider
from .media.signaling import NanoKVMWebRTCManager

_LOGGER = logging.getLogger(__name__)

LOGIN_TIMEOUT_SECONDS = 15
WEBSOCKET_HEARTBEAT_SECONDS = 30.0
MAX_PENDING_ICE_CANDIDATES = 64
SNAPSHOT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, kw_only=True)
class NanoKVMCameraEntityDescription(CameraEntityDescription):
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
        self._attr_is_streaming = False
        self._client_provider = NanoKVMStreamClientProvider(self.coordinator)
        self._webrtc = NanoKVMWebRTCManager(
            logger=_LOGGER,
            hass_provider=lambda: self.hass,
            client_factory=self._client_provider.create_client,
            authenticate_client=self._client_provider.async_authenticate,
            is_pro_hardware=lambda: self.coordinator.is_pro_hardware,
            session_state_callback=self._handle_streaming_state,
            login_timeout_seconds=LOGIN_TIMEOUT_SECONDS,
            websocket_heartbeat_seconds=WEBSOCKET_HEARTBEAT_SECONDS,
            max_pending_ice_candidates=MAX_PENDING_ICE_CANDIDATES,
        )

    @callback
    def _handle_streaming_state(self, streaming: bool) -> None:
        """Publish active WebRTC session state through the camera entity."""
        self._attr_is_streaming = streaming
        self.async_write_ha_state()

    async def _async_read_snapshot_frame(self) -> bytes | None:
        """Read one JPEG frame from NanoKVM MJPEG endpoint for snapshots."""
        if self.coordinator.is_pro_hardware:
            # NanoKVM Pro shares one encoder mode; opening MJPEG interrupts WebRTC.
            _LOGGER.debug(
                "Skipping NanoKVM Pro still image to avoid switching video mode"
            )
            return None

        client = self._client_provider.create_client()
        if client is None:
            return None

        async with client:
            await self._client_provider.async_authenticate(client)
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
            _LOGGER.warning("Error fetching still image: %s", err)
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
