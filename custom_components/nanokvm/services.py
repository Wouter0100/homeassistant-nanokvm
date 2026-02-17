"""Service registration for the Sipeed NanoKVM integration."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall

from nanokvm.client import NanoKVMClient
from nanokvm.models import GpioType, MouseJigglerMode

from .const import (
    ATTR_BUTTON_TYPE,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_MAC,
    ATTR_MODE,
    ATTR_TEXT,
    BUTTON_TYPE_POWER,
    BUTTON_TYPE_RESET,
    DOMAIN,
    SERVICE_PASTE_TEXT,
    SERVICE_PUSH_BUTTON,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_SET_MOUSE_JIGGLER,
    SERVICE_WAKE_ON_LAN,
)

_LOGGER = logging.getLogger(__name__)

_SERVICE_NAMES = (
    SERVICE_PUSH_BUTTON,
    SERVICE_PASTE_TEXT,
    SERVICE_REBOOT,
    SERVICE_RESET_HDMI,
    SERVICE_RESET_HID,
    SERVICE_WAKE_ON_LAN,
    SERVICE_SET_MOUSE_JIGGLER,
)

PUSH_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BUTTON_TYPE): vol.In([BUTTON_TYPE_POWER, BUTTON_TYPE_RESET]),
        vol.Optional(ATTR_DURATION, default=100): vol.All(
            vol.Coerce(int), vol.Range(min=100, max=5000)
        ),
    }
)

PASTE_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TEXT): str,
    }
)

WAKE_ON_LAN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MAC): str,
    }
)

SET_MOUSE_JIGGLER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): bool,
        vol.Optional(ATTR_MODE, default="absolute"): vol.In(["absolute", "relative"]),
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    if hass.services.has_service(DOMAIN, SERVICE_PUSH_BUTTON):
        return

    async def _execute_service(
        service_name: str,
        handler: Callable[[NanoKVMClient], Awaitable[None]],
    ) -> None:
        """Execute a service on all configured NanoKVM devices."""
        for coordinator in hass.data.get(DOMAIN, {}).values():
            client = coordinator.client
            try:
                async with client:
                    await handler(client)
            except Exception as err:
                _LOGGER.error(
                    "Error executing %s service for %s: %s", service_name, client.host, err
                )

    async def handle_push_button(call: ServiceCall) -> None:
        """Handle the push button service."""
        button_type = call.data[ATTR_BUTTON_TYPE]
        duration = call.data[ATTR_DURATION]
        gpio_type = GpioType.POWER if button_type == BUTTON_TYPE_POWER else GpioType.RESET

        async def service_logic(client: NanoKVMClient) -> None:
            await client.push_button(gpio_type, duration)
            _LOGGER.debug("Button %s pushed for %d ms on %s", button_type, duration, client.host)

        await _execute_service(SERVICE_PUSH_BUTTON, service_logic)

    async def handle_paste_text(call: ServiceCall) -> None:
        """Handle the paste text service."""
        text = call.data[ATTR_TEXT]

        async def service_logic(client: NanoKVMClient) -> None:
            await client.paste_text(text)
            _LOGGER.debug("Text pasted on %s", client.host)

        await _execute_service(SERVICE_PASTE_TEXT, service_logic)

    async def handle_reboot(_: ServiceCall) -> None:
        """Handle the reboot service."""

        async def service_logic(client: NanoKVMClient) -> None:
            await client.reboot_system()
            _LOGGER.debug("System reboot initiated on %s", client.host)

        await _execute_service(SERVICE_REBOOT, service_logic)

    async def handle_reset_hdmi(_: ServiceCall) -> None:
        """Handle the reset HDMI service."""

        async def service_logic(client: NanoKVMClient) -> None:
            await client.reset_hdmi()
            _LOGGER.debug("HDMI reset initiated on %s", client.host)

        await _execute_service(SERVICE_RESET_HDMI, service_logic)

    async def handle_reset_hid(_: ServiceCall) -> None:
        """Handle the reset HID service."""

        async def service_logic(client: NanoKVMClient) -> None:
            await client.reset_hid()
            _LOGGER.debug("HID reset initiated on %s", client.host)

        await _execute_service(SERVICE_RESET_HID, service_logic)

    async def handle_wake_on_lan(call: ServiceCall) -> None:
        """Handle the wake on LAN service."""
        mac = call.data[ATTR_MAC]

        async def service_logic(client: NanoKVMClient) -> None:
            await client.send_wake_on_lan(mac)
            _LOGGER.debug("Wake on LAN packet sent to %s via %s", mac, client.host)

        await _execute_service(SERVICE_WAKE_ON_LAN, service_logic)

    async def handle_set_mouse_jiggler(call: ServiceCall) -> None:
        """Handle the set mouse jiggler service."""
        enabled = call.data[ATTR_ENABLED]
        mode_str = call.data[ATTR_MODE]
        mode = (
            MouseJigglerMode.ABSOLUTE
            if mode_str == "absolute"
            else MouseJigglerMode.RELATIVE
        )

        async def service_logic(client: NanoKVMClient) -> None:
            await client.set_mouse_jiggler_state(enabled, mode)
            _LOGGER.debug(
                "Mouse jiggler on %s set to %s with mode %s", client.host, enabled, mode_str
            )

        await _execute_service(SERVICE_SET_MOUSE_JIGGLER, service_logic)

    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_BUTTON, handle_push_button, schema=PUSH_BUTTON_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PASTE_TEXT, handle_paste_text, schema=PASTE_TEXT_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_REBOOT, handle_reboot)
    hass.services.async_register(DOMAIN, SERVICE_RESET_HDMI, handle_reset_hdmi)
    hass.services.async_register(DOMAIN, SERVICE_RESET_HID, handle_reset_hid)
    hass.services.async_register(
        DOMAIN, SERVICE_WAKE_ON_LAN, handle_wake_on_lan, schema=WAKE_ON_LAN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MOUSE_JIGGLER,
        handle_set_mouse_jiggler,
        schema=SET_MOUSE_JIGGLER_SCHEMA,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister integration services."""
    for service_name in _SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
