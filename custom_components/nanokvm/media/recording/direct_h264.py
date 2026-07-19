"""Direct NanoKVM H.264 stream recording backend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from fractions import Fraction
from pathlib import Path
from typing import Any

import aiohttp
import av
from homeassistant.exceptions import HomeAssistantError

from ..signaling import AuthenticateClientCallable, StreamClientFactory
from . import async_wait_for_event, temporary_recording_path

DIRECT_H264_HEADER_SIZE = 9
DIRECT_H264_FRAMERATE = 60
DIRECT_H264_TIMESTAMP_TIME_BASE = Fraction(1, 1_000_000)


def direct_h264_timestamp_payload(message: bytes) -> tuple[int, bytes]:
    """Extract NanoKVM's microsecond timestamp and H.264 payload."""
    if len(message) <= DIRECT_H264_HEADER_SIZE:
        raise ValueError("NanoKVM direct H.264 packet is missing its payload")
    return (
        int.from_bytes(message[1:DIRECT_H264_HEADER_SIZE], "little"),
        message[DIRECT_H264_HEADER_SIZE:],
    )


def is_h264_keyframe(payload: bytes) -> bool:
    """Return whether an Annex B H.264 access unit contains an IDR NAL."""
    offset = 0
    while offset < len(payload) - 3:
        if payload[offset : offset + 3] == b"\x00\x00\x01":
            nal_header = offset + 3
        elif payload[offset : offset + 4] == b"\x00\x00\x00\x01":
            nal_header = offset + 4
        else:
            offset += 1
            continue

        if nal_header < len(payload) and payload[nal_header] & 0x1F == 5:
            return True
        offset = nal_header + 1
    return False


class DirectH264RecordingBackend:
    """Record NanoKVM's timestamped H.264 websocket without re-encoding."""

    def __init__(
        self,
        *,
        client_factory: StreamClientFactory,
        authenticate_client: AuthenticateClientCallable,
    ) -> None:
        """Initialize the direct H.264 backend."""
        self._client_factory = client_factory
        self._authenticate_client = authenticate_client
        self._output_factory = av.open
        self._packet_factory = av.Packet

    async def async_record(
        self,
        filename: str,
        duration: int,
        stop_event: asyncio.Event,
        on_started: Callable[[], None],
    ) -> None:
        """Record NanoKVM's timestamped H.264 websocket without re-encoding."""
        output_path = Path(filename)
        temporary_path = temporary_recording_path(output_path)
        temporary_path.unlink(missing_ok=True)

        client = self._client_factory()
        if client is None:
            raise HomeAssistantError("Missing NanoKVM direct stream client")

        websocket: aiohttp.ClientWebSocketResponse | None = None
        output_container: Any | None = None
        video_stream: Any | None = None
        reader_task: asyncio.Task[None] | None = None
        first_frame = asyncio.Event()
        stream_error: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        capture_active = False
        shutdown_requested = False
        timestamp_origin: int | None = None

        def set_stream_error(error: Exception) -> None:
            if not stream_error.done():
                stream_error.set_exception(error)

        async def consume_stream() -> None:
            nonlocal capture_active, timestamp_origin
            assert websocket is not None
            assert output_container is not None
            assert video_stream is not None

            try:
                async for message in websocket:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        try:
                            timestamp, payload = direct_h264_timestamp_payload(
                                message.data
                            )
                        except ValueError:
                            continue

                        if timestamp_origin is None:
                            if not is_h264_keyframe(payload):
                                continue
                            timestamp_origin = timestamp

                        packet = self._packet_factory(payload)
                        packet.pts = timestamp - timestamp_origin
                        packet.dts = timestamp - timestamp_origin
                        packet.time_base = DIRECT_H264_TIMESTAMP_TIME_BASE
                        packet.stream = video_stream
                        output_container.mux(packet)
                        if not first_frame.is_set():
                            capture_active = True
                            first_frame.set()
                            on_started()
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            except (ConnectionError, aiohttp.ClientError, OSError, av.FFmpegError) as err:
                set_stream_error(
                    HomeAssistantError(
                        f"NanoKVM direct H.264 stream failed: {err}"
                    )
                )

            if (
                not stream_error.done()
                and not stop_event.is_set()
                and not shutdown_requested
            ):
                set_stream_error(
                    HomeAssistantError("NanoKVM direct H.264 stream ended")
                )

        try:
            async with client:
                try:
                    await self._authenticate_client(client)
                    if client.token is None:
                        raise HomeAssistantError(
                            "NanoKVM direct stream client is not authenticated"
                        )
                    if client._session is None or client._ssl_config is None:
                        raise HomeAssistantError(
                            "NanoKVM direct stream client transport is not initialized"
                        )

                    websocket_url = client.url.with_scheme(
                        "wss" if client.url.scheme == "https" else "ws"
                    ) / "stream/h264/direct"
                    websocket = await client._session.ws_connect(
                        str(websocket_url),
                        headers={"Cookie": f"nano-kvm-token={client.token}"},
                        heartbeat=None,
                        ssl=client._ssl_config,
                    )
                    try:
                        output_container = self._output_factory(
                            str(temporary_path), mode="w"
                        )
                        video_stream = output_container.add_stream(
                            "h264", rate=DIRECT_H264_FRAMERATE
                        )
                        video_stream.time_base = DIRECT_H264_TIMESTAMP_TIME_BASE
                    except (OSError, av.FFmpegError) as err:
                        raise HomeAssistantError(
                            f"Unable to open NanoKVM direct HDMI recording: {err}"
                        ) from err

                    reader_task = asyncio.create_task(consume_stream())
                    await async_wait_for_event(
                        first_frame,
                        stream_error,
                        description="first NanoKVM HDMI video frame",
                    )

                    stop_task = asyncio.create_task(stop_event.wait())
                    duration_task = asyncio.create_task(asyncio.sleep(duration))
                    try:
                        done, _pending = await asyncio.wait(
                            {stop_task, duration_task, reader_task, stream_error},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stream_error in done:
                            raise stream_error.exception()  # type: ignore[misc]
                        if reader_task in done and not stop_event.is_set():
                            raise HomeAssistantError(
                                "NanoKVM direct H.264 stream ended"
                            )
                    finally:
                        stop_task.cancel()
                        duration_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await stop_task
                        with suppress(asyncio.CancelledError):
                            await duration_task
                finally:
                    shutdown_requested = True
                    if websocket is not None:
                        with suppress(Exception):
                            await websocket.close()
                    if reader_task is not None:
                        reader_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await reader_task
                    if output_container is not None:
                        with suppress(Exception):
                            output_container.close()
        finally:
            if (
                capture_active
                and temporary_path.exists()
                and temporary_path.stat().st_size > 0
            ):
                temporary_path.replace(output_path)
            else:
                temporary_path.unlink(missing_ok=True)
