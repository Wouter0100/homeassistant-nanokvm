"""Tests for NanoKVM SSH metric collection and parsing."""

from __future__ import annotations

import asyncio
import datetime
import inspect
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest

import custom_components.nanokvm.ssh_metrics as ssh_metrics_module
from custom_components.nanokvm.ssh_metrics import (
    SSHMetricsCollector,
    SSHMetricsSnapshot,
)


class _FakeSSHClient:
    """Controllable fake for the external NanoKVM SSH boundary."""

    def __init__(self, ssh_client: object | None) -> None:
        self.ssh_client = ssh_client
        self.authenticate_calls: list[str] = []
        self.authenticate_error: BaseException | None = None
        self.disconnect_calls = 0
        self.run_command_calls: list[str] = []
        self.run_command_result = ""
        self.run_command_error: BaseException | None = None
        self.run_command_callback: Callable[[str], str | Awaitable[str]] | None = None

    async def authenticate(self, password: str) -> None:
        """Record authentication and raise any configured boundary error."""
        self.authenticate_calls.append(password)
        if self.authenticate_error is not None:
            raise self.authenticate_error

    async def disconnect(self) -> None:
        """Record an SSH disconnect request."""
        self.disconnect_calls += 1

    async def run_command(self, command: str) -> str:
        """Return configured output or propagate a configured boundary failure."""
        self.run_command_calls.append(command)
        if self.run_command_error is not None:
            raise self.run_command_error
        if self.run_command_callback is not None:
            result = self.run_command_callback(command)
            if inspect.isawaitable(result):
                return await result
            return result
        return self.run_command_result


def _collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ssh_client: object | None = None,
) -> tuple[SSHMetricsCollector, _FakeSSHClient, MagicMock]:
    """Return a collector whose external SSH client boundary is mocked."""
    client = _FakeSSHClient(ssh_client)
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(ssh_metrics_module, "NanoKVMSSH", client_factory)

    collector = SSHMetricsCollector(
        host="nanokvm.local",
        password="secret",
        username="operator",
    )
    return collector, client, client_factory


