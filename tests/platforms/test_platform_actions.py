"""Tests for platform entity actions and Home Assistant-facing properties."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from homeassistant.exceptions import HomeAssistantError
from nanokvm.models import (
    DiskType,
    HidMode,
    LcdTimeFormat,
    MouseJigglerMode,
    VirtualDevice,
)
import pytest
from yarl import URL

import custom_components.nanokvm.binary_sensor as binary_sensor_module
import custom_components.nanokvm.button as button_module
import custom_components.nanokvm.number as number_module
import custom_components.nanokvm.select as select_module
import custom_components.nanokvm.sensor as sensor_module
import custom_components.nanokvm.switch as switch_module
import custom_components.nanokvm.update as update_module


def _coordinator(**overrides: object) -> SimpleNamespace:
    """Build a coordinator with a serialized client boundary and async actions."""
    client = MagicMock()
    client.url = URL("http://nanokvm.local/api/")

    @asynccontextmanager
    async def async_client() -> AsyncIterator[MagicMock]:
        yield client

    values: dict[str, object] = {
        "application_version_info": SimpleNamespace(current="1.0.0", latest="1.1.0"),
        "async_client": MagicMock(side_effect=async_client),
        "async_ensure_ssh_metrics_collector": AsyncMock(),
        "async_request_refresh": AsyncMock(),
        "client": client,
        "device_info": SimpleNamespace(application="1.0.0", device_key="test-device"),
        "gpio_info": SimpleNamespace(pwr=True, hdd=False),
        "hardware_info": None,
        "hostname_info": None,
        "is_pro_hardware": True,
        "last_update_success": True,
        "led_strip": SimpleNamespace(
            on=True,
            brightness=75,
            horizontal_count=30,
            vertical_count=20,
        ),
        "virtual_device_info": SimpleNamespace(network=False, disk=False, mic=False),
        "watchdog_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _description(descriptions: tuple[object, ...], key: str) -> object:
    """Return one entity description by key."""
    return next(item for item in descriptions if item.key == key)


@pytest.mark.asyncio
async def test_button_press_uses_serialized_client_and_refreshes() -> None:
    """Button actions must execute through coordinator client access and refresh."""
    coordinator = _coordinator()
    coordinator.client.reboot_system = AsyncMock()
    entity = button_module.NanoKVMButton(
        coordinator,
        _description(button_module.BUTTONS, "reboot"),
    )

    await entity.async_press()

    coordinator.async_client.assert_called_once_with()
    coordinator.client.reboot_system.assert_awaited_once_with()
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_button_without_handler_raises() -> None:
    """A malformed button description must fail clearly instead of doing nothing."""
    entity = button_module.NanoKVMButton(
        _coordinator(),
        button_module.NanoKVMButtonEntityDescription(key="missing"),
    )

    with pytest.raises(RuntimeError, match="Missing press handler for button: missing"):
        await entity.async_press()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (
            "led_brightness",
            25.9,
            {
                "on": True,
                "brightness": 25,
                "horizontal_count": 30,
                "vertical_count": 20,
            },
        ),
        (
            "led_horizontal_beads",
            40.9,
            {
                "on": True,
                "brightness": 75,
                "horizontal_count": 40,
                "vertical_count": 20,
            },
        ),
        (
            "led_vertical_beads",
            30.9,
            {
                "on": True,
                "brightness": 75,
                "horizontal_count": 30,
                "vertical_count": 30,
            },
        ),
    ],
)
async def test_number_actions_preserve_led_state_and_refresh(
    key: str,
    value: float,
    expected: dict[str, object],
) -> None:
    """Each LED number must update one integer field and preserve the others."""
    coordinator = _coordinator()
    coordinator.client.set_led_strip = AsyncMock()
    entity = number_module.NanoKVMNumber(
        coordinator,
        _description(number_module.NUMBERS, key),
    )

    await entity.async_set_native_value(value)

    coordinator.client.set_led_strip.assert_awaited_once_with(**expected)
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_number_validation_error_becomes_home_assistant_error() -> None:
    """Invalid LED values must surface as a Home Assistant service error."""
    coordinator = _coordinator()
    entity = number_module.NanoKVMNumber(
        coordinator,
        _description(number_module.NUMBERS, "led_brightness"),
    )

    with pytest.raises(HomeAssistantError, match="LED brightness"):
        await entity.async_set_native_value(101)

    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_number_without_handler_raises() -> None:
    """A malformed number description must fail before client access."""
    entity = number_module.NanoKVMNumber(
        _coordinator(),
        number_module.NanoKVMNumberEntityDescription(key="missing"),
    )

    with pytest.raises(
        RuntimeError, match="Missing number handler for number: missing"
    ):
        await entity.async_set_native_value(1)


def test_number_entity_exposes_dynamic_value_bounds_and_availability() -> None:
    """Number properties must delegate to the description and coordinator health."""
    coordinator = _coordinator()
    entity = number_module.NanoKVMNumber(
        coordinator,
        _description(number_module.NUMBERS, "led_horizontal_beads"),
    )

    assert entity.available is True
    assert entity.native_value == 30
    assert entity.native_min_value == 1
    assert entity.native_max_value == 110

    coordinator.last_update_success = False
    assert entity.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "option", "method", "expected_args", "expected_kwargs"),
    [
        ("hid_mode", "hid_only", "set_hid_mode", (HidMode.HID_ONLY,), {}),
        ("hid_mode", "unknown", "set_hid_mode", (HidMode.NORMAL,), {}),
        (
            "mouse_jiggler_mode",
            "relative_mode",
            "set_mouse_jiggler_state",
            (True, MouseJigglerMode.RELATIVE),
            {},
        ),
        (
            "mouse_jiggler_mode",
            "disable",
            "set_mouse_jiggler_state",
            (False, MouseJigglerMode.ABSOLUTE),
            {},
        ),
        ("oled_sleep_timeout", "30_sec", "set_oled_sleep", (30,), {}),
        ("oled_sleep_timeout", "unknown", "set_oled_sleep", (0,), {}),
        ("swap_size", "128_mb", "set_swap_size", (128,), {}),
        ("swap_size", "unknown", "set_swap_size", (0,), {}),
        (
            "lcd_time_format",
            "12h",
            "set_lcd_time_format",
            (LcdTimeFormat.TWELVE_HOUR,),
            {},
        ),
        (
            "lcd_time_format",
            "unknown",
            "set_lcd_time_format",
            (LcdTimeFormat.TWENTY_FOUR_HOUR,),
            {},
        ),
        (
            "virtual_disk_type",
            "sdcard",
            "update_virtual_device",
            (VirtualDevice.DISK,),
            {"disk_type": DiskType.SDCARD},
        ),
        (
            "virtual_disk_type",
            "unknown",
            "update_virtual_device",
            (VirtualDevice.DISK,),
            {"disk_type": DiskType.EMMC},
        ),
    ],
)
async def test_select_actions_map_options_and_refresh(
    key: str,
    option: str,
    method: str,
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
) -> None:
    """Select option keys must map to the exact library API values."""
    coordinator = _coordinator()
    client_method = AsyncMock()
    setattr(coordinator.client, method, client_method)
    entity = select_module.NanoKVMSelect(
        coordinator,
        _description(select_module.SELECTS, key),
    )

    await entity.async_select_option(option)

    client_method.assert_awaited_once_with(*expected_args, **expected_kwargs)
    coordinator.async_request_refresh.assert_awaited_once_with()


def test_select_entity_exposes_static_and_dynamic_options() -> None:
    """Select entities must expose description values and runtime disk options."""
    static = select_module.NanoKVMSelect(
        _coordinator(hid_mode=SimpleNamespace(mode=HidMode.HID_ONLY)),
        _description(select_module.SELECTS, "hid_mode"),
    )
    dynamic = select_module.NanoKVMSelect(
        _coordinator(
            virtual_device_info=SimpleNamespace(
                is_emmc_exist=True,
                is_sd_card_exist=False,
                mounted_disk="emmc",
            )
        ),
        _description(select_module.SELECTS, "virtual_disk_type"),
    )

    assert static.current_option == "hid_only"
    assert static.options == ["normal", "hid_only"]
    assert dynamic.current_option == "emmc"
    assert dynamic.options == ["emmc"]


@pytest.mark.asyncio
async def test_select_without_handler_raises() -> None:
    """A malformed select description must fail before client access."""
    entity = select_module.NanoKVMSelect(
        _coordinator(),
        select_module.NanoKVMSelectEntityDescription(key="missing"),
    )

    with pytest.raises(
        RuntimeError, match="Missing select handler for select: missing"
    ):
        await entity.async_select_option("anything")


def test_binary_sensor_and_sensor_entities_expose_coordinator_data() -> None:
    """Read-only entities must combine coordinator health with description behavior."""
    coordinator = _coordinator(value=True, attributes={"detail": "ready"})
    binary = binary_sensor_module.NanoKVMBinarySensor(
        coordinator,
        binary_sensor_module.NanoKVMBinarySensorEntityDescription(
            key="test",
            value_fn=lambda state: state.value,
            available_fn=lambda state: state.value,
        ),
    )
    sensor = sensor_module.NanoKVMSensor(
        coordinator,
        sensor_module.NanoKVMSensorEntityDescription(
            key="test",
            value_fn=lambda state: "online" if state.value else "offline",
            available_fn=lambda state: state.value,
            attributes_fn=lambda state: state.attributes,
        ),
    )

    assert binary.is_on is True
    assert binary.available is True
    assert sensor.native_value == "online"
    assert sensor.available is True
    assert sensor.extra_state_attributes == {"detail": "ready"}

    coordinator.last_update_success = False
    assert binary.available is False
    assert sensor.available is False


@pytest.mark.asyncio
async def test_generic_switch_actions_use_handlers_and_refresh() -> None:
    """Generic switches must execute both handlers through serialized client access."""
    coordinator = _coordinator(ssh_state=SimpleNamespace(enabled=False))
    coordinator.client.enable_ssh = AsyncMock()
    coordinator.client.disable_ssh = AsyncMock()
    entity = switch_module.NanoKVMSwitch(
        coordinator,
        _description(switch_module.SWITCHES, "ssh"),
    )

    await entity.async_turn_on()
    await entity.async_turn_off()

    coordinator.client.enable_ssh.assert_awaited_once_with()
    coordinator.client.disable_ssh.assert_awaited_once_with()
    assert coordinator.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_generic_switch_missing_handlers_raise() -> None:
    """Malformed generic switch descriptions must fail clearly."""
    entity = switch_module.NanoKVMSwitch(
        _coordinator(),
        switch_module.NanoKVMSwitchEntityDescription(key="missing"),
    )

    with pytest.raises(RuntimeError, match="Missing turn_on handler"):
        await entity.async_turn_on()
    with pytest.raises(RuntimeError, match="Missing turn_off handler"):
        await entity.async_turn_off()


@pytest.mark.asyncio
async def test_led_switch_preserves_strip_values_and_translates_missing_state() -> None:
    """LED power writes must preserve strip geometry and report unavailable state."""
    coordinator = _coordinator()
    coordinator.client.set_led_strip = AsyncMock()

    await switch_module._set_led_strip_on(coordinator, False)

    coordinator.client.set_led_strip.assert_awaited_once_with(
        on=False,
        brightness=75,
        horizontal_count=30,
        vertical_count=20,
    )

    coordinator.led_strip = None
    with pytest.raises(HomeAssistantError, match="LED strip state is unavailable"):
        await switch_module._set_led_strip_on(coordinator, True)


@pytest.mark.asyncio
async def test_power_state_refresh_handles_unavailable_gpio() -> None:
    """Power state refresh must return None when GPIO polling is unavailable."""
    entity = switch_module.NanoKVMPowerSwitch(
        _coordinator(gpio_info=None),
        _description(switch_module.SWITCHES, "power"),
    )

    assert await entity._async_current_power_state() is None


@pytest.mark.asyncio
async def test_power_switch_missing_handlers_raise() -> None:
    """Malformed power descriptions must fail before polling or client access."""
    entity = switch_module.NanoKVMPowerSwitch(
        _coordinator(),
        switch_module.NanoKVMSwitchEntityDescription(key="power"),
    )

    with pytest.raises(RuntimeError, match="Missing turn_on handler"):
        await entity.async_turn_on()
    with pytest.raises(RuntimeError, match="Missing turn_off handler"):
        await entity.async_turn_off()


@pytest.mark.asyncio
async def test_power_turn_off_presses_once_and_returns_when_gpio_turns_off() -> None:
    """Power-off monitoring must stop once a refreshed GPIO state is off."""
    coordinator = _coordinator()
    coordinator.client.push_button = AsyncMock()

    async def refresh() -> None:
        if coordinator.async_request_refresh.await_count == 2:
            coordinator.gpio_info.pwr = False

    coordinator.async_request_refresh.side_effect = refresh
    entity = switch_module.NanoKVMPowerSwitch(
        coordinator,
        _description(switch_module.SWITCHES, "power"),
    )
    loop = MagicMock()
    loop.time.side_effect = [0, 0]
    entity.hass = SimpleNamespace(loop=loop)

    await entity.async_turn_off()

    coordinator.client.push_button.assert_awaited_once()
    assert coordinator.async_request_refresh.await_count == 3


@pytest.mark.asyncio
async def test_power_turn_off_refreshes_after_timeout() -> None:
    """Power-off timeout must perform a final refresh without sleeping."""
    coordinator = _coordinator()
    coordinator.client.push_button = AsyncMock()
    entity = switch_module.NanoKVMPowerSwitch(
        coordinator,
        _description(switch_module.SWITCHES, "power"),
    )
    loop = MagicMock()
    loop.time.side_effect = [0, 301]
    entity.hass = SimpleNamespace(loop=loop)

    await entity.async_turn_off()

    coordinator.client.push_button.assert_awaited_once()
    assert coordinator.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_power_turn_off_sleeps_between_unsuccessful_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Power-off monitoring must delay between polls while GPIO remains on."""
    coordinator = _coordinator()
    coordinator.client.push_button = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(switch_module.asyncio, "sleep", sleep)
    entity = switch_module.NanoKVMPowerSwitch(
        coordinator,
        _description(switch_module.SWITCHES, "power"),
    )
    loop = MagicMock()
    loop.time.side_effect = [0, 0, 301]
    entity.hass = SimpleNamespace(loop=loop)

    await entity.async_turn_off()

    sleep.assert_awaited_once_with(5)
    assert coordinator.async_request_refresh.await_count == 3


