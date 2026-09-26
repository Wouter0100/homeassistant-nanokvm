"""Tests for the config-entry scoped NanoKVM stream client provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from yarl import URL

from custom_components.nanokvm.const import CONF_SSL_FINGERPRINT
from custom_components.nanokvm.media.client import NanoKVMStreamClientProvider


def _coordinator(*, data: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config_entry=SimpleNamespace(
            data=data
            if data is not None
            else {CONF_USERNAME: "admin", CONF_PASSWORD: "password"}
        ),
        client=SimpleNamespace(
            url=URL("https://nanokvm.local/api/"), token="existing-token"
        ),
    )


def test_provider_creates_client_from_active_transport_and_token() -> None:
    """Stream clients follow coordinator failover and stored TLS identity."""
    coordinator = _coordinator()
    coordinator.config_entry.data[CONF_SSL_FINGERPRINT] = "AABB"
    client_factory = MagicMock(return_value=object())
    provider = NanoKVMStreamClientProvider(
        coordinator, client_factory=client_factory
    )

    client = provider.create_client()

    assert client is client_factory.return_value
    client_factory.assert_called_once_with(
        "https://nanokvm.local/api/",
        token="existing-token",
        ssl_fingerprint="AABB",
    )


def test_provider_requires_complete_entry_data() -> None:
    """An unloaded or incomplete config entry cannot create a stream client."""
    provider = NanoKVMStreamClientProvider(_coordinator(data={}))

    assert provider.create_client() is None


@pytest.mark.asyncio
async def test_provider_rejects_incomplete_credentials() -> None:
    """A partially configured entry cannot authenticate a media client."""
    provider = NanoKVMStreamClientProvider(
        _coordinator(data={CONF_USERNAME: "admin"})
    )

    with pytest.raises(RuntimeError, match="Missing NanoKVM stream credentials"):
        await provider.async_authenticate(
            SimpleNamespace(token=None, authenticate=AsyncMock())
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


@pytest.mark.asyncio
async def test_provider_rejects_authentication_without_credentials() -> None:
    """Authentication errors explain missing config-entry ownership."""
    provider = NanoKVMStreamClientProvider(_coordinator(data={}))
    client = SimpleNamespace(token=None, authenticate=AsyncMock())

    with pytest.raises(RuntimeError, match="Missing NanoKVM stream credentials"):
        await provider.async_authenticate(client)
