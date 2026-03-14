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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from nanokvm.models import HWVersion

from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_POWER,
    SIGNAL_NEW_MEDIA_ENTITIES,
    ICON_WIFI,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity


@dataclass(frozen=True, kw_only=True)
class NanoKVMBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes NanoKVM binary sensor entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = (
        lambda _: False
    )
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = (
        lambda _: True
    )
    should_create_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = (
        lambda _: True
    )


def _is_alpha_hardware(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether the device is Alpha hardware."""
    return bool(
        coordinator.hardware_info
        and coordinator.hardware_info.version == HWVersion.ALPHA
    )


def _wifi_supported(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether Wi-Fi is supported."""
    return bool(coordinator.wifi_status and coordinator.wifi_status.supported)


def _has_mounted_image(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether there is a mounted image."""
    return bool(coordinator.mounted_image and coordinator.mounted_image.file != "")


MEDIA_BINARY_SENSORS: tuple[NanoKVMBinarySensorEntityDescription, ...] = (
    NanoKVMBinarySensorEntityDescription(
        key="cdrom_mode",
        name="CD-ROM Mode",
        translation_key="cdrom_mode",
        icon=ICON_DISK,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: bool(
            coordinator.cdrom_status and coordinator.cdrom_status.cdrom == 1
        ),
        available_fn=_has_mounted_image,
    ),
)


BINARY_SENSORS: tuple[NanoKVMBinarySensorEntityDescription, ...] = (
    NanoKVMBinarySensorEntityDescription(
        key="power_led",
        name="Power LED",
        translation_key="power_led",
        icon=ICON_POWER,
        value_fn=lambda coordinator: bool(
            coordinator.gpio_info and coordinator.gpio_info.pwr
        ),
    ),
    NanoKVMBinarySensorEntityDescription(
        key="hdd_led",
        name="HDD LED",
        translation_key="hdd_led",
        icon=ICON_DISK,
        value_fn=lambda coordinator: bool(
            coordinator.gpio_info and coordinator.gpio_info.hdd
        ),
        # HDD LED is only valid for Alpha hardware
        available_fn=_is_alpha_hardware,
        should_create_fn=_is_alpha_hardware,
    ),
    NanoKVMBinarySensorEntityDescription(
        key="wifi_connected",
        name="WiFi Connected",
        translation_key="wifi_connected",
        icon=ICON_WIFI,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: bool(
            coordinator.wifi_status and coordinator.wifi_status.connected
        ),
        available_fn=_wifi_supported,
        should_create_fn=_wifi_supported,
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
        if description.should_create_fn(coordinator)
    )

    media_entities_added = False

    @callback
    def async_add_media_binary_sensors() -> None:
        """Add media-backed binary sensors when media is first mounted."""
        nonlocal media_entities_added
        if media_entities_added:
            return
        if not _has_mounted_image(coordinator):
            return

        async_add_entities(
            NanoKVMBinarySensor(
                coordinator=coordinator,
                description=description,
            )
            for description in MEDIA_BINARY_SENSORS
        )
        media_entities_added = True

    if _has_mounted_image(coordinator):
        async_add_media_binary_sensors()

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_MEDIA_ENTITIES.format(entry.entry_id),
            async_add_media_binary_sensors,
        )
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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.entity_description.available_fn(self.coordinator)
