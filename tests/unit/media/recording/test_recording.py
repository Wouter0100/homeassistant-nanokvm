"""Behavior tests for NanoKVM WebRTC recording lifecycle."""

from __future__ import annotations

import asyncio
from fractions import Fraction
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from homeassistant.exceptions import HomeAssistantError
import pytest
from webrtc_models import RTCIceCandidateInit
from yarl import URL

from custom_components.nanokvm.media.recording import (
    async_wait_for_event as _async_wait_for_event,
    async_wait_for_result as _async_wait_for_result,
    temporary_recording_path as _temporary_recording_path,
)
from custom_components.nanokvm.media.recording.controller import (
    NanoKVMRecordingController,
)
from custom_components.nanokvm.media.recording.direct_h264 import (
    direct_h264_timestamp_payload as _direct_h264_timestamp_payload,
    is_h264_keyframe,
)
from custom_components.nanokvm.media.recording.webrtc import (
    _FirstFrameTrack,
    _candidate_for_aiortc,
    _configure_media_recorder,
)


class FakeConfigEntry:
    """Config-entry task owner used by recorder lifecycle tests."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []
        self.calls: list[tuple[object, str, bool]] = []

    def async_create_background_task(
        self,
        hass: object,
        target: object,
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[None]:
        """Create and retain the requested background task."""
        self.calls.append((hass, name, eager_start))
        task = asyncio.create_task(target)
        self.tasks.append(task)
        return task


def _recorder(
    *,
    hass: object | None = None,
    entry: FakeConfigEntry | None = None,
    client: object | None = None,
    use_direct_stream: bool = False,
) -> tuple[NanoKVMRecordingController, MagicMock, FakeConfigEntry]:
    """Create a recorder with observable lifecycle boundaries."""
    state_callback = MagicMock()
    config_entry = entry or FakeConfigEntry()
    recorder = NanoKVMRecordingController(
        logger=logging.getLogger("test.nanokvm.recorder"),
        hass_provider=lambda: hass if hass is not None else SimpleNamespace(),
        config_entry_provider=lambda: config_entry,
        client_factory=lambda: client if client is not None else SimpleNamespace(),
        authenticate_client=AsyncMock(),
        state_callback=state_callback,
        use_direct_stream=use_direct_stream,
    )
    return recorder, state_callback, config_entry


def test_temporary_recording_path_preserves_container_extension() -> None:
    """Temporary output keeps .mp4 so PyAV selects the intended container."""
    assert _temporary_recording_path(Path("/config/www/capture.mp4")) == Path(
        "/config/www/capture.tmp.mp4"
    )


def test_direct_h264_header_preserves_microsecond_timestamp() -> None:
    """Direct packets expose a WebCodecs-compatible microsecond timestamp."""
    timestamp = 9_876_543
    payload = b"h264-payload"

    assert _direct_h264_timestamp_payload(
        b"\x01" + timestamp.to_bytes(8, "little") + payload
    ) == (timestamp, payload)


def test_direct_h264_keyframe_detection_requires_an_idr_nal() -> None:
    """The direct recorder must not publish undecodable leading delta frames."""
    assert not is_h264_keyframe(b"\x00\x00\x00\x01\x41delta")
    assert is_h264_keyframe(
        b"\x00\x00\x00\x01\x67sps\x00\x00\x01\x68pps"
        b"\x00\x00\x01\x65idr"
    )


@pytest.mark.asyncio
async def test_start_requires_home_assistant_and_config_entry() -> None:
    """Recorder startup fails cleanly when entity ownership is not ready."""
    recorder = NanoKVMRecordingController(
        logger=logging.getLogger("test.nanokvm.recorder"),
        hass_provider=lambda: None,
        config_entry_provider=lambda: None,
        client_factory=lambda: None,
        authenticate_client=AsyncMock(),
        state_callback=MagicMock(),
    )

    with pytest.raises(HomeAssistantError, match="Home Assistant is not ready"):
        await recorder.async_start(
            filename="/config/www/capture.mp4",
            duration=60,
            include_audio=True,
        )


@pytest.mark.asyncio
async def test_start_uses_config_entry_background_task_and_waits_until_active(
    tmp_path: Path,
) -> None:
    """Start returns only after the worker confirms a captured video frame."""
    hass = object()
    recorder, state_callback, entry = _recorder(hass=hass)
    release = asyncio.Event()

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        assert filename == str(tmp_path / "capture.mp4")
        assert duration == 30
        assert include_audio is False
        on_started()
        await release.wait()

    recorder._async_record = record

    await recorder.async_start(
        filename=str(tmp_path / "capture.mp4"),
        duration=30,
        include_audio=False,
    )

    assert entry.calls == [(hass, "NanoKVM HDMI recording", False)]
    state_callback.assert_called_once_with(True)

    release.set()
    await entry.tasks[0]
    assert state_callback.call_args_list[-1].args == (False,)


@pytest.mark.asyncio
async def test_duplicate_start_is_rejected_and_stop_finishes_worker(
    tmp_path: Path,
) -> None:
    """One camera owns at most one recording while stop remains idempotent."""
    recorder, state_callback, entry = _recorder()

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        on_started()
        await stop_event.wait()

    recorder._async_record = record
    await recorder.async_start(
        filename=str(tmp_path / "first.mp4"), duration=60, include_audio=True
    )

    with pytest.raises(HomeAssistantError, match="already in progress"):
        await recorder.async_start(
            filename=str(tmp_path / "second.mp4"), duration=60, include_audio=True
        )

    await recorder.async_stop()
    await recorder.async_stop()

    assert entry.tasks[0].done()
    assert state_callback.call_args_list == [call(True), call(False)]


@pytest.mark.asyncio
async def test_controller_notifies_multiple_consumers_and_tracks_filename(
    tmp_path: Path,
) -> None:
    """Camera and switch can observe the same entry-scoped recording session."""
    recorder, state_callback, entry = _recorder()
    listener = MagicMock()
    remove_listener = recorder.async_add_state_listener(listener)

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        on_started()
        await stop_event.wait()

    recorder._async_record = record
    filename = str(tmp_path / "shared.mp4")

    await recorder.async_start(filename=filename, duration=60, include_audio=False)

    assert recorder.is_recording is True
    assert recorder.current_filename == filename
    assert state_callback.call_args_list == [call(True)]
    assert listener.call_args_list == [call(True)]

    remove_listener()
    await recorder.async_stop()
    await entry.tasks[0]

    assert recorder.is_recording is False
    assert recorder.current_filename is None
    assert state_callback.call_args_list == [call(True), call(False)]
    assert listener.call_args_list == [call(True)]


@pytest.mark.asyncio
async def test_state_listener_failure_does_not_leak_recording_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken consumer cannot prevent controller state from being released."""
    recorder, _state_callback, entry = _recorder()

    def failing_listener(recording: bool) -> None:
        if not recording:
            raise RuntimeError("listener failed")

    recorder.async_add_state_listener(failing_listener)

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        on_started()
        await stop_event.wait()

    recorder._async_record = record
    await recorder.async_start(
        filename=str(tmp_path / "listener.mp4"),
        duration=60,
        include_audio=False,
    )

    with caplog.at_level(logging.ERROR, logger="test.nanokvm.recorder"):
        await recorder.async_stop()
    await entry.tasks[0]

    assert recorder.is_recording is False
    assert recorder.current_filename is None
    assert "recording state listener failed" in caplog.text


