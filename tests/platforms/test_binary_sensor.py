"""Tests for NanoKVM binary-sensor helpers and entity descriptions."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from nanokvm.models import HWVersion, IPInfo

from custom_components.nanokvm.binary_sensor import (
    BINARY_SENSORS,
    MEDIA_BINARY_SENSORS,
    _cdrom_supported,
    _has_connection_type,
    _has_mounted_image,
    _is_alpha_hardware,
    _static_ip_available,
    _time_status_available,
    _wifi_supported,
    _wired_active,
)

CoordinatorStateFactory = Callable[..., SimpleNamespace]


def _description(key: str):
    """Return a binary-sensor description by key."""
    return next(
        description
        for description in (*BINARY_SENSORS, *MEDIA_BINARY_SENSORS)
        if description.key == key
    )


def _address(
    address: str,
    *,
    connection_type: str = "wired",
) -> IPInfo:
    """Build a NanoKVM address response."""
    return IPInfo(
        name="eth0",
        addr=address,
        version="IPv4",
        type=connection_type,
    )


@pytest.mark.parametrize(
    ("hardware_info", "expected"),
    [
        (None, False),
        (SimpleNamespace(version=HWVersion.ALPHA), True),
        (SimpleNamespace(version=HWVersion.PRO), False),
    ],
)
def test_is_alpha_hardware(
    coordinator_state_factory: CoordinatorStateFactory,
    hardware_info: SimpleNamespace | None,
    expected: bool,
) -> None:
    """Only Alpha hardware satisfies the Alpha capability predicate."""
    coordinator = coordinator_state_factory(hardware_info=hardware_info)

    assert _is_alpha_hardware(coordinator) is expected


@pytest.mark.parametrize(
    ("wifi_status", "expected"),
    [
        (None, False),
        (SimpleNamespace(supported=False), False),
        (SimpleNamespace(supported=True), True),
    ],
)
def test_wifi_supported(
    coordinator_state_factory: CoordinatorStateFactory,
    wifi_status: SimpleNamespace | None,
    expected: bool,
) -> None:
    """Wi-Fi entity creation follows the reported support flag."""
    coordinator = coordinator_state_factory(wifi_status=wifi_status)

    assert _wifi_supported(coordinator) is expected


@pytest.mark.parametrize(
    ("mounted_image", "expected"),
    [
        (None, False),
        (SimpleNamespace(file=""), False),
        (SimpleNamespace(file="images/rescue.iso"), True),
    ],
)
def test_has_mounted_image(
    coordinator_state_factory: CoordinatorStateFactory,
    mounted_image: SimpleNamespace | None,
    expected: bool,
) -> None:
    """Only a non-empty mounted image path is considered mounted."""
    coordinator = coordinator_state_factory(mounted_image=mounted_image)

    assert _has_mounted_image(coordinator) is expected


@pytest.mark.parametrize("supported", [False, True])
def test_cdrom_supported_uses_coordinator_capability(
    coordinator_state_factory: CoordinatorStateFactory,
    supported: bool,
) -> None:
    """CD-ROM entity creation follows the coordinator capability."""
    coordinator = coordinator_state_factory(supports_cdrom_endpoint=supported)

    assert _cdrom_supported(coordinator) is supported


def test_connection_type_is_case_insensitive_and_requires_an_address(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Connection activity ignores type case and empty addresses."""
    active = coordinator_state_factory(
        device_info=SimpleNamespace(
            ips=[_address("192.0.2.10", connection_type="WiReD")]
        )
    )
    empty = coordinator_state_factory(
        device_info=SimpleNamespace(ips=[_address("", connection_type="wired")])
    )
    wireless = coordinator_state_factory(
        device_info=SimpleNamespace(
            ips=[_address("192.0.2.20", connection_type="wireless")]
        )
    )

    assert _has_connection_type(active, "wired") is True
    assert _wired_active(active) is True
    assert _has_connection_type(empty, "wired") is False
    assert _wired_active(wireless) is False


