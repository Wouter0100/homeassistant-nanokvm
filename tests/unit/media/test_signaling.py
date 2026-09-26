"""Behavior tests for NanoKVM WebRTC signaling and session lifecycle."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
import json
import logging
from types import SimpleNamespace
from typing import Any

from aiohttp import WSMsgType
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from homeassistant.exceptions import HomeAssistantError
import pytest
import pytest_asyncio
from webrtc_models import RTCIceCandidateInit
from yarl import URL

from custom_components.nanokvm.media import signaling as webrtc_module
from custom_components.nanokvm.media.signaling import NanoKVMWebRTCManager


COMBINED_OFFER = "\r\n".join(
    (
        "v=0",
        "o=- 1 1 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE video-main audio-main",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=mid:video-main",
        "a=sendrecv",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=mid:audio-main",
        "a=sendrecv",
        "",
    )
)
VIDEO_ONLY_OFFER = "\r\n".join(
    (
        "v=0",
        "o=- 1 1 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE camera",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=mid:camera",
        "a=sendrecv",
        "",
    )
)
VIDEO_ANSWER = "\r\n".join(
    (
        "v=0",
        "o=- 2 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=ice-ufrag:video-ufrag",
        "a=ice-pwd:video-password",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=mid:0",
        "a=recvonly",
        "",
    )
)
AUDIO_ANSWER = "\r\n".join(
    (
        "v=0",
        "o=- 3 3 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=ice-ufrag:audio-ufrag",
        "a=ice-pwd:audio-password",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=mid:0",
        "a=recvonly",
        "",
    )
)
_END = object()


class FakeWebSocket:
    """Small asynchronous WebSocket boundary with observable I/O."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_calls = 0
        self.send_failures: dict[str, Exception] = {}
        self.close_error: Exception | None = None
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self._exception: Exception | None = None

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> object:
        item = await self._incoming.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, payload: dict[str, object]) -> None:
        event = payload.get("event")
        if isinstance(event, str) and event in self.send_failures:
            raise self.send_failures[event]
        self.sent.append(payload)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._incoming.put_nowait(_END)
        if self.close_error is not None:
            raise self.close_error

    def exception(self) -> Exception | None:
        """Return the transport exception exposed by aiohttp."""
        return self._exception

    def feed_signal(self, event: object, data: object = "") -> None:
        """Feed one JSON signaling message to the reader."""
        self.feed_raw(json.dumps({"event": event, "data": data}))

    def feed_raw(
        self, data: object, *, message_type: WSMsgType = WSMsgType.TEXT
    ) -> None:
        """Feed one aiohttp-shaped message to the reader."""
        self._incoming.put_nowait(SimpleNamespace(type=message_type, data=data))

    def feed_error(self, error: Exception) -> None:
        """Make the reader's next receive raise an exception."""
        self._exception = error
        self._incoming.put_nowait(error)

    def feed_close(self) -> None:
        """Feed the close frame observed by a normal reader shutdown."""
        self._incoming.put_nowait(SimpleNamespace(type=WSMsgType.CLOSE, data=None))


class FakeTransport:
    """NanoKVM client's aiohttp session boundary."""

    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.connect_started = asyncio.Event()
        self.connect_release: asyncio.Event | None = None

    async def ws_connect(self, url: str, **kwargs: object) -> FakeWebSocket:
        """Record the WebSocket request and return or fail the connection."""
        self.calls.append((url, kwargs))
        self.connect_started.set()
        if self.connect_release is not None:
            await self.connect_release.wait()
        if self.error is not None:
            raise self.error
        return self.websocket


class FakeClient:
    """Authenticated NanoKVM streaming-client boundary."""

    def __init__(
        self,
        websocket: FakeWebSocket | None = None,
        *,
        url: str = "http://nanokvm.local/api",
    ) -> None:
        self.url = URL(url)
        self.token: str | None = "stream-token"
        self.websocket = websocket or FakeWebSocket()
        self.transport = FakeTransport(self.websocket)
        self._session: FakeTransport | None = self.transport
        self._ssl_config: object | None = object()
        self.enter_calls = 0
        self.exit_calls = 0
        self.exit_error: Exception | None = None

    async def __aenter__(self) -> FakeClient:
        self.enter_calls += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.exit_calls += 1
        if self.exit_error is not None:
            raise self.exit_error


