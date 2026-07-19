"""Tests for Home Assistant platform setup and dynamic entity registration."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_HOST
from nanokvm.models import HWVersion
import pytest
from yarl import URL

import custom_components.nanokvm.binary_sensor as binary_sensor_module
import custom_components.nanokvm.button as button_module
from custom_components.nanokvm.const import (
    DOMAIN,
    SIGNAL_NEW_MEDIA_ENTITIES,
    SIGNAL_NEW_NETWORK_ENTITIES,
    SIGNAL_NEW_SSH_SENSORS,
    SIGNAL_NEW_SSH_SWITCHES,
)
import custom_components.nanokvm.number as number_module
import custom_components.nanokvm.select as select_module
import custom_components.nanokvm.sensor as sensor_module
import custom_components.nanokvm.switch as switch_module
import custom_components.nanokvm.update as update_module


def _coordinator(**overrides: object) -> SimpleNamespace:
    """Build a coordinator-shaped state object accepted by every platform."""
    values: dict[str, object] = {
        "application_version_info": SimpleNamespace(current="1.0.0", latest="1.1.0"),
        "client": SimpleNamespace(url=URL("http://nanokvm.local/api/")),
        "device_info": SimpleNamespace(
            application="1.0.0",
            device_key="test-device",
            image="2026-01-01",
            ips=[],
        ),
        "gpio_info": SimpleNamespace(pwr=False, hdd=False),
        "hardware_info": None,
        "hdmi_capture": None,
        "hdmi_passthrough": None,
        "hdmi_state": None,
        "hid_mode": None,
        "hostname_info": None,
        "is_pro_hardware": False,
        "last_update_success": True,
        "lcd_time_format": None,
        "led_strip": None,
        "low_power": None,
        "media": SimpleNamespace(
            recording=SimpleNamespace(
                is_recording=False,
                current_filename=None,
                async_add_state_listener=MagicMock(return_value=MagicMock()),
            ),
            async_start_automatic=AsyncMock(),
            async_stop_automatic=AsyncMock(),
        ),
        "mdns_state": SimpleNamespace(enabled=False),
        "mounted_image": None,
        "mouse_jiggler_state": None,
        "oled_info": None,
        "ssh_sensors_created": False,
        "ssh_state": SimpleNamespace(enabled=False),
        "ssh_switches_created": False,
        "static_ip": None,
        "supports_cdrom_endpoint": False,
        "supports_hdmi_endpoint": False,
        "supports_non_pro_virtual_device_controls": False,
        "supports_watchdog": False,
        "swap_size": None,
        "tailscale_status": None,
        "time_status": None,
        "virtual_device_info": None,
        "watchdog_enabled": None,
        "wifi_status": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _configure_hass(
    hass_mock: MagicMock, config_entry_mock: MagicMock, coordinator: object
) -> None:
    """Store a coordinator under the entry like integration setup does."""
    config_entry_mock.data[CONF_HOST] = "nanokvm.local"
    hass_mock.data = {DOMAIN: {config_entry_mock.entry_id: coordinator}}


def _entity_collector() -> tuple[Callable[[object], None], list[list[object]]]:
    """Return an add-entities callback which eagerly materializes generators."""
    batches: list[list[object]] = []

    def add_entities(entities: object) -> None:
        batches.append(list(entities))

    return add_entities, batches


def _capture_dispatchers(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
) -> dict[str, Callable[..., None]]:
    """Capture dispatcher callbacks registered by a platform setup function."""
    callbacks: dict[str, Callable[..., None]] = {}

    def connect(
        _hass: object, signal: str, callback: Callable[..., None]
    ) -> Callable[[], None]:
        callbacks[signal] = callback
        return MagicMock()

    monkeypatch.setattr(module, "async_dispatcher_connect", connect)
    return callbacks


@pytest.mark.asyncio
async def test_binary_sensor_setup_adds_dynamic_media_and_network_entities_once(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary sensor setup must register initial and one-shot dynamic entities."""
    coordinator = _coordinator()
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    callbacks = _capture_dispatchers(monkeypatch, binary_sensor_module)

    await binary_sensor_module.async_setup_entry(
        hass_mock, config_entry_mock, add_entities
    )

    assert [
        [entity.entity_description.key for entity in batch] for batch in batches
    ] == [["power_led"]]
    media_signal = SIGNAL_NEW_MEDIA_ENTITIES.format(config_entry_mock.entry_id)
    network_signal = SIGNAL_NEW_NETWORK_ENTITIES.format(config_entry_mock.entry_id)
    assert set(callbacks) == {media_signal, network_signal}
    assert config_entry_mock.async_on_unload.call_count == 2

    callbacks[media_signal]()
    callbacks[network_signal]("wireless")
    assert len(batches) == 1

    coordinator.mounted_image = SimpleNamespace(file="/data/image.iso")
    coordinator.supports_cdrom_endpoint = True
    callbacks[media_signal]()
    callbacks[media_signal]()
    assert [entity.entity_description.key for entity in batches[1]] == ["cdrom_mode"]

    coordinator.device_info.ips = [
        SimpleNamespace(addr="192.0.2.10", type="wired", version="IPv4", name="eth0")
    ]
    callbacks[network_signal]("wired")
    callbacks[network_signal]("wired")
    assert [entity.entity_description.key for entity in batches[2]] == [
        "wired_connected"
    ]
    assert len(batches) == 3


