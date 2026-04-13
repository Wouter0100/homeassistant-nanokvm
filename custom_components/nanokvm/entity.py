"""Base NanoKVM entity class."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_TITLE
from .coordinator import NanoKVMDataUpdateCoordinator
from .utils import api_base_url_to_web_url

_LOGGER = logging.getLogger(__name__)


class NanoKVMEntity(CoordinatorEntity[NanoKVMDataUpdateCoordinator]):
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
        device_data = self.coordinator.device_info
        hostname = (
            self.coordinator.hostname_info.hostname
            if self.coordinator.hostname_info is not None
            else INTEGRATION_TITLE
        )
        hw_version = (
            self.coordinator.hardware_info.version.value
            if self.coordinator.hardware_info is not None
            else "Unknown"
        )

        sw_version = device_data.application
        image = getattr(device_data, "image", None)
        if image:
            sw_version += f" (Image: {image})"

        return {
            "identifiers": {(DOMAIN, device_data.device_key)},
            "name": hostname,
            "manufacturer": "Sipeed",
            "model": f"{INTEGRATION_TITLE} {hw_version}",
            "sw_version": sw_version,
            "hw_version": hw_version,
            "configuration_url": api_base_url_to_web_url(str(self.coordinator.client.url)),
        }
