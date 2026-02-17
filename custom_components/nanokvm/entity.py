"""Base NanoKVM entity class."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_TITLE
from .coordinator import NanoKVMDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class NanoKVMEntity(CoordinatorEntity):
    """Base class for NanoKVM entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        unique_id_suffix: str,
        name: str | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        if name is not None:
            self._attr_name = name
        self._attr_unique_id = f"{coordinator.device_info.device_key}_{unique_id_suffix}"
        _LOGGER.debug(
            "Created entity %s with unique_id: %s", unique_id_suffix, self._attr_unique_id
        )

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information about this NanoKVM device."""
        sw_version = self.coordinator.device_info.application
        if hasattr(self.coordinator.device_info, "image") and self.coordinator.device_info.image:
            sw_version += f" (Image: {self.coordinator.device_info.image})"

        return {
            "identifiers": {(DOMAIN, self.coordinator.device_info.device_key)},
            "name": self.coordinator.hostname_info.hostname,
            "manufacturer": "Sipeed",
            "model": f"{INTEGRATION_TITLE} {self.coordinator.hardware_info.version.value}",
            "sw_version": sw_version,
            "hw_version": self.coordinator.hardware_info.version.value,
        }
