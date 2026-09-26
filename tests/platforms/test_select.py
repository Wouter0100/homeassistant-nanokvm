"""Tests for NanoKVM select platform helpers and descriptions."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from homeassistant.const import EntityCategory
from nanokvm.models import HidMode, LcdTimeFormat, MouseJigglerMode
import pytest

from custom_components.nanokvm.select import (
    DISK_TYPE_OPTIONS,
    HID_MODE_OPTIONS,
    LCD_TIME_FORMAT_OPTIONS,
    MOUSE_JIGGLER_OPTIONS,
    OLED_SLEEP_OPTIONS,
    SELECTS,
    SWAP_OPTIONS,
    NanoKVMSelectEntityDescription,
    _has_hid_mode,
    _has_lcd_time_format,
    _has_mouse_jiggler_state,
    _has_oled,
    _has_pro_disk_options,
    _has_swap_size,
    _hid_mode_value,
    _lcd_time_format_value,
    _mouse_jiggler_mode_value,
    _oled_sleep_value,
    _pro_disk_options,
    _pro_disk_value,
    _swap_size_value,
)


CoordinatorStateFactory = Callable[..., SimpleNamespace]


@pytest.mark.parametrize(
    ("attribute", "helper"),
    [
        ("hid_mode", _has_hid_mode),
        ("mouse_jiggler_state", _has_mouse_jiggler_state),
        ("swap_size", _has_swap_size),
    ],
)
def test_simple_select_availability_requires_state(
    coordinator_state_factory: CoordinatorStateFactory,
    attribute: str,
    helper: Callable[[SimpleNamespace], bool],
) -> None:
    """Simple select availability follows whether its coordinator state exists."""
    assert helper(coordinator_state_factory(**{attribute: None})) is False
    assert helper(coordinator_state_factory(**{attribute: object()})) is True


@pytest.mark.parametrize(
    ("oled_info", "expected"),
    [
        (None, False),
        (SimpleNamespace(exist=False), False),
        (SimpleNamespace(exist=True), True),
    ],
)
def test_oled_select_availability_requires_present_display(
    coordinator_state_factory: CoordinatorStateFactory,
    oled_info: SimpleNamespace | None,
    expected: bool,
) -> None:
    """OLED settings are available only when the display reports it exists."""
    assert _has_oled(coordinator_state_factory(oled_info=oled_info)) is expected


@pytest.mark.parametrize(
    ("is_pro_hardware", "lcd_time_format", "expected"),
    [
        (False, None, False),
        (False, object(), False),
        (True, None, False),
        (True, object(), True),
    ],
)
def test_lcd_time_format_availability_requires_pro_state(
    coordinator_state_factory: CoordinatorStateFactory,
    is_pro_hardware: bool,
    lcd_time_format: object | None,
    expected: bool,
) -> None:
    """LCD time-format controls require both Pro hardware and fetched state."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=is_pro_hardware,
        lcd_time_format=lcd_time_format,
    )

    assert _has_lcd_time_format(coordinator) is expected


@pytest.mark.parametrize(
    ("virtual_device_info", "expected"),
    [
        (None, []),
        (SimpleNamespace(is_emmc_exist=False, is_sd_card_exist=False), []),
        (SimpleNamespace(is_emmc_exist=True, is_sd_card_exist=False), ["emmc"]),
        (SimpleNamespace(is_emmc_exist=False, is_sd_card_exist=True), ["sdcard"]),
        (
            SimpleNamespace(is_emmc_exist=True, is_sd_card_exist=True),
            ["emmc", "sdcard"],
        ),
    ],
)
def test_pro_disk_options_follow_available_media(
    coordinator_state_factory: CoordinatorStateFactory,
    virtual_device_info: SimpleNamespace | None,
    expected: list[str],
) -> None:
    """Pro disk options preserve the stable eMMC-before-SD-card ordering."""
    coordinator = coordinator_state_factory(virtual_device_info=virtual_device_info)

    assert _pro_disk_options(coordinator) == expected


