"""SDP helpers for NanoKVM Pro split WebRTC signaling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from webrtc_models import RTCIceCandidateInit

MediaKind = Literal["audio", "video"]

_SUPPORTED_KINDS: tuple[MediaKind, ...] = ("audio", "video")
_TRANSPORT_SESSION_PREFIXES = (
    "a=ice-ufrag:",
    "a=ice-pwd:",
    "a=ice-options:",
    "a=fingerprint:",
    "a=setup:",
)


@dataclass(frozen=True, slots=True)
class ProMediaSection:
    """Media section metadata from the original Home Assistant offer."""

    kind: MediaKind
    mid: str
    index: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProWebRTCOffer:
    """Split Pro WebRTC offers and mapping back to the original HA offer."""

    offers: dict[MediaKind, str]
    media_sections: tuple[ProMediaSection, ...]

    @property
    def expected_kinds(self) -> tuple[MediaKind, ...]:
        """Return media kinds expected from the NanoKVM Pro."""
        return tuple(section.kind for section in self.media_sections)

    def section_for_kind(self, kind: MediaKind) -> ProMediaSection | None:
        """Return the original offer section for a media kind."""
        for section in self.media_sections:
            if section.kind == kind:
                return section
        return None

    def kinds_for_candidate(
        self, candidate: RTCIceCandidateInit
    ) -> tuple[MediaKind, ...]:
        """Return Pro media kinds that should receive a frontend candidate."""
        if candidate.sdp_mid is not None:
            for section in self.media_sections:
                if section.mid == str(candidate.sdp_mid):
                    return (section.kind,)

        if candidate.sdp_m_line_index is not None:
            for section in self.media_sections:
                if section.index == candidate.sdp_m_line_index:
                    return (section.kind,)

        return self.expected_kinds

    def upstream_candidate_for_kind(
        self, kind: MediaKind, candidate: RTCIceCandidateInit
    ) -> RTCIceCandidateInit | None:
        """Map a Home Assistant candidate into the split Pro peer connection."""
        section = self.section_for_kind(kind)
        if section is None:
            return None

        return RTCIceCandidateInit(
            candidate.candidate,
            sdp_mid="0",
            sdp_m_line_index=0,
            user_fragment=candidate.user_fragment,
        )

    def home_assistant_candidate_for_kind(
        self, kind: MediaKind, candidate: RTCIceCandidateInit
    ) -> RTCIceCandidateInit | None:
        """Map a Pro candidate back into the original HA media section."""
        section = self.section_for_kind(kind)
        if section is None:
            return None

        return RTCIceCandidateInit(
            candidate.candidate,
            sdp_mid=section.mid,
            sdp_m_line_index=section.index,
            user_fragment=candidate.user_fragment,
        )


def split_pro_webrtc_offer(offer_sdp: str) -> ProWebRTCOffer:
    """Split a combined HA offer into Pro video/audio websocket offers."""
    session_lines, media_lines = _split_sdp_sections(offer_sdp)
    media_sections: list[ProMediaSection] = []
    offers: dict[MediaKind, str] = {}

    for index, lines in enumerate(media_lines):
        kind = _media_kind(lines)
        if kind not in _SUPPORTED_KINDS or kind in offers:
            continue

        mid = _media_mid(lines, fallback=str(index))
        section = ProMediaSection(
            kind=kind,
            mid=mid,
            index=index,
            lines=tuple(lines),
        )
        media_sections.append(section)
        offers[kind] = _join_sdp_sections(
            _single_media_session_lines(session_lines),
            (_replace_mid(section.lines, "0"),),
        )

    return ProWebRTCOffer(offers=offers, media_sections=tuple(media_sections))


def merge_pro_webrtc_answers(
    offer: ProWebRTCOffer,
    answers: dict[MediaKind, str],
    *,
    reject_missing: bool = False,
) -> str:
    """Merge separate Pro video/audio answers into one HA-compatible answer."""
    if not answers:
        raise ValueError("No NanoKVM Pro WebRTC answers received")

    first_answer = next(iter(answers.values()))
    first_session_lines, _ = _split_sdp_sections(first_answer)
    merged_session_lines = _merged_session_lines(first_session_lines)
    merged_media_lines: list[tuple[str, ...]] = []

    for section in offer.media_sections:
        answer_sdp = answers.get(section.kind)
        if answer_sdp is None:
            if not reject_missing:
                raise ValueError(f"Missing {section.kind} WebRTC answer")
            merged_media_lines.append(_rejected_media_section(section))
            continue

        answer_session_lines, answer_media_sections = _split_sdp_sections(answer_sdp)
        answer_media = _first_media_section_for_kind(
            answer_media_sections, section.kind
        )
        if answer_media is None:
            raise ValueError(f"NanoKVM Pro {section.kind} answer has no media section")

        media_with_original_mid = _replace_mid(answer_media, section.mid)
        merged_media_lines.append(
            _with_media_transport_lines(
                media_with_original_mid,
                answer_session_lines,
            )
        )

    return _join_sdp_sections(merged_session_lines, tuple(merged_media_lines))


def _split_sdp_sections(sdp: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Split SDP into session-level lines and media sections."""
    lines = tuple(
        line
        for line in sdp.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line
    )
    session_lines: list[str] = []
    media_sections: list[list[str]] = []
    current_media: list[str] | None = None

    for line in lines:
        if line.startswith("m="):
            current_media = [line]
            media_sections.append(current_media)
        elif current_media is None:
            session_lines.append(line)
        else:
            current_media.append(line)

    return tuple(session_lines), tuple(tuple(section) for section in media_sections)


