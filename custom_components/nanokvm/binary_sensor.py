"""Binary sensor platform for Sipeed NanoKVM."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_POWER,
    ICON_WIFI,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity


@dataclass
class NanoKVMBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes NanoKVM binary sensor entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True


BINARY_SENSORS: tuple[NanoKVMBinarySensorEntityDescription, ...] = (
    NanoKVMBinarySensorEntityDescription(
        key="power_led",
        name="Power LED",
        translation_key="power_led",
        icon=ICON_POWER,
        value_fn=lambda coordinator: coordinator.gpio_info.pwr,
    ),
    NanoKVMBinarySensorEntityDescription(
        key="hdd_led",
        name="HDD LED",
        translation_key="hdd_led",
        icon=ICON_DISK,
        value_fn=lambda coordinator: coordinator.gpio_info.hdd,
        # HDD LED is only valid for Alpha hardware
        available_fn=lambda coordinator: coordinator.hardware_info.version.value == "Alpha",
    ),
    NanoKVMBinarySensorEntityDescription(
        key="wifi_connected",
        name="WiFi Connected",
        translation_key="wifi_connected",
        icon=ICON_WIFI,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.wifi_status.connected,
        available_fn=lambda coordinator: coordinator.wifi_status.supported,
    ),
    NanoKVMBinarySensorEntityDescription(
        key="cdrom_mode",
        name="CD-ROM Mode",
        translation_key="cdrom_mode",
        icon=ICON_DISK,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.cdrom_status.cdrom == 1,
        available_fn=lambda coordinator: coordinator.mounted_image.file != "",
    ),
    NanoKVMBinarySensorEntityDescription(
        key="update_available",
        name="Update Available",
        translation_key="update_available",
        icon="mdi:update",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: (
            coordinator.application_version_info.current
            != coordinator.application_version_info.latest
        ),
        available_fn=lambda coordinator: coordinator.application_version_info is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM binary sensor based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMBinarySensor(
            coordinator=coordinator,
            description=description,
        )
        for description in BINARY_SENSORS
        if description.available_fn(coordinator)
    )


class NanoKVMBinarySensor(NanoKVMEntity, BinarySensorEntity):
    """Defines a NanoKVM binary sensor."""

    entity_description: NanoKVMBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMBinarySensorEntityDescription,
    ) -> None:
        """Initialize NanoKVM binary sensor."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"binary_sensor_{description.key}",
        )

    @property
    def is_on(self) -> bool:
        """Return the state of the binary sensor."""
        return self.entity_description.value_fn(self.coordinator)
