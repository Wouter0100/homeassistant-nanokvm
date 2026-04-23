"""WebRTC signaling helpers for the NanoKVM camera entity."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
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
from nanokvm.client import NanoKVMClient
from webrtc_models import RTCIceCandidateInit
from yarl import URL

from .camera_webrtc_sdp import (
    MediaKind,
    ProWebRTCOffer,
    merge_pro_webrtc_answers,
    split_pro_webrtc_offer,
)

_ANSWER_EVENTS: dict[str, MediaKind] = {
    "video-answer": "video",
    "audio-answer": "audio",
}
_CANDIDATE_EVENTS: dict[str, MediaKind] = {
    "video-candidate": "video",
    "audio-candidate": "audio",
}
_PRO_OFFER_ORDER: tuple[MediaKind, ...] = ("video", "audio")
_PRO_VIDEO_STATUS_INCONSISTENT_MODE = -4
_STATUS_EVENTS = {"video-status", "audio-status", "heartbeat"}
_PRO_VIDEO_STATUS_NAMES = {
    1: "normal",
    -1: "no_image",
    _PRO_VIDEO_STATUS_INCONSISTENT_MODE: "inconsistent_video_mode",
}


@dataclass(slots=True)
class _NanoKVMWebRTCSession:
    """Internal state for an active NanoKVM WebRTC signaling session."""

    client: NanoKVMClient
    websocket: aiohttp.ClientWebSocketResponse
    reader_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    pro_offer: ProWebRTCOffer | None = None
    pro_answers: dict[MediaKind, str] = field(default_factory=dict)
    pending_remote_candidates: list[tuple[MediaKind, RTCIceCandidateInit]] = field(
        default_factory=list
    )
    answer_sent: bool = False


class _WebSocketTimeoutKwargs(TypedDict, total=False):
    """Typed kwargs for aiohttp websocket timeout compatibility."""

    timeout: aiohttp.ClientWSTimeout
    receive_timeout: float


StreamClientFactory = Callable[[], NanoKVMClient | None]
AuthenticateClientCallable = Callable[[NanoKVMClient], Awaitable[None]]


class NanoKVMWebRTCManager:
    """Manage native Home Assistant WebRTC signaling for NanoKVM cameras."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        hass_provider: Callable[[], HomeAssistant | None],
        client_factory: StreamClientFactory,
        authenticate_client: AuthenticateClientCallable,
        is_pro_hardware: Callable[[], bool] | None = None,
        login_timeout_seconds: int = 15,
        websocket_heartbeat_seconds: float = 30.0,
        signaling_heartbeat_seconds: float = 60.0,
        max_pending_ice_candidates: int = 64,
    ) -> None:
        """Initialize WebRTC manager."""
        self._logger = logger
        self._hass_provider = hass_provider
        self._client_factory = client_factory
        self._authenticate_client = authenticate_client
        self._is_pro_hardware = is_pro_hardware or (lambda: False)
        self._login_timeout_seconds = login_timeout_seconds
        self._websocket_heartbeat_seconds = websocket_heartbeat_seconds
        self._signaling_heartbeat_seconds = signaling_heartbeat_seconds
        self._max_pending_ice_candidates = max_pending_ice_candidates
        self._sessions: dict[str, _NanoKVMWebRTCSession] = {}
        self._pending_candidates: dict[str, list[RTCIceCandidateInit]] = {}
        self._session_lock = asyncio.Lock()

    def _webrtc_stream_url(self, base_url: URL, *, pro: bool) -> str:
        """Build NanoKVM h264 WebRTC websocket URL from API base URL."""
        ws_scheme = "wss" if base_url.scheme == "https" else "ws"
        stream_path = "stream/h264/webrtc" if pro else "stream/h264"
        return str(base_url.with_scheme(ws_scheme) / stream_path)

    def _websocket_timeout_kwargs(self) -> _WebSocketTimeoutKwargs:
        """Return websocket timeout kwargs compatible with installed aiohttp."""
        ws_timeout_cls = getattr(aiohttp, "ClientWSTimeout", None)
        if ws_timeout_cls is None:
            return {"receive_timeout": float(self._login_timeout_seconds)}
        return {"timeout": ws_timeout_cls(ws_close=self._login_timeout_seconds)}

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle Home Assistant WebRTC offer using NanoKVM signaling."""
        client = self._client_factory()
        if client is None:
            raise HomeAssistantError("Missing NanoKVM WebRTC client")

        hass = self._hass_provider()
        if hass is None:
            raise HomeAssistantError("Home Assistant is not ready for WebRTC")

        is_pro = self._is_pro_hardware()
        pro_offer = split_pro_webrtc_offer(offer_sdp) if is_pro else None
        if pro_offer is not None and not pro_offer.offers:
            raise HomeAssistantError(
                "NanoKVM Pro WebRTC offer does not contain audio or video media"
            )

        registered = False

        if is_pro:
            await self._async_close_other_webrtc_sessions(session_id)

        async with self._session_lock:
            self._pending_candidates.setdefault(session_id, [])

        try:
            await client.__aenter__()
            await self._authenticate_client(client)

            if client.token is None:
                raise RuntimeError("NanoKVM client authentication did not produce a token")
            if client._session is None or client._ssl_config is None:
                raise RuntimeError("NanoKVM client transport is not initialized")

            websocket = await client._session.ws_connect(
                self._webrtc_stream_url(client.url, pro=is_pro),
                headers={"Cookie": f"nano-kvm-token={client.token}"},
                heartbeat=None if is_pro else self._websocket_heartbeat_seconds,
                ssl=client._ssl_config,
                **self._websocket_timeout_kwargs(),
            )

            webrtc_session = _NanoKVMWebRTCSession(
                client=client,
                websocket=websocket,
                pro_offer=pro_offer,
            )
            async with self._session_lock:
                self._sessions[session_id] = webrtc_session
                registered = True

            webrtc_session.reader_task = hass.async_create_task(
                self._async_webrtc_reader(session_id, send_message)
            )
            if is_pro:
                self._logger.debug(
                    "NanoKVM Pro WebRTC signaling heartbeat enabled: interval=%ss",
                    self._signaling_heartbeat_seconds,
                )
                webrtc_session.heartbeat_task = hass.async_create_task(
                    self._async_webrtc_heartbeat(session_id)
                )

            if pro_offer is None:
                await self._async_send_legacy_offer(websocket, offer_sdp)
            else:
                await self._async_send_pro_offers(websocket, pro_offer)

            await self._async_flush_pending_candidates(session_id)
        except Exception as err:
            if registered:
                await self._async_close_webrtc_session(session_id)
            else:
                async with self._session_lock:
                    self._pending_candidates.pop(session_id, None)
                with suppress(Exception):
                    await client.__aexit__(None, None, None)
            raise HomeAssistantError(
                f"Unable to establish NanoKVM WebRTC signaling: {err}"
            ) from err

    async def _async_send_legacy_offer(
        self, websocket: aiohttp.ClientWebSocketResponse, offer_sdp: str
    ) -> None:
        """Send the legacy NanoKVM single video offer."""
        offer_data = json.dumps({"type": "offer", "sdp": offer_sdp})
        await websocket.send_json({"event": "video-offer", "data": offer_data})

    async def _async_send_pro_offers(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        pro_offer: ProWebRTCOffer,
    ) -> None:
        """Send split video/audio offers to NanoKVM Pro."""
        self._logger.debug(
            "NanoKVM Pro WebRTC offer media sections: %s",
            ", ".join(pro_offer.expected_kinds),
        )
        self._logger.debug(
            "NanoKVM Pro WebRTC offer send order: %s",
            ", ".join(kind for kind in _PRO_OFFER_ORDER if kind in pro_offer.offers),
        )
        if "audio" not in pro_offer.offers:
            self._logger.debug(
                "Home Assistant WebRTC offer did not include audio; negotiating video-only"
            )

        for kind in _PRO_OFFER_ORDER:
            section = pro_offer.section_for_kind(kind)
            if section is None:
                continue
            offer_sdp = pro_offer.offers.get(section.kind)
            if offer_sdp is None:
                continue
            offer_data = json.dumps({"type": "offer", "sdp": offer_sdp})
            await websocket.send_json(
                {"event": f"{section.kind}-offer", "data": offer_data}
            )

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

                payload = self._decode_signal_message(msg.data)
                if payload is None:
                    continue

                event = payload.get("event")
                if not isinstance(event, str):
                    continue

                if event == "heartbeat":
                    continue

                raw_data = payload.get("data")
                data = self._decode_signal_data(raw_data)
                if session.pro_offer is None:
                    self._handle_legacy_event(event, data, send_message)
                else:
                    await self._async_handle_pro_event(
                        session_id, event, data, raw_data, send_message
                    )
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

    async def _async_webrtc_heartbeat(self, session_id: str) -> None:
        """Send NanoKVM signaling heartbeats while a WebRTC session is active."""
        while True:
            await asyncio.sleep(self._signaling_heartbeat_seconds)
            async with self._session_lock:
                session = self._sessions.get(session_id)

            if session is None or session.websocket.closed:
                return

            try:
                await session.websocket.send_json({"event": "heartbeat", "data": ""})
            except Exception as err:
                self._logger.debug("NanoKVM WebRTC heartbeat failed: %s", err)
                await self._async_close_webrtc_session(session_id)
                return

    def _decode_signal_message(self, raw_message: str) -> dict[str, object] | None:
        """Decode a websocket signaling envelope."""
        try:
            payload = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            self._logger.debug(
                "Invalid WebRTC signal message from NanoKVM: %r", raw_message
            )
            return None

        if not isinstance(payload, dict):
            return None
        return payload

    def _decode_signal_data(self, raw_data: object) -> dict[str, object] | None:
        """Decode the nested NanoKVM signaling data payload."""
        if isinstance(raw_data, dict):
            return raw_data
        if not isinstance(raw_data, str):
            return None

        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            self._logger.debug("Invalid WebRTC signal payload: %r", raw_data)
            return None

        if not isinstance(data, dict):
            return None
        return data

    def _handle_legacy_event(
        self,
        event: str,
        data: dict[str, object] | None,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Handle legacy NanoKVM WebRTC events."""
        if data is None:
            return

        if event == "video-answer":
            sdp = data.get("sdp")
            if isinstance(sdp, str) and sdp:
                send_message(WebRTCAnswer(answer=sdp))
            return

        if event == "video-candidate":
            candidate = self._candidate_from_payload(event, data)
            if candidate is not None:
                send_message(WebRTCCandidate(candidate=candidate))
            return

        self._logger.debug("Unhandled NanoKVM WebRTC event: %s", event)

    async def _async_handle_pro_event(
        self,
        session_id: str,
        event: str,
        data: dict[str, object] | None,
        raw_data: object,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Handle NanoKVM Pro split video/audio WebRTC events."""
        if event in _STATUS_EVENTS:
            status_code = self._status_code_from_signal_data(data, raw_data)
            status_name = (
                _PRO_VIDEO_STATUS_NAMES.get(status_code)
                if event == "video-status"
                else None
            )
            self._logger.debug(
                "NanoKVM Pro WebRTC status event %s: status=%s decoded=%s raw=%r",
                event,
                status_name or status_code,
                data,
                raw_data,
            )
            if (
                event == "video-status"
                and status_code == _PRO_VIDEO_STATUS_INCONSISTENT_MODE
            ):
                message = (
                    "NanoKVM Pro stopped WebRTC video because another video mode "
                    "is active"
                )
                send_message(
                    WebRTCError(
                        code="webrtc_inconsistent_video_mode",
                        message=message,
                    )
                )
                self._logger.warning("%s; closing signaling session", message)
                await self._async_close_webrtc_session(session_id)
            return

        if (kind := _ANSWER_EVENTS.get(event)) is not None:
            if data is None:
                return
            sdp = data.get("sdp")
            if isinstance(sdp, str) and sdp:
                await self._async_store_pro_answer(
                    session_id, kind, sdp, send_message
                )
            return

        if (kind := _CANDIDATE_EVENTS.get(event)) is not None:
            if data is None:
                return
            candidate = self._candidate_from_payload(event, data)
            if candidate is not None:
                await self._async_store_pro_candidate(
                    session_id, kind, candidate, send_message
                )
            return

        self._logger.debug("Unhandled NanoKVM Pro WebRTC event: %s", event)

    def _status_code_from_signal_data(
        self,
        data: dict[str, object] | None,
        raw_data: object,
    ) -> int | None:
        """Return a numeric Pro status code from a signaling status payload."""
        if isinstance(data, dict):
            for key in ("status", "code", "value"):
                value = data.get(key)
                if isinstance(value, (int, float, str)):
                    with suppress(ValueError, TypeError):
                        return int(value)

        if isinstance(raw_data, (int, float)):
            return int(raw_data)

        if isinstance(raw_data, str):
            with suppress(ValueError, TypeError):
                return int(raw_data)

            with suppress(json.JSONDecodeError, ValueError, TypeError):
                parsed = json.loads(raw_data)
                if isinstance(parsed, (int, float, str)):
                    return int(parsed)

        return None

    async def _async_store_pro_answer(
        self,
        session_id: str,
        kind: MediaKind,
        sdp: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Store a Pro answer and emit the merged HA answer when complete."""
        answer: str | None = None
        pending_candidates: list[RTCIceCandidateInit] = []

        async with self._session_lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.pro_offer is None
                or session.answer_sent
                or kind not in session.pro_offer.offers
            ):
                return

            session.pro_answers[kind] = sdp
            if set(session.pro_offer.expected_kinds).issubset(session.pro_answers):
                answer = merge_pro_webrtc_answers(
                    session.pro_offer, session.pro_answers
                )
                pending_candidates = [
                    mapped
                    for candidate_kind, candidate in session.pending_remote_candidates
                    if (
                        mapped := session.pro_offer.home_assistant_candidate_for_kind(
                            candidate_kind, candidate
                        )
                    )
                    is not None
                ]
                session.pending_remote_candidates.clear()
                session.answer_sent = True

        if answer is None:
            return

        send_message(WebRTCAnswer(answer=answer))
        for candidate in pending_candidates:
            send_message(WebRTCCandidate(candidate=candidate))

    async def _async_store_pro_candidate(
        self,
        session_id: str,
        kind: MediaKind,
        candidate: RTCIceCandidateInit,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Store or emit a Pro ICE candidate with HA media-section mapping."""
        candidate_to_send: RTCIceCandidateInit | None = None

        async with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None or session.pro_offer is None:
                return

            if session.answer_sent:
                candidate_to_send = session.pro_offer.home_assistant_candidate_for_kind(
                    kind, candidate
                )
            else:
                session.pending_remote_candidates.append((kind, candidate))

        if candidate_to_send is not None:
            send_message(WebRTCCandidate(candidate=candidate_to_send))

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
            await self._async_send_candidate_for_session(session, candidate)
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to forward WebRTC candidate to NanoKVM: {err}"
            ) from err

    async def _async_send_candidate_for_session(
        self,
        session: _NanoKVMWebRTCSession,
        candidate: RTCIceCandidateInit,
    ) -> None:
        """Send one frontend ICE candidate to legacy or Pro signaling."""
        if session.pro_offer is None:
            await self._async_send_candidate(
                session.websocket, "video-candidate", candidate
            )
            return

        for kind in session.pro_offer.kinds_for_candidate(candidate):
            mapped_candidate = session.pro_offer.upstream_candidate_for_kind(
                kind, candidate
            )
            if mapped_candidate is None:
                continue
            await self._async_send_candidate(
                session.websocket, f"{kind}-candidate", mapped_candidate
            )

    async def _async_send_candidate(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        event: str,
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
            payload["usernameFragment"] = candidate.user_fragment

        await websocket.send_json(
            {
                "event": event,
                "data": json.dumps(payload),
            }
        )

    async def _async_flush_pending_candidates(self, session_id: str) -> None:
        """Flush candidates queued before websocket session became active."""
        async with self._session_lock:
            session = self._sessions.get(session_id)
            pending = self._pending_candidates.pop(session_id, [])

        if session is None:
            return

        for candidate in pending:
            await self._async_send_candidate_for_session(session, candidate)

    def _candidate_from_payload(
        self, event: str, data: dict[str, object]
    ) -> RTCIceCandidateInit | None:
        """Build a WebRTC candidate from NanoKVM signaling payload."""
        candidate_data = dict(data)
        if "usernameFragment" in candidate_data and "userFragment" not in candidate_data:
            candidate_data["userFragment"] = candidate_data["usernameFragment"]

        try:
            return RTCIceCandidateInit.from_dict(candidate_data)
        except Exception as err:
            self._logger.debug(
                "Invalid %s payload from NanoKVM: %s (%r)",
                event,
                err,
                data,
            )
            return None

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

        if (
            session.heartbeat_task is not None
            and session.heartbeat_task is not current_task
        ):
            session.heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await session.heartbeat_task

        with suppress(Exception):
            if not session.websocket.closed:
                await session.websocket.close()

        with suppress(Exception):
            await session.client.__aexit__(None, None, None)

    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session when frontend unsubscribes."""
        hass = self._hass_provider()
        if hass is None:
            return
        hass.async_create_task(self._async_close_webrtc_session(session_id))

    async def _async_close_other_webrtc_sessions(self, session_id: str) -> None:
        """Close existing sessions before starting a Pro WebRTC stream."""
        async with self._session_lock:
            session_ids = [
                existing_session_id
                for existing_session_id in self._sessions
                if existing_session_id != session_id
            ]

        for existing_session_id in session_ids:
            self._logger.debug(
                "Closing existing NanoKVM WebRTC session before starting Pro session: "
                "old_session_id=%s new_session_id=%s",
                existing_session_id,
                session_id,
            )
            await self._async_close_webrtc_session(existing_session_id)

    async def async_shutdown(self) -> None:
        """Close all active WebRTC sessions."""
        for session_id in list(self._sessions):
            await self._async_close_webrtc_session(session_id)
