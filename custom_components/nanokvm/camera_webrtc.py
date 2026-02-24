"""WebRTC signaling helpers for the NanoKVM camera entity."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TypedDict

import aiohttp
from aiohttp import WSMsgType
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from webrtc_models import RTCIceCandidateInit
from yarl import URL

@dataclass(slots=True)
class _NanoKVMWebRTCSession:
    """Internal state for an active NanoKVM WebRTC signaling session."""

    http_session: aiohttp.ClientSession
    websocket: aiohttp.ClientWebSocketResponse
    reader_task: asyncio.Task[None] | None = None


class _WebSocketTimeoutKwargs(TypedDict, total=False):
    """Typed kwargs for aiohttp websocket timeout compatibility."""

    timeout: aiohttp.ClientWSTimeout
    receive_timeout: float


class NanoKVMWebRTCManager:
    """Manage native Home Assistant WebRTC signaling for NanoKVM cameras."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        hass_provider: Callable[[], HomeAssistant | None],
        connection_info_provider: Callable[[], tuple[str, str, str] | None],
        authenticate_stream: Callable[
            [aiohttp.ClientSession, str, str, str], Awaitable[str]
        ],
        login_timeout_seconds: int = 15,
        websocket_heartbeat_seconds: float = 30.0,
        max_pending_ice_candidates: int = 64,
    ) -> None:
        """Initialize WebRTC manager."""
        self._logger = logger
        self._hass_provider = hass_provider
        self._connection_info_provider = connection_info_provider
        self._authenticate_stream = authenticate_stream
        self._login_timeout_seconds = login_timeout_seconds
        self._websocket_heartbeat_seconds = websocket_heartbeat_seconds
        self._max_pending_ice_candidates = max_pending_ice_candidates
        self._sessions: dict[str, _NanoKVMWebRTCSession] = {}
        self._pending_candidates: dict[str, list[RTCIceCandidateInit]] = {}
        self._session_lock = asyncio.Lock()

    def _webrtc_stream_url(self, base_url: str) -> str:
        """Build NanoKVM h264 WebRTC websocket URL from API base URL."""
        api_url = URL(base_url)
        ws_scheme = "wss" if api_url.scheme == "https" else "ws"
        return str(api_url.with_scheme(ws_scheme) / "stream/h264")

    def _websocket_timeout_kwargs(self) -> _WebSocketTimeoutKwargs:
        """Return websocket timeout kwargs compatible with installed aiohttp."""
        ws_timeout_cls = getattr(aiohttp, "ClientWSTimeout", None)
        if ws_timeout_cls is None:
            return {"receive_timeout": float(self._login_timeout_seconds)}
        return {"timeout": ws_timeout_cls(ws_close=self._login_timeout_seconds)}

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle Home Assistant WebRTC offer using NanoKVM /stream/h264 signaling."""
        conn = self._connection_info_provider()
        if conn is None:
            raise HomeAssistantError("Missing NanoKVM connection info")

        hass = self._hass_provider()
        if hass is None:
            raise HomeAssistantError("Home Assistant is not ready for WebRTC")

        base_url, username, password = conn
        ws_url = self._webrtc_stream_url(base_url)

        http_session = aiohttp.ClientSession()
        registered = False

        # Frontend can send ICE candidates before signaling websocket is ready.
        async with self._session_lock:
            self._pending_candidates.setdefault(session_id, [])

        try:
            token = await self._authenticate_stream(
                http_session, base_url, username, password
            )
            websocket = await http_session.ws_connect(
                ws_url,
                headers={"Cookie": f"nano-kvm-token={token}"},
                heartbeat=self._websocket_heartbeat_seconds,
                **self._websocket_timeout_kwargs(),
            )

            webrtc_session = _NanoKVMWebRTCSession(
                http_session=http_session,
                websocket=websocket,
            )
            async with self._session_lock:
                self._sessions[session_id] = webrtc_session
                registered = True

            webrtc_session.reader_task = hass.async_create_task(
                self._async_webrtc_reader(session_id, send_message)
            )

            offer_data = json.dumps({"type": "offer", "sdp": offer_sdp})
            await websocket.send_json({"event": "video-offer", "data": offer_data})
            await self._async_flush_pending_candidates(session_id, websocket)
        except Exception as err:
            if registered:
                await self._async_close_webrtc_session(session_id)
            else:
                async with self._session_lock:
                    self._pending_candidates.pop(session_id, None)
                with suppress(Exception):
                    await http_session.close()
            raise HomeAssistantError(
                f"Unable to establish NanoKVM WebRTC signaling: {err}"
            ) from err

    async def _async_webrtc_reader(
        self, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Read NanoKVM signaling messages and forward them to HA frontend."""
        async with self._session_lock:
            session = self._sessions.get(session_id)

        if session is None:
            return

        ws = session.websocket

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                    continue

                try:
                    payload = json.loads(msg.data)
                except (TypeError, json.JSONDecodeError):
                    self._logger.debug(
                        "Invalid WebRTC signal message from NanoKVM: %r", msg.data
                    )
                    continue

                if not isinstance(payload, dict):
                    continue

                event = payload.get("event")
                data_raw = payload.get("data")

                if event == "heartbeat":
                    continue
                if not isinstance(data_raw, str):
                    continue

                try:
                    data = json.loads(data_raw)
                except json.JSONDecodeError:
                    self._logger.debug("Invalid WebRTC signal payload: %r", data_raw)
                    continue

                if event == "video-answer":
                    sdp = data.get("sdp") if isinstance(data, dict) else None
                    if isinstance(sdp, str) and sdp:
                        send_message(WebRTCAnswer(answer=sdp))
                elif event == "video-candidate":
                    if isinstance(data, dict):
                        try:
                            candidate = RTCIceCandidateInit.from_dict(data)
                        except Exception as err:
                            self._logger.debug(
                                "Invalid video-candidate payload from NanoKVM: %s (%r)",
                                err,
                                data,
                            )
                            continue
                        send_message(WebRTCCandidate(candidate=candidate))
                else:
                    self._logger.debug("Unhandled NanoKVM WebRTC event: %s", event)
        except Exception as err:
            self._logger.error("Error reading NanoKVM WebRTC signaling: %s", err)
            send_message(
                WebRTCError(
                    code="webrtc_signal_failed",
                    message=str(err),
                )
            )
        finally:
            self._logger.debug(
                "NanoKVM WebRTC signaling ended: session_id=%s close_code=%s exception=%r",
                session_id,
                ws.close_code,
                ws.exception(),
            )
            await self._async_close_webrtc_session(session_id)

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward frontend ICE candidates to NanoKVM signaling websocket."""
        async with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None or session.websocket.closed:
                queue = self._pending_candidates.setdefault(session_id, [])
                if len(queue) < self._max_pending_ice_candidates:
                    queue.append(candidate)
                return

        try:
            await self._async_send_candidate(session.websocket, candidate)
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to forward WebRTC candidate to NanoKVM: {err}"
            ) from err

    async def _async_send_candidate(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        candidate: RTCIceCandidateInit,
    ) -> None:
        """Send a single ICE candidate to NanoKVM signaling websocket."""
        payload: dict[str, object] = {
            "candidate": candidate.candidate,
        }
        if candidate.sdp_mid is not None:
            payload["sdpMid"] = candidate.sdp_mid
        if candidate.sdp_m_line_index is not None:
            payload["sdpMLineIndex"] = candidate.sdp_m_line_index
        if candidate.user_fragment is not None:
            # NanoKVM (Go/Pion) expects this exact key.
            payload["usernameFragment"] = candidate.user_fragment

        await websocket.send_json(
            {
                "event": "video-candidate",
                "data": json.dumps(payload),
            }
        )

    async def _async_flush_pending_candidates(
        self, session_id: str, websocket: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Flush candidates queued before websocket session became active."""
        async with self._session_lock:
            pending = self._pending_candidates.pop(session_id, [])

        for candidate in pending:
            await self._async_send_candidate(websocket, candidate)

    async def _async_close_webrtc_session(self, session_id: str) -> None:
        """Close and cleanup an active NanoKVM WebRTC signaling session."""
        async with self._session_lock:
            session = self._sessions.pop(session_id, None)
            self._pending_candidates.pop(session_id, None)

        if session is None:
            return

        current_task = asyncio.current_task()

        if session.reader_task is not None and session.reader_task is not current_task:
            session.reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await session.reader_task

        with suppress(Exception):
            if not session.websocket.closed:
                await session.websocket.close()

        with suppress(Exception):
            await session.http_session.close()

    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session when frontend unsubscribes."""
        hass = self._hass_provider()
        if hass is None:
            return
        hass.async_create_task(self._async_close_webrtc_session(session_id))

    async def async_shutdown(self) -> None:
        """Close all active WebRTC sessions."""
        for session_id in list(self._sessions):
            await self._async_close_webrtc_session(session_id)
