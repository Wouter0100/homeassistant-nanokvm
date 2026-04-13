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


def _web_ui_path(path: str) -> str:
    """Convert an API base path into the corresponding web UI path."""
    normalized_path = path.rstrip("/")
    if normalized_path.endswith("/api"):
        normalized_path = normalized_path[:-4]
    return f"{normalized_path}/" if normalized_path else "/"


def _ssh_host(origin: URL) -> str:
    """Return the hostname to use for SSH connections."""
    host = origin.host or origin.raw_host
    if host is None:
        raise ValueError(f"Invalid NanoKVM host value: {origin}")
    return host


@dataclass(frozen=True, slots=True)
class NanoKVMConnectionTarget:
    """Parsed connection target derived from a configured host value."""

    origin: URL
    has_explicit_scheme: bool

    @classmethod
    def from_host(cls, host: str) -> NanoKVMConnectionTarget:
        """Parse the stored host value into a reusable connection target."""
        origin, has_explicit_scheme = _parse_host(host)
        return cls(origin=origin, has_explicit_scheme=has_explicit_scheme)

    @property
    def ssh_host(self) -> str:
        """Return the hostname to use for SSH connections."""
        return _ssh_host(self.origin)

    @property
    def match_key(self) -> tuple[str, int | None, str]:
        """Return a normalized key for matching configured devices."""
        return self.ssh_host, self.origin.port, _normalize_api_path(self.origin.path)

    def api_connection_options(
        self,
        ssl_fingerprint: str | None = None,
        *,
        preferred_url: str | None = None,
    ) -> tuple[NanoKVMAPIConnectionOption, ...]:
        """Return candidate API connection options for this target."""
        schemes = (
            (self.origin.scheme,) if self.has_explicit_scheme else ("http", "https")
        )
        options = [
            NanoKVMAPIConnectionOption(
                base_url=_api_base_url(self.origin, scheme),
                scheme=scheme,
                ssl_fingerprint=ssl_fingerprint if scheme == "https" else None,
            )
            for scheme in schemes
        ]
        if preferred_url is None:
            return tuple(options)

        preferred = [option for option in options if option.base_url == preferred_url]
        if not preferred:
            return tuple(options)

        remaining = [option for option in options if option.base_url != preferred_url]
        return tuple(preferred + remaining)

    @property
    def https_probe_url(self) -> str:
        """Return the HTTPS API base URL for certificate fingerprint probing."""
        return _api_base_url(self.origin, "https")


def api_connection_options(
    host: str,
    ssl_fingerprint: str | None = None,
    *,
    preferred_url: str | None = None,
) -> tuple[NanoKVMAPIConnectionOption, ...]:
    """Return candidate API connection options for a configured host."""
    return NanoKVMConnectionTarget.from_host(host).api_connection_options(
        ssl_fingerprint=ssl_fingerprint,
        preferred_url=preferred_url,
    )


def https_probe_url(host: str) -> str:
    """Return the HTTPS API base URL for certificate fingerprint probing."""
    return NanoKVMConnectionTarget.from_host(host).https_probe_url


def api_base_url_to_web_url(base_url: str) -> str:
    """Convert a NanoKVM API base URL into the corresponding web UI URL."""
    parsed_url = URL(base_url).with_query(None).with_fragment(None)
    return str(parsed_url.with_path(_web_ui_path(parsed_url.path)))


def normalize_host(host: str, ssl_fingerprint: str | None = None) -> str:
    """Return the first candidate API base URL for a configured host."""
    return NanoKVMConnectionTarget.from_host(host).api_connection_options(
        ssl_fingerprint=ssl_fingerprint,
    )[0].base_url


def normalize_mdns(mdns: str) -> str:
    """Normalize mDNS hostnames to include a trailing dot."""
    return mdns if mdns.endswith(".") else f"{mdns}."


def extract_ssh_host(host: str) -> str:
    """Extract SSH host value from integration host configuration."""
    return NanoKVMConnectionTarget.from_host(host).ssh_host


def host_match_key(host: str) -> tuple[str, int | None, str]:
    """Return a normalized key for matching a host to a config entry."""
    return NanoKVMConnectionTarget.from_host(host).match_key