def _join_sdp_sections(
    session_lines: tuple[str, ...],
    media_sections: tuple[tuple[str, ...], ...],
) -> str:
    """Join SDP lines using CRLF endings."""
    lines = list(session_lines)
    for media_section in media_sections:
        lines.extend(media_section)
    return "\r\n".join(lines) + "\r\n"


def _media_kind(lines: tuple[str, ...]) -> MediaKind | None:
    """Return the kind of an SDP media section."""
    if not lines or not lines[0].startswith("m="):
        return None

    kind = lines[0][2:].split(maxsplit=1)[0]
    if kind in _SUPPORTED_KINDS:
        return kind
    return None


def _media_mid(lines: tuple[str, ...], *, fallback: str) -> str:
    """Return the SDP media section MID."""
    for line in lines:
        if line.startswith("a=mid:"):
            return line.removeprefix("a=mid:")
    return fallback


def _single_media_session_lines(session_lines: tuple[str, ...]) -> tuple[str, ...]:
    """Return session lines for one split Pro peer connection."""
    replaced_bundle = False
    filtered: list[str] = []

    for line in session_lines:
        if line.startswith("a=group:BUNDLE"):
            if not replaced_bundle:
                filtered.append("a=group:BUNDLE 0")
                replaced_bundle = True
            continue
        filtered.append(line)

    return tuple(filtered)


def _merged_session_lines(session_lines: tuple[str, ...]) -> tuple[str, ...]:
    """Return session lines safe for answers built from separate peer connections."""
    merged: list[str] = []
    for line in session_lines:
        if line.startswith("a=group:BUNDLE"):
            continue
        if _is_transport_session_line(line):
            continue
        merged.append(line)
    return tuple(merged)


def _first_media_section_for_kind(
    media_sections: tuple[tuple[str, ...], ...],
    kind: MediaKind,
) -> tuple[str, ...] | None:
    """Return the first answer media section for the requested kind."""
    for section in media_sections:
        if _media_kind(section) == kind:
            return section
    return None


def _replace_mid(lines: tuple[str, ...], mid: str) -> tuple[str, ...]:
    """Replace or add a media section MID."""
    replaced = False
    result: list[str] = []
    for line in lines:
        if line.startswith("a=mid:"):
            result.append(f"a=mid:{mid}")
            replaced = True
        else:
            result.append(line)

    if not replaced:
        result.append(f"a=mid:{mid}")

    return tuple(result)


def _with_media_transport_lines(
    media_lines: tuple[str, ...],
    session_lines: tuple[str, ...],
) -> tuple[str, ...]:
    """Move per-answer transport attributes into the merged media section."""
    existing_prefixes = {
        prefix for prefix in _TRANSPORT_SESSION_PREFIXES if _has_prefix(media_lines, prefix)
    }
    result = list(media_lines)

    for line in session_lines:
        for prefix in _TRANSPORT_SESSION_PREFIXES:
            if prefix in existing_prefixes or not line.startswith(prefix):
                continue
            result.append(line)
            existing_prefixes.add(prefix)
            break

    return tuple(result)


def _rejected_media_section(section: ProMediaSection) -> tuple[str, ...]:
    """Build a rejected answer media section for a missing optional answer."""
    media_line_parts = section.lines[0].split()
    if len(media_line_parts) >= 2:
        media_line_parts[1] = "0"
    result = [" ".join(media_line_parts), f"a=mid:{section.mid}", "a=inactive"]

    for line in section.lines:
        if line.startswith("c="):
            result.insert(1, line)
            break
    else:
        result.insert(1, "c=IN IP4 0.0.0.0")

    return tuple(result)


def _has_prefix(lines: tuple[str, ...], prefix: str) -> bool:
    """Return whether any SDP line starts with prefix."""
    return any(line.startswith(prefix) for line in lines)


def _is_transport_session_line(line: str) -> bool:
    """Return whether a session line carries transport data for one peer connection."""
    return any(line.startswith(prefix) for prefix in _TRANSPORT_SESSION_PREFIXES)
