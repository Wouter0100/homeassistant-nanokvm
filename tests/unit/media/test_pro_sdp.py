"""Tests for NanoKVM Pro split WebRTC SDP helpers."""

from __future__ import annotations

from webrtc_models import RTCIceCandidateInit
import pytest

from custom_components.nanokvm.media.pro_sdp import (
    ProMediaSection,
    ProWebRTCOffer,
    _first_media_section_for_kind,
    _is_transport_session_line,
    _media_kind,
    _split_sdp_sections,
    merge_pro_webrtc_answers,
    split_pro_webrtc_offer,
)


COMBINED_OFFER = "\r\n".join(
    (
        "v=0",
        "o=- 1 1 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE video-main audio-main data-main duplicate-video",
        "a=group:BUNDLE ignored-duplicate",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "c=IN IP4 0.0.0.0",
        "a=mid:video-main",
        "a=sendrecv",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "c=IN IP4 0.0.0.0",
        "a=mid:audio-main",
        "a=sendrecv",
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
        "a=mid:data-main",
        "m=video 9 UDP/TLS/RTP/SAVPF 97",
        "a=mid:duplicate-video",
        "",
    )
)

VIDEO_ANSWER = "\r\n".join(
    (
        "v=0",
        "o=- 2 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=ice-ufrag:video-session-ufrag",
        "a=ice-pwd:video-session-pwd",
        "a=ice-options:trickle",
        "a=fingerprint:sha-256 VIDEO",
        "a=setup:active",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "c=IN IP4 192.0.2.10",
        "a=mid:0",
        "a=ice-ufrag:video-media-ufrag",
        "a=recvonly",
        "",
    )
)

AUDIO_ANSWER = "\n".join(
    (
        "v=0",
        "o=- 3 3 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=ice-ufrag:audio-ufrag",
        "a=ice-pwd:audio-pwd",
        "a=fingerprint:sha-256 AUDIO",
        "a=setup:active",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "c=IN IP4 192.0.2.11",
        "a=mid:0",
        "a=recvonly",
        "",
    )
)


def _split_offer() -> ProWebRTCOffer:
    """Return the canonical combined offer split for Pro signaling."""
    return split_pro_webrtc_offer(COMBINED_OFFER)


def test_split_offer_keeps_first_supported_section_per_kind() -> None:
    """Only the first video and audio sections must become Pro offers."""
    offer = _split_offer()

    assert offer.expected_kinds == ("video", "audio")
    assert tuple(offer.offers) == ("video", "audio")
    assert [(section.mid, section.index) for section in offer.media_sections] == [
        ("video-main", 0),
        ("audio-main", 1),
    ]
    assert "m=application" not in offer.offers["video"]
    assert "a=mid:duplicate-video" not in offer.offers["video"]


def test_split_offer_rewrites_bundle_and_mid_for_each_peer() -> None:
    """Each split peer must advertise exactly its one rewritten media section."""
    offer = _split_offer()

    for split_sdp in offer.offers.values():
        assert split_sdp.count("a=group:BUNDLE 0") == 1
        assert "ignored-duplicate" not in split_sdp
        assert split_sdp.count("m=") == 1
        assert "a=mid:0\r\n" in split_sdp
        assert split_sdp.endswith("\r\n")


def test_split_offer_uses_media_index_when_mid_is_missing() -> None:
    """Malformed offers without MID must retain a stable index-based mapping."""
    offer = split_pro_webrtc_offer("v=0\rm=audio 9 RTP/AVP 0\ra=sendrecv\r")

    assert offer.media_sections[0].mid == "0"
    assert "a=mid:0\r\n" in offer.offers["audio"]


def test_split_offer_without_supported_media_is_empty() -> None:
    """Session-only and unsupported offers must not invent Pro media peers."""
    offer = split_pro_webrtc_offer("v=0\r\nm=application 9 UDP/DTLS/SCTP 5000\r\n")

    assert offer.offers == {}
    assert offer.media_sections == ()
    assert offer.expected_kinds == ()


