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
    ICON_NETWORK,
    ICON_POWER,
    SIGNAL_NEW_MEDIA_ENTITIES,
    SIGNAL_NEW_NETWORK_ENTITIES,
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


def _cdrom_supported(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether the dedicated /storage/cdrom endpoint exists on this device."""
    return coordinator.supports_cdrom_endpoint


def _has_connection_type(
    coordinator: NanoKVMDataUpdateCoordinator, connection_type: str
) -> bool:
    """Return whether an IP connection type is active."""
    return any(
        address.addr and address.type.casefold() == connection_type
        for address in coordinator.device_info.ips
    )


def _wired_active(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether a wired network connection is active."""
    return _has_connection_type(coordinator, "wired")


def _static_ip_available(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether Pro static IP state is available."""
    return coordinator.is_pro_hardware and coordinator.static_ip is not None


def _time_status_available(coordinator: NanoKVMDataUpdateCoordinator) -> bool:
    """Return whether Pro time synchronization state is available."""
    return coordinator.is_pro_hardware and coordinator.time_status is not None


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
        available_fn=lambda coordinator: _has_mounted_image(coordinator)
        and _cdrom_supported(coordinator),
        should_create_fn=_cdrom_supported,
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
    NanoKVMBinarySensorEntityDescription(
        key="wired_connected",
        name="Wired Connected",
        translation_key="wired_connected",
        icon=ICON_NETWORK,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_wired_active,
        should_create_fn=_wired_active,
    ),
    NanoKVMBinarySensorEntityDescription(
        key="static_ip_enabled",
        name="Static IP Enabled",
        translation_key="static_ip_enabled",
        icon=ICON_NETWORK,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: bool(
            coordinator.static_ip and coordinator.static_ip.enabled
        ),
        available_fn=_static_ip_available,
        should_create_fn=_static_ip_available,
    ),
    NanoKVMBinarySensorEntityDescription(
        key="time_synchronized",
        name="Time Synchronized",
        translation_key="time_synchronized",
        icon="mdi:clock-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: bool(
            coordinator.time_status and coordinator.time_status.is_synchronized
        ),
        available_fn=_time_status_available,
        should_create_fn=_time_status_available,
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
            if description.should_create_fn(coordinator)
        )
        media_entities_added = True

    if _has_mounted_image(coordinator):
        async_add_media_binary_sensors()

    network_entities_added = {
        description.key
        for description in BINARY_SENSORS
        if description.should_create_fn(coordinator)
    }

    @callback
    def async_add_network_binary_sensors(connection_type: str) -> None:
        """Add network binary sensors when a connection type appears."""
        if connection_type != "wired":
            return

        entities = [
            NanoKVMBinarySensor(
                coordinator=coordinator,
                description=description,
            )
            for description in BINARY_SENSORS
            if description.key not in network_entities_added
            and description.should_create_fn(coordinator)
        ]
        if not entities:
            return

        async_add_entities(entities)
        network_entities_added.update(entity.entity_description.key for entity in entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_MEDIA_ENTITIES.format(entry.entry_id),
            async_add_media_binary_sensors,
        )
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_NETWORK_ENTITIES.format(entry.entry_id),
            async_add_network_binary_sensors,
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