@pytest.mark.asyncio
async def test_virtual_device_switch_toggles_only_when_state_differs() -> None:
    """Toggle-only virtual device APIs must not be called for an achieved state."""
    coordinator = _coordinator(
        virtual_device_info=SimpleNamespace(network=False, disk=False, mic=False)
    )
    coordinator.client.update_virtual_device = AsyncMock()
    entity = switch_module.NanoKVMVirtualDeviceSwitch(
        coordinator,
        _description(switch_module.SWITCHES, "virtual_network"),
    )

    await entity.async_turn_on()
    coordinator.client.update_virtual_device.assert_awaited_once_with(
        VirtualDevice.NETWORK
    )
    assert coordinator.async_request_refresh.await_count == 2

    coordinator.async_request_refresh.reset_mock()
    coordinator.client.update_virtual_device.reset_mock()
    coordinator.virtual_device_info.network = True
    await entity.async_turn_on()
    coordinator.client.update_virtual_device.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once_with()

    coordinator.virtual_device_info.network = True
    await entity.async_turn_off()
    coordinator.client.update_virtual_device.assert_awaited_once_with(
        VirtualDevice.NETWORK
    )


@pytest.mark.asyncio
async def test_virtual_device_switch_requires_device_type() -> None:
    """A malformed virtual-device description must fail before refresh."""
    entity = switch_module.NanoKVMVirtualDeviceSwitch(
        _coordinator(),
        switch_module.NanoKVMSwitchEntityDescription(key="missing"),
    )

    with pytest.raises(RuntimeError, match="Missing virtual device type"):
        await entity.async_turn_on()


