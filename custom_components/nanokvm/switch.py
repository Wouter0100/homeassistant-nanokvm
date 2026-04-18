"""Switch platform for Sipeed NanoKVM."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from nanokvm.models import GpioType, VirtualDevice

from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_HDMI,
    ICON_MDNS,
    ICON_NETWORK,
    ICON_SSH,
    ICON_POWER,
    ICON_WATCHDOG,
    SIGNAL_NEW_SSH_SWITCHES,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NanoKVMSwitchEntityDescription(SwitchEntityDescription):
    """Describes NanoKVM switch entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = (
        lambda _: False
    )
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    turn_on_fn: Callable[[NanoKVMDataUpdateCoordinator], Awaitable[Any]] | None = None
    turn_off_fn: Callable[[NanoKVMDataUpdateCoordinator], Awaitable[Any]] | None = None
    virtual_device: VirtualDevice | None = None


def _hdmi_value(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return HDMI switch state."""
    return coordinator.hdmi_state.enabled if coordinator.hdmi_state else False


def _hdmi_available(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether HDMI controls are available."""
    return coordinator.supports_hdmi_endpoint and coordinator.hdmi_state is not None


def _watchdog_value(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return watchdog switch state."""
    return bool(coordinator.watchdog_enabled)


def _watchdog_available(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether the watchdog switch should be available."""
    return coordinator.supports_watchdog and coordinator.watchdog_enabled is not None


def _virtual_device_available(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether the legacy virtual-device switches apply to this device."""
    return coordinator.supports_legacy_virtual_device_controls


SWITCHES: tuple[NanoKVMSwitchEntityDescription, ...] = (
    NanoKVMSwitchEntityDescription(
        key="ssh",
        name="SSH",
        translation_key="ssh",
        icon=ICON_SSH,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: bool(
            coordinator.ssh_state and coordinator.ssh_state.enabled
        ),
        turn_on_fn=lambda coordinator: coordinator.client.enable_ssh(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_ssh(),
    ),
    NanoKVMSwitchEntityDescription(
        key="mdns",
        name="mDNS",
        translation_key="mdns",
        icon=ICON_MDNS,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: bool(
            coordinator.mdns_state and coordinator.mdns_state.enabled
        ),
        turn_on_fn=lambda coordinator: coordinator.client.enable_mdns(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_mdns(),
    ),
    NanoKVMSwitchEntityDescription(
        key="virtual_network",
        name="Virtual Network",
        translation_key="virtual_network",
        icon=ICON_NETWORK,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: bool(
            coordinator.virtual_device_info and coordinator.virtual_device_info.network
        ),
        available_fn=_virtual_device_available,
        virtual_device=VirtualDevice.NETWORK,
    ),
    NanoKVMSwitchEntityDescription(
        key="virtual_disk",
        name="Virtual Disk",
        translation_key="virtual_disk",
        icon=ICON_DISK,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: bool(
            coordinator.virtual_device_info and coordinator.virtual_device_info.disk
        ),
        available_fn=_virtual_device_available,
        virtual_device=VirtualDevice.DISK,
    ),
    NanoKVMSwitchEntityDescription(
        key="power",
        name="Power",
        translation_key="power",
        icon=ICON_POWER,
        value_fn=lambda coordinator: bool(
            coordinator.gpio_info and coordinator.gpio_info.pwr
        ),
        turn_on_fn=lambda coordinator: coordinator.client.push_button(GpioType.POWER, 200),
        turn_off_fn=lambda coordinator: coordinator.client.push_button(GpioType.POWER, 200),
    ),
    NanoKVMSwitchEntityDescription(
        key="hdmi",
        name="HDMI Output",
        translation_key="hdmi",
        icon=ICON_HDMI,
        entity_category=EntityCategory.CONFIG,
        value_fn=_hdmi_value,
        turn_on_fn=lambda coordinator: coordinator.client.enable_hdmi(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_hdmi(),
        available_fn=_hdmi_available,
    ),
)

SSH_SWITCHES: tuple[NanoKVMSwitchEntityDescription, ...] = (
    NanoKVMSwitchEntityDescription(
        key="watchdog",
        name="Watchdog",
        translation_key="watchdog",
        icon=ICON_WATCHDOG,
        entity_category=EntityCategory.CONFIG,
        value_fn=_watchdog_value,
        available_fn=_watchdog_available,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM switch based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for description in SWITCHES:
        if not description.available_fn(coordinator):
            continue

        if description.virtual_device is not None:
            entities.append(
                NanoKVMVirtualDeviceSwitch(
                    coordinator=coordinator,
                    description=description,
                )
            )
            continue

        if description.key == "power":
            entities.append(
                NanoKVMPowerSwitch(
                    coordinator=coordinator,
                    description=description,
                )
            )
            continue
        else:
            entities.append(
                NanoKVMSwitch(
                    coordinator=coordinator,
                    description=description,
                )
            )

    async_add_entities(entities)

    ssh_entities_added = False

    @callback
    def async_add_ssh_switches() -> None:
        """Add SSH-backed switches when they become available."""
        nonlocal ssh_entities_added
        if ssh_entities_added:
            return

        entities = [
            NanoKVMWatchdogSwitch(
                coordinator=coordinator,
                description=description,
            )
            for description in SSH_SWITCHES
            if description.available_fn(coordinator)
        ]
        if not entities:
            return

        async_add_entities(entities)
        ssh_entities_added = True
        coordinator.ssh_switches_created = True

    if any(description.available_fn(coordinator) for description in SSH_SWITCHES):
        async_add_ssh_switches()

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_SSH_SWITCHES.format(entry.entry_id),
            async_add_ssh_switches,
        )
    )


class NanoKVMSwitch(NanoKVMEntity, SwitchEntity):
    """Defines a NanoKVM switch."""

    entity_description: NanoKVMSwitchEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMSwitchEntityDescription,
    ) -> None:
        """Initialize NanoKVM switch."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"switch_{description.key}",
        )

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.entity_description.available_fn(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        if self.entity_description.turn_on_fn is None:
            raise RuntimeError(f"Missing turn_on handler for switch: {self.entity_description.key}")
        async with self.coordinator.client:
            await self.entity_description.turn_on_fn(self.coordinator)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        if self.entity_description.turn_off_fn is None:
            raise RuntimeError(f"Missing turn_off handler for switch: {self.entity_description.key}")
        async with self.coordinator.client:
            await self.entity_description.turn_off_fn(self.coordinator)
        await self.coordinator.async_request_refresh()


class NanoKVMPowerSwitch(NanoKVMSwitch):
    """Defines a NanoKVM power switch with special shutdown behavior."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        if self.entity_description.turn_on_fn is None:
            raise RuntimeError(f"Missing turn_on handler for switch: {self.entity_description.key}")
        async with self.coordinator.client:
            await self.entity_description.turn_on_fn(self.coordinator)
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the power switch with monitoring for actual shutdown."""
        if self.entity_description.turn_off_fn is None:
            raise RuntimeError(f"Missing turn_off handler for switch: {self.entity_description.key}")
        async with self.coordinator.client:
            await self.entity_description.turn_off_fn(self.coordinator)

        SHUTDOWN_TIMEOUT = 300
        SHUTDOWN_POLL_INTERVAL = 5

        start_time = self.hass.loop.time()
        while self.hass.loop.time() - start_time < SHUTDOWN_TIMEOUT:
            await self.coordinator.async_request_refresh()
            if self.coordinator.gpio_info and not self.coordinator.gpio_info.pwr:
                await self.coordinator.async_request_refresh()
                return
            await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)

        _LOGGER.warning("Device did not turn off within %s seconds", SHUTDOWN_TIMEOUT)
        await self.coordinator.async_request_refresh()


class NanoKVMVirtualDeviceSwitch(NanoKVMSwitch):
    """Defines a virtual device switch backed by a toggle-only API."""

    async def _async_set_virtual_device_state(self, enabled: bool) -> None:
        """Refresh first, then toggle only when the requested state differs."""
        virtual_device = self.entity_description.virtual_device
        if virtual_device is None:
            raise RuntimeError(
                f"Missing virtual device type for switch: {self.entity_description.key}"
            )

        await self.coordinator.async_request_refresh()
        if self.is_on == enabled:
            return

        async with self.coordinator.client:
            await self.coordinator.client.update_virtual_device(virtual_device)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the virtual device switch."""
        del kwargs
        await self._async_set_virtual_device_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the virtual device switch."""
        del kwargs
        await self._async_set_virtual_device_state(False)


class NanoKVMWatchdogSwitch(NanoKVMSwitch):
    """Defines a NanoKVM watchdog switch backed by SSH file control."""

    async def _async_set_watchdog_state(self, enabled: bool) -> None:
        """Set watchdog state via SSH and refresh coordinator state."""
        collector = await self.coordinator.async_ensure_ssh_metrics_collector()
        await collector.set_watchdog_enabled(enabled)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the watchdog."""
        del kwargs
        await self._async_set_watchdog_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the watchdog."""
        del kwargs
        await self._async_set_watchdog_state(False)
