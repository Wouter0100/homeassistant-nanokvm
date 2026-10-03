"""Tests for the config-entry scoped NanoKVM stream client provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from custom_components.nanokvm.const import CONF_SSL_FINGERPRINT
import custom_components.nanokvm.media.client as client_module
from custom_components.nanokvm.media.client import NanoKVMStreamClientProvider


def _coordinator(url: str = "https://nanokvm.local/api/") -> SimpleNamespace:
    return SimpleNamespace(
        config_entry=SimpleNamespace(data={CONF_SSL_FINGERPRINT: "AABB"}),
        client=SimpleNamespace(url=URL(url), token="existing-token"),
        username="admin",
        password="password",
    )


@pytest.mark.parametrize(
    ("url", "ssl_fingerprint"),
    [
        ("https://nanokvm.local/api/", "AABB"),
        ("http://nanokvm.local/api/", None),
    ],
)
def test_provider_creates_client_from_active_transport_and_token(
    monkeypatch: pytest.MonkeyPatch, url: str, ssl_fingerprint: str | None
) -> None:
    """Stream clients follow coordinator failover and stored TLS identity."""
    client_factory = MagicMock(return_value=object())
    monkeypatch.setattr(client_module, "NanoKVMClient", client_factory)
    provider = NanoKVMStreamClientProvider(_coordinator(url))

    assert provider.create_client() is client_factory.return_value
    client_factory.assert_called_once_with(
        url,
        token="existing-token",
        ssl_fingerprint=ssl_fingerprint,
    )


@pytest.mark.asyncio
async def test_provider_reuses_token_or_authenticates_with_entry_credentials() -> None:
    """Existing session tokens avoid login while empty clients authenticate once."""
    provider = NanoKVMStreamClientProvider(_coordinator())
    authenticated = SimpleNamespace(token="token", authenticate=AsyncMock())
    unauthenticated = SimpleNamespace(token=None, authenticate=AsyncMock())

    await provider.async_authenticate(authenticated)
    await provider.async_authenticate(unauthenticated)

    authenticated.authenticate.assert_not_awaited()
    unauthenticated.authenticate.assert_awaited_once_with("admin", "password")