class _Transport:
    """Minimal transport boundary consumed by the collector."""

    def __init__(self, *, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        """Return the configured transport state."""
        return self._active


class _Connection:
    """Minimal SSH connection boundary consumed by the collector."""

    def __init__(self, transport: _Transport | None) -> None:
        self._transport = transport
        self.get_transport_calls = 0

    def get_transport(self) -> _Transport | None:
        """Return the configured transport and record the observation."""
        self.get_transport_calls += 1
        return self._transport


def _active_connection() -> _Connection:
    """Return an SSH connection with an active transport."""
    return _Connection(_Transport(active=True))


def _command_outputs(*, watchdog: str = "1") -> dict[str, str]:
    """Return representative NanoKVM command output."""
    return {
        "cat /proc/stat": "cpu  1 2 3\nbtime 1700000000\nprocesses 9",
        "cat /sys/class/thermal/thermal_zone0/temp": "42000\n",
        "cat /proc/meminfo": ("MemTotal:       1048576 kB\nMemAvailable:    262144 kB"),
        "df -k /": (
            "Filesystem 1K-blocks Used Available Use% Mounted on\n"
            "/dev/root 2097152 524288 1572864 25% /"
        ),
        "test -f /etc/kvm/watchdog && echo 1 || echo 0": watchdog,
    }


def _serve_outputs(client: _FakeSSHClient, *, watchdog: str = "1") -> None:
    """Configure the mocked SSH boundary to serve command-specific output."""
    outputs = _command_outputs(watchdog=watchdog)

    async def run_command(command: str) -> str:
        return outputs[command]

    client.run_command_callback = run_command


def test_snapshot_exposes_all_collected_values() -> None:
    """A snapshot preserves each independently collected metric."""
    uptime = datetime.datetime(2023, 11, 14, 22, 13, 20, tzinfo=datetime.UTC)

    snapshot = SSHMetricsSnapshot(
        uptime=uptime,
        cpu_temperature=42.0,
        memory_total=1024.0,
        memory_used_percent=75.0,
        storage_total=2048.0,
        storage_used_percent=25.0,
        watchdog_enabled=True,
    )

    assert snapshot.uptime is uptime
    assert snapshot.cpu_temperature == 42.0
    assert snapshot.memory_total == 1024.0
    assert snapshot.memory_used_percent == 75.0
    assert snapshot.storage_total == 2048.0
    assert snapshot.storage_used_percent == 25.0
    assert snapshot.watchdog_enabled is True


def test_constructor_configures_the_library_ssh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collector construction forwards only connection identity to the client."""
    collector, client, client_factory = _collector(monkeypatch)

    client_factory.assert_called_once_with(host="nanokvm.local", username="operator")
    assert collector._client is client


@pytest.mark.parametrize("connected", [False, True])
async def test_disconnect_only_closes_an_existing_connection(
    monkeypatch: pytest.MonkeyPatch,
    connected: bool,
) -> None:
    """Disconnect is safe before connection and closes an active client once."""
    connection = _active_connection() if connected else None
    collector, client, _ = _collector(monkeypatch, ssh_client=connection)

    await collector.disconnect()

    if connected:
        assert client.disconnect_calls == 1
    else:
        assert client.disconnect_calls == 0


@pytest.mark.parametrize(
    "connection_state",
    ["missing-client", "missing-transport", "inactive-transport"],
)
async def test_ensure_connected_authenticates_unusable_connections(
    monkeypatch: pytest.MonkeyPatch,
    connection_state: str,
) -> None:
    """Missing or inactive SSH transports trigger password authentication."""
    connection: _Connection | None = None
    if connection_state != "missing-client":
        transport = None
        if connection_state == "inactive-transport":
            transport = _Transport(active=False)
        connection = _Connection(transport)
    collector, client, _ = _collector(monkeypatch, ssh_client=connection)

    await collector._async_ensure_connected()

    assert client.authenticate_calls == ["secret"]


async def test_ensure_connected_reuses_an_active_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active SSH transport is reusable without authentication."""
    connection = _active_connection()
    collector, client, _ = _collector(monkeypatch, ssh_client=connection)

    await collector._async_ensure_connected()

    assert connection.get_transport_calls == 1
    assert client.authenticate_calls == []


async def test_collect_returns_parsed_metrics_without_optional_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default collection produces one complete snapshot from device output."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    _serve_outputs(client)

    snapshot = await collector.collect()

    assert snapshot == SSHMetricsSnapshot(
        uptime=datetime.datetime(2023, 11, 14, 22, 13, 20, tzinfo=datetime.UTC),
        cpu_temperature=42.0,
        memory_total=1024.0,
        memory_used_percent=75.0,
        storage_total=2048.0,
        storage_used_percent=25.0,
        watchdog_enabled=None,
    )
    assert client.run_command_calls == [
        "cat /proc/stat",
        "cat /sys/class/thermal/thermal_zone0/temp",
        "cat /proc/meminfo",
        "df -k /",
    ]
    assert client.authenticate_calls == []


async def test_collect_can_include_watchdog_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers can request the optional watchdog value in the same snapshot."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    _serve_outputs(client)

    snapshot = await collector.collect(include_watchdog=True)

    assert snapshot.watchdog_enabled is True
    assert client.run_command_calls[-1] == (
        "test -f /etc/kvm/watchdog && echo 1 || echo 0"
    )


async def test_repeated_collection_reuses_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated refreshes remain idempotent while the transport stays active."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    _serve_outputs(client)

    first = await collector.collect()
    second = await collector.collect()

    assert first == second
    assert len(client.run_command_calls) == 8
    assert client.authenticate_calls == []


async def test_concurrent_collection_reuses_an_active_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent readers can share an already active SSH connection."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    outputs = _command_outputs()
    first_commands_started = 0
    both_collectors_started = asyncio.Event()

    async def run_command(command: str) -> str:
        nonlocal first_commands_started
        if command == "cat /proc/stat":
            first_commands_started += 1
            if first_commands_started == 2:
                both_collectors_started.set()
            await both_collectors_started.wait()
        return outputs[command]

    client.run_command_callback = run_command

    snapshots = await asyncio.gather(collector.collect(), collector.collect())

    assert first_commands_started == 2
    assert snapshots[0] == snapshots[1]
    assert len(client.run_command_calls) == 8
    assert client.authenticate_calls == []


async def test_collect_propagates_authentication_errors_without_running_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failures reach the coordinator without partial collection."""
    collector, client, _ = _collector(monkeypatch)
    client.authenticate_error = RuntimeError("authentication failed")

    with pytest.raises(RuntimeError, match="authentication failed"):
        await collector.collect()

    assert client.run_command_calls == []


async def test_collect_stops_after_an_ssh_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command failures propagate and later metrics are not requested."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    client.run_command_error = RuntimeError("SSH command failed")

    with pytest.raises(RuntimeError, match="SSH command failed"):
        await collector.collect()

    assert client.run_command_calls == ["cat /proc/stat"]


async def test_collect_propagates_cancellation_without_starting_later_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an in-flight command promptly cancels the collection."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    command_started = asyncio.Event()

    async def stalled_command(command: str) -> str:
        assert command == "cat /proc/stat"
        command_started.set()
        await asyncio.Event().wait()
        return ""

    client.run_command_callback = stalled_command
    task = asyncio.create_task(collector.collect())
    await command_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.run_command_calls == ["cat /proc/stat"]


@pytest.mark.parametrize(
    ("output", "expected"),
    [("1", True), (" 1\n", True), ("0", False), ("", False)],
)
async def test_fetch_watchdog_enabled_parses_device_output(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: bool,
) -> None:
    """Only the device's exact enabled marker reports an active watchdog."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )
    client.run_command_result = output

    assert await collector.fetch_watchdog_enabled() is expected
    assert client.run_command_calls == ["test -f /etc/kvm/watchdog && echo 1 || echo 0"]


@pytest.mark.parametrize(
    ("enabled", "command"),
    [
        (True, "touch /etc/kvm/watchdog"),
        (False, "rm -f /etc/kvm/watchdog"),
    ],
)
async def test_set_watchdog_enabled_runs_the_safe_device_command(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    command: str,
) -> None:
    """Both watchdog transitions use their corresponding idempotent command."""
    collector, client, _ = _collector(
        monkeypatch,
        ssh_client=_active_connection(),
    )

    await collector.set_watchdog_enabled(enabled)

    assert client.run_command_calls == [command]


async def test_fetch_uptime_returns_none_without_a_valid_boot_time_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated and malformed proc-stat records do not invent an uptime."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = "cpu\nintr 1\nbtime 1 extra\nctxt 4"

    assert await collector._fetch_uptime() is None


async def test_fetch_uptime_propagates_an_invalid_boot_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid numeric device output remains an explicit collection failure."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = "btime invalid"

    with pytest.raises(ValueError, match="invalid literal"):
        await collector._fetch_uptime()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("malformed\nMemAvailable: 512", {"total": None, "used_percent": None}),
        ("MemTotal: 0\nMemAvailable: 512", {"total": 0.0, "used_percent": None}),
        ("MemTotal: 1024", {"total": 1.0, "used_percent": None}),
        (
            "MemTotal: 1024\nMemAvailable: 256",
            {"total": 1.0, "used_percent": 75.0},
        ),
    ],
)
async def test_fetch_memory_handles_partial_and_complete_meminfo(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: dict[str, float | None],
) -> None:
    """Memory parsing returns only values supported by complete device records."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = output

    assert await collector._fetch_memory() == expected


async def test_fetch_memory_propagates_invalid_numeric_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric meminfo values remain visible to coordinator recovery logic."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = "MemTotal: invalid"

    with pytest.raises(ValueError, match="invalid literal"):
        await collector._fetch_memory()


@pytest.mark.parametrize(
    ("output", "expected"),
    [("42000\n", 42.0), ("42", 42.0), ("1000", 1000.0)],
)
async def test_fetch_cpu_temperature_normalizes_millidegrees(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: float,
) -> None:
    """Thermal values above the threshold convert from millidegrees Celsius."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = output

    assert await collector._fetch_cpu_temperature() == expected


async def test_fetch_cpu_temperature_propagates_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing numeric temperature remains an explicit collection error."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = ""

    with pytest.raises(ValueError, match="could not convert"):
        await collector._fetch_cpu_temperature()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Filesystem 1K-blocks Used", {"total": None, "used_percent": None}),
        (
            "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/root 100",
            {"total": None, "used_percent": None},
        ),
        (
            "Filesystem 1K-blocks Used Available Use% Mounted on\n"
            "/dev/root 1048576 524288 524288 50% /",
            {"total": 1024.0, "used_percent": 50.0},
        ),
    ],
)
async def test_fetch_storage_handles_partial_and_complete_df_output(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: dict[str, float | None],
) -> None:
    """Storage parsing returns values only for a complete data row."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = output

    assert await collector._fetch_storage() == expected


async def test_fetch_storage_propagates_invalid_numeric_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid df numbers remain visible to coordinator recovery logic."""
    collector, client, _ = _collector(monkeypatch)
    client.run_command_result = (
        "Filesystem 1K-blocks Used Available Use% Mounted on\n"
        "/dev/root invalid 1 1 1% /"
    )

    with pytest.raises(ValueError, match="invalid literal"):
        await collector._fetch_storage()