@pytest.mark.parametrize(
    ("is_pro_hardware", "virtual_device_info", "expected"),
    [
        (False, SimpleNamespace(is_emmc_exist=True, is_sd_card_exist=True), False),
        (True, None, False),
        (True, SimpleNamespace(is_emmc_exist=False, is_sd_card_exist=False), False),
        (True, SimpleNamespace(is_emmc_exist=True, is_sd_card_exist=False), True),
    ],
)
def test_pro_disk_availability_requires_pro_hardware_and_option(
    coordinator_state_factory: CoordinatorStateFactory,
    is_pro_hardware: bool,
    virtual_device_info: SimpleNamespace | None,
    expected: bool,
) -> None:
    """The disk select exists only for a Pro device with usable media."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=is_pro_hardware,
        virtual_device_info=virtual_device_info,
    )

    assert _has_pro_disk_options(coordinator) is expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (None, "normal"),
        (HidMode.NORMAL, "normal"),
        (HidMode.HID_ONLY, "hid_only"),
        (object(), "normal"),
    ],
)
def test_hid_mode_value_normalizes_known_and_unknown_modes(
    coordinator_state_factory: CoordinatorStateFactory,
    mode: object | None,
    expected: str,
) -> None:
    """Missing and unknown HID modes use the current normal-mode fallback."""
    hid_mode = None if mode is None else SimpleNamespace(mode=mode)

    assert _hid_mode_value(coordinator_state_factory(hid_mode=hid_mode)) == expected


@pytest.mark.parametrize(
    ("mouse_jiggler_state", "expected"),
    [
        (None, "disable"),
        (SimpleNamespace(enabled=False, mode=MouseJigglerMode.RELATIVE), "disable"),
        (
            SimpleNamespace(enabled=True, mode=MouseJigglerMode.RELATIVE),
            "relative_mode",
        ),
        (
            SimpleNamespace(enabled=True, mode=MouseJigglerMode.ABSOLUTE),
            "absolute_mode",
        ),
    ],
)
def test_mouse_jiggler_value_maps_enabled_mode(
    coordinator_state_factory: CoordinatorStateFactory,
    mouse_jiggler_state: SimpleNamespace | None,
    expected: str,
) -> None:
    """Disabled state wins; enabled state includes the device mode."""
    coordinator = coordinator_state_factory(mouse_jiggler_state=mouse_jiggler_state)

    assert _mouse_jiggler_mode_value(coordinator) == expected


@pytest.mark.parametrize(
    ("sleep", "expected"),
    [
        (None, "never"),
        (0, "never"),
        (600, "10_min"),
    ],
)
def test_oled_sleep_value_maps_supported_timeouts(
    coordinator_state_factory: CoordinatorStateFactory,
    sleep: int | None,
    expected: str,
) -> None:
    """Supported OLED timeouts must map to declared select option keys."""
    oled_info = None if sleep is None else SimpleNamespace(sleep=sleep)

    assert _oled_sleep_value(coordinator_state_factory(oled_info=oled_info)) == expected


@pytest.mark.parametrize(
    ("swap_size", "expected"),
    [
        (None, "disable"),
        (0, "disable"),
        (256, "256_mb"),
    ],
)
def test_swap_size_value_maps_supported_sizes(
    coordinator_state_factory: CoordinatorStateFactory,
    swap_size: int | None,
    expected: str,
) -> None:
    """Supported swap sizes must map to declared select option keys."""
    assert _swap_size_value(coordinator_state_factory(swap_size=swap_size)) == expected


@pytest.mark.parametrize(
    ("time_format", "expected"),
    [
        (None, None),
        (LcdTimeFormat.TWELVE_HOUR, "12h"),
        (LcdTimeFormat.TWENTY_FOUR_HOUR, "24h"),
    ],
)
def test_lcd_time_format_value_maps_device_format(
    coordinator_state_factory: CoordinatorStateFactory,
    time_format: LcdTimeFormat | None,
    expected: str | None,
) -> None:
    """LCD format values map directly to select option keys."""
    lcd_state = None if time_format is None else SimpleNamespace(format=time_format)

    assert (
        _lcd_time_format_value(coordinator_state_factory(lcd_time_format=lcd_state))
        == expected
    )


@pytest.mark.parametrize(
    ("virtual_device_info", "expected"),
    [
        (None, None),
        (
            SimpleNamespace(
                mounted_disk="emmc",
                is_emmc_exist=False,
                is_sd_card_exist=True,
            ),
            None,
        ),
        (
            SimpleNamespace(
                mounted_disk=None,
                is_emmc_exist=True,
                is_sd_card_exist=True,
            ),
            None,
        ),
        (
            SimpleNamespace(
                mounted_disk="emmc",
                is_emmc_exist=True,
                is_sd_card_exist=True,
            ),
            "emmc",
        ),
        (
            SimpleNamespace(
                mounted_disk="sdcard",
                is_emmc_exist=True,
                is_sd_card_exist=True,
            ),
            "sdcard",
        ),
    ],
)
def test_pro_disk_value_requires_mounted_available_media(
    coordinator_state_factory: CoordinatorStateFactory,
    virtual_device_info: SimpleNamespace | None,
    expected: str | None,
) -> None:
    """Mounted media is a current option only while that medium exists."""
    coordinator = coordinator_state_factory(virtual_device_info=virtual_device_info)

    assert _pro_disk_value(coordinator) == expected


def test_select_description_defaults_are_safe(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """An uncustomized select description has inert value/action defaults."""
    description = NanoKVMSelectEntityDescription(key="test")
    coordinator = coordinator_state_factory()

    assert description.value_fn(coordinator) == ""
    assert description.available_fn(coordinator) is True
    assert description.options_fn is None
    assert description.select_option_fn is None


def test_select_description_keys_and_common_contract() -> None:
    """All select descriptions expose stable keys and actionable metadata."""
    assert [description.key for description in SELECTS] == [
        "hid_mode",
        "mouse_jiggler_mode",
        "oled_sleep_timeout",
        "swap_size",
        "lcd_time_format",
        "virtual_disk_type",
    ]

    for description in SELECTS:
        assert description.translation_key == description.key
        assert description.entity_category is EntityCategory.CONFIG
        assert description.select_option_fn is not None


@pytest.mark.parametrize(
    ("index", "expected_options", "expected_value_fn", "expected_available_fn"),
    [
        (0, list(HID_MODE_OPTIONS), _hid_mode_value, _has_hid_mode),
        (
            1,
            list(MOUSE_JIGGLER_OPTIONS),
            _mouse_jiggler_mode_value,
            _has_mouse_jiggler_state,
        ),
        (2, list(OLED_SLEEP_OPTIONS), _oled_sleep_value, _has_oled),
        (3, list(SWAP_OPTIONS), _swap_size_value, _has_swap_size),
        (
            4,
            list(LCD_TIME_FORMAT_OPTIONS),
            _lcd_time_format_value,
            _has_lcd_time_format,
        ),
        (5, list(DISK_TYPE_OPTIONS), _pro_disk_value, _has_pro_disk_options),
    ],
)
def test_select_description_specific_contracts(
    index: int,
    expected_options: list[str],
    expected_value_fn: Callable[..., object],
    expected_available_fn: Callable[..., bool],
) -> None:
    """Each description is wired to its declared options and state helpers."""
    description = SELECTS[index]

    assert description.options == expected_options
    assert description.value_fn is expected_value_fn
    assert description.available_fn is expected_available_fn
    assert (description.options_fn is _pro_disk_options) is (index == 5)
