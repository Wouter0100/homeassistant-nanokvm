"""Tests for NanoKVM Pro LED strip configuration helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from nanokvm.models import GetLedStripRsp
import pytest

from custom_components.nanokvm.const import LED_BEAD_TOTAL_LIMIT
from custom_components.nanokvm.led import (
    LedStripConfig,
    async_set_led_strip,
    build_led_strip_config,
    max_horizontal_count,
    max_vertical_count,
    note_reported_led_strip,
    validate_led_strip_config,
)


def _device_state() -> GetLedStripRsp:
    """Return a complete library response matching a valid LED configuration."""
    return GetLedStripRsp(on=True, hor=30, ver=20, brightness=75)


def test_led_axis_limits_share_the_total_bead_constraint() -> None:
    """Axis-specific maximum helpers must enforce the aggregate strip limit."""
    assert max_horizontal_count(20) == 110
    assert max_vertical_count(30) == 60
    assert max_horizontal_count(0) == LED_BEAD_TOTAL_LIMIT


@pytest.mark.parametrize("brightness", [0, 100])
def test_led_config_accepts_brightness_boundaries(brightness: int) -> None:
    """Both documented brightness boundaries must be valid."""
    validate_led_strip_config(
        LedStripConfig(
            on=True,
            brightness=brightness,
            horizontal_count=1,
            vertical_count=1,
        )
    )


@pytest.mark.parametrize("brightness", [-1, 101])
def test_led_config_rejects_out_of_range_brightness(brightness: int) -> None:
    """Brightness outside the hardware range must be rejected."""
    with pytest.raises(ValueError, match="LED brightness must be between 0 and 100"):
        validate_led_strip_config(
            LedStripConfig(
                on=True,
                brightness=brightness,
                horizontal_count=1,
                vertical_count=1,
            )
        )


def test_led_config_rejects_missing_horizontal_bead() -> None:
    """At least one horizontal bead is required."""
    with pytest.raises(ValueError, match="Horizontal LED beads must be at least 1"):
        validate_led_strip_config(
            LedStripConfig(
                on=True,
                brightness=50,
                horizontal_count=0,
                vertical_count=1,
            )
        )


def test_led_config_rejects_missing_vertical_bead() -> None:
    """At least one vertical bead is required."""
    with pytest.raises(ValueError, match="Vertical LED beads must be at least 1"):
        validate_led_strip_config(
            LedStripConfig(
                on=True,
                brightness=50,
                horizontal_count=1,
                vertical_count=0,
            )
        )


def test_led_config_rejects_aggregate_limit_overflow() -> None:
    """The full strip must fit within the device's aggregate bead limit."""
    with pytest.raises(
        ValueError, match=r"horizontal_count \+ \(2 \* vertical_count\)"
    ):
        validate_led_strip_config(
            LedStripConfig(
                on=True,
                brightness=50,
                horizontal_count=101,
                vertical_count=25,
            )
        )


def test_build_led_config_requires_current_device_state() -> None:
    """Partial updates cannot be constructed without a complete device baseline."""
    with pytest.raises(ValueError, match="LED strip state is unavailable"):
        build_led_strip_config(None, brightness=50)


def test_build_led_config_preserves_unspecified_device_values() -> None:
    """A partial update must copy every field that was not explicitly changed."""
    assert build_led_strip_config(_device_state(), brightness=25) == LedStripConfig(
        on=True,
        brightness=25,
        horizontal_count=30,
        vertical_count=20,
    )


def test_build_led_config_without_overrides_copies_current_state() -> None:
    """A no-op update must still produce the complete validated value object."""
    assert build_led_strip_config(_device_state()) == LedStripConfig(
        on=True,
        brightness=75,
        horizontal_count=30,
        vertical_count=20,
    )


def test_build_led_config_applies_complete_valid_update() -> None:
    """Explicit updates must override all device values and remain validated."""
    assert build_led_strip_config(
        _device_state(),
        on=False,
        brightness=0,
        horizontal_count=50,
        vertical_count=50,
    ) == LedStripConfig(
        on=False,
        brightness=0,
        horizontal_count=50,
        vertical_count=50,
    )


def test_build_led_config_validates_the_merged_result() -> None:
    """Invalid partial updates must be rejected after merging with device state."""
    with pytest.raises(ValueError, match="LED brightness"):
        build_led_strip_config(_device_state(), brightness=101)


def _led_coordinator(brightness: int = 68) -> SimpleNamespace:
    """Return a coordinator-shaped object with a reported LED strip state."""
    return SimpleNamespace(
        led_strip=GetLedStripRsp(on=False, hor=114, ver=18, brightness=brightness),
        client=SimpleNamespace(set_led_strip=AsyncMock()),
    )


def _sent_brightness(coordinator: SimpleNamespace) -> int:
    """Return the brightness of the most recent device write."""
    return coordinator.client.set_led_strip.await_args.kwargs["brightness"]


async def test_led_toggle_keeps_requested_brightness_when_device_reports_lower() -> None:
    """A lower reported brightness is not written back by an unrelated change."""
    coordinator = _led_coordinator(brightness=68)

    await async_set_led_strip(coordinator, on=True)
    assert _sent_brightness(coordinator) == 68

    # The device now reports the brightness it applies instead of the stored one.
    coordinator.led_strip = GetLedStripRsp(on=True, hor=114, ver=18, brightness=57)
    note_reported_led_strip(coordinator, coordinator.led_strip)
    await async_set_led_strip(coordinator, on=False)

    assert _sent_brightness(coordinator) == 68
    coordinator.client.set_led_strip.assert_awaited_with(
        on=False, brightness=68, horizontal_count=114, vertical_count=18
    )


async def test_led_write_before_the_next_poll_reuses_the_pending_request() -> None:
    """Two quick writes keep the brightness of the first one."""
    coordinator = _led_coordinator(brightness=68)

    await async_set_led_strip(coordinator, brightness=40)
    await async_set_led_strip(coordinator, on=True)

    assert _sent_brightness(coordinator) == 40


async def test_led_toggle_follows_brightness_changed_outside_home_assistant() -> None:
    """A brightness changed on the device itself replaces the remembered request."""
    coordinator = _led_coordinator(brightness=68)
    await async_set_led_strip(coordinator, on=True)
    note_reported_led_strip(coordinator, coordinator.led_strip)
    note_reported_led_strip(coordinator, None)

    coordinator.led_strip = GetLedStripRsp(on=True, hor=114, ver=18, brightness=20)
    note_reported_led_strip(coordinator, coordinator.led_strip)
    await async_set_led_strip(coordinator, on=False)

    assert _sent_brightness(coordinator) == 20


async def test_led_write_failure_keeps_the_previous_request() -> None:
    """A rejected or invalid write does not replace the remembered brightness."""
    coordinator = _led_coordinator(brightness=68)
    await async_set_led_strip(coordinator, brightness=40)
    coordinator.client.set_led_strip.side_effect = RuntimeError("device busy")

    with pytest.raises(RuntimeError):
        await async_set_led_strip(coordinator, brightness=90)
    with pytest.raises(ValueError):
        await async_set_led_strip(coordinator, brightness=500)

    assert coordinator.led_brightness_request.requested == 40
