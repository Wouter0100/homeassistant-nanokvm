"""Tests for NanoKVM switch helpers, descriptions, and entity properties."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import EntityCategory
from nanokvm.models import VirtualDevice
import pytest

from custom_components.nanokvm.switch import (
    SSH_SWITCHES,
    SWITCHES,
    NanoKVMRecordingSwitch,
    NanoKVMSwitch,
    NanoKVMSwitchEntityDescription,
    _hdmi_available,
    _hdmi_capture_available,
    _hdmi_passthrough_available,
    _hdmi_value,
    _led_strip_available,
    _led_strip_value,
    _low_power_available,
    _non_pro_virtual_device_available,
    _pro_virtual_device_available,
    _pro_virtual_mic_available,
    _watchdog_available,
    _watchdog_value,
)


def _description_identity(
    description: NanoKVMSwitchEntityDescription,
) -> tuple[str, VirtualDevice | None]:
    """Return the key that distinguishes hardware-specific description variants."""
    return description.key, description.virtual_device


def test_switch_descriptions_expose_stable_inventory() -> None:
    """Switch descriptions must retain their hardware-specific inventory."""
    assert [_description_identity(description) for description in SWITCHES] == [
        ("ssh", None),
        ("mdns", None),
        ("virtual_network", VirtualDevice.NETWORK),
        ("virtual_disk", VirtualDevice.DISK),
        ("virtual_network", VirtualDevice.NETWORK),
        ("virtual_mic", VirtualDevice.MIC),
        ("power", None),
        ("hdmi", None),
        ("hdmi_capture", None),
        ("hdmi_passthrough", None),
        ("low_power", None),
        ("led_strip", None),
    ]
    assert [description.key for description in SSH_SWITCHES] == ["watchdog"]

    assert all(
        description.translation_key == description.key for description in SWITCHES
    )
    assert all(description.icon for description in (*SWITCHES, *SSH_SWITCHES))
    assert (
        next(item for item in SWITCHES if item.key == "power").entity_category is None
    )
    assert all(
        description.entity_category == EntityCategory.CONFIG
        for description in (*SWITCHES, *SSH_SWITCHES)
        if description.key != "power"
    )


def test_switch_description_handlers_match_entity_strategy() -> None:
    """Descriptions must declare either API handlers or a virtual-device strategy."""
    for description in SWITCHES:
        if description.virtual_device is None:
            assert description.turn_on_fn is not None
            assert description.turn_off_fn is not None
        else:
            assert description.turn_on_fn is None
            assert description.turn_off_fn is None

    watchdog = SSH_SWITCHES[0]
    assert watchdog.turn_on_fn is None
    assert watchdog.turn_off_fn is None


@pytest.mark.parametrize(
    ("hdmi_state", "expected"),
    [
        (None, False),
        (SimpleNamespace(enabled=False), False),
        (SimpleNamespace(enabled=True), True),
    ],
)
def test_hdmi_value_handles_missing_and_reported_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    hdmi_state: SimpleNamespace | None,
    expected: bool,
) -> None:
    """HDMI state must be false until the endpoint reports it enabled."""
    assert _hdmi_value(coordinator_state_factory(hdmi_state=hdmi_state)) is expected


@pytest.mark.parametrize(
    ("supports_endpoint", "hdmi_state", "expected"),
    [
        (False, None, False),
        (False, SimpleNamespace(enabled=True), False),
        (True, None, False),
        (True, SimpleNamespace(enabled=False), True),
    ],
)
def test_hdmi_availability_requires_capability_and_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    supports_endpoint: bool,
    hdmi_state: SimpleNamespace | None,
    expected: bool,
) -> None:
    """HDMI availability must distinguish a disabled output from a missing endpoint."""
    coordinator = coordinator_state_factory(
        supports_hdmi_endpoint=supports_endpoint,
        hdmi_state=hdmi_state,
    )

    assert _hdmi_available(coordinator) is expected


@pytest.mark.parametrize(
    ("watchdog_enabled", "expected"),
    [(None, False), (False, False), (True, True)],
)
def test_watchdog_value_normalizes_optional_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    watchdog_enabled: bool | None,
    expected: bool,
) -> None:
    """Unknown watchdog state must not be presented as enabled."""
    assert (
        _watchdog_value(coordinator_state_factory(watchdog_enabled=watchdog_enabled))
        is expected
    )


@pytest.mark.parametrize(
    ("supports_watchdog", "watchdog_enabled", "expected"),
    [
        (False, None, False),
        (False, False, False),
        (True, None, False),
        (True, False, True),
    ],
)
def test_watchdog_availability_requires_support_and_known_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    supports_watchdog: bool,
    watchdog_enabled: bool | None,
    expected: bool,
) -> None:
    """A known false watchdog value must remain available when supported."""
    coordinator = coordinator_state_factory(
        supports_watchdog=supports_watchdog,
        watchdog_enabled=watchdog_enabled,
    )

    assert _watchdog_available(coordinator) is expected


def test_virtual_device_availability_separates_hardware_families(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Legacy and Pro virtual-device descriptions must be mutually exclusive."""
    non_pro = coordinator_state_factory(
        supports_non_pro_virtual_device_controls=True,
        is_pro_hardware=False,
        virtual_device_info=SimpleNamespace(mic=None),
    )
    pro = coordinator_state_factory(
        supports_non_pro_virtual_device_controls=False,
        is_pro_hardware=True,
        virtual_device_info=SimpleNamespace(mic=False),
    )

    assert _non_pro_virtual_device_available(non_pro)
    assert not _pro_virtual_device_available(non_pro)
    assert not _pro_virtual_mic_available(non_pro)

    assert not _non_pro_virtual_device_available(pro)
    assert _pro_virtual_device_available(pro)
    assert _pro_virtual_mic_available(pro)


