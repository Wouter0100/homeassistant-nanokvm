"""Service registration for the Sipeed NanoKVM integration."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from nanokvm.client import NanoKVMClient
from nanokvm.models import GpioType, MouseJigglerMode

from .const import (
    ATTR_BUTTON_TYPE,
    ATTR_BRIGHTNESS,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_HORIZONTAL_COUNT,
    ATTR_MAC,
    ATTR_MODE,
    ATTR_ON,
    ATTR_TEXT,
    ATTR_VERTICAL_COUNT,
    BUTTON_TYPE_POWER,
    BUTTON_TYPE_RESET,
    CONF_PREFERRED_HOST,
    DOMAIN,
    LED_BEAD_MIN,
    LED_BEAD_TOTAL_LIMIT,
    LED_BRIGHTNESS_MAX,
    LED_BRIGHTNESS_MIN,
    SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
    SERVICE_IMAGE_DOWNLOAD_ENABLED,
    SERVICE_LIST_CUSTOM_EDIDS,
    SERVICE_LIST_IMAGES,
    SERVICE_PASTE_TEXT,
    SERVICE_PUSH_BUTTON,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_SCAN_WIFI,
    SERVICE_SET_LED_STRIP,
    SERVICE_SET_MOUSE_JIGGLER,
    SERVICE_WAKE_ON_LAN,
)
from .coordinator import NanoKVMDataUpdateCoordinator
from .led import async_set_led_strip
from .utils import host_match_key

_LOGGER = logging.getLogger(__name__)

_OPTIONAL_HOST_FIELD = {
    vol.Optional(ATTR_DEVICE_ID): vol.All(str, vol.Length(min=1)),
    vol.Optional(CONF_HOST): vol.All(str, vol.Length(min=1)),
}

PUSH_BUTTON_SCHEMA = vol.Schema(
    _OPTIONAL_HOST_FIELD | {
        vol.Required(ATTR_BUTTON_TYPE): vol.In([BUTTON_TYPE_POWER, BUTTON_TYPE_RESET]),
        vol.Optional(ATTR_DURATION, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=100, max=5000)
        ),
    }
)

PASTE_TEXT_SCHEMA = vol.Schema(
    _OPTIONAL_HOST_FIELD | {
        vol.Required(ATTR_TEXT): str,
    }
)

WAKE_ON_LAN_SCHEMA = vol.Schema(
    _OPTIONAL_HOST_FIELD | {
        vol.Required(ATTR_MAC): str,
    }
)

SET_MOUSE_JIGGLER_SCHEMA = vol.Schema(
    _OPTIONAL_HOST_FIELD | {
        vol.Required(ATTR_ENABLED): bool,
        vol.Optional(
            ATTR_MODE, default=MouseJigglerMode.ABSOLUTE.value
        ): vol.In([MouseJigglerMode.ABSOLUTE.value, MouseJigglerMode.RELATIVE.value]),
    }
)

HOST_ONLY_SCHEMA = vol.Schema(_OPTIONAL_HOST_FIELD)

SET_LED_STRIP_SCHEMA = vol.Schema(
    _OPTIONAL_HOST_FIELD
    | {
        vol.Optional(ATTR_ON): bool,
        vol.Optional(ATTR_BRIGHTNESS): vol.All(
            vol.Coerce(int),
            vol.Range(min=LED_BRIGHTNESS_MIN, max=LED_BRIGHTNESS_MAX),
        ),
        vol.Optional(ATTR_HORIZONTAL_COUNT): vol.All(
            vol.Coerce(int),
            vol.Range(min=LED_BEAD_MIN, max=LED_BEAD_TOTAL_LIMIT),
        ),
        vol.Optional(ATTR_VERTICAL_COUNT): vol.All(
            vol.Coerce(int),
            vol.Range(min=LED_BEAD_MIN, max=LED_BEAD_TOTAL_LIMIT),
        ),
    }
)

def _ensure_pro(coordinator: NanoKVMDataUpdateCoordinator, service_name: str) -> None:
    """Raise when a service requires NanoKVM Pro hardware."""
    if not coordinator.is_pro_hardware:
        raise HomeAssistantError(
            f"The {service_name} service is only available for NanoKVM Pro devices"
        )


@dataclass(frozen=True, kw_only=True)
class _Service:
    """One registered service: its schema and the device call it makes."""

    name: str
    call: Callable[
        [NanoKVMDataUpdateCoordinator, NanoKVMClient, ServiceCall], Awaitable[Any]
    ]
    schema: vol.Schema = field(default_factory=lambda: HOST_ONLY_SCHEMA)
    pro_only: bool = False
    returns_response: bool = False
    refresh: bool = False


async def _set_led_strip(
    coordinator: NanoKVMDataUpdateCoordinator, client: NanoKVMClient, call: ServiceCall
) -> None:
    """Apply the LED strip fields given in the service call."""
    try:
        await async_set_led_strip(
            coordinator,
            on=call.data.get(ATTR_ON),
            brightness=call.data.get(ATTR_BRIGHTNESS),
            horizontal_count=call.data.get(ATTR_HORIZONTAL_COUNT),
            vertical_count=call.data.get(ATTR_VERTICAL_COUNT),
        )
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err


_SERVICES: tuple[_Service, ...] = (
    _Service(
        name=SERVICE_PUSH_BUTTON,
        schema=PUSH_BUTTON_SCHEMA,
        call=lambda _, client, call: client.push_button(
            GpioType.POWER
            if call.data[ATTR_BUTTON_TYPE] == BUTTON_TYPE_POWER
            else GpioType.RESET,
            call.data[ATTR_DURATION],
        ),
    ),
    _Service(
        name=SERVICE_PASTE_TEXT,
        schema=PASTE_TEXT_SCHEMA,
        call=lambda _, client, call: client.paste_text(call.data[ATTR_TEXT]),
    ),
    _Service(name=SERVICE_REBOOT, call=lambda _, client, call: client.reboot_system()),
    _Service(name=SERVICE_RESET_HDMI, call=lambda _, client, call: client.reset_hdmi()),
    _Service(name=SERVICE_RESET_HID, call=lambda _, client, call: client.reset_hid()),
    _Service(
        name=SERVICE_WAKE_ON_LAN,
        schema=WAKE_ON_LAN_SCHEMA,
        call=lambda _, client, call: client.send_wake_on_lan(call.data[ATTR_MAC]),
    ),
    _Service(
        name=SERVICE_SET_MOUSE_JIGGLER,
        schema=SET_MOUSE_JIGGLER_SCHEMA,
        call=lambda _, client, call: client.set_mouse_jiggler_state(
            call.data[ATTR_ENABLED], MouseJigglerMode(call.data[ATTR_MODE])
        ),
        refresh=True,
    ),
    _Service(
        name=SERVICE_SET_LED_STRIP,
        schema=SET_LED_STRIP_SCHEMA,
        call=_set_led_strip,
        pro_only=True,
        refresh=True,
    ),
    _Service(
        name=SERVICE_SCAN_WIFI,
        call=lambda _, client, call: client.scan_wifi(),
        pro_only=True,
        returns_response=True,
    ),
    _Service(
        name=SERVICE_LIST_IMAGES,
        call=lambda _, client, call: client.get_images(),
        returns_response=True,
    ),
    _Service(
        name=SERVICE_IMAGE_DOWNLOAD_ENABLED,
        call=lambda _, client, call: client.is_image_download_enabled(),
        returns_response=True,
    ),
    _Service(
        name=SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
        call=lambda _, client, call: client.get_image_download_status(),
        returns_response=True,
    ),
    _Service(
        name=SERVICE_LIST_CUSTOM_EDIDS,
        call=lambda _, client, call: client.get_custom_edid_list(),
        pro_only=True,
        returns_response=True,
    ),
)


def _resolve_target_coordinator(
    hass: HomeAssistant, call: ServiceCall
) -> NanoKVMDataUpdateCoordinator:
    """Resolve the single NanoKVM device targeted by a service call."""
    domain_data = hass.data.get(DOMAIN, {})
    coordinators = list(domain_data.values())
    if not coordinators:
        raise ServiceValidationError("No NanoKVM devices are configured")

    device_id = call.data.get(ATTR_DEVICE_ID)
    if device_id is not None:
        device = dr.async_get(hass).async_get(device_id)
        for entry_id in device.config_entries if device is not None else ():
            if entry_id in domain_data:
                return domain_data[entry_id]
        raise ServiceValidationError(
            f"No NanoKVM device is configured for device {device_id}"
        )

    requested_host = call.data.get(CONF_HOST)
    if requested_host is None:
        if len(coordinators) == 1:
            return coordinators[0]
        raise ServiceValidationError(
            "Multiple NanoKVM devices are configured; specify the device_id or host field to target one device"
        )

    # An entry on a fallback address still answers to its preferred host.
    requested_host_key = host_match_key(requested_host)
    matches = [
        coordinator
        for coordinator in coordinators
        if requested_host_key
        in {
            host_match_key(host)
            for host in (
                coordinator.config_entry.data[CONF_HOST],
                coordinator.config_entry.data.get(CONF_PREFERRED_HOST),
            )
            if isinstance(host, str)
        }
    ]

    if not matches:
        raise ServiceValidationError(f"No NanoKVM device is configured for host {requested_host}")

    if len(matches) > 1:
        raise ServiceValidationError(
            f"Multiple NanoKVM devices match host {requested_host}; fix the duplicate configuration before calling this service"
        )

    return matches[0]


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    if hass.services.has_service(DOMAIN, SERVICE_PUSH_BUTTON):
        return

    def make_handler(
        service: _Service,
    ) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
        async def handle(call: ServiceCall) -> ServiceResponse:
            if service.name == SERVICE_SET_LED_STRIP and not any(
                field in call.data
                for field in (
                    ATTR_ON,
                    ATTR_BRIGHTNESS,
                    ATTR_HORIZONTAL_COUNT,
                    ATTR_VERTICAL_COUNT,
                )
            ):
                raise HomeAssistantError("At least one LED strip field is required")

            coordinator = _resolve_target_coordinator(hass, call)
            host = coordinator.config_entry.data.get(CONF_HOST, "<unknown>")
            try:
                async with coordinator.async_client() as client:
                    if service.pro_only:
                        _ensure_pro(coordinator, service.name)
                    result = await service.call(coordinator, client, call)
            except HomeAssistantError:
                raise
            except Exception as err:
                _LOGGER.error(
                    "Error executing %s service for %s: %s", service.name, host, err
                )
                raise HomeAssistantError(
                    f"Failed to execute {service.name} for {host}: {err}"
                ) from err

            _LOGGER.debug("Executed %s on %s", service.name, host)
            if service.refresh:
                await coordinator.async_request_refresh()
            return result.model_dump(mode="json") if service.returns_response else None

        return handle

    for service in _SERVICES:
        hass.services.async_register(
            DOMAIN,
            service.name,
            make_handler(service),
            schema=service.schema,
            supports_response=(
                SupportsResponse.ONLY
                if service.returns_response
                else SupportsResponse.NONE
            ),
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister integration services."""
    for service in _SERVICES:
        if hass.services.has_service(DOMAIN, service.name):
            hass.services.async_remove(DOMAIN, service.name)