def test_offer_section_lookup_returns_matching_or_missing_section() -> None:
    """Section lookup must distinguish offered and unsupported media kinds."""
    offer = _split_offer()

    assert offer.section_for_kind("audio") is offer.media_sections[1]
    assert offer.section_for_kind("video") is offer.media_sections[0]

    video_only = ProWebRTCOffer(
        offers={"video": offer.offers["video"]},
        media_sections=(offer.media_sections[0],),
    )
    assert video_only.section_for_kind("audio") is None


def test_candidate_routes_by_mid_before_media_index() -> None:
    """An explicit matching MID must take precedence over a conflicting index."""
    offer = _split_offer()
    candidate = RTCIceCandidateInit(
        "candidate:1 1 UDP 1 192.0.2.30 5000 typ host",
        sdp_mid="audio-main",
        sdp_m_line_index=0,
    )

    assert offer.kinds_for_candidate(candidate) == ("audio",)


def test_candidate_falls_back_to_matching_media_index() -> None:
    """An unknown or absent MID must allow routing by original media index."""
    offer = _split_offer()
    candidate = RTCIceCandidateInit(
        "candidate:1 1 UDP 1 192.0.2.30 5000 typ host",
        sdp_mid="unknown",
        sdp_m_line_index=0,
    )

    assert offer.kinds_for_candidate(candidate) == ("video",)


def test_candidate_without_known_mapping_routes_to_all_expected_kinds() -> None:
    """Unmapped candidates must be broadcast to each expected split peer."""
    candidate = RTCIceCandidateInit(
        "candidate:1", sdp_mid="unknown", sdp_m_line_index=99
    )

    assert _split_offer().kinds_for_candidate(candidate) == ("video", "audio")


def test_candidate_without_identifiers_uses_library_default_media_index() -> None:
    """The WebRTC model's default index must route an otherwise bare candidate."""
    candidate = RTCIceCandidateInit("candidate:2")

    assert candidate.sdp_m_line_index == 0
    assert _split_offer().kinds_for_candidate(candidate) == ("video",)


def test_upstream_candidate_maps_to_single_peer_mid_and_index() -> None:
    """Frontend candidates must map to the split peer's sole media section."""
    candidate = RTCIceCandidateInit(
        "candidate:1",
        sdp_mid="video-main",
        sdp_m_line_index=0,
        user_fragment="frontend-ufrag",
    )

    mapped = _split_offer().upstream_candidate_for_kind("video", candidate)

    assert mapped == RTCIceCandidateInit(
        "candidate:1",
        sdp_mid="0",
        sdp_m_line_index=0,
        user_fragment="frontend-ufrag",
    )


def test_candidate_mapping_returns_none_for_unoffered_kind() -> None:
    """Candidate mapping must reject kinds that were not in the HA offer."""
    offer = ProWebRTCOffer(offers={}, media_sections=())
    candidate = RTCIceCandidateInit("candidate:1")

    assert offer.upstream_candidate_for_kind("audio", candidate) is None
    assert offer.home_assistant_candidate_for_kind("video", candidate) is None


def test_device_candidate_maps_back_to_original_section() -> None:
    """Pro candidates must recover the original HA MID and media index."""
    candidate = RTCIceCandidateInit(
        "candidate:1",
        sdp_mid="0",
        sdp_m_line_index=0,
        user_fragment="device-ufrag",
    )

    mapped = _split_offer().home_assistant_candidate_for_kind("audio", candidate)

    assert mapped == RTCIceCandidateInit(
        "candidate:1",
        sdp_mid="audio-main",
        sdp_m_line_index=1,
        user_fragment="device-ufrag",
    )


def test_merge_answers_restores_mids_and_per_media_transport() -> None:
    """Merged answers must map media and transport data back to the HA offer."""
    merged = merge_pro_webrtc_answers(
        _split_offer(),
        {"audio": AUDIO_ANSWER, "video": VIDEO_ANSWER},
    )
    session_lines, media_sections = _split_sdp_sections(merged)

    assert not any(line.startswith("a=group:BUNDLE") for line in session_lines)
    assert not any(_is_transport_session_line(line) for line in session_lines)
    assert [section[0].split()[0] for section in media_sections] == [
        "m=video",
        "m=audio",
    ]
    assert "a=mid:video-main" in media_sections[0]
    assert "a=mid:audio-main" in media_sections[1]
    assert "a=ice-ufrag:video-media-ufrag" in media_sections[0]
    assert "a=ice-ufrag:video-session-ufrag" not in media_sections[0]
    assert "a=ice-pwd:video-session-pwd" in media_sections[0]
    assert "a=ice-options:trickle" in media_sections[0]
    assert "a=fingerprint:sha-256 VIDEO" in media_sections[0]
    assert "a=setup:active" in media_sections[0]
    assert "a=ice-ufrag:audio-ufrag" in media_sections[1]
    assert merged.endswith("\r\n")


