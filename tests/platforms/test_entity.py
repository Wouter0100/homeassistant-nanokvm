"""Tests for shared NanoKVM entity metadata."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from nanokvm.models import HWVersion
import pytest
from yarl import URL

from custom_components.nanokvm.const import DOMAIN, INTEGRATION_TITLE
from custom_components.nanokvm.entity import NanoKVMEntity


def _coordinator(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    *,
    hostname: str | None = "nano-pro",
    hardware_version: HWVersion | None = HWVersion.PRO,
    application: str = "2.3.4",
    image: str | None = "2026-07-01",
    last_update_success: bool = True,
    url: str = "https://nanokvm.local/api/",
) -> SimpleNamespace:
    """Build the coordinator metadata consumed by NanoKVMEntity."""
    device_values: dict[str, object] = {
        "device_key": "device-key",
        "application": application,
    }
    if image is not None:
        device_values["image"] = image

    return coordinator_state_factory(
        device_info=SimpleNamespace(**device_values),
        hostname_info=(
            SimpleNamespace(hostname=hostname) if hostname is not None else None
        ),
        hardware_info=(
            SimpleNamespace(version=hardware_version)
            if hardware_version is not None
            else None
        ),
        client=SimpleNamespace(url=URL(url)),
        last_update_success=last_update_success,
    )


def test_entity_initialization_sets_name_and_stable_unique_id(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Base entities must namespace their IDs by device and platform suffix."""
    entity = NanoKVMEntity(
        _coordinator(coordinator_state_factory),
        unique_id_suffix="sensor_temperature",
        name="Temperature",
    )

    assert entity.unique_id == "device-key_sensor_temperature"
    assert entity.name == "Temperature"
    assert entity.has_entity_name is True
    assert entity.should_poll is False


@pytest.mark.parametrize(
    ("last_update_success", "expected"),
    [(True, True), (False, False)],
)
def test_entity_availability_follows_coordinator_health(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    last_update_success: bool,
    expected: bool,
) -> None:
    """All coordinator-backed entities must inherit coordinator availability."""
    entity = NanoKVMEntity(
        _coordinator(
            coordinator_state_factory,
            last_update_success=last_update_success,
        ),
        unique_id_suffix="sensor_status",
    )

    assert entity.available is expected


def test_device_info_exposes_complete_hardware_and_software_metadata(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Reported device metadata must map to Home Assistant's device registry."""
    entity = NanoKVMEntity(
        _coordinator(
            coordinator_state_factory,
            url="https://nanokvm.local/custom/api/",
        ),
        unique_id_suffix="sensor_status",
    )

    assert entity.device_info == {
        "identifiers": {(DOMAIN, "device-key")},
        "name": "nano-pro",
        "manufacturer": "Sipeed",
        "model": f"{INTEGRATION_TITLE} Pro",
        "sw_version": "2.3.4 (Image: 2026-07-01)",
        "hw_version": "Pro",
        "configuration_url": "https://nanokvm.local/custom/",
    }


def test_device_info_uses_fallbacks_for_unreported_optional_metadata(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Missing hostname, hardware, and image data must have stable fallbacks."""
    entity = NanoKVMEntity(
        _coordinator(
            coordinator_state_factory,
            hostname=None,
            hardware_version=None,
            application="1.0.0",
            image=None,
            url="http://192.0.2.10/api/",
        ),
        unique_id_suffix="sensor_status",
    )

    assert entity.device_info == {
        "identifiers": {(DOMAIN, "device-key")},
        "name": INTEGRATION_TITLE,
        "manufacturer": "Sipeed",
        "model": f"{INTEGRATION_TITLE} Unknown",
        "sw_version": "1.0.0",
        "hw_version": "Unknown",
        "configuration_url": "http://192.0.2.10/",
    }