@pytest.mark.asyncio
async def test_startup_failure_is_returned_without_recording_state(
    tmp_path: Path,
) -> None:
    """Failures before first frame fail the service call and never report recording."""
    recorder, state_callback, entry = _recorder()
    recorder._async_record = AsyncMock(side_effect=RuntimeError("no video track"))

    with pytest.raises(HomeAssistantError, match="Unable to start HDMI recording"):
        await recorder.async_start(
            filename=str(tmp_path / "capture.mp4"),
            duration=60,
            include_audio=True,
        )

    await entry.tasks[0]
    state_callback.assert_not_called()


@pytest.mark.asyncio
async def test_worker_return_before_activation_fails_start_instead_of_hanging(
    tmp_path: Path,
) -> None:
    """A defensive early worker return must always settle the waiting service call."""
    recorder, state_callback, entry = _recorder()
    recorder._async_record = AsyncMock(return_value=None)

    with pytest.raises(HomeAssistantError, match="ended before it became active"):
        async with asyncio.timeout(1):
            await recorder.async_start(
                filename=str(tmp_path / "capture.mp4"),
                duration=60,
                include_audio=True,
            )

    await entry.tasks[0]
    state_callback.assert_not_called()


@pytest.mark.asyncio
async def test_background_task_creation_failure_releases_recorder_ownership(
    tmp_path: Path,
) -> None:
    """A scheduling error cannot permanently leave the camera marked busy."""

    class BrokenConfigEntry(FakeConfigEntry):
        def async_create_background_task(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("scheduler unavailable")

    recorder, _state_callback, _entry = _recorder(entry=BrokenConfigEntry())

    with pytest.raises(HomeAssistantError, match="Unable to schedule HDMI recording"):
        await recorder.async_start(
            filename=str(tmp_path / "capture.mp4"),
            duration=60,
            include_audio=True,
        )

    assert recorder._session is None


@pytest.mark.asyncio
async def test_runtime_failure_is_logged_and_clears_recording_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure after activation preserves service success but returns state to idle."""
    recorder, state_callback, entry = _recorder()

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        on_started()
        await asyncio.sleep(0)
        raise RuntimeError("signaling disconnected")

    recorder._async_record = record

    with caplog.at_level(logging.ERROR, logger="test.nanokvm.recorder"):
        await recorder.async_start(
            filename=str(tmp_path / "capture.mp4"),
            duration=60,
            include_audio=True,
        )
        await entry.tasks[0]

    assert state_callback.call_args_list == [call(True), call(False)]
    assert "HDMI recording ended unexpectedly: signaling disconnected" in caplog.text


@pytest.mark.asyncio
async def test_stop_during_startup_cancels_worker_and_unblocks_start(
    tmp_path: Path,
) -> None:
    """Unload or stop cannot leave a service call waiting for startup forever."""
    recorder, state_callback, _entry = _recorder()
    worker_started = asyncio.Event()

    async def record(
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: object,
    ) -> None:
        worker_started.set()
        await asyncio.Event().wait()

    recorder._async_record = record
    start_task = asyncio.create_task(
        recorder.async_start(
            filename=str(tmp_path / "capture.mp4"),
            duration=60,
            include_audio=True,
        )
    )
    await worker_started.wait()
    await recorder.async_shutdown()

    with pytest.raises(HomeAssistantError, match="stopped before it became active"):
        await start_task
    state_callback.assert_not_called()


@pytest.mark.asyncio
async def test_worker_cancelled_before_first_instruction_unblocks_start(
    tmp_path: Path,
) -> None:
    """Config-entry unload can cancel a newly scheduled task before its coroutine runs."""

    class ImmediatelyCancelledEntry(FakeConfigEntry):
        def async_create_background_task(
            self,
            hass: object,
            target: object,
            name: str,
            eager_start: bool = True,
        ) -> asyncio.Task[None]:
            task = super().async_create_background_task(
                hass, target, name, eager_start
            )
            task.cancel()
            return task

    recorder, state_callback, _entry = _recorder(entry=ImmediatelyCancelledEntry())

    with pytest.raises(HomeAssistantError, match="stopped before it became active"):
        async with asyncio.timeout(1):
            await recorder.async_start(
                filename=str(tmp_path / "capture.mp4"),
                duration=60,
                include_audio=True,
            )

    assert recorder._session is None
    assert recorder.current_filename is None
    state_callback.assert_not_called()


class FakeSourceTrack:
    """Track source returning one prescribed frame."""

    kind = "video"

    def __init__(self) -> None:
        self.frame = SimpleNamespace(pts=0, time_base=Fraction(1, 90000))

    async def recv(self) -> object:
        """Return the next frame."""
        return self.frame


@pytest.mark.asyncio
async def test_first_frame_track_marks_actual_capture_before_returning_frame() -> None:
    """The startup barrier is tied to the first frame consumed by MediaRecorder."""
    source = FakeSourceTrack()
    first_frame = asyncio.Event()
    track = _FirstFrameTrack(source, first_frame)

    assert track.kind == "video"
    assert await track.recv() is source.frame
    assert first_frame.is_set()


@pytest.mark.asyncio
async def test_video_track_downsamples_60_fps_to_monotonic_30_fps_timestamps() -> None:
    """The 30 fps MP4 encoder must never receive two frames for one output tick."""

    class TimestampSource:
        kind = "video"

        def __init__(self) -> None:
            self.frames = iter(
                SimpleNamespace(pts=pts, time_base=Fraction(1, 90000))
                for pts in (0, 1500, 3000)
            )
            self.recv_count = 0

        async def recv(self) -> object:
            self.recv_count += 1
            return next(self.frames)

    source = TimestampSource()
    capture_times = iter((0.0, 0.01, 0.034))
    track = _FirstFrameTrack(
        source,
        asyncio.Event(),
        clock=lambda: next(capture_times),
    )

    first = await track.recv()
    second = await track.recv()

    assert source.recv_count == 3
    assert (first.pts, first.time_base) == (0, Fraction(1, 30))
    assert (second.pts, second.time_base) == (1, Fraction(1, 30))


@pytest.mark.asyncio
async def test_video_track_rebases_large_timestamp_discontinuities() -> None:
    """A WebRTC timestamp jump must not become a multi-hour MP4 gap."""

    class TimestampSource:
        kind = "video"

        def __init__(self) -> None:
            self.frames = iter(
                SimpleNamespace(pts=pts, time_base=Fraction(1, 90000))
                for pts in (0, 3000, 4_294_967_000, 4_294_970_000)
            )

        async def recv(self) -> object:
            return next(self.frames)

    capture_times = iter((0.0, 1 / 30, 2 / 30, 3 / 30))
    track = _FirstFrameTrack(
        TimestampSource(),
        asyncio.Event(),
        clock=lambda: next(capture_times),
    )

    output = [await track.recv() for _ in range(4)]

    assert [frame.pts for frame in output] == [0, 1, 2, 3]
    assert {frame.time_base for frame in output} == {Fraction(1, 30)}


@pytest.mark.asyncio
async def test_video_track_synthesizes_timestamps_when_source_has_none() -> None:
    """A defensive timestamp fallback remains monotonic for unusual tracks."""

    class UntimedSource:
        kind = "video"

        async def recv(self) -> object:
            return SimpleNamespace(pts=None, time_base=None)

    capture_times = iter((0.0, 1 / 30))
    track = _FirstFrameTrack(
        UntimedSource(),
        asyncio.Event(),
        clock=lambda: next(capture_times),
    )

    first = await track.recv()
    second = await track.recv()

    assert (first.pts, first.time_base) == (0, Fraction(1, 30))
    assert (second.pts, second.time_base) == (1, Fraction(1, 30))


@pytest.mark.asyncio
async def test_video_track_uses_capture_clock_instead_of_drifting_source_time() -> None:
    """MP4 duration follows elapsed capture time, not unreliable device RTP time."""

    class TimestampSource:
        kind = "video"

        def __init__(self) -> None:
            self.frames = iter(
                SimpleNamespace(pts=pts, time_base=Fraction(1, 90000))
                for pts in (0, 90000)
            )

        async def recv(self) -> object:
            return next(self.frames)

    capture_times = iter((100.0, 104.0))
    track = _FirstFrameTrack(
        TimestampSource(),
        asyncio.Event(),
        clock=lambda: next(capture_times),
    )

    first = await track.recv()
    second = await track.recv()

    assert (first.pts, first.time_base) == (0, Fraction(1, 30))
    assert (second.pts, second.time_base) == (120, Fraction(1, 30))


def test_aiortc_candidate_conversion_preserves_native_candidates() -> None:
    """Already-native aiortc candidates pass through without reparsing."""
    candidate = object()

    assert _candidate_for_aiortc(candidate) is candidate


def test_media_recorder_uses_low_latency_video_encoder_options() -> None:
    """WebRTC recording must not block HA with x264's expensive default preset."""
    video_stream = SimpleNamespace(options={})
    audio_stream = SimpleNamespace(options={"profile": "aac_low"})
    video_track = MagicMock()
    video_track.kind = "video"
    audio_track = MagicMock()
    audio_track.kind = "audio"
    recorder = SimpleNamespace(
        _MediaRecorder__tracks={
            video_track: SimpleNamespace(stream=video_stream),
            audio_track: SimpleNamespace(stream=audio_stream),
        }
    )

    _configure_media_recorder(recorder)

    assert video_stream.options == {
        "crf": "23",
        "preset": "ultrafast",
        "tune": "zerolatency",
    }
    assert audio_stream.options == {"profile": "aac_low"}


class FakePeerConnection:
    """aiortc peer boundary with controllable tracks and connection state."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.transceivers: list[tuple[str, str]] = []
        self.localDescription = SimpleNamespace(sdp="combined-offer", type="offer")
        self.remote_descriptions: list[object] = []
        self.candidates: list[object] = []
        self.connectionState = "new"
        self.closed = False
        self.video_track = FakeSourceTrack()
        self.audio_track = FakeSourceTrack()
        self.audio_track.kind = "audio"

    def on(self, event: str) -> object:
        """Register an aiortc-shaped event callback."""

        def decorator(handler: object) -> object:
            self.handlers[event] = handler
            return handler

        return decorator

    def addTransceiver(self, kind: str, direction: str) -> None:
        """Record requested receive-only media."""
        self.transceivers.append((kind, direction))

    async def createOffer(self) -> object:
        """Return a local offer placeholder."""
        return self.localDescription

    async def setLocalDescription(self, description: object) -> None:
        """Accept the local offer."""

    async def setRemoteDescription(self, description: object) -> None:
        """Accept the answer and emit the negotiated remote tracks."""
        self.remote_descriptions.append(description)
        self.handlers["track"](self.video_track)
        if ("audio", "recvonly") in self.transceivers:
            self.handlers["track"](self.audio_track)

    async def addIceCandidate(self, candidate: object) -> None:
        """Store one mapped remote candidate."""
        self.candidates.append(candidate)

    async def close(self) -> None:
        """Close the peer connection."""
        self.closed = True

    def fail(self) -> None:
        """Emit a failed connection state."""
        self.connectionState = "failed"
        self.handlers["connectionstatechange"]()


class FakeMediaRecorder:
    """MediaRecorder boundary that consumes the first video frame."""

    def __init__(self, filename: str, peer: FakePeerConnection) -> None:
        self.filename = Path(filename)
        self.peer = peer
        self.tracks: list[object] = []
        self.started = False
        self.stopped = False
        self.fail_after_start = False
        self.media_task_error: Exception | None = None
        self._MediaRecorder__tracks: dict[object, object] = {}

    def addTrack(self, track: object) -> None:
        """Retain a negotiated track."""
        self.tracks.append(track)

    async def start(self) -> None:
        """Consume one video frame as the real recorder would."""
        self.started = True
        video_track = next(track for track in self.tracks if track.kind == "video")
        await video_track.recv()
        if self.fail_after_start:
            self.peer.fail()
        if self.media_task_error is not None:
            error = self.media_task_error

            async def fail_media_task() -> None:
                await asyncio.sleep(0)
                raise error

            task = asyncio.create_task(fail_media_task())
            self._MediaRecorder__tracks = {
                object(): SimpleNamespace(task=task)
            }

    async def stop(self) -> None:
        """Finalize a non-empty MP4 placeholder."""
        self.stopped = True
        self.filename.write_bytes(b"mp4-data")


class FakeSignalingManager:
    """NanoKVM WebRTC signaling boundary returning a Pro answer/candidate."""

    instances: list[FakeSignalingManager] = []
    error: WebRTCError | None = None

    def __init__(self, **kwargs: object) -> None:
        self.options = kwargs
        self.offers: list[tuple[str, str]] = []
        self.shutdown = False
        type(self).instances.append(self)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: object
    ) -> None:
        """Return either a signaling error or an answer and ICE candidate."""
        self.offers.append((offer_sdp, session_id))
        if self.error is not None:
            send_message(self.error)
            return
        send_message(WebRTCAnswer(answer="combined-answer"))
        send_message(
            WebRTCCandidate(
                candidate=RTCIceCandidateInit(
                    candidate=(
                        "candidate:1 1 udp 2122260223 192.0.2.1 5000 typ host"
                    ),
                    sdp_mid="video-main",
                    sdp_m_line_index=0,
                    user_fragment=None,
                )
            )
        )

    async def async_shutdown(self) -> None:
        """Mark signaling resources closed."""
        self.shutdown = True


class FakeDirectWebSocket:
    """Direct H.264 websocket that yields one frame and then waits to close."""

    def __init__(
        self,
        messages: list[object] | None = None,
        iterator_error: Exception | None = None,
    ) -> None:
        self.closed = False
        self._closed = asyncio.Event()
        self._messages = messages
        self._iterator_error = iterator_error
        self._index = 0

    def __aiter__(self) -> FakeDirectWebSocket:
        return self

    async def __anext__(self) -> object:
        if self._messages is not None:
            if self._index < len(self._messages):
                message = self._messages[self._index]
                self._index += 1
                if isinstance(message, BaseException):
                    raise message
                return message
            raise StopAsyncIteration
        if self._index == 0:
            self._index += 1
            if self._iterator_error is not None:
                raise self._iterator_error
            return SimpleNamespace(
                type=aiohttp.WSMsgType.BINARY,
                data=(
                    b"\x01"
                    + (123).to_bytes(8, "little")
                    + b"\x00\x00\x00\x01\x65h264"
                ),
            )
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class FakeDirectContainer:
    """PyAV output boundary used by direct-stream lifecycle tests."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.stream = SimpleNamespace(time_base=None)
        self.packets: list[object] = []
        self.closed = False

    def add_stream(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        """Return a controllable H.264 stream."""
        return self.stream

    def mux(self, packet: object) -> None:
        """Accept one packet and retain it for assertions."""
        self.packets.append(packet)

    def close(self) -> None:
        """Finalize a non-empty MP4 placeholder."""
        self.closed = True
        self.output.write_bytes(b"mp4-data")


class FakeDirectPacket:
    """Packet object with the fields populated by the direct muxer."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.pts: int | None = None
        self.dts: int | None = None
        self.time_base: object | None = None
        self.stream: object | None = None


def _direct_client(websocket: FakeDirectWebSocket) -> object:
    """Build an authenticated client with a controllable websocket session."""

    class FakeSession:
        async def ws_connect(
            self, *_args: object, **_kwargs: object
        ) -> FakeDirectWebSocket:
            return websocket

    class FakeClient:
        def __init__(self) -> None:
            self.token: str | None = "token"
            self.url = URL("http://kvm/api/")
            self._session: object | None = FakeSession()
            self._ssl_config: object | None = False
            self.enter = AsyncMock()
            self.exit = AsyncMock()

        async def __aenter__(self) -> FakeClient:
            await self.enter()
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            await self.exit(exc_type, exc, traceback)

    return FakeClient()


@pytest.mark.asyncio
async def test_direct_stream_records_timestamped_h264_without_reencoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct stream preserves H.264 packets and finalizes atomically."""
    output = tmp_path / "direct.mp4"
    websocket = FakeDirectWebSocket()
    container = FakeDirectContainer(_temporary_recording_path(output))

    client = _direct_client(websocket)
    recorder, _state_callback, _entry = _recorder(
        client=client,
        use_direct_stream=True,
    )
    recorder.direct_backend._output_factory = lambda *_args, **_kwargs: container
    recorder.direct_backend._packet_factory = FakeDirectPacket
    started = MagicMock()
    stop_event = asyncio.Event()
    stop_event.set()

    await recorder._async_record(
        str(output), 60, False, stop_event, started
    )

    started.assert_called_once_with()
    assert container.closed is True
    assert len(container.packets) == 1
    packet = container.packets[0]
    assert isinstance(packet, FakeDirectPacket)
    assert packet.payload == b"\x00\x00\x00\x01\x65h264"
    assert packet.pts == 0
    assert packet.dts == 0
    assert packet.time_base == Fraction(1, 1_000_000)
    assert packet.stream is container.stream
    assert output.read_bytes() == b"mp4-data"
    assert not _temporary_recording_path(output).exists()
    assert websocket.closed is True
    client.enter.assert_awaited_once_with()
    client.exit.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_direct_stream_discards_delta_frames_before_first_idr(
    tmp_path: Path,
) -> None:
    """A new MP4 begins at timestamp zero with a decodable keyframe."""
    delta = SimpleNamespace(
        type=aiohttp.WSMsgType.BINARY,
        data=(
            b"\x01"
            + (100).to_bytes(8, "little")
            + b"\x00\x00\x00\x01\x41delta"
        ),
    )
    keyframe = SimpleNamespace(
        type=aiohttp.WSMsgType.BINARY,
        data=(
            b"\x01"
            + (1_100).to_bytes(8, "little")
            + b"\x00\x00\x00\x01\x65idr"
        ),
    )
    websocket = FakeDirectWebSocket([delta, keyframe])
    output = tmp_path / "keyframe.mp4"
    container = FakeDirectContainer(_temporary_recording_path(output))
    recorder, _state_callback, _entry = _recorder(
        client=_direct_client(websocket),
        use_direct_stream=True,
    )
    recorder.direct_backend._output_factory = lambda *_args, **_kwargs: container
    recorder.direct_backend._packet_factory = FakeDirectPacket
    stop_event = asyncio.Event()
    stop_event.set()

    await recorder.direct_backend.async_record(
        str(output), 60, stop_event, MagicMock()
    )

    assert [packet.payload for packet in container.packets] == [
        b"\x00\x00\x00\x01\x65idr"
    ]
    assert container.packets[0].pts == 0
    assert container.packets[0].dts == 0


@pytest.mark.asyncio
async def test_direct_recording_with_audio_uses_webrtc_fallback() -> None:
    """Audio remains on the WebRTC path because direct H.264 is video-only."""
    recorder, _state_callback, _entry = _recorder(use_direct_stream=True)
    recorder.direct_backend.async_record = AsyncMock()
    recorder.webrtc_backend.async_record = AsyncMock()

    await recorder._async_record(
        "/tmp/capture.mp4", 1, True, asyncio.Event(), MagicMock()
    )

    recorder.direct_backend.async_record.assert_not_awaited()
    recorder.webrtc_backend.async_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_recording_requires_a_client(tmp_path: Path) -> None:
    """A missing stream client fails before any output is touched."""
    recorder, _state_callback, _entry = _recorder()
    recorder.direct_backend._client_factory = lambda: None

    with pytest.raises(HomeAssistantError, match="Missing NanoKVM direct stream client"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "missing.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
async def test_direct_recording_rejects_unauthenticated_client(tmp_path: Path) -> None:
    """The websocket must not be opened until the client has a token."""
    client = _direct_client(FakeDirectWebSocket())
    client.token = None
    recorder, _state_callback, _entry = _recorder(client=client)

    with pytest.raises(HomeAssistantError, match="not authenticated"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "unauthenticated.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["_session", "_ssl_config"])
async def test_direct_recording_rejects_uninitialized_transport(
    tmp_path: Path, missing: str
) -> None:
    """A client without its transport setup cannot create the websocket."""
    client = _direct_client(FakeDirectWebSocket())
    setattr(client, missing, None)
    recorder, _state_callback, _entry = _recorder(client=client)

    with pytest.raises(HomeAssistantError, match="transport is not initialized"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "transport.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
async def test_direct_recording_reports_output_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct output initialization error is translated into an HA error."""
    client = _direct_client(FakeDirectWebSocket())
    recorder, _state_callback, _entry = _recorder(client=client)

    def output_failure(*_args: object, **_kwargs: object) -> Any:
        raise OSError("muxer unavailable")

    recorder.direct_backend._output_factory = output_failure

    with pytest.raises(HomeAssistantError, match="Unable to open NanoKVM direct"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "no-ffmpeg.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
async def test_direct_stream_short_frame_and_close_are_reported(
    tmp_path: Path,
) -> None:
    """Malformed/closed streams fail startup instead of publishing an empty file."""
    websocket = FakeDirectWebSocket(
        [
            SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"short"),
            SimpleNamespace(type=aiohttp.WSMsgType.CLOSE, data=None),
        ]
    )
    client = _direct_client(websocket)
    container = FakeDirectContainer(_temporary_recording_path(tmp_path / "closed.mp4"))

    recorder, _state_callback, _entry = _recorder(client=client)
    recorder.direct_backend._output_factory = lambda *_args, **_kwargs: container
    recorder.direct_backend._packet_factory = FakeDirectPacket

    with pytest.raises(HomeAssistantError, match="direct H.264 stream ended"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "closed.mp4"), 1, asyncio.Event(), MagicMock()
        )

    assert not (tmp_path / "closed.mp4").exists()
    assert container.closed is True


@pytest.mark.asyncio
async def test_direct_stream_connection_error_is_reported(tmp_path: Path) -> None:
    """Transport errors from the direct websocket settle the startup barrier."""
    websocket = FakeDirectWebSocket(iterator_error=ConnectionError("socket lost"))
    client = _direct_client(websocket)
    container = FakeDirectContainer(_temporary_recording_path(tmp_path / "error.mp4"))
    recorder, _state_callback, _entry = _recorder(client=client)
    recorder.direct_backend._output_factory = lambda *_args, **_kwargs: container
    recorder.direct_backend._packet_factory = FakeDirectPacket

    with pytest.raises(HomeAssistantError, match="direct H.264 stream failed"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "error.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
async def test_direct_stream_mux_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct mux failure is surfaced instead of silently publishing data."""
    websocket = FakeDirectWebSocket()

    class FailedContainer(FakeDirectContainer):
        def mux(self, packet: object) -> None:
            raise OSError("mux failed")

    client = _direct_client(websocket)
    recorder, _state_callback, _entry = _recorder(client=client)
    recorder.direct_backend._output_factory = lambda *_args, **_kwargs: FailedContainer(
        _temporary_recording_path(tmp_path / "failed.mp4")
    )
    recorder.direct_backend._packet_factory = FakeDirectPacket

    with pytest.raises(HomeAssistantError, match="direct H.264 stream failed"):
        await recorder.direct_backend.async_record(
            str(tmp_path / "failed.mp4"), 1, asyncio.Event(), MagicMock()
        )


@pytest.mark.asyncio
async def test_direct_wait_helpers_report_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup wait helpers translate timeout and signaling failures consistently."""
    import custom_components.nanokvm.media.recording as recorder_module

    monkeypatch.setattr(recorder_module, "RECORDING_START_TIMEOUT_SECONDS", 0)
    result = asyncio.get_running_loop().create_future()
    error = asyncio.get_running_loop().create_future()
    with pytest.raises(HomeAssistantError, match="Timed out waiting for answer"):
        await _async_wait_for_result(result, error, description="answer")

    event = asyncio.Event()
    with pytest.raises(HomeAssistantError, match="Timed out waiting for event"):
        await _async_wait_for_event(event, error, description="event")


@pytest.mark.asyncio
async def test_wait_helpers_propagate_error_future() -> None:
    """Startup wait helpers return the original signaling exception."""
    error = asyncio.get_running_loop().create_future()
    error.set_exception(HomeAssistantError("signaling failed"))
    result = asyncio.get_running_loop().create_future()
    result.set_result("answer")
    with pytest.raises(HomeAssistantError, match="signaling failed"):
        await _async_wait_for_result(result, error, description="answer")

    event = asyncio.Event()
    with pytest.raises(HomeAssistantError, match="signaling failed"):
        await _async_wait_for_event(event, error, description="event")


@pytest.mark.asyncio
@pytest.mark.parametrize("include_audio", [True, False])
async def test_record_session_negotiates_media_and_atomically_publishes_mp4(
    tmp_path: Path,
    include_audio: bool,
) -> None:
    """The worker records expected tracks, applies ICE, and replaces output at stop."""
    peer = FakePeerConnection()
    media: list[FakeMediaRecorder] = []
    FakeSignalingManager.instances.clear()
    FakeSignalingManager.error = None
    output = tmp_path / "capture.mp4"
    output.write_bytes(b"old-file")
    stop_event = asyncio.Event()
    stop_event.set()
    started = MagicMock()
    recorder, _state_callback, _entry = _recorder()
    recorder.webrtc_backend._peer_connection_factory = lambda: peer
    recorder.webrtc_backend._media_recorder_factory = lambda filename: media.append(
        FakeMediaRecorder(filename, peer)
    ) or media[-1]
    recorder.webrtc_backend._signaling_manager_factory = FakeSignalingManager

    await recorder._async_record(
        str(output), 60, include_audio, stop_event, started
    )

    expected = [("video", "recvonly")]
    if include_audio:
        expected.append(("audio", "recvonly"))
    assert peer.transceivers == expected
    assert [track.kind for track in media[0].tracks] == [
        kind for kind, _direction in expected
    ]
    assert media[0].started is True
    assert media[0].stopped is True
    assert peer.remote_descriptions[0].sdp == "combined-answer"
    assert len(peer.candidates) == 1
    assert peer.candidates[0].sdpMid == "video-main"
    assert output.read_bytes() == b"mp4-data"
    assert not (tmp_path / "capture.tmp.mp4").exists()
    assert started.call_count == 1
    assert peer.closed is True
    assert FakeSignalingManager.instances[-1].shutdown is True
    assert (
        FakeSignalingManager.instances[-1].options[
            "signaling_heartbeat_seconds"
        ]
        == 10.0
    )


@pytest.mark.asyncio
async def test_record_session_startup_error_preserves_existing_output(
    tmp_path: Path,
) -> None:
    """Signaling failure before capture deletes the temp file and keeps the old MP4."""
    peer = FakePeerConnection()
    FakeSignalingManager.error = WebRTCError(
        code="webrtc_signal_failed", message="device rejected offer"
    )
    output = tmp_path / "capture.mp4"
    output.write_bytes(b"old-file")
    recorder, _state_callback, _entry = _recorder()
    recorder.webrtc_backend._peer_connection_factory = lambda: peer
    recorder.webrtc_backend._media_recorder_factory = lambda filename: FakeMediaRecorder(filename, peer)
    recorder.webrtc_backend._signaling_manager_factory = FakeSignalingManager

    with pytest.raises(HomeAssistantError, match="device rejected offer"):
        await recorder._async_record(
            str(output), 60, True, asyncio.Event(), MagicMock()
        )

    assert output.read_bytes() == b"old-file"
    assert not (tmp_path / "capture.tmp.mp4").exists()
    assert peer.closed is True


@pytest.mark.asyncio
async def test_record_session_rejects_missing_local_description(
    tmp_path: Path,
) -> None:
    """A peer that cannot produce local SDP fails before signaling begins."""
    peer = FakePeerConnection()
    peer.localDescription = None
    output = tmp_path / "capture.mp4"
    output.write_bytes(b"old-file")
    recorder, _state_callback, _entry = _recorder()
    recorder.webrtc_backend._peer_connection_factory = lambda: peer
    recorder.webrtc_backend._media_recorder_factory = lambda filename: FakeMediaRecorder(filename, peer)
    recorder.webrtc_backend._signaling_manager_factory = FakeSignalingManager

    with pytest.raises(HomeAssistantError, match="Unable to create.*offer"):
        await recorder._async_record(
            str(output), 60, False, asyncio.Event(), MagicMock()
        )

    assert output.read_bytes() == b"old-file"
    assert peer.closed is True


@pytest.mark.asyncio
async def test_record_session_runtime_failure_keeps_partial_recording(
    tmp_path: Path,
) -> None:
    """Once active, a peer failure still finalizes and publishes the partial MP4."""
    peer = FakePeerConnection()
    media: list[FakeMediaRecorder] = []
    FakeSignalingManager.error = None
    output = tmp_path / "capture.mp4"
    recorder, _state_callback, _entry = _recorder()
    recorder.webrtc_backend._peer_connection_factory = lambda: peer

    def media_factory(filename: str) -> FakeMediaRecorder:
        instance = FakeMediaRecorder(filename, peer)
        instance.fail_after_start = True
        media.append(instance)
        return instance

    recorder.webrtc_backend._media_recorder_factory = media_factory
    recorder.webrtc_backend._signaling_manager_factory = FakeSignalingManager
    started = MagicMock()

    with pytest.raises(HomeAssistantError, match="WebRTC peer connection failed"):
        await recorder._async_record(
            str(output), 60, False, asyncio.Event(), started
        )

    started.assert_called_once_with()
    assert output.read_bytes() == b"mp4-data"
    assert media[0].stopped is True


@pytest.mark.asyncio
async def test_media_writer_failure_stops_recording_and_keeps_partial_output(
    tmp_path: Path,
) -> None:
    """A hidden MediaRecorder worker exception must stop unconsumed frame buildup."""
    peer = FakePeerConnection()
    FakeSignalingManager.error = None
    output = tmp_path / "capture.mp4"
    recorder, _state_callback, _entry = _recorder()
    recorder.webrtc_backend._peer_connection_factory = lambda: peer

    def media_factory(filename: str) -> FakeMediaRecorder:
        instance = FakeMediaRecorder(filename, peer)
        instance.media_task_error = RuntimeError("encoder exploded")
        return instance

    recorder.webrtc_backend._media_recorder_factory = media_factory
    recorder.webrtc_backend._signaling_manager_factory = FakeSignalingManager
    started = MagicMock()

    with pytest.raises(HomeAssistantError, match="media pipeline failed: encoder exploded"):
        async with asyncio.timeout(1):
            await recorder._async_record(
                str(output), 60, False, asyncio.Event(), started
            )

    started.assert_called_once_with()
    assert output.read_bytes() == b"mp4-data"
