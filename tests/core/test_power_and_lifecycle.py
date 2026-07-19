"""Regression tests for NanoKVM power and coordinator lifecycle behavior."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntry
from nanokvm.models import GpioType
from pytest import MonkeyPatch

from custom_components.nanokvm import async_unload_entry
from custom_components.nanokvm.const import DOMAIN
from custom_components.nanokvm.coordinator import NanoKVMDataUpdateCoordinator
from custom_components.nanokvm.switch import (
    SWITCHES,
    NanoKVMPowerSwitch,
    NanoKVMSwitchEntityDescription,
)
import custom_components.nanokvm.switch as switch_module


class FakeClient:
    """Minimal async context-managed NanoKVM client for power tests."""

    def __init__(self) -> None:
        self.enter_count = 0
        self.push_button = AsyncMock()

    async def __aenter__(self) -> FakeClient:
        """Reject entity code that enters the shared client directly."""
        raise AssertionError("power switch bypassed coordinator client access")

    async def __aexit__(self, *_: object) -> None:
        """Exit the fake client context."""


def _power_description() -> NanoKVMSwitchEntityDescription:
    """Return the power switch description."""
    return next(description for description in SWITCHES if description.key == "power")


def _power_switch(power_state: bool) -> tuple[NanoKVMPowerSwitch, SimpleNamespace]:
    """Create a power switch backed by a minimal coordinator."""
    client = FakeClient()

    @contextlib.asynccontextmanager
    async def async_client():
        client.enter_count += 1
        yield client

    coordinator = SimpleNamespace(
        async_client=MagicMock(side_effect=async_client),
        async_request_refresh=AsyncMock(),
        client=client,
        device_info=SimpleNamespace(device_key="test-device"),
        gpio_info=SimpleNamespace(pwr=power_state),
    )
    return NanoKVMPowerSwitch(coordinator, _power_description()), coordinator


def test_power_turn_on_is_noop_when_already_on() -> None:
    """Turning on an already-on host must not press the momentary power button."""
    switch, coordinator = _power_switch(True)

    asyncio.run(switch.async_turn_on())

    coordinator.async_request_refresh.assert_awaited_once_with()
    assert coordinator.client.enter_count == 0
    coordinator.client.push_button.assert_not_awaited()


def test_power_turn_off_is_noop_when_already_off() -> None:
    """Turning off an already-off host must not press the momentary power button."""
    switch, coordinator = _power_switch(False)

    asyncio.run(switch.async_turn_off())

    coordinator.async_request_refresh.assert_awaited_once_with()
    assert coordinator.client.enter_count == 0
    coordinator.client.push_button.assert_not_awaited()


def test_power_turn_on_uses_coordinator_client_access(
    monkeypatch: MonkeyPatch,
) -> None:
    """A power action must use serialized coordinator client access."""
    switch, coordinator = _power_switch(False)
    sleep = AsyncMock()
    monkeypatch.setattr(switch_module.asyncio, "sleep", sleep)

    asyncio.run(switch.async_turn_on())

    coordinator.async_client.assert_called_once_with()
    coordinator.client.push_button.assert_awaited_once_with(GpioType.POWER, 200)
    assert coordinator.client.enter_count == 1
    sleep.assert_awaited_once_with(1)


def _coordinator() -> tuple[NanoKVMDataUpdateCoordinator, MagicMock]:
    """Create a coordinator with mocked Home Assistant dependencies."""
    hass = MagicMock()
    entry = MagicMock(spec=ConfigEntry)
    coordinator = NanoKVMDataUpdateCoordinator(
        hass,
        entry,
        client=MagicMock(),
        username="admin",
        password="password",
        device_info=SimpleNamespace(device_key="test-device", application="1.0.0"),
    )
    return coordinator, entry


def test_coordinator_registers_config_entry_with_base_class() -> None:
    """The base coordinator must own config-entry unload registration."""
    coordinator, entry = _coordinator()

    assert coordinator.config_entry is entry
    entry.async_on_unload.assert_called_once_with(coordinator.async_shutdown)


def test_coordinator_shutdown_stops_base_and_owned_resources() -> None:
    """Shutdown must stop refreshes, cancel background work, and disconnect SSH."""

    async def run_shutdown() -> None:
        coordinator, _ = _coordinator()
        app_version_task = asyncio.create_task(asyncio.Event().wait())
        collector = SimpleNamespace(disconnect=AsyncMock())
        media = SimpleNamespace(async_shutdown=AsyncMock())
        coordinator._app_version_fetch_task = app_version_task
        coordinator.ssh_metrics_collector = collector
        coordinator.media = media

        await coordinator.async_shutdown()

        assert coordinator._shutdown_requested is True
        assert app_version_task.cancelled()
        assert coordinator._app_version_fetch_task is None
        collector.disconnect.assert_awaited_once_with()
        assert coordinator.ssh_metrics_collector is None
        media.async_shutdown.assert_awaited_once_with()

    asyncio.run(run_shutdown())


def test_integration_unload_leaves_coordinator_shutdown_to_config_entry() -> None:
    """Integration unload must not invoke the registered coordinator callback twice."""

    async def run_unload() -> None:
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = MagicMock(spec=ConfigEntry)
        entry.entry_id = "test-entry"
        coordinator = SimpleNamespace(async_shutdown=AsyncMock())
        hass.data = {DOMAIN: {entry.entry_id: coordinator}}
        hass.services.has_service.return_value = False

        assert await async_unload_entry(hass, entry) is True

        coordinator.async_shutdown.assert_not_awaited()
        assert DOMAIN not in hass.data

    asyncio.run(run_unload())
