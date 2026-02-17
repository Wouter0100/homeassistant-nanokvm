"""Switch platform for Sipeed NanoKVM."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from nanokvm.models import VirtualDevice, GpioType

from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_HDMI,
    ICON_MDNS,
    ICON_NETWORK,
    ICON_SSH,
    ICON_POWER,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)


@dataclass
class NanoKVMSwitchEntityDescription(SwitchEntityDescription):
    """Describes NanoKVM switch entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    turn_on_fn: Callable[[NanoKVMDataUpdateCoordinator], None] = None
    turn_off_fn: Callable[[NanoKVMDataUpdateCoordinator], None] = None


SWITCHES: tuple[NanoKVMSwitchEntityDescription, ...] = (
    NanoKVMSwitchEntityDescription(
        key="ssh",
        name="SSH",
        translation_key="ssh",
        icon=ICON_SSH,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: coordinator.ssh_state.enabled,
        turn_on_fn=lambda coordinator: coordinator.client.enable_ssh(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_ssh(),
    ),
    NanoKVMSwitchEntityDescription(
        key="mdns",
        name="mDNS",
        translation_key="mdns",
        icon=ICON_MDNS,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: coordinator.mdns_state.enabled,
        turn_on_fn=lambda coordinator: coordinator.client.enable_mdns(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_mdns(),
    ),
    NanoKVMSwitchEntityDescription(
        key="virtual_network",
        name="Virtual Network",
        translation_key="virtual_network",
        icon=ICON_NETWORK,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: coordinator.virtual_device_info.network,
        turn_on_fn=lambda coordinator: coordinator.client.update_virtual_device(VirtualDevice.NETWORK),
        turn_off_fn=lambda coordinator: coordinator.client.update_virtual_device(VirtualDevice.NETWORK),
    ),
    NanoKVMSwitchEntityDescription(
        key="virtual_disk",
        name="Virtual Disk",
        translation_key="virtual_disk",
        icon=ICON_DISK,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: coordinator.virtual_device_info.disk,
        turn_on_fn=lambda coordinator: coordinator.client.update_virtual_device(VirtualDevice.DISK),
        turn_off_fn=lambda coordinator: coordinator.client.update_virtual_device(VirtualDevice.DISK),
    ),
    NanoKVMSwitchEntityDescription(
        key="power",
        name="Power",
        translation_key="power",
        icon=ICON_POWER,
        value_fn=lambda coordinator: coordinator.gpio_info.pwr,
        turn_on_fn=lambda coordinator: coordinator.client.push_button(GpioType.POWER, 200),
        turn_off_fn=lambda coordinator: coordinator.client.push_button(GpioType.POWER, 200),
    ),
    NanoKVMSwitchEntityDescription(
        key="hdmi",
        name="HDMI Output",
        translation_key="hdmi",
        icon=ICON_HDMI,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda coordinator: coordinator.hdmi_state.enabled if coordinator.hdmi_state else False,
        turn_on_fn=lambda coordinator: coordinator.client.enable_hdmi(),
        turn_off_fn=lambda coordinator: coordinator.client.disable_hdmi(),
        available_fn=lambda coordinator: (
            coordinator.hdmi_state is not None and
            coordinator.hardware_info.version.value == "PCIE"
        ),
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        async with self.coordinator.client:
            await self.entity_description.turn_on_fn(self.coordinator)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        async with self.coordinator.client:
            await self.entity_description.turn_off_fn(self.coordinator)
        await self.coordinator.async_request_refresh()


class NanoKVMPowerSwitch(NanoKVMSwitch):
    """Defines a NanoKVM power switch with special shutdown behavior."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        async with self.coordinator.client:
            await self.entity_description.turn_on_fn(self.coordinator)
        await asyncio.sleep(1)  # Give the device a moment to respond
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the power switch with monitoring for actual shutdown."""
        async with self.coordinator.client:
            await self.entity_description.turn_off_fn(self.coordinator)

        # Wait for the device to be off, with a timeout
        SHUTDOWN_TIMEOUT = 300
        SHUTDOWN_POLL_INTERVAL = 5

        start_time = self.hass.loop.time()
        while self.hass.loop.time() - start_time < SHUTDOWN_TIMEOUT:
            await self.coordinator.async_request_refresh()  # Request a refresh of coordinator data
            if self.coordinator.gpio_info and not self.coordinator.gpio_info.pwr:
                # Device is off, refresh one last time to ensure state is updated
                await self.coordinator.async_request_refresh()
                return
            await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)

        # If timeout is reached and device is still on, log a warning
        _LOGGER.warning("Device did not turn off within %s seconds", SHUTDOWN_TIMEOUT)
        await self.coordinator.async_request_refresh()  # Refresh to show current (likely still on) state