class FakeHass:
    """Home Assistant task-scheduling boundary."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def async_create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Schedule and retain an integration task."""
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class WebRTCHarness:
    """Manager with controllable external boundaries."""

    def __init__(
        self,
        *,
        clients: list[FakeClient | None] | None = None,
        hass: FakeHass | None = None,
        hass_ready: bool = True,
        pro: bool = False,
        authenticate_error: Exception | None = None,
        session_state_callback: Callable[[bool], None] | None = None,
        signaling_heartbeat_seconds: float = 60.0,
        max_pending_ice_candidates: int = 64,
    ) -> None:
        self.clients = deque(clients or [FakeClient()])
        self.hass = hass or FakeHass()
        self.hass_ready = hass_ready
        self.authenticate_error = authenticate_error
        self.authenticated: list[FakeClient] = []

        async def authenticate(client: FakeClient) -> None:
            self.authenticated.append(client)
            if self.authenticate_error is not None:
                raise self.authenticate_error

        self.manager = NanoKVMWebRTCManager(
            logger=logging.getLogger(__name__),
            hass_provider=lambda: self.hass if self.hass_ready else None,
            client_factory=lambda: self.clients.popleft(),
            authenticate_client=authenticate,
            is_pro_hardware=lambda: pro,
            session_state_callback=session_state_callback,
            signaling_heartbeat_seconds=signaling_heartbeat_seconds,
            max_pending_ice_candidates=max_pending_ice_candidates,
        )


@pytest_asyncio.fixture
async def harness_factory() -> Any:
    """Build harnesses and guarantee active signaling tasks are collected."""
    harnesses: list[WebRTCHarness] = []

    def factory(**kwargs: object) -> WebRTCHarness:
        harness = WebRTCHarness(**kwargs)
        harnesses.append(harness)
        return harness

    yield factory

    for harness in harnesses:
        await harness.manager.async_shutdown()
        if harness.hass.tasks:
            await asyncio.gather(*harness.hass.tasks, return_exceptions=True)


def _candidate(
    value: str = "candidate:1 1 UDP 1 192.0.2.10 5000 typ host",
    *,
    mid: str | None = "video-main",
    index: int | None = 0,
    user_fragment: str | None = "ufrag",
) -> RTCIceCandidateInit:
    """Return a representative frontend or device ICE candidate."""
    return RTCIceCandidateInit(
        value,
        sdp_mid=mid,
        sdp_m_line_index=index,
        user_fragment=user_fragment,
    )


def _decode_sent_data(message: dict[str, object]) -> dict[str, object]:
    """Decode one outbound NanoKVM signaling data field."""
    data = message["data"]
    assert isinstance(data, str)
    decoded = json.loads(data)
    assert isinstance(decoded, dict)
    return decoded


async def _finish_reader(client: FakeClient, hass: FakeHass) -> None:
    """End a reader through the normal WebSocket close-frame path."""
    client.websocket.feed_close()
    await hass.tasks[0]


async def test_non_pro_session_signals_offer_answer_candidate_and_cleanup(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A complete non-Pro exchange must preserve SDP, ICE, and transport state."""
    client = FakeClient(url="https://nanokvm.local/api")
    harness = harness_factory(clients=[client])
    messages: list[object] = []

    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", messages.append
    )

    assert client.enter_calls == 1
    assert harness.authenticated == [client]
    assert len(client.transport.calls) == 1
    url, kwargs = client.transport.calls[0]
    assert url == "wss://nanokvm.local/api/stream/h264"
    assert kwargs["headers"] == {"Cookie": "nano-kvm-token=stream-token"}
    assert kwargs["heartbeat"] == 30.0
    assert kwargs["ssl"] is client._ssl_config
    timeout = kwargs["timeout"]
    assert isinstance(timeout, webrtc_module.aiohttp.ClientWSTimeout)
    assert timeout.ws_close == 15
    assert timeout.ws_receive is None
    assert client.websocket.sent == [
        {
            "event": "video-offer",
            "data": json.dumps({"type": "offer", "sdp": VIDEO_ONLY_OFFER}),
        }
    ]

    client.websocket.feed_signal(
        "video-answer", json.dumps({"type": "answer", "sdp": VIDEO_ANSWER})
    )
    client.websocket.feed_signal(
        "video-candidate",
        json.dumps(
            {
                "candidate": "candidate:device",
                "sdpMid": "0",
                "sdpMLineIndex": 0,
                "usernameFragment": "device-ufrag",
            }
        ),
    )
    await _finish_reader(client, harness.hass)

    assert messages == [
        WebRTCAnswer(answer=VIDEO_ANSWER),
        WebRTCCandidate(
            candidate=RTCIceCandidateInit(
                "candidate:device",
                sdp_mid="0",
                sdp_m_line_index=0,
                user_fragment="device-ufrag",
            )
        ),
    ]
    assert client.websocket.closed
    assert client.exit_calls == 1


async def test_session_state_callback_tracks_first_open_and_last_close(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Only the first open and last close transition the public streaming state."""
    clients = [FakeClient(), FakeClient()]
    states: list[bool] = []
    harness = harness_factory(
        clients=clients,
        session_state_callback=states.append,
    )

    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "first", lambda _message: None
    )
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "second", lambda _message: None
    )

    assert states == [True]

    await harness.manager._async_close_webrtc_session("first")
    assert states == [True]

    await harness.manager._async_close_webrtc_session("second")
    assert states == [True, False]


