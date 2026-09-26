"""Tests for NanoKVM sensor helpers and entity descriptions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nanokvm.models import IPInfo

from custom_components.nanokvm.sensor import (
    MEDIA_SENSORS,
    NETWORK_SENSORS,
    SENSORS,
    SSH_SENSORS,
    _addresses_for_connection_type,
    _connection_ip_attributes,
    _connection_ip_value,
    _has_connection_type,
    _has_mounted_image,
    _ip_address_attributes,
    _memory_total_attribute,
    _mounted_image_value,
    _normalize_tailscale_state,
    _primary_ip_address,
    _storage_total_attribute,
    _tailscale_attributes,
    _tailscale_state_value,
)

CoordinatorStateFactory = Callable[..., SimpleNamespace]


def _address(
    address: str,
    *,
    version: str = "IPv4",
    connection_type: str = "wired",
    name: str = "eth0",
) -> IPInfo:
    """Build a NanoKVM address response."""
    return IPInfo(
        name=name,
        addr=address,
        version=version,
        type=connection_type,
    )


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        (None, None),
        ("notInstall", "not_install"),
        ("notRunning", "not_running"),
        ("notLogin", "not_login"),
        ("running", "running"),
    ],
)
def test_normalize_tailscale_state(
    raw_state: str | None,
    expected: str | None,
) -> None:
    """Tailscale API states are normalized only when a mapping exists."""
    assert _normalize_tailscale_state(raw_state) == expected


@pytest.mark.parametrize(
    ("mounted_image", "expected_available", "expected_value"),
    [
        (None, False, ""),
        (SimpleNamespace(file=""), False, ""),
        (SimpleNamespace(file="images/rescue.iso"), True, "images/rescue.iso"),
    ],
)
def test_mounted_image_helpers_and_description_contract(
    coordinator_state_factory: CoordinatorStateFactory,
    mounted_image: SimpleNamespace | None,
    expected_available: bool,
    expected_value: str,
) -> None:
    """Mounted-image helpers and their description expose the same state."""
    coordinator = coordinator_state_factory(mounted_image=mounted_image)
    description = MEDIA_SENSORS[0]

    assert _has_mounted_image(coordinator) is expected_available
    assert _mounted_image_value(coordinator) == expected_value
    assert description.should_create_fn(coordinator) is expected_available
    assert description.available_fn(coordinator) is expected_available
    assert description.value_fn(coordinator) == expected_value


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        ([], None),
        (
            [
                _address("fd00::10", version="IPv6"),
                _address("192.0.2.10"),
            ],
            "192.0.2.10",
        ),
        ([_address("fd00::10", version="IPv6")], "fd00::10"),
    ],
)
def test_primary_ip_address_prefers_ipv4(
    coordinator_state_factory: CoordinatorStateFactory,
    addresses: list[IPInfo],
    expected: str | None,
) -> None:
    """The primary address prefers IPv4 and otherwise uses the first address."""
    coordinator = coordinator_state_factory(device_info=SimpleNamespace(ips=addresses))

    assert _primary_ip_address(coordinator) == expected


def test_ip_address_attributes_preserve_reported_address_data(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """IP address attributes serialize all fields in device order."""
    addresses = [
        _address("192.0.2.10"),
        _address(
            "2001:db8::20",
            version="IPv6",
            connection_type="wireless",
            name="wlan0",
        ),
    ]
    coordinator = coordinator_state_factory(device_info=SimpleNamespace(ips=addresses))

    assert _ip_address_attributes(coordinator) == {
        "addresses": [
            {
                "name": "eth0",
                "addr": "192.0.2.10",
                "version": "IPv4",
                "type": "wired",
            },
            {
                "name": "wlan0",
                "addr": "2001:db8::20",
                "version": "IPv6",
                "type": "wireless",
            },
        ]
    }


def test_connection_helpers_filter_case_insensitively_and_prefer_ipv4(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Connection helpers isolate one type and choose its IPv4 address."""
    wired_ipv6 = _address("fd00::10", version="IPv6", connection_type="WiReD")
    wireless = _address(
        "192.0.2.20",
        connection_type="wireless",
        name="wlan0",
    )
    wired_ipv4 = _address("192.0.2.10", connection_type="WIRED")
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(ips=[wired_ipv6, wireless, wired_ipv4])
    )

    assert _addresses_for_connection_type(coordinator, "wired") == [
        wired_ipv6,
        wired_ipv4,
    ]
    assert _has_connection_type(coordinator, "wired") is True
    assert _connection_ip_value(coordinator, "wired") == "192.0.2.10"
    assert _connection_ip_attributes(coordinator, "wired") == {
        "addresses": [
            {
                "name": "eth0",
                "addr": "fd00::10",
                "version": "IPv6",
                "type": "WiReD",
            },
            {
                "name": "eth0",
                "addr": "192.0.2.10",
                "version": "IPv4",
                "type": "WIRED",
            },
        ]
    }