def test_merge_requires_at_least_one_answer() -> None:
    """An empty answer set cannot form a combined Home Assistant answer."""
    with pytest.raises(ValueError, match="No NanoKVM Pro WebRTC answers received"):
        merge_pro_webrtc_answers(_split_offer(), {})


def test_merge_requires_each_expected_answer_by_default() -> None:
    """Missing expected media must fail unless explicit rejection is requested."""
    with pytest.raises(ValueError, match="Missing audio WebRTC answer"):
        merge_pro_webrtc_answers(_split_offer(), {"video": VIDEO_ANSWER})


def test_merge_rejects_missing_media_with_original_connection_line() -> None:
    """Optional missing media must be rejected while retaining its connection line."""
    merged = merge_pro_webrtc_answers(
        _split_offer(),
        {"video": VIDEO_ANSWER},
        reject_missing=True,
    )
    _, media_sections = _split_sdp_sections(merged)

    assert media_sections[1][0] == "m=audio 0 UDP/TLS/RTP/SAVPF 111"
    assert media_sections[1][1] == "c=IN IP4 0.0.0.0"
    assert "a=mid:audio-main" in media_sections[1]
    assert "a=inactive" in media_sections[1]


def test_merge_rejected_media_adds_fallback_connection_line() -> None:
    """Rejected malformed media must receive a safe zero-address connection line."""
    offer = ProWebRTCOffer(
        offers={"video": VIDEO_ANSWER, "audio": "m=audio\r\n"},
        media_sections=(
            ProMediaSection("video", "video-main", 0, ("m=video 9 RTP/AVP 96",)),
            ProMediaSection("audio", "audio-main", 1, ("m=audio",)),
        ),
    )

    merged = merge_pro_webrtc_answers(
        offer,
        {"video": VIDEO_ANSWER},
        reject_missing=True,
    )
    _, media_sections = _split_sdp_sections(merged)

    assert media_sections[1] == (
        "m=audio",
        "c=IN IP4 0.0.0.0",
        "a=mid:audio-main",
        "a=inactive",
    )


def test_merge_rejects_answer_without_requested_media_section() -> None:
    """A mislabeled Pro answer must not be merged into the wrong HA section."""
    with pytest.raises(
        ValueError,
        match="NanoKVM Pro video answer has no media section",
    ):
        merge_pro_webrtc_answers(
            ProWebRTCOffer(
                offers={"video": "offer"},
                media_sections=(
                    ProMediaSection("video", "video-main", 0, ("m=video 9",)),
                ),
            ),
            {"video": AUDIO_ANSWER},
        )


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ((), None),
        (("a=mid:0",), None),
        (("m=application 9 UDP/DTLS/SCTP 5000",), None),
        (("m=video 9 UDP/TLS/RTP/SAVPF 96",), "video"),
    ],
)
def test_media_kind_recognizes_only_supported_media(
    lines: tuple[str, ...], expected: str | None
) -> None:
    """Media kind detection must reject malformed and unsupported sections."""
    assert _media_kind(lines) == expected


def test_first_media_section_returns_none_without_requested_kind() -> None:
    """Answer lookup must report when no section has the requested media kind."""
    sections = (("m=application 9 UDP/DTLS/SCTP 5000",),)

    assert _first_media_section_for_kind(sections, "video") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("a=ice-ufrag:value", True),
        ("a=ice-pwd:value", True),
        ("a=ice-options:trickle", True),
        ("a=fingerprint:sha-256 VALUE", True),
        ("a=setup:active", True),
        ("a=mid:0", False),
    ],
)
def test_transport_session_line_detection(line: str, expected: bool) -> None:
    """Only split-peer transport attributes must be moved into media sections."""
    assert _is_transport_session_line(line) is expected