async def test_reader_ignores_malformed_irrelevant_and_non_text_messages(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Noise on the signaling socket must not surface false HA messages."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    messages: list[object] = []
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", messages.append
    )

    client.websocket.feed_raw("not json")
    client.websocket.feed_raw(json.dumps(["not", "an", "envelope"]))
    client.websocket.feed_raw(json.dumps({"data": "{}"}))
    client.websocket.feed_signal("heartbeat")
    client.websocket.feed_signal("video-answer", "not json")
    client.websocket.feed_signal("video-answer", json.dumps(["not", "a", "mapping"]))
    client.websocket.feed_signal("video-answer", {})
    client.websocket.feed_signal("unknown", {"value": 1})
    client.websocket.feed_raw(b"ignored", message_type=WSMsgType.BINARY)
    await _finish_reader(client, harness.hass)

    assert messages == []
    assert client.exit_calls == 1


async def test_pro_session_splits_offer_and_merges_answers_and_candidates(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Pro split peers must appear to HA as one combined peer connection."""
    client = FakeClient()
    harness = harness_factory(clients=[client], pro=True)
    messages: list[object] = []

    await harness.manager.async_handle_async_webrtc_offer(
        COMBINED_OFFER, "pro-session", messages.append
    )

    assert [message["event"] for message in client.websocket.sent] == [
        "video-offer",
        "audio-offer",
    ]
    assert client.transport.calls[0][0].endswith("/api/stream/h264/webrtc")
    assert client.transport.calls[0][1]["heartbeat"] is None
    client.websocket.feed_signal(
        "video-candidate",
        json.dumps(
            {
                "candidate": "candidate:video-before-answer",
                "sdpMid": "0",
                "sdpMLineIndex": 0,
            }
        ),
    )
    client.websocket.feed_signal("video-answer", json.dumps({"sdp": VIDEO_ANSWER}))
    client.websocket.feed_signal("audio-answer", json.dumps({"sdp": AUDIO_ANSWER}))
    client.websocket.feed_signal(
        "audio-candidate",
        json.dumps(
            {
                "candidate": "candidate:audio-after-answer",
                "sdpMid": "0",
                "sdpMLineIndex": 0,
            }
        ),
    )
    await _finish_reader(client, harness.hass)

    assert isinstance(messages[0], WebRTCAnswer)
    assert "a=mid:video-main\r\n" in messages[0].answer
    assert "a=mid:audio-main\r\n" in messages[0].answer
    assert messages[1:] == [
        WebRTCCandidate(
            candidate=RTCIceCandidateInit(
                "candidate:video-before-answer",
                sdp_mid="video-main",
                sdp_m_line_index=0,
            )
        ),
        WebRTCCandidate(
            candidate=RTCIceCandidateInit(
                "candidate:audio-after-answer",
                sdp_mid="audio-main",
                sdp_m_line_index=1,
            )
        ),
    ]


async def test_pro_video_only_offer_completes_with_one_answer(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Pro video-only offers must not wait for an audio answer that was not offered."""
    client = FakeClient()
    harness = harness_factory(clients=[client], pro=True)
    messages: list[object] = []

    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "pro-video", messages.append
    )
    client.websocket.feed_signal("video-status", {"status": 1})
    client.websocket.feed_signal("audio-status", None)
    client.websocket.feed_signal("audio-answer", None)
    client.websocket.feed_signal("video-answer", {})
    client.websocket.feed_signal("audio-candidate", None)
    client.websocket.feed_signal("video-candidate", {})
    client.websocket.feed_signal("unknown", {"value": 1})
    client.websocket.feed_signal("audio-answer", json.dumps({"sdp": AUDIO_ANSWER}))
    client.websocket.feed_signal("video-answer", json.dumps({"sdp": VIDEO_ANSWER}))
    await _finish_reader(client, harness.hass)

    assert [message["event"] for message in client.websocket.sent] == ["video-offer"]
    assert len(messages) == 1
    assert isinstance(messages[0], WebRTCAnswer)


