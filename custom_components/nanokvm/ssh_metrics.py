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
    memory_total: float | None
    memory_used_percent: float | None
    storage_total: float | None
    storage_used_percent: float | None


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

    async def collect(self) -> SSHMetricsSnapshot:
        """Collect uptime, memory and storage stats."""
        if (
            not self._client.ssh_client
            or not self._client.ssh_client.get_transport()
            or not self._client.ssh_client.get_transport().is_active()
        ):
            await self._client.authenticate(self._password)

        uptime = await self._fetch_uptime()
        memory_stats = await self._fetch_memory()
        storage_stats = await self._fetch_storage()

        return SSHMetricsSnapshot(
            uptime=uptime,
            memory_total=memory_stats.get("total"),
            memory_used_percent=memory_stats.get("used_percent"),
            storage_total=storage_stats.get("total"),
            storage_used_percent=storage_stats.get("used_percent"),
        )

    async def _fetch_uptime(self) -> datetime.datetime | None:
        """Fetch uptime via SSH."""
        uptime_raw = await self._client.run_command("cat /proc/uptime")
        uptime_seconds = float(uptime_raw.split()[0])
        return dt_util.utcnow() - datetime.timedelta(seconds=uptime_seconds)

    async def _fetch_memory(self) -> dict[str, float | None]:
        """Fetch memory stats via SSH."""
        meminfo = await self._client.run_command("cat /proc/meminfo")
        mem_data = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mem_data[parts[0].rstrip(":")] = int(parts[1])

        stats = {"total": None, "used_percent": None}
        if "MemTotal" in mem_data:
            stats["total"] = round(mem_data["MemTotal"] / 1024, 2)
            if stats["total"] > 0 and "MemAvailable" in mem_data:
                memory_free = round(mem_data["MemAvailable"] / 1024, 2)
                memory_used = round(stats["total"] - memory_free, 2)
                stats["used_percent"] = round((memory_used / stats["total"]) * 100, 2)
        return stats

    async def _fetch_storage(self) -> dict[str, float | None]:
        """Fetch storage stats via SSH."""
        df_output = await self._client.run_command("df -k /")
        lines = df_output.splitlines()
        stats = {"total": None, "used_percent": None}
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                stats["total"] = round(int(parts[1]) / 1024, 2)
                stats["used_percent"] = float(parts[4].rstrip("%"))
        return stats