def test_pro_virtual_mic_requires_reported_mic_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """A false mic state is known and available, while None is unsupported."""
    assert not _pro_virtual_mic_available(
        coordinator_state_factory(
            is_pro_hardware=True,
            virtual_device_info=SimpleNamespace(mic=None),
        )
    )
    assert not _pro_virtual_mic_available(
        coordinator_state_factory(is_pro_hardware=True, virtual_device_info=None)
    )
    assert _pro_virtual_mic_available(
        coordinator_state_factory(
            is_pro_hardware=True,
            virtual_device_info=SimpleNamespace(mic=False),
        )
    )


@pytest.mark.parametrize(
    ("helper", "state_attribute"),
    [
        (_hdmi_capture_available, "hdmi_capture"),
        (_hdmi_passthrough_available, "hdmi_passthrough"),
        (_low_power_available, "low_power"),
        (_led_strip_available, "led_strip"),
    ],
)
def test_pro_feature_availability_requires_pro_hardware_and_reported_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    helper: Callable[[SimpleNamespace], bool],
    state_attribute: str,
) -> None:
    """Pro feature switches must require both hardware and endpoint state."""
    assert not helper(
        coordinator_state_factory(is_pro_hardware=False, **{state_attribute: object()})
    )
    assert not helper(
        coordinator_state_factory(is_pro_hardware=True, **{state_attribute: None})
    )
    assert helper(
        coordinator_state_factory(is_pro_hardware=True, **{state_attribute: object()})
    )


@pytest.mark.parametrize(
    ("led_strip", "expected"),
    [
        (None, False),
        (SimpleNamespace(on=False), False),
        (SimpleNamespace(on=True), True),
    ],
)
def test_led_strip_value_handles_missing_and_reported_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    led_strip: SimpleNamespace | None,
    expected: bool,
) -> None:
    """LED state must be false until the Pro endpoint reports it enabled."""
    assert _led_strip_value(coordinator_state_factory(led_strip=led_strip)) is expected


