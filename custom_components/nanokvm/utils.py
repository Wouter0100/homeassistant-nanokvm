"""Shared utility helpers for the Sipeed NanoKVM integration."""
from __future__ import annotations

from dataclasses import dataclass

from yarl import URL


@dataclass(frozen=True, slots=True)
class NanoKVMAPIConnectionOption:
    """Resolved API base URL plus the applicable SSL fingerprint setting."""

    base_url: str
    scheme: str
    ssl_fingerprint: str | None


def _normalize_api_path(path: str) -> str:
    """Normalize an origin path to the NanoKVM API base path."""
    normalized_path = path.rstrip("/")
    if not normalized_path:
        return "/api/"
    if normalized_path.endswith("/api"):
        return f"{normalized_path}/"
    return f"{normalized_path}/api/"


def _parse_host(host: str) -> tuple[URL, bool]:
    """Parse a configured host and return the origin plus scheme state."""
    raw_host = host.strip()
    has_explicit_scheme = "://" in raw_host
    origin = URL(raw_host if has_explicit_scheme else f"http://{raw_host}")
    return origin.with_query(None).with_fragment(None), has_explicit_scheme


def _api_base_url(origin: URL, scheme: str) -> str:
    """Build an API base URL from a parsed origin and target scheme."""
    return str(origin.with_scheme(scheme).with_path(_normalize_api_path(origin.path)))


def api_connection_options(
    host: str,
    ssl_fingerprint: str | None = None,
    *,
    preferred_url: str | None = None,
) -> tuple[NanoKVMAPIConnectionOption, ...]:
    """Return candidate API connection options for a configured host."""
    origin, has_explicit_scheme = _parse_host(host)
    options = [
        NanoKVMAPIConnectionOption(
            base_url=_api_base_url(origin, scheme),
            scheme=scheme,
            ssl_fingerprint=ssl_fingerprint if scheme == "https" else None,
        )
        for scheme in ((origin.scheme,) if has_explicit_scheme else ("http", "https"))
    ]
    # A stable sort moves the preferred URL first and keeps the rest in order.
    return tuple(sorted(options, key=lambda option: option.base_url != preferred_url))


def https_probe_url(host: str) -> str:
    """Return the HTTPS API base URL for certificate fingerprint probing."""
    return _api_base_url(_parse_host(host)[0], "https")


def verification_url(host: str, default_scheme: str = "https") -> str:
    """Return the API base URL for verifying a host, keeping an explicit scheme."""
    origin, has_explicit_scheme = _parse_host(host)
    return _api_base_url(
        origin, origin.scheme if has_explicit_scheme else default_scheme
    )


def api_base_url_to_web_url(base_url: str) -> str:
    """Convert a NanoKVM API base URL into the corresponding web UI URL."""
    parsed_url = URL(base_url).with_query(None).with_fragment(None)
    web_path = parsed_url.path.rstrip("/").removesuffix("/api")
    return str(parsed_url.with_path(f"{web_path}/"))


def device_sw_version(application: str, image: str | None) -> str:
    """Return the software version shown for a NanoKVM device."""
    return f"{application} (Image: {image})" if image else application


def normalize_host(host: str, ssl_fingerprint: str | None = None) -> str:
    """Return the first candidate API base URL for a configured host."""
    return api_connection_options(host, ssl_fingerprint)[0].base_url


def normalize_mdns(mdns: str) -> str:
    """Normalize mDNS hostnames to include a trailing dot."""
    return mdns if mdns.endswith(".") else f"{mdns}."


def extract_ssh_host(host: str) -> str:
    """Extract SSH host value from integration host configuration."""
    origin = _parse_host(host)[0]
    ssh_host = origin.host or origin.raw_host
    if ssh_host is None:
        raise ValueError(f"Invalid NanoKVM host value: {origin}")
    return ssh_host


def host_match_key(host: str) -> tuple[str, int | None, str]:
    """Return a normalized key for matching a host to a config entry."""
    origin = _parse_host(host)[0]
    return (
        extract_ssh_host(host),
        origin.explicit_port,
        _normalize_api_path(origin.path),
    )
