"""Tests for NanoKVM number platform helpers and descriptions."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from homeassistant.components.number import NumberMode
from homeassistant.const import EntityCategory, PERCENTAGE
import pytest

from custom_components.nanokvm.const import (
    LED_BEAD_MIN,
    LED_BEAD_TOTAL_LIMIT,
    LED_BRIGHTNESS_MAX,
    LED_BRIGHTNESS_MIN,
)
from custom_components.nanokvm.number import (
    NUMBERS,
    NanoKVMNumberEntityDescription,
    _has_led_strip,
    _led_brightness_value,
    _led_horizontal_max,
    _led_horizontal_value,
    _led_vertical_max,
    _led_vertical_value,
)


CoordinatorStateFactory = Callable[..., SimpleNamespace]


def _led_state() -> SimpleNamespace:
    """Return representative LED strip state."""
    return SimpleNamespace(
        brightness=75,
        horizontal_count=30,
        vertical_count=20,
    )


@pytest.mark.parametrize(
    ("is_pro_hardware", "led_strip", "expected"),
    [
        (False, None, False),
        (False, _led_state(), False),
        (True, None, False),
        (True, _led_state(), True),
    ],
)
def test_led_strip_availability_requires_pro_hardware_and_state(
    coordinator_state_factory: CoordinatorStateFactory,
    is_pro_hardware: bool,
    led_strip: SimpleNamespace | None,
    expected: bool,
) -> None:
    """LED numbers are available only when Pro state has been fetched."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=is_pro_hardware,
        led_strip=led_strip,
    )

    assert _has_led_strip(coordinator) is expected


def test_led_number_values_are_read_from_coordinator_state(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Each value helper exposes its corresponding LED field."""
    coordinator = coordinator_state_factory(led_strip=_led_state())

    assert _led_brightness_value(coordinator) == 75
    assert _led_horizontal_value(coordinator) == 30
    assert _led_vertical_value(coordinator) == 20


def test_led_number_values_are_none_without_state(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Missing LED state must not be represented as a numeric value."""
    coordinator = coordinator_state_factory(led_strip=None)

    assert _led_brightness_value(coordinator) is None
    assert _led_horizontal_value(coordinator) is None
    assert _led_vertical_value(coordinator) is None


def test_dynamic_axis_maxima_use_the_other_axis(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Each bead-count maximum reserves capacity for the other axis."""
    coordinator = coordinator_state_factory(led_strip=_led_state())

    assert _led_horizontal_max(coordinator) == 110
    assert _led_vertical_max(coordinator) == 60


def test_dynamic_axis_maxima_use_minimum_axis_without_state(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Missing state uses one bead on the opposite axis as the safe baseline."""
    coordinator = coordinator_state_factory(led_strip=None)

    assert _led_horizontal_max(coordinator) == 148
    assert _led_vertical_max(coordinator) == 74


def test_number_description_defaults_are_safe(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """An uncustomized description has inert value/action defaults."""
    description = NanoKVMNumberEntityDescription(key="test")
    coordinator = coordinator_state_factory()

    assert description.value_fn(coordinator) is None
    assert description.available_fn(coordinator) is True
    assert description.min_value_fn(coordinator) == LED_BEAD_MIN
    assert description.max_value_fn(coordinator) == LED_BEAD_TOTAL_LIMIT
    assert description.set_value_fn is None


def test_number_description_keys_and_common_contract() -> None:
    """All LED number descriptions have stable registry and action metadata."""
    assert [description.key for description in NUMBERS] == [
        "led_brightness",
        "led_horizontal_beads",
        "led_vertical_beads",
    ]

    for description in NUMBERS:
        assert description.translation_key == description.key
        assert description.entity_category is EntityCategory.CONFIG
        assert description.native_step == 1
        assert description.available_fn is _has_led_strip
        assert description.set_value_fn is not None


def test_brightness_description_contract() -> None:
    """Brightness is a percentage slider across the full hardware range."""
    description = NUMBERS[0]

    assert description.native_min_value == LED_BRIGHTNESS_MIN
    assert description.native_max_value == LED_BRIGHTNESS_MAX
    assert description.native_unit_of_measurement == PERCENTAGE
    assert description.mode is NumberMode.SLIDER
    assert description.value_fn is _led_brightness_value
    assert description.min_value_fn(SimpleNamespace()) == LED_BRIGHTNESS_MIN
    assert description.max_value_fn(SimpleNamespace()) == LED_BRIGHTNESS_MAX


def test_horizontal_bead_description_contract() -> None:
    """Horizontal beads use a boxed integer with a dynamic aggregate maximum."""
    description = NUMBERS[1]

    assert description.native_min_value == LED_BEAD_MIN
    assert description.native_max_value == LED_BEAD_TOTAL_LIMIT - 2
    assert description.mode is NumberMode.BOX
    assert description.value_fn is _led_horizontal_value
    assert description.max_value_fn is _led_horizontal_max


def test_vertical_bead_description_contract() -> None:
    """Vertical beads use a boxed integer with a dynamic aggregate maximum."""
    description = NUMBERS[2]

    assert description.native_min_value == LED_BEAD_MIN
    assert description.native_max_value == (LED_BEAD_TOTAL_LIMIT - 1) // 2
    assert description.mode is NumberMode.BOX
    assert description.value_fn is _led_vertical_value
    assert description.max_value_fn is _led_vertical_max
