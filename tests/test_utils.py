"""Regression tests for NanoKVM host normalization helpers."""

from custom_components.nanokvm.utils import host_match_key


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
