"""Select platform for Sipeed NanoKVM."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from nanokvm.models import HidMode, MouseJigglerMode

from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_HID,
    ICON_MOUSE_JIGGLER,
    ICON_OLED,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)


@dataclass
class NanoKVMSelectEntityDescription(SelectEntityDescription):
    """Describes NanoKVM select entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], str] = None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    select_option_fn: Callable[[NanoKVMDataUpdateCoordinator, str], Any] = None


MOUSE_JIGGLER_OPTIONS = {
    "disable": None,
    "relative_mode": MouseJigglerMode.RELATIVE,
    "absolute_mode": MouseJigglerMode.ABSOLUTE,
}

HID_MODE_OPTIONS = {
    "normal": HidMode.NORMAL,
    "hid_only": HidMode.HID_ONLY,
}
HID_MODE_VALUES = {v: k for k, v in HID_MODE_OPTIONS.items()}

OLED_SLEEP_OPTIONS = {
    "never": 0,
    "15_sec": 15,
    "30_sec": 30,
    "1_min": 60,
    "3_min": 180,
    "5_min": 300,
    "10_min": 600,
    "30_min": 1800,
    "1_hour": 3600,
}
OLED_SLEEP_VALUES = {v: k for k, v in OLED_SLEEP_OPTIONS.items()}

SWAP_OPTIONS = {
    "disable": 0,
    "64_mb": 64,
    "128_mb": 128,
    "256_mb": 256,
    "512_mb": 512,
}
SWAP_VALUES = {v: k for k, v in SWAP_OPTIONS.items()}


def _hid_mode_value(coordinator: NanoKVMDataUpdateCoordinator) -> str:
    """Return current HID mode option."""
    return HID_MODE_VALUES.get(coordinator.hid_mode.mode, HID_MODE_VALUES[HidMode.NORMAL])


def _set_hid_mode(coordinator: NanoKVMDataUpdateCoordinator, option: str) -> Any:
    """Set HID mode from option key."""
    return coordinator.client.set_hid_mode(HID_MODE_OPTIONS.get(option, HidMode.NORMAL))


def _mouse_jiggler_mode_value(coordinator: NanoKVMDataUpdateCoordinator) -> str:
    """Return current mouse jiggler option."""
    if not coordinator.mouse_jiggler_state or not coordinator.mouse_jiggler_state.enabled:
        return "disable"
    return f"{coordinator.mouse_jiggler_state.mode.value}_mode"


def _set_mouse_jiggler_mode(coordinator: NanoKVMDataUpdateCoordinator, option: str) -> Any:
    """Set mouse jiggler state from option key."""
    return coordinator.client.set_mouse_jiggler_state(
        MOUSE_JIGGLER_OPTIONS.get(option) is not None,
        MOUSE_JIGGLER_OPTIONS.get(option) or MouseJigglerMode.ABSOLUTE,
    )


def _oled_sleep_value(coordinator: NanoKVMDataUpdateCoordinator) -> str:
    """Return current OLED sleep option."""
    return OLED_SLEEP_VALUES.get(coordinator.oled_info.sleep, f"{coordinator.oled_info.sleep}_sec")


def _set_oled_sleep(coordinator: NanoKVMDataUpdateCoordinator, option: str) -> Any:
    """Set OLED sleep timeout from option key."""
    return coordinator.client.set_oled_sleep(OLED_SLEEP_OPTIONS.get(option, 0))


def _swap_size_value(coordinator: NanoKVMDataUpdateCoordinator) -> str:
    """Return current swap size option."""
    if coordinator.swap_size is None:
        return "disable"
    return SWAP_VALUES.get(coordinator.swap_size, f"{coordinator.swap_size}_mb")


def _set_swap_size(coordinator: NanoKVMDataUpdateCoordinator, option: str) -> Any:
    """Set swap size from option key."""
    return coordinator.client.set_swap_size(SWAP_OPTIONS.get(option, 0))


SELECTS: tuple[NanoKVMSelectEntityDescription, ...] = (
    NanoKVMSelectEntityDescription(
        key="hid_mode",
        name="HID Mode (Reboot Required)",
        translation_key="hid_mode",
        icon=ICON_HID,
        entity_category=EntityCategory.CONFIG,
        options=list(HID_MODE_OPTIONS.keys()),
        value_fn=_hid_mode_value,
        select_option_fn=_set_hid_mode,
        available_fn=lambda coordinator: coordinator.hid_mode is not None,
    ),
    NanoKVMSelectEntityDescription(
        key="mouse_jiggler_mode",
        name="Mouse Jiggler Mode",
        translation_key="mouse_jiggler_mode",
        icon=ICON_MOUSE_JIGGLER,
        entity_category=EntityCategory.CONFIG,
        options=list(MOUSE_JIGGLER_OPTIONS.keys()),
        value_fn=_mouse_jiggler_mode_value,
        select_option_fn=_set_mouse_jiggler_mode,
        available_fn=lambda coordinator: coordinator.mouse_jiggler_state is not None,
    ),
    NanoKVMSelectEntityDescription(
        key="oled_sleep_timeout",
        name="OLED Sleep Timeout",
        translation_key="oled_sleep_timeout",
        icon=ICON_OLED,
        entity_category=EntityCategory.CONFIG,
        options=list(OLED_SLEEP_OPTIONS.keys()),
        value_fn=_oled_sleep_value,
        select_option_fn=_set_oled_sleep,
        available_fn=lambda coordinator: coordinator.oled_info.exist,
    ),
    NanoKVMSelectEntityDescription(
        key="swap_size",
        name="Swap Size",
        translation_key="swap_size",
        icon=ICON_DISK,
        entity_category=EntityCategory.CONFIG,
        options=list(SWAP_OPTIONS.keys()),
        value_fn=_swap_size_value,
        select_option_fn=_set_swap_size,
        available_fn=lambda coordinator: coordinator.swap_size is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM select based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMSelect(
            coordinator=coordinator,
            description=description,
        )
        for description in SELECTS
        if description.available_fn(coordinator)
    )


class NanoKVMSelect(NanoKVMEntity, SelectEntity):
    """Defines a NanoKVM select."""

    entity_description: NanoKVMSelectEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMSelectEntityDescription,
    ) -> None:
        """Initialize NanoKVM select."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"select_{description.key}",
        )

    @property
    def current_option(self) -> str:
        """Return the current selected option."""
        return self.entity_description.value_fn(self.coordinator)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        async with self.coordinator.client:
            await self.entity_description.select_option_fn(self.coordinator, option)
        await self.coordinator.async_request_refresh()
