"""Number platform for Sipeed NanoKVM."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ICON_LED_STRIP,
    LED_BEAD_MIN,
    LED_BEAD_TOTAL_LIMIT,
    LED_BRIGHTNESS_MAX,
    LED_BRIGHTNESS_MIN,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity
from .led import build_led_strip_config, max_horizontal_count, max_vertical_count


@dataclass(frozen=True, kw_only=True)
class NanoKVMNumberEntityDescription(NumberEntityDescription):
    """Describes NanoKVM number entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], float | None] = lambda _: None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    min_value_fn: Callable[[NanoKVMDataUpdateCoordinator], float] = (
        lambda _: LED_BEAD_MIN
    )
    max_value_fn: Callable[[NanoKVMDataUpdateCoordinator], float] = (
        lambda _: LED_BEAD_TOTAL_LIMIT
    )
    set_value_fn: Callable[
        [NanoKVMDataUpdateCoordinator, float], Awaitable[Any]
    ] | None = None


def _has_led_strip(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether Pro LED strip state is available."""
    return coordinator.is_pro_hardware and coordinator.led_strip is not None


def _led_brightness_value(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> float | None:
    """Return LED strip brightness."""
    return coordinator.led_strip.brightness if coordinator.led_strip else None


def _led_horizontal_value(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> float | None:
    """Return horizontal LED bead count."""
    return coordinator.led_strip.horizontal_count if coordinator.led_strip else None


def _led_vertical_value(
    coordinator: NanoKVMDataUpdateCoordinator,
) -> float | None:
    """Return vertical LED bead count."""
    return coordinator.led_strip.vertical_count if coordinator.led_strip else None


def _led_horizontal_max(coordinator: NanoKVMDataUpdateCoordinator) -> float:
    """Return maximum horizontal beads for the current vertical count."""
    vertical_count = (
        coordinator.led_strip.vertical_count
        if coordinator.led_strip
        else LED_BEAD_MIN
    )
    return max_horizontal_count(vertical_count)


def _led_vertical_max(coordinator: NanoKVMDataUpdateCoordinator) -> float:
    """Return maximum vertical beads for the current horizontal count."""
    horizontal_count = (
        coordinator.led_strip.horizontal_count
        if coordinator.led_strip
        else LED_BEAD_MIN
    )
    return max_vertical_count(horizontal_count)


async def _set_led_brightness(
    coordinator: NanoKVMDataUpdateCoordinator, value: float
) -> None:
    """Set LED strip brightness while preserving other LED settings."""
    config = build_led_strip_config(coordinator.led_strip, brightness=int(value))
    await coordinator.client.set_led_strip(
        on=config.on,
        brightness=config.brightness,
        horizontal_count=config.horizontal_count,
        vertical_count=config.vertical_count,
    )


async def _set_led_horizontal_count(
    coordinator: NanoKVMDataUpdateCoordinator, value: float
) -> None:
    """Set horizontal LED bead count while preserving other LED settings."""
    config = build_led_strip_config(coordinator.led_strip, horizontal_count=int(value))
    await coordinator.client.set_led_strip(
        on=config.on,
        brightness=config.brightness,
        horizontal_count=config.horizontal_count,
        vertical_count=config.vertical_count,
    )


async def _set_led_vertical_count(
    coordinator: NanoKVMDataUpdateCoordinator, value: float
) -> None:
    """Set vertical LED bead count while preserving other LED settings."""
    config = build_led_strip_config(coordinator.led_strip, vertical_count=int(value))
    await coordinator.client.set_led_strip(
        on=config.on,
        brightness=config.brightness,
        horizontal_count=config.horizontal_count,
        vertical_count=config.vertical_count,
    )


NUMBERS: tuple[NanoKVMNumberEntityDescription, ...] = (
    NanoKVMNumberEntityDescription(
        key="led_brightness",
        name="LED Brightness",
        translation_key="led_brightness",
        icon=ICON_LED_STRIP,
        entity_category=EntityCategory.CONFIG,
        native_min_value=LED_BRIGHTNESS_MIN,
        native_max_value=LED_BRIGHTNESS_MAX,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        value_fn=_led_brightness_value,
        available_fn=_has_led_strip,
        min_value_fn=lambda _: LED_BRIGHTNESS_MIN,
        max_value_fn=lambda _: LED_BRIGHTNESS_MAX,
        set_value_fn=_set_led_brightness,
    ),
    NanoKVMNumberEntityDescription(
        key="led_horizontal_beads",
        name="LED Horizontal Beads",
        translation_key="led_horizontal_beads",
        icon=ICON_LED_STRIP,
        entity_category=EntityCategory.CONFIG,
        native_min_value=LED_BEAD_MIN,
        native_max_value=LED_BEAD_TOTAL_LIMIT - 2,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=_led_horizontal_value,
        available_fn=_has_led_strip,
        min_value_fn=lambda _: LED_BEAD_MIN,
        max_value_fn=_led_horizontal_max,
        set_value_fn=_set_led_horizontal_count,
    ),
    NanoKVMNumberEntityDescription(
        key="led_vertical_beads",
        name="LED Vertical Beads",
        translation_key="led_vertical_beads",
        icon=ICON_LED_STRIP,
        entity_category=EntityCategory.CONFIG,
        native_min_value=LED_BEAD_MIN,
        native_max_value=(LED_BEAD_TOTAL_LIMIT - 1) // 2,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=_led_vertical_value,
        available_fn=_has_led_strip,
        min_value_fn=lambda _: LED_BEAD_MIN,
        max_value_fn=_led_vertical_max,
        set_value_fn=_set_led_vertical_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM number based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMNumber(
            coordinator=coordinator,
            description=description,
        )
        for description in NUMBERS
        if description.available_fn(coordinator)
    )


class NanoKVMNumber(NanoKVMEntity, NumberEntity):
    """Defines a NanoKVM number."""

    entity_description: NanoKVMNumberEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMNumberEntityDescription,
    ) -> None:
        """Initialize NanoKVM number."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"number_{description.key}",
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.entity_description.available_fn(
            self.coordinator
        )

    @property
    def native_value(self) -> float | None:
        """Return the number value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def native_min_value(self) -> float:
        """Return the dynamic minimum value."""
        return self.entity_description.min_value_fn(self.coordinator)

    @property
    def native_max_value(self) -> float:
        """Return the dynamic maximum value."""
        return self.entity_description.max_value_fn(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        """Set the number value."""
        if self.entity_description.set_value_fn is None:
            raise RuntimeError(
                f"Missing number handler for number: {self.entity_description.key}"
            )

        try:
            async with self.coordinator.client:
                await self.entity_description.set_value_fn(self.coordinator, value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        await self.coordinator.async_request_refresh()
