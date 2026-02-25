"""Update platform for Sipeed NanoKVM."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NanoKVMDataUpdateCoordinator
from .entity import NanoKVMEntity


@dataclass(frozen=True, kw_only=True)
class NanoKVMUpdateEntityDescription(UpdateEntityDescription):
    """Describes NanoKVM update entity."""

    available_fn: Callable[[NanoKVMDataUpdateCoordinator], bool] = lambda _: True


UPDATES: tuple[NanoKVMUpdateEntityDescription, ...] = (
    NanoKVMUpdateEntityDescription(
        key="application",
        name="Application",
        translation_key="application",
        icon="mdi:update",
        device_class=UpdateDeviceClass.FIRMWARE,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NanoKVM update based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        NanoKVMUpdate(
            coordinator=coordinator,
            description=description,
        )
        for description in UPDATES
        if description.available_fn(coordinator)
    )


class NanoKVMUpdate(NanoKVMEntity, UpdateEntity):
    """Defines a NanoKVM update entity."""

    entity_description: NanoKVMUpdateEntityDescription
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        description: NanoKVMUpdateEntityDescription,
    ) -> None:
        """Initialize NanoKVM update."""
        self.entity_description = description
        super().__init__(
            coordinator=coordinator,
            unique_id_suffix=f"update_{description.key}",
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            super().available
            and self.entity_description.available_fn(self.coordinator)
            and self.coordinator.application_version_info is not None
        )

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        if self.coordinator.application_version_info is None:
            return None
        return self.coordinator.application_version_info.current

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        if self.coordinator.application_version_info is None:
            return None
        return self.coordinator.application_version_info.latest

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Trigger NanoKVM application update."""
        del version, backup, kwargs
        async with self.coordinator.client:
            await self.coordinator.client.update_application()
        await self.coordinator.async_request_refresh()
