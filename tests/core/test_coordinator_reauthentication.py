"""Regression tests for coordinator reauthentication error handling."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntry
from nanokvm.client import NanoKVMAuthenticationFailure
from pytest import raises

from custom_components.nanokvm.coordinator import (
    NanoKVMDataUpdateCoordinator,
    UpdateFailed,
)


def _coordinator() -> NanoKVMDataUpdateCoordinator:
    """Create a coordinator with mocked Home Assistant dependencies."""
    return NanoKVMDataUpdateCoordinator(
        MagicMock(),
        MagicMock(spec=ConfigEntry),
        client=MagicMock(),
        username="admin",
        password="password",
        device_info=SimpleNamespace(device_key="test-device", application="1.0.0"),
    )


def test_post_reauthentication_timeout_uses_update_failed_contract() -> None:
    """A timed-out fetch after reauthentication must remain retryable."""

    async def run_test() -> None:
        coordinator = _coordinator()
        coordinator._async_fetch_with_client = AsyncMock(
            side_effect=[
                NanoKVMAuthenticationFailure("expired token"),
                asyncio.TimeoutError(),
            ]
        )
        coordinator._async_reauthenticate_client = AsyncMock()

        with raises(
            UpdateFailed,
            match="Timed out communicating with NanoKVM after 10 seconds",
        ):
            await coordinator._async_fetch_once()

        coordinator._async_reauthenticate_client.assert_awaited_once()
        assert coordinator._async_fetch_with_client.await_count == 2

    asyncio.run(run_test())
