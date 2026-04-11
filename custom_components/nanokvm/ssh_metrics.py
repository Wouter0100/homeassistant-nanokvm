"""SSH metrics collection helpers for NanoKVM."""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from homeassistant.util import dt as dt_util

from nanokvm.ssh_client import NanoKVMSSH


@dataclass(slots=True)
class SSHMetricsSnapshot:
    """Snapshot of metrics collected via SSH."""

    uptime: datetime.datetime | None
    cpu_temperature: float | None
    memory_total: float | None
    memory_used_percent: float | None
    storage_total: float | None
    storage_used_percent: float | None
    watchdog_enabled: bool | None


class SSHMetricsCollector:
    """Collect metrics from NanoKVM over SSH."""

    def __init__(self, host: str, password: str, username: str = "root") -> None:
        """Initialize the SSH collector."""
        self._password = password
        self._client = NanoKVMSSH(host=host, username=username)

    async def disconnect(self) -> None:
        """Disconnect the underlying SSH client if connected."""
        if self._client.ssh_client:
            await self._client.disconnect()

    async def _async_ensure_connected(self) -> None:
        """Connect the SSH client when needed."""
        ssh_client = self._client.ssh_client
        transport = ssh_client.get_transport() if ssh_client is not None else None

        if ssh_client is None or transport is None or not transport.is_active():
            await self._client.authenticate(self._password)

    async def collect(self, *, include_watchdog: bool = False) -> SSHMetricsSnapshot:
        """Collect uptime, memory, storage, and optional watchdog state."""
        await self._async_ensure_connected()

        uptime = await self._fetch_uptime()
        cpu_temperature = await self._fetch_cpu_temperature()
        memory_stats = await self._fetch_memory()
        storage_stats = await self._fetch_storage()
        watchdog_enabled = None
        if include_watchdog:
            watchdog_enabled = await self.fetch_watchdog_enabled()

        return SSHMetricsSnapshot(
            uptime=uptime,
            cpu_temperature=cpu_temperature,
            memory_total=memory_stats.get("total"),
            memory_used_percent=memory_stats.get("used_percent"),
            storage_total=storage_stats.get("total"),
            storage_used_percent=storage_stats.get("used_percent"),
            watchdog_enabled=watchdog_enabled,
        )

    async def fetch_watchdog_enabled(self) -> bool:
        """Return whether the NanoKVM watchdog file exists."""
        await self._async_ensure_connected()
        output = await self._client.run_command("test -f /etc/kvm/watchdog && echo 1 || echo 0")
        return output.strip() == "1"

    async def set_watchdog_enabled(self, enabled: bool) -> None:
        """Enable or disable the NanoKVM watchdog file."""
        await self._async_ensure_connected()
        command = "touch /etc/kvm/watchdog" if enabled else "rm -f /etc/kvm/watchdog"
        await self._client.run_command(command)

    async def _fetch_uptime(self) -> datetime.datetime | None:
        """Fetch uptime via SSH."""
        stat_raw = await self._client.run_command("cat /proc/stat")
        for line in stat_raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "btime":
                return dt_util.utc_from_timestamp(int(parts[1]))
        return None

    async def _fetch_memory(self) -> dict[str, float | None]:
        """Fetch memory stats via SSH."""
        meminfo = await self._client.run_command("cat /proc/meminfo")
        mem_data: dict[str, int] = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mem_data[parts[0].rstrip(":")] = int(parts[1])

        total_mb: float | None = None
        used_percent: float | None = None

        mem_total_kb = mem_data.get("MemTotal")
        if mem_total_kb is not None:
            total_mb = round(mem_total_kb / 1024, 2)
            mem_available_kb = mem_data.get("MemAvailable")
            if total_mb > 0 and mem_available_kb is not None:
                memory_free = round(mem_available_kb / 1024, 2)
                memory_used = round(total_mb - memory_free, 2)
                used_percent = round((memory_used / total_mb) * 100, 2)

        return {"total": total_mb, "used_percent": used_percent}

    async def _fetch_cpu_temperature(self) -> float | None:
        """Fetch CPU temperature in Celsius via SSH."""
        output = await self._client.run_command("cat /sys/class/thermal/thermal_zone0/temp")
        value = float(output.strip())
        if value > 1000:
            value /= 1000
        return round(value, 1)

    async def _fetch_storage(self) -> dict[str, float | None]:
        """Fetch storage stats via SSH."""
        df_output = await self._client.run_command("df -k /")
        lines = df_output.splitlines()
        total_mb: float | None = None
        used_percent: float | None = None

        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                total_mb = round(int(parts[1]) / 1024, 2)
                used_percent = float(parts[4].rstrip("%"))

        return {"total": total_mb, "used_percent": used_percent}