def test_connection_helpers_return_empty_state_when_type_is_absent(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Connection helpers return unavailable state for missing types."""
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(
            ips=[_address("192.0.2.20", connection_type="wireless", name="wlan0")]
        )
    )

    assert _addresses_for_connection_type(coordinator, "wired") == []
    assert _has_connection_type(coordinator, "wired") is False
    assert _connection_ip_value(coordinator, "wired") is None
    assert _connection_ip_attributes(coordinator, "wired") == {"addresses": []}


def test_connection_ip_value_falls_back_to_first_ipv6_address(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """A connection without IPv4 uses its first reported IPv6 address."""
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(
            ips=[
                _address("fd00::10", version="IPv6"),
                _address("fd00::11", version="IPv6"),
            ]
        )
    )

    assert _connection_ip_value(coordinator, "wired") == "fd00::10"


def test_connection_type_requires_a_nonempty_address(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """An interface without an address does not make its connection active."""
    coordinator = coordinator_state_factory(
        device_info=SimpleNamespace(ips=[_address("")])
    )

    assert _has_connection_type(coordinator, "wired") is False


def test_tailscale_helpers_return_empty_state_when_status_is_missing(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Missing Tailscale status produces no state or attributes."""
    coordinator = coordinator_state_factory(tailscale_status=None)

    assert _tailscale_state_value(coordinator) is None
    assert _tailscale_attributes(coordinator) == {}


def test_tailscale_helpers_expose_normalized_state_and_attributes(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Tailscale state and identity fields are exposed together."""
    coordinator = coordinator_state_factory(
        tailscale_status=SimpleNamespace(
            state=SimpleNamespace(value="notRunning"),
            name="nano",
            ip="100.64.0.10",
            account="owner@example.com",
        )
    )

    assert _tailscale_state_value(coordinator) == "not_running"
    assert _tailscale_attributes(coordinator) == {
        "name": "nano",
        "ip": "100.64.0.10",
        "account": "owner@example.com",
    }


@pytest.mark.parametrize(
    ("attribute_name", "helper", "result_key"),
    [
        ("memory_total", _memory_total_attribute, "total_mb"),
        ("storage_total", _storage_total_attribute, "total_mb"),
    ],
)
def test_total_attributes_preserve_zero_and_omit_missing_values(
    coordinator_state_factory: CoordinatorStateFactory,
    attribute_name: str,
    helper: Callable[[SimpleNamespace], dict[str, object]],
    result_key: str,
) -> None:
    """A measured zero is retained while an unavailable total is omitted."""
    missing = coordinator_state_factory(**{attribute_name: None})
    zero = coordinator_state_factory(**{attribute_name: 0})

    assert helper(missing) == {}
    assert helper(zero) == {result_key: 0}


def test_sensor_description_inventory_contracts() -> None:
    """Sensor descriptions keep stable unique keys and callable state hooks."""
    descriptions = (*MEDIA_SENSORS, *SENSORS, *NETWORK_SENSORS, *SSH_SENSORS)
    keys = [description.key for description in descriptions]

    assert len(keys) == len(set(keys))
    assert all(
        description.translation_key == description.key for description in descriptions
    )
    assert all(callable(description.value_fn) for description in descriptions)
    assert all(callable(description.available_fn) for description in descriptions)
    assert all(callable(description.should_create_fn) for description in descriptions)
    assert all(callable(description.attributes_fn) for description in descriptions)
    assert {
        description.key: description.connection_type for description in NETWORK_SENSORS
    } == {
        "wired_ip_address": "wired",
        "wireless_ip_address": "wireless",
    }


def test_ssh_sensor_descriptions_treat_zero_as_available(
    coordinator_state_factory: CoordinatorStateFactory,
) -> None:
    """Valid zero-valued SSH measurements remain available."""
    values = {
        "uptime": datetime(2026, 1, 1, tzinfo=UTC),
        "cpu_temperature": 0.0,
        "memory_used_percent": 0.0,
        "storage_used_percent": 0.0,
    }

    for description in SSH_SENSORS:
        coordinator = coordinator_state_factory(**values)
        assert description.available_fn(coordinator) is True
        assert description.value_fn(coordinator) == values[description.key]