@pytest.mark.parametrize(
    ("is_pro_hardware", "state", "expected"),
    [
        (False, None, False),
        (False, SimpleNamespace(enabled=True), False),
        (True, None, False),
        (True, SimpleNamespace(enabled=False), True),
    ],
)
def test_static_ip_availability_requires_pro_state(
    coordinator_state_factory: CoordinatorStateFactory,
    is_pro_hardware: bool,
    state: SimpleNamespace | None,
    expected: bool,
) -> None:
    """Static-IP availability requires Pro hardware and reported state."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=is_pro_hardware,
        static_ip=state,
    )

    assert _static_ip_available(coordinator) is expected


@pytest.mark.parametrize(
    ("is_pro_hardware", "state", "expected"),
    [
        (False, None, False),
        (False, SimpleNamespace(is_synchronized=True), False),
        (True, None, False),
        (True, SimpleNamespace(is_synchronized=False), True),
    ],
)
def test_time_status_availability_requires_pro_state(
    coordinator_state_factory: CoordinatorStateFactory,
    is_pro_hardware: bool,
    state: SimpleNamespace | None,
    expected: bool,
) -> None:
    """Time-status availability requires Pro hardware and reported state."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=is_pro_hardware,
        time_status=state,
    )

    assert _time_status_available(coordinator) is expected


def test_gpio_description_values_and_alpha_gate(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """GPIO descriptions read their flags and gate HDD state to Alpha hardware."""
    alpha = coordinator_state_factory(
        hardware_info=SimpleNamespace(version=HWVersion.ALPHA),
        gpio_info=SimpleNamespace(pwr=True, hdd=True),
    )
    pro = coordinator_state_factory(
        hardware_info=SimpleNamespace(version=HWVersion.PRO),
        gpio_info=SimpleNamespace(pwr=False, hdd=True),
    )
    power = _description("power_led")
    hdd = _description("hdd_led")

    assert power.value_fn(alpha) is True
    assert power.value_fn(pro) is False
    assert hdd.value_fn(alpha) is True
    assert hdd.available_fn(alpha) is True
    assert hdd.should_create_fn(alpha) is True
    assert hdd.available_fn(pro) is False
    assert hdd.should_create_fn(pro) is False


def test_wifi_description_separates_support_from_connection_state(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """A supported but disconnected radio remains an available entity."""
    coordinator = coordinator_state_factory(
        wifi_status=SimpleNamespace(supported=True, connected=False)
    )
    description = _description("wifi_connected")

    assert description.should_create_fn(coordinator) is True
    assert description.available_fn(coordinator) is True
    assert description.value_fn(coordinator) is False


def test_wired_description_uses_active_connection_for_creation_and_value(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """The wired description is created only for an addressed wired interface."""
    active = coordinator_state_factory(
        device_info=SimpleNamespace(ips=[_address("192.0.2.10")])
    )
    inactive = coordinator_state_factory(device_info=SimpleNamespace(ips=[]))
    description = _description("wired_connected")

    assert description.should_create_fn(active) is True
    assert description.value_fn(active) is True
    assert description.should_create_fn(inactive) is False
    assert description.value_fn(inactive) is False


def test_pro_state_descriptions_keep_false_state_available(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Reported false Pro state is available and represented as off."""
    coordinator = coordinator_state_factory(
        is_pro_hardware=True,
        static_ip=SimpleNamespace(enabled=False),
        time_status=SimpleNamespace(is_synchronized=False),
    )

    for key in ("static_ip_enabled", "time_synchronized"):
        description = _description(key)
        assert description.should_create_fn(coordinator) is True
        assert description.available_fn(coordinator) is True
        assert description.value_fn(coordinator) is False


@pytest.mark.parametrize(
    ("mounted_file", "cdrom", "expected_available", "expected_value"),
    [
        ("", 1, False, True),
        ("images/rescue.iso", 0, True, False),
        ("images/rescue.iso", 1, True, True),
    ],
)
def test_cdrom_description_contract(
    coordinator_state_factory: CoordinatorStateFactory,
    mounted_file: str,
    cdrom: int,
    expected_available: bool,
    expected_value: bool,
) -> None:
    """CD-ROM state is created by capability and available only while mounted."""
    coordinator = coordinator_state_factory(
        supports_cdrom_endpoint=True,
        mounted_image=SimpleNamespace(file=mounted_file),
        cdrom_status=SimpleNamespace(cdrom=cdrom),
    )
    description = MEDIA_BINARY_SENSORS[0]

    assert description.should_create_fn(coordinator) is True
    assert description.available_fn(coordinator) is expected_available
    assert description.value_fn(coordinator) is expected_value


def test_binary_sensor_description_inventory_contracts() -> None:
    """Binary-sensor descriptions keep stable keys and callable state hooks."""
    descriptions = (*BINARY_SENSORS, *MEDIA_BINARY_SENSORS)
    keys = [description.key for description in descriptions]

    assert len(keys) == len(set(keys))
    assert all(
        description.translation_key == description.key for description in descriptions
    )
    assert all(callable(description.value_fn) for description in descriptions)
    assert all(callable(description.available_fn) for description in descriptions)
    assert all(callable(description.should_create_fn) for description in descriptions)