def test_all_switch_value_functions_read_their_reported_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Every description value function must expose its corresponding true state."""
    coordinator = coordinator_state_factory(
        ssh_state=SimpleNamespace(enabled=True),
        mdns_state=SimpleNamespace(enabled=True),
        virtual_device_info=SimpleNamespace(network=True, disk=True, mic=True),
        gpio_info=SimpleNamespace(pwr=True),
        hdmi_state=SimpleNamespace(enabled=True),
        hdmi_capture=SimpleNamespace(enabled=True),
        hdmi_passthrough=SimpleNamespace(enabled=True),
        low_power=SimpleNamespace(enabled=True),
        led_strip=SimpleNamespace(on=True),
        watchdog_enabled=True,
    )

    assert all(
        description.value_fn(coordinator) for description in (*SWITCHES, *SSH_SWITCHES)
    )


def test_all_switch_value_functions_handle_missing_state(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Every description value function must normalize missing state to false."""
    coordinator = coordinator_state_factory(
        ssh_state=None,
        mdns_state=None,
        virtual_device_info=None,
        gpio_info=None,
        hdmi_state=None,
        hdmi_capture=None,
        hdmi_passthrough=None,
        low_power=None,
        led_strip=None,
        watchdog_enabled=None,
    )

    assert not any(
        description.value_fn(coordinator) for description in (*SWITCHES, *SSH_SWITCHES)
    )


@pytest.mark.parametrize(
    ("last_update_success", "feature_available", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_switch_entity_delegates_state_and_combines_availability(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    last_update_success: bool,
    feature_available: bool,
    expected: bool,
) -> None:
    """Switch availability must combine coordinator health and feature support."""
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(device_key="device-key"),
        last_update_success=last_update_success,
        reported_on=True,
        feature_available=feature_available,
    )
    description = NanoKVMSwitchEntityDescription(
        key="sample",
        value_fn=lambda state: state.reported_on,
        available_fn=lambda state: state.feature_available,
    )

    entity = NanoKVMSwitch(coordinator, description)

    assert entity.is_on is True
    assert entity.available is expected
    assert entity.unique_id == "device-key_switch_sample"


@pytest.mark.asyncio
async def test_recording_switch_controls_and_observes_shared_media_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording switch is a view over the entry-scoped controller."""
    listener_remove = MagicMock()
    recording = SimpleNamespace(
        is_recording=False,
        current_filename=None,
        async_add_state_listener=MagicMock(return_value=listener_remove),
    )
    media = SimpleNamespace(
        recording=recording,
        async_start_automatic=AsyncMock(
            return_value="/media/nanokvm/device-key/20260719000300.mp4"
        ),
        async_stop_automatic=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        device_info=SimpleNamespace(device_key="device-key"),
        last_update_success=True,
        media=media,
    )
    entity = NanoKVMRecordingSwitch(coordinator)
    write_state = MagicMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write_state)

    assert entity.unique_id == "device-key_switch_hdmi_recording"
    assert entity.is_on is False
    assert entity.extra_state_attributes["maximum_duration_minutes"] == 30

    await entity.async_turn_on()
    media.async_start_automatic.assert_awaited_once_with()

    recording.is_recording = True
    recording.current_filename = "/media/nanokvm/device-key/20260719000300.mp4"
    listener = recording.async_add_state_listener.call_args.args[0]
    listener(True)
    assert entity.is_on is True
    assert entity.extra_state_attributes["filename"] == recording.current_filename
    write_state.assert_called_once_with()

    await entity.async_turn_off()
    media.async_stop_automatic.assert_awaited_once_with()

    monkeypatch.setattr(
        NanoKVMSwitch,
        "async_will_remove_from_hass",
        AsyncMock(),
    )
    await entity.async_will_remove_from_hass()
    listener_remove.assert_called_once_with()


def test_recording_switch_requires_entry_scoped_media_runtime() -> None:
    """The dedicated switch cannot create a private recorder as a fallback."""
    coordinator = SimpleNamespace(
        device_info=SimpleNamespace(device_key="device-key"),
        last_update_success=True,
        media=None,
    )

    with pytest.raises(RuntimeError, match="media runtime is not initialized"):
        NanoKVMRecordingSwitch(coordinator)
