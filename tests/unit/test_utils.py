"""Tests for NanoKVM host normalization helpers."""

from __future__ import annotations

import pytest
from yarl import URL

from custom_components.nanokvm.utils import (
    NanoKVMAPIConnectionOption,
    NanoKVMConnectionTarget,
    api_base_url_to_web_url,
    api_connection_options,
    extract_ssh_host,
    host_match_key,
    https_probe_url,
    normalize_host,
    normalize_mdns,
)


def test_host_match_key_ignores_default_transport_scheme() -> None:
    """Bare, HTTP, and HTTPS default URLs must identify the same device."""
    bare_key = host_match_key("nanokvm.local")

    assert host_match_key("http://nanokvm.local") == bare_key
    assert host_match_key("https://nanokvm.local") == bare_key


def test_host_match_key_preserves_explicit_custom_ports() -> None:
    """Explicit custom ports must continue to identify distinct targets."""
    assert host_match_key("https://nanokvm.local:8443") != host_match_key(
        "https://nanokvm.local:9443"
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("nanokvm.local", "http://nanokvm.local/api/"),
        ("  nanokvm.local  ", "http://nanokvm.local/api/"),
        ("http://nanokvm.local/", "http://nanokvm.local/api/"),
        ("http://nanokvm.local/api", "http://nanokvm.local/api/"),
        ("http://nanokvm.local/api/", "http://nanokvm.local/api/"),
        ("http://nanokvm.local/custom", "http://nanokvm.local/custom/api/"),
        (
            "http://nanokvm.local/custom/?ignored=yes#fragment",
            "http://nanokvm.local/custom/api/",
        ),
    ],
)
def test_normalize_host_builds_canonical_api_urls(host: str, expected: str) -> None:
    """Configured hosts must resolve to canonical NanoKVM API base URLs."""
    assert normalize_host(host) == expected


def test_bare_host_offers_http_then_pinned_https() -> None:
    """Bare hosts must expose both transports and pin only the TLS option."""
    options = api_connection_options("nanokvm.local", "AA:BB")

    assert options == (
        NanoKVMAPIConnectionOption(
            base_url="http://nanokvm.local/api/",
            scheme="http",
            ssl_fingerprint=None,
        ),
        NanoKVMAPIConnectionOption(
            base_url="https://nanokvm.local/api/",
            scheme="https",
            ssl_fingerprint="AA:BB",
        ),
    )


@pytest.mark.parametrize(
    ("host", "expected_scheme", "expected_fingerprint"),
    [
        ("http://nanokvm.local", "http", None),
        ("https://nanokvm.local", "https", "AA:BB"),
    ],
)
def test_explicit_scheme_produces_one_connection_option(
    host: str,
    expected_scheme: str,
    expected_fingerprint: str | None,
) -> None:
    """Explicit transports must not gain an alternate fallback option."""
    options = api_connection_options(host, "AA:BB")

    assert len(options) == 1
    assert options[0].scheme == expected_scheme
    assert options[0].ssl_fingerprint == expected_fingerprint


def test_preferred_connection_url_is_tried_first() -> None:
    """A known working transport must be reordered ahead of other candidates."""
    options = api_connection_options(
        "nanokvm.local",
        preferred_url="https://nanokvm.local/api/",
    )

    assert [option.scheme for option in options] == ["https", "http"]


def test_already_preferred_connection_url_keeps_default_order() -> None:
    """Preferring the default HTTP URL must preserve both candidate options."""
    options = api_connection_options(
        "nanokvm.local",
        preferred_url="http://nanokvm.local/api/",
    )

    assert [option.scheme for option in options] == ["http", "https"]


def test_unknown_preferred_connection_url_preserves_default_order() -> None:
    """An unrelated preferred URL must not remove or reorder candidates."""
    options = api_connection_options(
        "nanokvm.local",
        preferred_url="https://other-device.local/api/",
    )

    assert [option.scheme for option in options] == ["http", "https"]


def test_connection_target_exposes_normalized_properties() -> None:
    """A parsed target must share one origin across API, SSH, and matching helpers."""
    target = NanoKVMConnectionTarget.from_host(
        "https://192.0.2.20:8443/custom?query=yes#fragment"
    )

    assert target.origin == URL("https://192.0.2.20:8443/custom")
    assert target.has_explicit_scheme is True
    assert target.ssh_host == "192.0.2.20"
    assert target.match_key == ("192.0.2.20", 8443, "/custom/api/")
    assert target.https_probe_url == "https://192.0.2.20:8443/custom/api/"


def test_invalid_connection_target_has_no_ssh_host() -> None:
    """Malformed targets without a hostname must be rejected for SSH use."""
    target = NanoKVMConnectionTarget(origin=URL(""), has_explicit_scheme=False)

    with pytest.raises(ValueError, match="Invalid NanoKVM host value"):
        _ = target.ssh_host


def test_public_ssh_helper_rejects_empty_host() -> None:
    """An empty configured host must fail before an SSH connection is attempted."""
    with pytest.raises(ValueError, match="Invalid NanoKVM host value"):
        extract_ssh_host("   ")


@pytest.mark.parametrize(
    ("api_url", "web_url"),
    [
        ("http://nanokvm.local/api/", "http://nanokvm.local/"),
        ("https://nanokvm.local/custom/api", "https://nanokvm.local/custom/"),
        (
            "https://nanokvm.local/custom/api/?query=yes#fragment",
            "https://nanokvm.local/custom/",
        ),
        ("https://nanokvm.local/custom", "https://nanokvm.local/custom/"),
    ],
)
def test_api_base_url_converts_to_web_ui_url(api_url: str, web_url: str) -> None:
    """API base URLs must map back to clean browser-facing device URLs."""
    assert api_base_url_to_web_url(api_url) == web_url


def test_public_url_and_ssh_helpers_delegate_to_connection_target() -> None:
    """Convenience helpers must use the same normalized connection target."""
    assert https_probe_url("nanokvm.local/custom") == (
        "https://nanokvm.local/custom/api/"
    )
    assert extract_ssh_host("https://[2001:db8::10]:8443/api/") == "2001:db8::10"


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("nanokvm.local", "nanokvm.local."),
        ("nanokvm.local.", "nanokvm.local."),
    ],
)
def test_normalize_mdns_adds_exactly_one_trailing_dot(
    hostname: str, expected: str
) -> None:
    """mDNS names must end with exactly the existing or newly added dot."""
    assert normalize_mdns(hostname) == expected