async def test_pro_inconsistent_video_status_reports_error_and_closes(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A conflicting firmware video mode must not leave a frozen HA session."""
    client = FakeClient()
    harness = harness_factory(clients=[client], pro=True)
    messages: list[object] = []
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "pro-session", messages.append
    )

    client.websocket.feed_signal("video-status", json.dumps({"status": "-4"}))
    await harness.hass.tasks[0]

    try:
        assert messages == [
            WebRTCError(
                code="webrtc_inconsistent_video_mode",
                message=(
                    "NanoKVM Pro stopped WebRTC video because another video mode "
                    "is active"
                ),
            )
        ]
        assert client.websocket.closed
        assert client.exit_calls == 1
    finally:
        await harness.manager.async_shutdown()


@pytest.mark.parametrize(
    ("data", "raw_data", "expected"),
    [
        ({"code": 1.9}, None, 1),
        ({"value": "-4"}, None, -4),
        ({"status": "bad"}, -1.2, -1),
        (None, '"1"', 1),
        (None, "{}", None),
        (None, None, None),
        (None, "not-a-status", None),
    ],
)
def test_pro_status_decoding_accepts_device_payload_variants(
    harness_factory: Callable[..., WebRTCHarness],
    data: dict[str, object] | None,
    raw_data: object,
    expected: int | None,
) -> None:
    """Known Pro firmware status shapes must normalize to numeric codes."""
    manager = harness_factory().manager

    assert manager._status_code_from_signal_data(data, raw_data) == expected


async def test_candidate_concurrent_with_connection_is_queued_capped_and_flushed(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """ICE arriving during setup must flush after the offer without exceeding the cap."""
    client = FakeClient()
    client.transport.connect_release = asyncio.Event()
    harness = harness_factory(clients=[client], max_pending_ice_candidates=2)
    offer_task = asyncio.create_task(
        harness.manager.async_handle_async_webrtc_offer(
            VIDEO_ONLY_OFFER, "session", lambda message: None
        )
    )
    try:
        await client.transport.connect_started.wait()

        candidates = [_candidate(f"candidate:{index}") for index in range(3)]
        await asyncio.gather(
            *(
                harness.manager.async_on_webrtc_candidate("session", item)
                for item in candidates
            )
        )
        client.transport.connect_release.set()
        await offer_task
    finally:
        client.transport.connect_release.set()
        if not offer_task.done():
            offer_task.cancel()
            await asyncio.gather(offer_task, return_exceptions=True)

    assert [message["event"] for message in client.websocket.sent] == [
        "video-offer",
        "video-candidate",
        "video-candidate",
    ]
    assert [
        _decode_sent_data(message)["candidate"] for message in client.websocket.sent[1:]
    ] == ["candidate:0", "candidate:1"]


async def test_frontend_candidates_route_to_non_pro_and_pro_media(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Frontend ICE must use the signaling event and media mapping for each model."""
    non_pro_client = FakeClient()
    non_pro = harness_factory(clients=[non_pro_client])
    await non_pro.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "non-pro", lambda message: None
    )
    await non_pro.manager.async_on_webrtc_candidate(
        "non-pro", _candidate(mid=None, index=None, user_fragment=None)
    )

    pro_client = FakeClient()
    pro = harness_factory(clients=[pro_client], pro=True)
    await pro.manager.async_handle_async_webrtc_offer(
        COMBINED_OFFER, "pro", lambda message: None
    )
    await pro.manager.async_on_webrtc_candidate(
        "pro", _candidate(mid="audio-main", index=0)
    )
    await pro.manager.async_on_webrtc_candidate(
        "pro", _candidate(mid="unknown", index=99)
    )

    non_pro_message = non_pro_client.websocket.sent[-1]
    assert non_pro_message["event"] == "video-candidate"
    assert _decode_sent_data(non_pro_message) == {
        "candidate": "candidate:1 1 UDP 1 192.0.2.10 5000 typ host",
        "sdpMLineIndex": 0,
    }
    assert [message["event"] for message in pro_client.websocket.sent[-3:]] == [
        "audio-candidate",
        "video-candidate",
        "audio-candidate",
    ]
    for message in pro_client.websocket.sent[-3:]:
        assert _decode_sent_data(message)["sdpMid"] == "0"
        assert _decode_sent_data(message)["sdpMLineIndex"] == 0


