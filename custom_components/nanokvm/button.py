"""Button platform for Sipeed NanoKVM."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from nanokvm.models import GpioType

from .const import (
    DOMAIN,
    ICON_HID,
    ICON_KVM,
    ICON_POWER,
    ICON_RESET,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity


@dataclass
class NanoKVMButtonEntityDescription(ButtonEntityDescription):
    """Describes NanoKVM button entity."""

    press_fn: Callable[[NanoKVMDataUpdateCoordinator], None] = None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True


BUTTONS: tuple[NanoKVMButtonEntityDescription, ...] = (
    NanoKVMButtonEntityDescription(
        key="power",
        name="Power Button",
        translation_key="power",
        icon=ICON_POWER,
        press_fn=lambda coordinator: coordinator.client.push_button(GpioType.POWER, 100),
    ),
    NanoKVMButtonEntityDescription(
        key="reset",
        name="Reset Button",
        translation_key="reset",
        icon=ICON_RESET,
        press_fn=lambda coordinator: coordinator.client.push_button(GpioType.RESET, 100),
    ),
    NanoKVMButtonEntityDescription(
        key="reboot",
        name="Reboot System",
        translation_key="reboot",
        icon=ICON_RESET,
        press_fn=lambda coordinator: coordinator.client.reboot_system(),
    ),
    NanoKVMButtonEntityDescription(
        key="reset_hdmi",
        name="Reset HDMI",
        translation_key="reset_hdmi",
        icon=ICON_KVM,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda coordinator: coordinator.client.reset_hdmi(),
        available_fn=lambda coordinator: coordinator.hardware_info.version.value == "PCIE",
    ),
    NanoKVMButtonEntityDescription(
        key="reset_hid",
        name="Reset HID",
        translation_key="reset_hid",
        icon=ICON_HID,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda coordinator: coordinator.client.reset_hid(),
    ),
    NanoKVMButtonEntityDescription(
        key="update_application",
        name="Update Application",
        translation_key="update_application",
        icon=ICON_KVM,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda coordinator: coordinator.client.update_application(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM button based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMButton(
            coordinator=coordinator,
            description=description,
        )
        for description in BUTTONS
        if description.available_fn(coordinator)
    )


class NanoKVMButton(NanoKVMEntity, ButtonEntity):
    """Defines a NanoKVM button."""

    entity_description: NanoKVMButtonEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMButtonEntityDescription,
    ) -> None:
        """Initialize NanoKVM button."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"button_{description.key}",
        )

    async def async_press(self) -> None:
        """Press the button."""
        async with self.coordinator.client:
            await self.entity_description.press_fn(self.coordinator)
        await self.coordinator.async_request_refresh()
