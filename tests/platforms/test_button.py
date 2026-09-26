"""Tests for NanoKVM button descriptions and availability helpers."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from homeassistant.const import EntityCategory
from nanokvm.models import HWVersion

from custom_components.nanokvm.button import (
    BUTTONS,
    NanoKVMButton,
    _is_pcie_hardware,
    _is_pro_hardware,
)
from custom_components.nanokvm.const import (
    ICON_CLOCK,
    ICON_HID,
    ICON_KVM,
    ICON_POWER,
    ICON_RESET,
)


def test_button_descriptions_expose_stable_metadata() -> None:
    """Button descriptions must retain their UI-facing metadata contract."""
    assert [
        (
            description.key,
            description.name,
            description.translation_key,
            description.icon,
            description.entity_category,
        )
        for description in BUTTONS
    ] == [
        ("power", "Power Button", "power", ICON_POWER, None),
        ("reset", "Reset Button", "reset", ICON_RESET, None),
        ("reboot", "Reboot System", "reboot", ICON_RESET, None),
        (
            "reset_hdmi",
            "Reset HDMI",
            "reset_hdmi",
            ICON_KVM,
            EntityCategory.CONFIG,
        ),
        (
            "reset_hid",
            "Reset HID",
            "reset_hid",
            ICON_HID,
            EntityCategory.CONFIG,
        ),
        (
            "sync_time",
            "Sync Time",
            "sync_time",
            ICON_CLOCK,
            EntityCategory.CONFIG,
        ),
    ]
    assert all(description.press_fn is not None for description in BUTTONS)


def test_pcie_button_availability_requires_detected_pcie_hardware(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """The HDMI reset button must only apply to detected PCIe hardware."""
    assert _is_pcie_hardware(
        coordinator_state_factory(hardware_info=SimpleNamespace(version=HWVersion.PCIE))
    )
    assert not _is_pcie_hardware(
        coordinator_state_factory(hardware_info=SimpleNamespace(version=HWVersion.PRO))
    )
    assert not _is_pcie_hardware(coordinator_state_factory(hardware_info=None))


def test_pro_button_availability_delegates_to_coordinator_capability(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """The sync-time button must use the coordinator's Pro capability flag."""
    assert _is_pro_hardware(coordinator_state_factory(is_pro_hardware=True))
    assert not _is_pro_hardware(coordinator_state_factory(is_pro_hardware=False))


def test_button_description_availability_matches_each_hardware_family(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Hardware-specific descriptions must not hide generic buttons."""
    states = {
        "non_pcie": coordinator_state_factory(
            hardware_info=SimpleNamespace(version=HWVersion.BETA),
            is_pro_hardware=False,
        ),
        "pcie": coordinator_state_factory(
            hardware_info=SimpleNamespace(version=HWVersion.PCIE),
            is_pro_hardware=False,
        ),
        "pro": coordinator_state_factory(
            hardware_info=SimpleNamespace(version=HWVersion.PRO),
            is_pro_hardware=True,
        ),
    }

    assert {
        name: {
            description.key
            for description in BUTTONS
            if description.available_fn(state)
        }
        for name, state in states.items()
    } == {
        "non_pcie": {"power", "reset", "reboot", "reset_hid"},
        "pcie": {"power", "reset", "reboot", "reset_hdmi", "reset_hid"},
        "pro": {"power", "reset", "reboot", "reset_hid", "sync_time"},
    }


def test_button_entity_uses_description_key_in_unique_id(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Button entities must derive a stable platform-qualified unique ID."""
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(device_key="device-key")
    )
    description = next(item for item in BUTTONS if item.key == "power")

    entity = NanoKVMButton(coordinator, description)

    assert entity.entity_description is description
    assert entity.unique_id == "device-key_button_power"