async def test_candidate_send_failure_is_home_assistant_error(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A live signaling write failure must use the integration's public error type."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda message: None
    )
    client.websocket.send_failures["video-candidate"] = RuntimeError("socket lost")

    with pytest.raises(
        HomeAssistantError,
        match="Unable to forward WebRTC candidate to NanoKVM: socket lost",
    ):
        await harness.manager.async_on_webrtc_candidate("session", _candidate())


@pytest.mark.parametrize(
    ("clients", "hass_ready", "message"),
    [
        ([None], True, "Missing NanoKVM WebRTC client"),
        ([FakeClient()], False, "Home Assistant is not ready for WebRTC"),
    ],
)
async def test_offer_requires_client_and_home_assistant(
    harness_factory: Callable[..., WebRTCHarness],
    clients: list[FakeClient | None],
    hass_ready: bool,
    message: str,
) -> None:
    """Missing runtime providers must fail before opening transport resources."""
    harness = harness_factory(clients=clients, hass_ready=hass_ready)

    with pytest.raises(HomeAssistantError, match=message):
        await harness.manager.async_handle_async_webrtc_offer(
            VIDEO_ONLY_OFFER, "session", lambda value: None
        )

    if clients[0] is not None:
        assert clients[0].enter_calls == 0


async def test_pro_offer_without_supported_media_is_rejected_before_client_entry(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A Pro peer request with no audio/video section must not open a client."""
    client = FakeClient()
    harness = harness_factory(clients=[client], pro=True)

    with pytest.raises(
        HomeAssistantError,
        match="offer does not contain audio or video media",
    ):
        await harness.manager.async_handle_async_webrtc_offer(
            "v=0\r\nm=application 9 UDP/DTLS/SCTP 5000\r\n",
            "session",
            lambda value: None,
        )

    assert client.enter_calls == 0


@pytest.mark.parametrize(
    ("configure", "expected"),
    [
        (
            lambda harness, client: setattr(
                harness, "authenticate_error", RuntimeError("bad credentials")
            ),
            "bad credentials",
        ),
        (
            lambda harness, client: setattr(client, "token", None),
            "NanoKVM client authentication did not produce a token",
        ),
        (
            lambda harness, client: setattr(client, "_session", None),
            "NanoKVM client transport is not initialized",
        ),
        (
            lambda harness, client: setattr(client, "_ssl_config", None),
            "NanoKVM client transport is not initialized",
        ),
        (
            lambda harness, client: setattr(
                client.transport, "error", TimeoutError("connect timed out")
            ),
            "connect timed out",
        ),
    ],
)
async def test_offer_setup_failures_release_unregistered_client(
    harness_factory: Callable[..., WebRTCHarness],
    configure: Callable[[WebRTCHarness, FakeClient], None],
    expected: str,
) -> None:
    """Failures before session startup must remove queues and exit the client."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    configure(harness, client)

    with pytest.raises(
        HomeAssistantError,
        match=f"Unable to establish NanoKVM WebRTC signaling: {expected}",
    ):
        await harness.manager.async_handle_async_webrtc_offer(
            VIDEO_ONLY_OFFER, "session", lambda value: None
        )

    assert client.exit_calls == 1
    assert client.websocket.close_calls == 0
    assert "session" not in harness.manager._pending_candidates


async def test_offer_write_failure_closes_registered_session(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A failed initial offer write must close WebSocket, task, and client."""
    client = FakeClient()
    client.websocket.send_failures["video-offer"] = RuntimeError("write failed")
    harness = harness_factory(clients=[client])

    with pytest.raises(
        HomeAssistantError,
        match="Unable to establish NanoKVM WebRTC signaling: write failed",
    ):
        await harness.manager.async_handle_async_webrtc_offer(
            VIDEO_ONLY_OFFER, "session", lambda value: None
        )

    assert client.websocket.closed
    assert client.exit_calls == 1
    assert harness.hass.tasks[0].cancelled()


async def test_reader_timeout_reports_error_and_releases_session(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """An aiohttp receive timeout must be visible to HA and clean up the session."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    messages: list[object] = []
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", messages.append
    )

    client.websocket.feed_error(TimeoutError("signal timed out"))
    await harness.hass.tasks[0]

    assert messages == [
        WebRTCError(code="webrtc_signal_failed", message="signal timed out")
    ]
    assert client.websocket.closed
    assert client.exit_calls == 1


async def test_pro_heartbeat_is_sent_until_shutdown_cancels_tasks(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Pro sessions must heartbeat and shutdown must cancel both owned tasks."""
    client = FakeClient()
    harness = harness_factory(
        clients=[client], pro=True, signaling_heartbeat_seconds=0.001
    )
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda value: None
    )

    async with asyncio.timeout(1):
        while not any(
            message["event"] == "heartbeat" for message in client.websocket.sent
        ):
            await asyncio.sleep(0)
    owned_tasks = tuple(harness.hass.tasks)
    await harness.manager.async_shutdown()

    assert [message["event"] for message in client.websocket.sent].count(
        "heartbeat"
    ) >= 1
    assert all(task.cancelled() for task in owned_tasks)
    assert client.exit_calls == 1


async def test_heartbeat_failure_closes_session(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A failed Pro heartbeat must terminate and release the signaling session."""
    client = FakeClient()
    client.websocket.send_failures["heartbeat"] = RuntimeError("heartbeat failed")
    harness = harness_factory(
        clients=[client], pro=True, signaling_heartbeat_seconds=0.001
    )
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda value: None
    )

    async with asyncio.timeout(1):
        while client.exit_calls == 0:
            await asyncio.sleep(0)

    assert client.websocket.closed
    assert client.exit_calls == 1


async def test_close_callback_schedules_cleanup_and_noops_without_hass(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Frontend unsubscribe must schedule cleanup only while HA is available."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda value: None
    )

    harness.manager.close_webrtc_session("session")
    await harness.hass.tasks[-1]
    assert client.exit_calls == 1

    unavailable = harness_factory(hass_ready=False)
    unavailable.manager.close_webrtc_session("missing")
    assert unavailable.hass.tasks == []


async def test_shutdown_closes_all_non_pro_sessions_and_tolerates_close_errors(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Manager shutdown must attempt every session even when resource closes fail."""
    first = FakeClient()
    first.websocket.close_error = RuntimeError("close failed")
    first.exit_error = RuntimeError("exit failed")
    second = FakeClient()
    harness = harness_factory(clients=[first, second])
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "first", lambda value: None
    )
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "second", lambda value: None
    )
    second.websocket.closed = True

    await harness.manager.async_shutdown()

    assert first.websocket.close_calls == 1
    assert second.websocket.close_calls == 0
    assert first.exit_calls == second.exit_calls == 1


async def test_heartbeat_stops_when_websocket_is_already_closed(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """A Pro heartbeat worker must retire after observing a closed transport."""
    client = FakeClient()
    harness = harness_factory(
        clients=[client], pro=True, signaling_heartbeat_seconds=0.001
    )
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda value: None
    )

    client.websocket.closed = True
    heartbeat_task = harness.hass.tasks[1]
    async with asyncio.timeout(1):
        await heartbeat_task

    assert heartbeat_task.done()
    assert [message["event"] for message in client.websocket.sent] == ["video-offer"]


async def test_new_pro_session_keeps_other_active_session_alive(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """NanoKVM Pro can broadcast one encoder stream to multiple clients."""
    first = FakeClient()
    second = FakeClient()
    harness = harness_factory(clients=[first, second], pro=True)
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "first", lambda value: None
    )

    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "second", lambda value: None
    )

    assert not first.websocket.closed
    assert first.exit_calls == 0
    assert not second.websocket.closed
    assert second.exit_calls == 0


async def test_legacy_aiohttp_timeout_uses_receive_timeout(
    harness_factory: Callable[..., WebRTCHarness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older aiohttp releases must receive their supported timeout keyword."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    monkeypatch.delattr(webrtc_module.aiohttp, "ClientWSTimeout")

    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", lambda value: None
    )

    kwargs = client.transport.calls[0][1]
    assert kwargs["receive_timeout"] == 15.0
    assert "timeout" not in kwargs


async def test_invalid_device_candidate_is_ignored(
    harness_factory: Callable[..., WebRTCHarness],
) -> None:
    """Malformed ICE from a device must not escape the signaling reader."""
    client = FakeClient()
    harness = harness_factory(clients=[client])
    messages: list[object] = []
    await harness.manager.async_handle_async_webrtc_offer(
        VIDEO_ONLY_OFFER, "session", messages.append
    )

    client.websocket.feed_signal("video-candidate", json.dumps({"sdpMid": "0"}))
    await _finish_reader(client, harness.hass)

    assert messages == []
