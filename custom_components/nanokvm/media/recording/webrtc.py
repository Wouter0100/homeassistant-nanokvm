"""aiortc WebRTC recording backend for NanoKVM Pro."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from fractions import Fraction
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRecorder
from aiortc.sdp import candidate_from_sdp
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..signaling import (
    AuthenticateClientCallable,
    NanoKVMWebRTCManager,
    StreamClientFactory,
)
from . import async_wait_for_event, async_wait_for_result, temporary_recording_path

# NanoKVM's Pro signaling socket is otherwise idle after negotiation. Keep it
# active more frequently than common LAN/NAT idle timeouts while recording.
RECORDING_SIGNALING_HEARTBEAT_SECONDS = 10.0
WEBRTC_VIDEO_ENCODER_OPTIONS = {
    "crf": "23",
    "preset": "ultrafast",
    "tune": "zerolatency",
}


class _FirstFrameTrack(MediaStreamTrack):
    """Proxy a remote track and expose when its first frame is consumed."""

    def __init__(
        self,
        source: MediaStreamTrack,
        first_frame: asyncio.Event,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the proxy track."""
        super().__init__()
        self._source = source
        self._first_frame = first_frame
        self._clock = clock
        self._capture_origin: float | None = None
        self._last_output_pts = -1

    @property
    def kind(self) -> str:
        """Return the proxied media kind."""
        return self._source.kind

    async def recv(self) -> Any:
        """Receive one frame and release the startup barrier."""
        while True:
            frame = await self._source.recv()
            capture_time = self._clock()
            if self._capture_origin is None:
                self._capture_origin = capture_time
            output_pts = int((capture_time - self._capture_origin) * 30)

            if output_pts <= self._last_output_pts:
                continue
            self._last_output_pts = output_pts
            frame.pts = output_pts
            frame.time_base = Fraction(1, 30)
            break

        self._first_frame.set()
        return frame



