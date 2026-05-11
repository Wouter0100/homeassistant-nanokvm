"""NanoKVM Pro LED strip helpers."""
from __future__ import annotations

from dataclasses import dataclass

from nanokvm.models import GetLedStripRsp

from .const import (
    LED_BEAD_MIN,
    LED_BEAD_TOTAL_LIMIT,
    LED_BRIGHTNESS_MAX,
    LED_BRIGHTNESS_MIN,
)


@dataclass(frozen=True, slots=True)
class LedStripConfig:
    """Complete NanoKVM Pro LED strip configuration."""

    on: bool
    brightness: int
    horizontal_count: int
    vertical_count: int


def max_horizontal_count(vertical_count: int) -> int:
    """Return maximum horizontal bead count for a vertical bead count."""
    return LED_BEAD_TOTAL_LIMIT - (2 * vertical_count)


def max_vertical_count(horizontal_count: int) -> int:
    """Return maximum vertical bead count for a horizontal bead count."""
    return (LED_BEAD_TOTAL_LIMIT - horizontal_count) // 2


def validate_led_strip_config(config: LedStripConfig) -> None:
    """Validate NanoKVM Pro LED strip values."""
    if not LED_BRIGHTNESS_MIN <= config.brightness <= LED_BRIGHTNESS_MAX:
        raise ValueError(
            f"LED brightness must be between {LED_BRIGHTNESS_MIN} and "
            f"{LED_BRIGHTNESS_MAX}"
        )

    if config.horizontal_count < LED_BEAD_MIN:
        raise ValueError(f"Horizontal LED beads must be at least {LED_BEAD_MIN}")

    if config.vertical_count < LED_BEAD_MIN:
        raise ValueError(f"Vertical LED beads must be at least {LED_BEAD_MIN}")

    if config.horizontal_count + (2 * config.vertical_count) > LED_BEAD_TOTAL_LIMIT:
        raise ValueError(
            "LED bead counts must satisfy horizontal_count + "
            f"(2 * vertical_count) <= {LED_BEAD_TOTAL_LIMIT}"
        )


def build_led_strip_config(
    current: GetLedStripRsp | None,
    *,
    on: bool | None = None,
    brightness: int | None = None,
    horizontal_count: int | None = None,
    vertical_count: int | None = None,
) -> LedStripConfig:
    """Build and validate a complete LED strip config from partial updates."""
    if current is None:
        raise ValueError("LED strip state is unavailable")

    config = LedStripConfig(
        on=current.on if on is None else on,
        brightness=current.brightness if brightness is None else brightness,
        horizontal_count=(
            current.horizontal_count
            if horizontal_count is None
            else horizontal_count
        ),
        vertical_count=(
            current.vertical_count if vertical_count is None else vertical_count
        ),
    )
    validate_led_strip_config(config)
    return config
