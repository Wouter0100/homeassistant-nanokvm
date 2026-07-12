"""Regression tests for serialized coordinator client access."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from pytest import MonkeyPatch
from yarl import URL

import custom_components.nanokvm.coordinator as coordinator_module
from custom_components.nanokvm.coordinator import NanoKVMDataUpdateCoordinator


class TrackingClient:
    """Async context manager that records overlapping entries."""

    def __init__(self, url: str = "http://nanokvm.local/api/") -> None:
        self.active_entries = 0
        self.maximum_active_entries = 0
        self.token = "authenticated"
        self.url = URL(url)

    async def __aenter__(self) -> TrackingClient:
        """Record an active client user."""
        self.active_entries += 1
        self.maximum_active_entries = max(
            self.maximum_active_entries, self.active_entries
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        """Record a completed client user."""
        self.active_entries -= 1


class DirectEntryForbiddenClient(TrackingClient):
    """Client that fails when code bypasses the coordinator access boundary."""

    token = "authenticated"

    async def __aenter__(self) -> DirectEntryForbiddenClient:
        """Reject direct context entry."""
        raise AssertionError("shared client was entered directly")


def _coordinator(client: TrackingClient) -> NanoKVMDataUpdateCoordinator:
    """Create a coordinator backed by the tracking client."""
    entry = MagicMock(spec=ConfigEntry)
    entry.data = {CONF_HOST: "http://nanokvm.local"}
    return NanoKVMDataUpdateCoordinator(
        MagicMock(),
        entry,
        client=client,
        username="admin",
        password="password",
        device_info=SimpleNamespace(device_key="test-device", application="1.0.0"),
    )


def test_coordinator_serializes_shared_client_users() -> None:
    """A second client user must wait until the first user exits."""

    async def run_test() -> None:
        client = TrackingClient()
        coordinator = _coordinator(client)
        async_client = coordinator.async_client
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_user() -> None:
            async with async_client():
                first_entered.set()
                await release_first.wait()

        async def second_user() -> None:
            async with async_client():
                second_entered.set()

        first_task = asyncio.create_task(first_user())
        await first_entered.wait()
        second_task = asyncio.create_task(second_user())
        await asyncio.sleep(0)

        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()
        assert client.maximum_active_entries == 1

    asyncio.run(run_test())


def test_cancelled_client_user_releases_serialized_access() -> None:
    """Cancellation must release the client context and coordinator lock."""

    async def run_test() -> None:
        client = TrackingClient()
        coordinator = _coordinator(client)
        entered = asyncio.Event()

        async def cancelled_user() -> None:
            async with coordinator.async_client():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancelled_user())
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert client.active_entries == 0
        async with asyncio.timeout(1), coordinator.async_client():
            pass

    asyncio.run(run_test())


def test_coordinator_poll_uses_serialized_client_access() -> None:
    """Coordinator polling must enter the client through its access boundary."""

    async def run_test() -> None:
        client = DirectEntryForbiddenClient()
        coordinator = _coordinator(client)

        @contextlib.asynccontextmanager
        async def serialized_client():
            yield client

        coordinator.async_client = MagicMock(side_effect=serialized_client)
        coordinator._async_fetch_core_data = AsyncMock()
        coordinator._async_fetch_storage_data = AsyncMock()
        coordinator._async_refresh_ssh_data = AsyncMock()
        coordinator._async_maybe_create_network_entities = MagicMock()
        coordinator._async_maybe_create_media_entities = MagicMock()
        coordinator._async_schedule_app_version_refresh = MagicMock()
        coordinator._build_update_data = MagicMock(return_value={"ready": True})

        assert await coordinator._async_fetch_with_client() == {"ready": True}
        coordinator.async_client.assert_called_once_with()

    asyncio.run(run_test())


def test_client_replacement_waits_for_active_user(monkeypatch: MonkeyPatch) -> None:
    """Reauthentication must not replace a client that is still in use."""

    async def run_test() -> None:
        replacement_started = asyncio.Event()

        class ReplacementClient(TrackingClient):
            """Candidate client used by the reauthentication path."""

            def __init__(self, url: str, **_: object) -> None:
                super().__init__(url)

            async def authenticate(self, _username: str, _password: str) -> None:
                replacement_started.set()

        monkeypatch.setattr(coordinator_module, "NanoKVMClient", ReplacementClient)
        current_client = TrackingClient()
        coordinator = _coordinator(current_client)

        async with coordinator.async_client():
            replacement_task = asyncio.create_task(
                coordinator._async_reauthenticate_client(RuntimeError("reauthenticate"))
            )
            await asyncio.sleep(0)
            assert not replacement_started.is_set()

        await replacement_task
        assert replacement_started.is_set()
        assert coordinator.client is not current_client

    asyncio.run(run_test())