class WebRTCRecordingBackend:
    """Record a NanoKVM Pro WebRTC session into an MP4 file."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        hass_provider: Callable[[], HomeAssistant | None],
        client_factory: StreamClientFactory,
        authenticate_client: AuthenticateClientCallable,
    ) -> None:
        """Initialize the WebRTC backend."""
        self._logger = logger
        self._hass_provider = hass_provider
        self._client_factory = client_factory
        self._authenticate_client = authenticate_client
        self._peer_connection_factory = RTCPeerConnection
        self._media_recorder_factory = MediaRecorder
        self._signaling_manager_factory = NanoKVMWebRTCManager

    async def async_record(
        self,
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: Callable[[], None],
    ) -> None:
        """Record a NanoKVM WebRTC session into an MP4 file."""
        output_path = Path(filename)
        temporary_path = temporary_recording_path(output_path)
        temporary_path.unlink(missing_ok=True)

        peer_connection = self._peer_connection_factory()
        media_recorder = self._media_recorder_factory(str(temporary_path))
        signaling = self._signaling_manager_factory(
            logger=self._logger,
            hass_provider=self._hass_provider,
            client_factory=self._client_factory,
            authenticate_client=self._authenticate_client,
            is_pro_hardware=lambda: True,
            signaling_heartbeat_seconds=RECORDING_SIGNALING_HEARTBEAT_SECONDS,
        )
        expected_kinds = {"video"}
        if include_audio:
            expected_kinds.add("audio")

        first_video_frame = asyncio.Event()
        tracks_ready = asyncio.Event()
        remote_description_set = False
        received_kinds: set[str] = set()
        pending_candidates: list[Any] = []
        candidate_tasks: set[asyncio.Task[None]] = set()
        answer_future: asyncio.Future[str] = (
            asyncio.get_running_loop().create_future()
        )
        signaling_error: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        recorder_started = False
        capture_active = False

        async def add_candidate(candidate: Any) -> None:
            try:
                await peer_connection.addIceCandidate(
                    _candidate_for_aiortc(candidate)
                )
            except Exception as err:
                if not signaling_error.done():
                    signaling_error.set_exception(
                        HomeAssistantError(
                            f"Unable to apply NanoKVM WebRTC candidate: {err}"
                        )
                    )

        def send_message(message: object) -> None:
            nonlocal remote_description_set
            if isinstance(message, WebRTCAnswer):
                if not answer_future.done():
                    answer_future.set_result(message.answer)
                return
            if isinstance(message, WebRTCCandidate):
                if remote_description_set:
                    task = asyncio.create_task(add_candidate(message.candidate))
                    candidate_tasks.add(task)
                    task.add_done_callback(candidate_tasks.discard)
                else:
                    pending_candidates.append(message.candidate)
                return
            if isinstance(message, WebRTCError) and not signaling_error.done():
                signaling_error.set_exception(HomeAssistantError(message.message))

        def on_media_task_done(task: asyncio.Task[None], kind: str) -> None:
            if task.cancelled() or signaling_error.done():
                return
            error = task.exception()
            detail = str(error) if error is not None else "media track ended"
            self._logger.warning(
                "NanoKVM media recorder worker ended: kind=%s detail=%s "
                "peer_state=%s",
                kind,
                detail,
                peer_connection.connectionState,
            )
            signaling_error.set_exception(
                HomeAssistantError(f"NanoKVM media pipeline failed: {detail}")
            )

        @peer_connection.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind not in expected_kinds or track.kind in received_kinds:
                return
            received_kinds.add(track.kind)
            media_recorder.addTrack(
                _FirstFrameTrack(track, first_video_frame)
                if track.kind == "video"
                else track
            )
            if expected_kinds.issubset(received_kinds):
                tracks_ready.set()

        @peer_connection.on("connectionstatechange")
        def on_connection_state_change() -> None:
            if (
                peer_connection.connectionState in {"failed", "disconnected"}
                and not signaling_error.done()
            ):
                signaling_error.set_exception(
                    HomeAssistantError(
                        "NanoKVM WebRTC peer connection failed or disconnected"
                    )
                )

        try:
            peer_connection.addTransceiver("video", direction="recvonly")
            if include_audio:
                peer_connection.addTransceiver("audio", direction="recvonly")

            offer = await peer_connection.createOffer()
            await peer_connection.setLocalDescription(offer)
            local_description = peer_connection.localDescription
            if local_description is None:
                raise HomeAssistantError("Unable to create an HDMI recording offer")

            await signaling.async_handle_async_webrtc_offer(
                local_description.sdp,
                f"recording-{uuid4().hex}",
                send_message,
            )
            answer_sdp = await async_wait_for_result(
                answer_future,
                signaling_error,
                description="NanoKVM WebRTC answer",
            )
            await peer_connection.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            remote_description_set = True
            for candidate in pending_candidates:
                await add_candidate(candidate)
            pending_candidates.clear()
            if signaling_error.done():
                raise signaling_error.exception()  # type: ignore[misc]

            await async_wait_for_event(
                tracks_ready,
                signaling_error,
                description="NanoKVM HDMI media tracks",
            )
            _configure_media_recorder(media_recorder)
            await media_recorder.start()
            recorder_started = True
            for kind, task in _media_recorder_tasks(media_recorder):
                task.add_done_callback(
                    lambda completed, track_kind=kind: on_media_task_done(
                        completed, track_kind
                    )
                )
            await async_wait_for_event(
                first_video_frame,
                signaling_error,
                description="first NanoKVM HDMI video frame",
            )
            capture_active = True
            on_started()

            stop_task = asyncio.create_task(stop_event.wait())
            duration_task = asyncio.create_task(asyncio.sleep(duration))
            try:
                done, _pending = await asyncio.wait(
                    {stop_task, duration_task, signaling_error},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if signaling_error in done:
                    raise signaling_error.exception()  # type: ignore[misc]
            finally:
                stop_task.cancel()
                duration_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                with suppress(asyncio.CancelledError):
                    await duration_task
        finally:
            if recorder_started:
                with suppress(Exception):
                    await media_recorder.stop()
            for task in candidate_tasks:
                task.cancel()
            if candidate_tasks:
                await asyncio.gather(*candidate_tasks, return_exceptions=True)
            await signaling.async_shutdown()
            await peer_connection.close()

            if (
                capture_active
                and temporary_path.exists()
                and temporary_path.stat().st_size > 0
            ):
                temporary_path.replace(output_path)
            else:
                temporary_path.unlink(missing_ok=True)


def _candidate_for_aiortc(candidate: Any) -> Any:
    """Convert the HA WebRTC candidate model into aiortc's representation."""
    if not hasattr(candidate, "candidate"):
        return candidate

    candidate_sdp = candidate.candidate.removeprefix("candidate:")
    parsed = candidate_from_sdp(candidate_sdp)
    parsed.sdpMid = candidate.sdp_mid
    parsed.sdpMLineIndex = candidate.sdp_m_line_index
    return parsed


def _configure_media_recorder(media_recorder: Any) -> None:
    """Configure WebRTC video encoding for low-latency Home Assistant use."""
    contexts = getattr(media_recorder, "_MediaRecorder__tracks", {}).items()
    for track, context in contexts:
        if getattr(track, "kind", None) != "video":
            continue
        context.stream.options = {
            **getattr(context.stream, "options", {}),
            **WEBRTC_VIDEO_ENCODER_OPTIONS,
        }


def _media_recorder_tasks(
    media_recorder: Any,
) -> tuple[tuple[str, asyncio.Task[None]], ...]:
    """Return aiortc MediaRecorder workers for failure monitoring."""
    contexts = getattr(media_recorder, "_MediaRecorder__tracks", {}).items()
    return tuple(
        (getattr(track, "kind", "unknown"), context.task)
        for track, context in contexts
        if context.task is not None
    )