@pytest.mark.asyncio
async def test_binary_sensor_setup_handles_media_already_mounted(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mounted media present during setup must create its entity immediately."""
    coordinator = _coordinator(
        mounted_image=SimpleNamespace(file="/data/image.iso"),
        supports_cdrom_endpoint=True,
    )
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    _capture_dispatchers(monkeypatch, binary_sensor_module)

    await binary_sensor_module.async_setup_entry(
        hass_mock, config_entry_mock, add_entities
    )

    assert [entity.entity_description.key for entity in batches[1]] == ["cdrom_mode"]


@pytest.mark.asyncio
async def test_sensor_setup_adds_dynamic_network_media_and_ssh_entities_once(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sensor setup must deduplicate each dynamically signaled entity group."""
    coordinator = _coordinator()
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    callbacks = _capture_dispatchers(monkeypatch, sensor_module)

    await sensor_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == [
        "ip_address",
        "tailscale_state",
    ]
    media_signal = SIGNAL_NEW_MEDIA_ENTITIES.format(config_entry_mock.entry_id)
    network_signal = SIGNAL_NEW_NETWORK_ENTITIES.format(config_entry_mock.entry_id)
    ssh_signal = SIGNAL_NEW_SSH_SENSORS.format(config_entry_mock.entry_id)
    assert set(callbacks) == {media_signal, network_signal, ssh_signal}
    assert config_entry_mock.async_on_unload.call_count == 3

    callbacks[network_signal]("wired")
    callbacks[media_signal]()
    assert len(batches) == 1

    coordinator.device_info.ips = [
        SimpleNamespace(addr="192.0.2.10", type="wired", version="IPv4", name="eth0")
    ]
    callbacks[network_signal]("wired")
    callbacks[network_signal]("wired")
    assert [entity.entity_description.key for entity in batches[1]] == [
        "wired_ip_address"
    ]

    coordinator.mounted_image = SimpleNamespace(file="/data/image.iso")
    callbacks[media_signal]()
    callbacks[media_signal]()
    assert [entity.entity_description.key for entity in batches[2]] == ["mounted_image"]

    callbacks[ssh_signal]()
    callbacks[ssh_signal]()
    assert [entity.entity_description.key for entity in batches[3]] == [
        "uptime",
        "cpu_temperature",
        "memory_used_percent",
        "storage_used_percent",
    ]
    assert coordinator.ssh_sensors_created is True
    assert len(batches) == 4


@pytest.mark.asyncio
async def test_sensor_setup_handles_initial_media_and_ssh_state(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial mounted media and enabled SSH must create dynamic entities at setup."""
    coordinator = _coordinator(
        mounted_image=SimpleNamespace(file="/data/image.iso"),
        ssh_state=SimpleNamespace(enabled=True),
    )
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    _capture_dispatchers(monkeypatch, sensor_module)

    await sensor_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [
        [entity.entity_description.key for entity in batch] for batch in batches[1:]
    ] == [
        ["mounted_image"],
        ["uptime", "cpu_temperature", "memory_used_percent", "storage_used_percent"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hardware_info", "is_pro_hardware", "expected_keys"),
    [
        (None, False, ["power", "reset", "reboot", "reset_hid"]),
        (
            SimpleNamespace(version=HWVersion.PCIE),
            True,
            ["power", "reset", "reboot", "reset_hdmi", "reset_hid", "sync_time"],
        ),
    ],
)
async def test_button_setup_filters_entities_by_hardware(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    hardware_info: object,
    is_pro_hardware: bool,
    expected_keys: list[str],
) -> None:
    """Button setup must expose only actions supported by detected hardware."""
    coordinator = _coordinator(
        hardware_info=hardware_info,
        is_pro_hardware=is_pro_hardware,
    )
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()

    await button_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == expected_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("has_led", [False, True])
async def test_number_setup_filters_on_led_capability(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    has_led: bool,
) -> None:
    """LED numbers must be created only when Pro LED state is available."""
    led_state = (
        SimpleNamespace(on=True, brightness=50, horizontal_count=30, vertical_count=20)
        if has_led
        else None
    )
    coordinator = _coordinator(is_pro_hardware=has_led, led_strip=led_state)
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()

    await number_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == (
        ["led_brightness", "led_horizontal_beads", "led_vertical_beads"]
        if has_led
        else []
    )


@pytest.mark.asyncio
async def test_select_setup_filters_on_available_state(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
) -> None:
    """Select setup must expose each control whose backing state is available."""
    coordinator = _coordinator(
        hid_mode=SimpleNamespace(mode=object()),
        is_pro_hardware=True,
        lcd_time_format=SimpleNamespace(format=object()),
        mouse_jiggler_state=SimpleNamespace(enabled=False),
        oled_info=SimpleNamespace(exist=True, sleep=0),
        swap_size=0,
        virtual_device_info=SimpleNamespace(
            is_emmc_exist=True,
            is_sd_card_exist=True,
            mounted_disk="emmc",
        ),
    )
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()

    await select_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == [
        "hid_mode",
        "mouse_jiggler_mode",
        "oled_sleep_timeout",
        "swap_size",
        "lcd_time_format",
        "virtual_disk_type",
    ]


@pytest.mark.asyncio
async def test_switch_setup_uses_specialized_entities_and_dynamic_watchdog(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch setup must select specialized entity classes and add watchdog once."""
    coordinator = _coordinator(
        supports_non_pro_virtual_device_controls=True,
        virtual_device_info=SimpleNamespace(network=False, disk=False, mic=None),
    )
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    callbacks = _capture_dispatchers(monkeypatch, switch_module)

    await switch_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == [
        "ssh",
        "mdns",
        "virtual_network",
        "virtual_disk",
        "power",
        "hdmi_recording",
    ]
    assert isinstance(batches[0][2], switch_module.NanoKVMVirtualDeviceSwitch)
    assert isinstance(batches[0][3], switch_module.NanoKVMVirtualDeviceSwitch)
    assert isinstance(batches[0][4], switch_module.NanoKVMPowerSwitch)
    assert isinstance(batches[0][5], switch_module.NanoKVMRecordingSwitch)

    signal = SIGNAL_NEW_SSH_SWITCHES.format(config_entry_mock.entry_id)
    assert set(callbacks) == {signal}
    callbacks[signal]()
    assert len(batches) == 1

    coordinator.supports_watchdog = True
    coordinator.watchdog_enabled = False
    callbacks[signal]()
    callbacks[signal]()
    assert [entity.entity_description.key for entity in batches[1]] == ["watchdog"]
    assert isinstance(batches[1][0], switch_module.NanoKVMWatchdogSwitch)
    assert coordinator.ssh_switches_created is True
    assert len(batches) == 2


@pytest.mark.asyncio
async def test_switch_setup_handles_initial_watchdog_state(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already available watchdog must be registered during setup."""
    coordinator = _coordinator(supports_watchdog=True, watchdog_enabled=True)
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()
    _capture_dispatchers(monkeypatch, switch_module)

    await switch_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[1]] == ["watchdog"]


@pytest.mark.asyncio
async def test_update_setup_adds_application_update(
    hass_mock: MagicMock,
    config_entry_mock: MagicMock,
) -> None:
    """Update setup must expose the application firmware updater."""
    coordinator = _coordinator()
    _configure_hass(hass_mock, config_entry_mock, coordinator)
    add_entities, batches = _entity_collector()

    await update_module.async_setup_entry(hass_mock, config_entry_mock, add_entities)

    assert [entity.entity_description.key for entity in batches[0]] == ["application"]