@pytest.mark.asyncio
async def test_watchdog_switch_writes_both_states_through_collector() -> None:
    """Watchdog actions must use the ensured SSH collector and refresh state."""
    coordinator = _coordinator()
    collector = SimpleNamespace(set_watchdog_enabled=AsyncMock())
    coordinator.async_ensure_ssh_metrics_collector.return_value = collector
    entity = switch_module.NanoKVMWatchdogSwitch(
        coordinator,
        switch_module.SSH_SWITCHES[0],
    )

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert collector.set_watchdog_enabled.await_args_list[0].args == (True,)
    assert collector.set_watchdog_enabled.await_args_list[1].args == (False,)
    assert coordinator.async_request_refresh.await_count == 2


def _update_entity(coordinator: SimpleNamespace) -> update_module.NanoKVMUpdate:
    """Create the application update entity."""
    return update_module.NanoKVMUpdate(coordinator, update_module.UPDATES[0])


@pytest.mark.asyncio
async def test_update_install_calls_client_and_refreshes() -> None:
    """Successful application updates must refresh coordinator state."""
    coordinator = _coordinator()
    coordinator.client.update_application = AsyncMock()

    await _update_entity(coordinator).async_install(None, False, ignored=True)

    coordinator.client.update_application.assert_awaited_once_with()
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_update_install_accepts_expected_server_disconnect() -> None:
    """A server disconnect after starting update must not become an HA error."""
    coordinator = _coordinator()
    coordinator.client.update_application = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("updating")
    )

    await _update_entity(coordinator).async_install("1.1.0", True)

    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [aiohttp.ClientError("network"), TimeoutError("timeout")],
)
async def test_update_install_translates_client_failures(error: Exception) -> None:
    """Known client failures must surface as Home Assistant errors."""
    coordinator = _coordinator()
    coordinator.client.update_application = AsyncMock(side_effect=error)

    with pytest.raises(HomeAssistantError, match="Failed to start NanoKVM"):
        await _update_entity(coordinator).async_install(None, False)

    coordinator.async_request_refresh.assert_not_awaited()
