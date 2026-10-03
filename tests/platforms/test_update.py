"""Tests for NanoKVM update metadata and synchronous entity properties."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
import pytest

from custom_components.nanokvm.update import NanoKVMUpdate, _normalize_version


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  2.3.4  ", "2.3.4"),
    ],
)
def test_normalize_version_strips_values_and_rejects_empty_strings(
    version: str | None,
    expected: str | None,
) -> None:
    """Version fields must expose clean non-empty values to Home Assistant."""
    assert _normalize_version(version) == expected


def test_update_description_exposes_stable_metadata() -> None:
    """The application updater must retain its entity-registry metadata."""
    description = NanoKVMUpdate.entity_description

    assert description.key == "application"
    assert description.name == "Application"
    assert description.translation_key == "application"
    assert description.icon == "mdi:update"
    assert description.device_class == UpdateDeviceClass.FIRMWARE


def _update_entity(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    *,
    application: str | None = "1.0.0",
    current: str | None = "1.0.0",
    latest: str | None = "1.1.0",
    version_info_available: bool = True,
    last_update_success: bool = True,
) -> NanoKVMUpdate:
    """Build an update entity from simple coordinator state."""
    version_info = (
        SimpleNamespace(current=current, latest=latest)
        if version_info_available
        else None
    )
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(
            device_key="device-key",
            application=application,
        ),
        application_version_info=version_info,
        last_update_success=last_update_success,
    )
    return NanoKVMUpdate(coordinator)


def test_update_entity_exposes_platform_metadata(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Update entities must advertise installation and a stable unique ID."""
    entity = _update_entity(coordinator_state_factory)

    assert entity.unique_id == "device-key_update_application"
    assert entity.supported_features == UpdateEntityFeature.INSTALL


@pytest.mark.parametrize(
    ("current", "application", "expected"),
    [
        (" 2.0.0 ", "1.0.0", "2.0.0"),
        ("", " 1.0.0 ", "1.0.0"),
        (None, "", None),
    ],
)
def test_installed_version_prefers_api_current_then_device_application(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    current: str | None,
    application: str | None,
    expected: str | None,
) -> None:
    """Installed version must fall back when version metadata is empty."""
    entity = _update_entity(
        coordinator_state_factory,
        current=current,
        application=application,
    )

    assert entity.installed_version == expected


def test_installed_version_uses_device_application_without_version_info(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Device application data must remain useful while the optional fetch is absent."""
    entity = _update_entity(
        coordinator_state_factory,
        application=" 1.2.3 ",
        version_info_available=False,
    )

    assert entity.installed_version == "1.2.3"


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        (" 2.1.0 ", "2.0.0", "2.1.0"),
        ("", " 2.0.0 ", "2.0.0"),
        (None, "", None),
    ],
)
def test_latest_version_prefers_latest_then_installed_version(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    latest: str | None,
    current: str | None,
    expected: str | None,
) -> None:
    """Latest version must fall back to installed when update data is empty."""
    entity = _update_entity(
        coordinator_state_factory,
        latest=latest,
        current=current,
        application="",
    )

    assert entity.latest_version == expected


def test_latest_version_uses_installed_fallback_without_version_info(
    coordinator_state_factory: Callable[..., SimpleNamespace],
) -> None:
    """Missing optional version data must fall back to the device application."""
    entity = _update_entity(
        coordinator_state_factory,
        application=" 1.2.3 ",
        version_info_available=False,
    )

    assert entity.latest_version == "1.2.3"


@pytest.mark.parametrize(
    (
        "last_update_success",
        "version_info_available",
        "expected",
    ),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
    ],
)
def test_update_availability_requires_health_feature_and_version_info(
    coordinator_state_factory: Callable[..., SimpleNamespace],
    last_update_success: bool,
    version_info_available: bool,
    expected: bool,
) -> None:
    """The updater must only be available with healthy fetched version metadata."""
    entity = _update_entity(
        coordinator_state_factory,
        last_update_success=last_update_success,
        version_info_available=version_info_available,
    )

    assert entity.available is expected
