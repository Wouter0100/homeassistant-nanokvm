"""Sensor platform for Sipeed NanoKVM."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NanoKVMDataUpdateCoordinator
from .const import (
    DOMAIN,
    ICON_DISK,
    ICON_IMAGE,
    ICON_KVM,
    ICON_NETWORK,
    ICON_SSH,
    SIGNAL_NEW_SSH_SENSORS,
)
from .entity import NanoKVMEntity

_LOGGER = logging.getLogger(__name__)

_TAILSCALE_STATE_MAP: dict[str, str] = {
    "notInstall": "not_install",
    "notRunning": "not_running",
    "notLogin": "not_login",
}


def _normalize_tailscale_state(raw_state: str | None) -> str | None:
    """Map API tailscale states to translation-safe keys."""
    if raw_state is None:
        return None
    return _TAILSCALE_STATE_MAP.get(raw_state, raw_state)


@dataclass
class NanoKVMSensorEntityDescription(SensorEntityDescription):
    """Describes NanoKVM sensor entity."""

    value_fn: Callable[[NanoKVMDataUpdateCoordinator], Any] = None
    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    should_create_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True
    attributes_fn: Callable[[NanoKVMDataUpdateCoordinator], dict[str, Any]] = lambda _: {}


SENSORS: tuple[NanoKVMSensorEntityDescription, ...] = (
    NanoKVMSensorEntityDescription(
        key="firmware_version",
        name="Firmware Version",
        translation_key="firmware_version",
        icon=ICON_KVM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.device_info.application,
        attributes_fn=lambda coordinator: {
            "image_version": coordinator.device_info.image,
            "hardware_version": coordinator.hardware_info.version.value,
            "wifi_supported": coordinator.wifi_status.supported,
            "oled_present": coordinator.oled_info.exist,
        },
    ),
    NanoKVMSensorEntityDescription(
        key="mounted_image",
        name="Mounted Image",
        translation_key="mounted_image",
        icon=ICON_IMAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.mounted_image.file,
        should_create_fn=lambda coordinator: coordinator.mounted_image.file != "",
        available_fn=lambda coordinator: coordinator.mounted_image.file != "",
    ),
    NanoKVMSensorEntityDescription(
        key="tailscale_state",
        name="Tailscale",
        translation_key="tailscale_state",
        icon=ICON_NETWORK,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: _normalize_tailscale_state(
            coordinator.tailscale_status.state.value
        ),
        should_create_fn=lambda coordinator: coordinator.tailscale_status is not None,
        available_fn=lambda coordinator: coordinator.tailscale_status is not None,
        attributes_fn=lambda coordinator: {
            "name": coordinator.tailscale_status.name,
            "ip": coordinator.tailscale_status.ip,
            "account": coordinator.tailscale_status.account,
        }
        if coordinator.tailscale_status is not None
        else {},
    ),
)

SSH_SENSORS: tuple[NanoKVMSensorEntityDescription, ...] = (
    NanoKVMSensorEntityDescription(
        key="uptime",
        name="Uptime",
        translation_key="uptime",
        icon=ICON_SSH,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.uptime,
        available_fn=lambda coordinator: coordinator.uptime is not None,
    ),
    NanoKVMSensorEntityDescription(
        key="cpu_temperature",
        name="CPU Temperature",
        translation_key="cpu_temperature",
        icon="mdi:thermometer",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.cpu_temperature,
        available_fn=lambda coordinator: coordinator.cpu_temperature is not None,
    ),
    NanoKVMSensorEntityDescription(
        key="memory_used_percent",
        name="Memory Used",
        translation_key="memory_used_percent",
        icon="mdi:memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.memory_used_percent,
        available_fn=lambda coordinator: coordinator.memory_used_percent is not None,
        attributes_fn=lambda coordinator: (
            {"total_mb": coordinator.memory_total}
            if coordinator.memory_total is not None
            else {}
        ),
    ),
    NanoKVMSensorEntityDescription(
        key="storage_used_percent",
        name="Storage Used",
        translation_key="storage_used_percent",
        icon=ICON_DISK,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.storage_used_percent,
        available_fn=lambda coordinator: coordinator.storage_used_percent is not None,
        attributes_fn=lambda coordinator: (
            {"total_mb": coordinator.storage_total}
            if coordinator.storage_total is not None
            else {}
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM sensor based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMSensor(
            coordinator=coordinator,
            description=description,
        )
        for description in SENSORS
        if description.should_create_fn(coordinator)
    )

    if coordinator.ssh_state and coordinator.ssh_state.enabled:
        _LOGGER.debug("SSH already enabled, creating SSH sensors")
        async_add_entities(
            NanoKVMSensor(
                coordinator=coordinator,
                description=description,
            )
            for description in SSH_SENSORS
        )
        coordinator.ssh_sensors_created = True

    @callback
    def async_add_ssh_sensors() -> None:
        """Add SSH sensors when SSH is enabled."""
        _LOGGER.debug("Received signal to create SSH sensors")
        async_add_entities(
            NanoKVMSensor(
                coordinator=coordinator,
                description=description,
            )
            for description in SSH_SENSORS
        )

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_SSH_SENSORS.format(entry.entry_id), async_add_ssh_sensors
        )
    )


class NanoKVMSensor(NanoKVMEntity, SensorEntity):
    """Defines a NanoKVM sensor."""

    entity_description: NanoKVMSensorEntityDescription

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMSensorEntityDescription,
    ) -> None:
        """Initialize NanoKVM sensor."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"sensor_{description.key}",
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.entity_description.available_fn(self.coordinator)

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return self.entity_description.attributes_fn(self.coordinator)
