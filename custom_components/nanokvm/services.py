"""Service registration for the Sipeed NanoKVM integration."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

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
    CONF_HOST,
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
from .led import build_led_strip_config
from .utils import host_match_key

_LOGGER = logging.getLogger(__name__)

_SERVICE_NAMES = (
    SERVICE_PUSH_BUTTON,
    SERVICE_PASTE_TEXT,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_WAKE_ON_LAN,
    SERVICE_SET_MOUSE_JIGGLER,
    SERVICE_SET_LED_STRIP,
    SERVICE_SCAN_WIFI,
    SERVICE_LIST_IMAGES,
    SERVICE_IMAGE_DOWNLOAD_ENABLED,
    SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
    SERVICE_LIST_CUSTOM_EDIDS,
)

_OPTIONAL_HOST_FIELD = {
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


def _model_to_response(value: Any) -> ServiceResponse:
    """Convert pydantic responses into Home Assistant service responses."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {"value": value}


def _ensure_pro(coordinator: NanoKVMDataUpdateCoordinator, service_name: str) -> None:
    """Raise when a service requires NanoKVM Pro hardware."""
    if not coordinator.is_pro_hardware:
        raise HomeAssistantError(
            f"The {service_name} service is only available for NanoKVM Pro devices"
        )


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    if hass.services.has_service(DOMAIN, SERVICE_PUSH_BUTTON):
        return

    def _resolve_target_coordinator(call: ServiceCall) -> NanoKVMDataUpdateCoordinator:
        """Resolve the single NanoKVM device targeted by a service call."""
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            raise HomeAssistantError("No NanoKVM devices are configured")

        requested_host = call.data.get(CONF_HOST)
        if requested_host is None:
            if len(coordinators) == 1:
                return coordinators[0]
            raise HomeAssistantError(
                "Multiple NanoKVM devices are configured; specify the host field to target one device"
            )

        requested_host_key = host_match_key(requested_host)
        matches = [
            coordinator
            for coordinator in coordinators
            if host_match_key(coordinator.config_entry.data[CONF_HOST])
            == requested_host_key
        ]

        if not matches:
            raise HomeAssistantError(f"No NanoKVM device is configured for host {requested_host}")

        if len(matches) > 1:
            raise HomeAssistantError(
                f"Multiple NanoKVM devices match host {requested_host}; fix the duplicate configuration before calling this service"
            )

        return matches[0]

    async def _execute_service(
        call: ServiceCall,
        service_name: str,
        handler: Callable[
            [NanoKVMDataUpdateCoordinator, NanoKVMClient, str], Awaitable[None]
        ],
    ) -> None:
        """Execute a service on the targeted NanoKVM device."""
        coordinator = _resolve_target_coordinator(call)
        client = coordinator.client
        host = coordinator.config_entry.data.get(CONF_HOST, "<unknown>")

        try:
            async with client:
                await handler(coordinator, client, host)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Error executing %s service for %s: %s", service_name, host, err
            )
            raise HomeAssistantError(
                f"Failed to execute {service_name} for {host}: {err}"
            ) from err

    async def _execute_response_service(
        call: ServiceCall,
        service_name: str,
        handler: Callable[
            [NanoKVMDataUpdateCoordinator, NanoKVMClient, str], Awaitable[Any]
        ],
    ) -> ServiceResponse:
        """Execute a response-returning service on the targeted NanoKVM device."""
        coordinator = _resolve_target_coordinator(call)
        client = coordinator.client
        host = coordinator.config_entry.data.get(CONF_HOST, "<unknown>")

        try:
            async with client:
                return _model_to_response(await handler(coordinator, client, host))
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Error executing %s service for %s: %s", service_name, host, err
            )
            raise HomeAssistantError(
                f"Failed to execute {service_name} for {host}: {err}"
            ) from err

    async def handle_push_button(call: ServiceCall) -> None:
        """Handle the push button service."""
        button_type = call.data[ATTR_BUTTON_TYPE]
        duration = call.data[ATTR_DURATION]
        gpio_type = GpioType.POWER if button_type == BUTTON_TYPE_POWER else GpioType.RESET

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.push_button(gpio_type, duration)
            _LOGGER.debug("Button %s pushed for %d ms on %s", button_type, duration, host)

        await _execute_service(call, SERVICE_PUSH_BUTTON, service_logic)

    async def handle_paste_text(call: ServiceCall) -> None:
        """Handle the paste text service."""
        text = call.data[ATTR_TEXT]

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.paste_text(text)
            _LOGGER.debug("Text pasted on %s", host)

        await _execute_service(call, SERVICE_PASTE_TEXT, service_logic)

    async def handle_reboot(call: ServiceCall) -> None:
        """Handle the reboot service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.reboot_system()
            _LOGGER.debug("System reboot initiated on %s", host)

        await _execute_service(call, SERVICE_REBOOT, service_logic)

    async def handle_reset_hdmi(call: ServiceCall) -> None:
        """Handle the reset HDMI service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.reset_hdmi()
            _LOGGER.debug("HDMI reset initiated on %s", host)

        await _execute_service(call, SERVICE_RESET_HDMI, service_logic)

    async def handle_reset_hid(call: ServiceCall) -> None:
        """Handle the reset HID service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.reset_hid()
            _LOGGER.debug("HID reset initiated on %s", host)

        await _execute_service(call, SERVICE_RESET_HID, service_logic)

    async def handle_wake_on_lan(call: ServiceCall) -> None:
        """Handle the wake on LAN service."""
        mac = call.data[ATTR_MAC]

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.send_wake_on_lan(mac)
            _LOGGER.debug("Wake on LAN packet sent to %s via %s", mac, host)

        await _execute_service(call, SERVICE_WAKE_ON_LAN, service_logic)

    async def handle_set_mouse_jiggler(call: ServiceCall) -> None:
        """Handle the set mouse jiggler service."""
        enabled = call.data[ATTR_ENABLED]
        mode_str = call.data[ATTR_MODE]
        mode = (
            MouseJigglerMode.ABSOLUTE
            if mode_str == MouseJigglerMode.ABSOLUTE.value
            else MouseJigglerMode.RELATIVE
        )

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            await client.set_mouse_jiggler_state(enabled, mode)
            _LOGGER.debug(
                "Mouse jiggler on %s set to %s with mode %s", host, enabled, mode_str
            )

        await _execute_service(call, SERVICE_SET_MOUSE_JIGGLER, service_logic)

    async def handle_set_led_strip(call: ServiceCall) -> None:
        """Handle the set LED strip service."""
        if not any(
            field in call.data
            for field in (
                ATTR_ON,
                ATTR_BRIGHTNESS,
                ATTR_HORIZONTAL_COUNT,
                ATTR_VERTICAL_COUNT,
            )
        ):
            raise HomeAssistantError("At least one LED strip field is required")

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> None:
            _ensure_pro(coordinator, SERVICE_SET_LED_STRIP)
            try:
                config = build_led_strip_config(
                    coordinator.led_strip,
                    on=call.data.get(ATTR_ON),
                    brightness=call.data.get(ATTR_BRIGHTNESS),
                    horizontal_count=call.data.get(ATTR_HORIZONTAL_COUNT),
                    vertical_count=call.data.get(ATTR_VERTICAL_COUNT),
                )
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err

            await client.set_led_strip(
                on=config.on,
                brightness=config.brightness,
                horizontal_count=config.horizontal_count,
                vertical_count=config.vertical_count,
            )
            _LOGGER.debug("LED strip settings updated on %s", host)

        await _execute_service(call, SERVICE_SET_LED_STRIP, service_logic)
        await _resolve_target_coordinator(call).async_request_refresh()

    async def handle_scan_wifi(call: ServiceCall) -> ServiceResponse:
        """Handle the scan Wi-Fi response service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> Any:
            _ensure_pro(coordinator, SERVICE_SCAN_WIFI)
            return await client.scan_wifi()

        return await _execute_response_service(call, SERVICE_SCAN_WIFI, service_logic)

    async def handle_list_images(call: ServiceCall) -> ServiceResponse:
        """Handle the list images response service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> Any:
            return await client.get_images()

        return await _execute_response_service(call, SERVICE_LIST_IMAGES, service_logic)

    async def handle_image_download_enabled(call: ServiceCall) -> ServiceResponse:
        """Handle the image download enabled response service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> Any:
            return await client.is_image_download_enabled()

        return await _execute_response_service(
            call,
            SERVICE_IMAGE_DOWNLOAD_ENABLED,
            service_logic,
        )

    async def handle_get_image_download_status(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Handle the image download status response service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> Any:
            return await client.get_image_download_status()

        return await _execute_response_service(
            call,
            SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
            service_logic,
        )

    async def handle_list_custom_edids(call: ServiceCall) -> ServiceResponse:
        """Handle the list custom EDIDs response service."""

        async def service_logic(
            coordinator: NanoKVMDataUpdateCoordinator,
            client: NanoKVMClient,
            host: str,
        ) -> Any:
            _ensure_pro(coordinator, SERVICE_LIST_CUSTOM_EDIDS)
            return await client.get_custom_edid_list()

        return await _execute_response_service(
            call,
            SERVICE_LIST_CUSTOM_EDIDS,
            service_logic,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_BUTTON, handle_push_button, schema=PUSH_BUTTON_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PASTE_TEXT, handle_paste_text, schema=PASTE_TEXT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REBOOT, handle_reboot, schema=HOST_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HDMI, handle_reset_hdmi, schema=HOST_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_HID, handle_reset_hid, schema=HOST_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WAKE_ON_LAN, handle_wake_on_lan, schema=WAKE_ON_LAN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MOUSE_JIGGLER,
        handle_set_mouse_jiggler,
        schema=SET_MOUSE_JIGGLER_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LED_STRIP,
        handle_set_led_strip,
        schema=SET_LED_STRIP_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_WIFI,
        handle_scan_wifi,
        schema=HOST_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_IMAGES,
        handle_list_images,
        schema=HOST_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMAGE_DOWNLOAD_ENABLED,
        handle_image_download_enabled,
        schema=HOST_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_IMAGE_DOWNLOAD_STATUS,
        handle_get_image_download_status,
        schema=HOST_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_CUSTOM_EDIDS,
        handle_list_custom_edids,
        schema=HOST_ONLY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister integration services."""
    for service_name in _SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
