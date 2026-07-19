"""Tests for the config-entry scoped NanoKVM media runtime."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.nanokvm.media.runtime import (
    MAX_AUTOMATIC_RECORDING_DURATION_SECONDS,
    NanoKVMMediaRuntime,
    automatic_recording_path,
)


def test_automatic_recording_path_uses_device_and_timestamp_with_collision_suffix(
    tmp_path: Path,
) -> None:
    """Automatic filenames are sortable and never overwrite an existing capture."""
    now = datetime.datetime(2026, 7, 19, 0, 3, 0)

    first = automatic_recording_path(tmp_path, "device/key", now)
    first.write_bytes(b"existing")
    second = automatic_recording_path(tmp_path, "device/key", now)

    assert first == tmp_path / "nanokvm" / "device-key" / "20260719000300.mp4"
    assert second == tmp_path / "nanokvm" / "device-key" / "20260719000300-01.mp4"


def test_automatic_recording_path_falls_back_for_empty_device_key(
    tmp_path: Path,
) -> None:
    """A device key containing only separators still gets a safe directory."""
    output = automatic_recording_path(
        tmp_path,
        "///",
        datetime.datetime(2026, 7, 19, 0, 3, 0),
    )

    assert output.parent.name == "device"


@pytest.mark.asyncio
async def test_runtime_shutdown_stops_shared_recording_controller() -> None:
    """Config-entry shutdown owns recorder cleanup even without a camera entity."""
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(),
        config_entry=SimpleNamespace(),
        client=SimpleNamespace(),
        is_pro_hardware=False,
    )
    runtime = NanoKVMMediaRuntime(coordinator, logger=logging.getLogger("test"))
    runtime.recording = SimpleNamespace(async_shutdown=AsyncMock())

    await runtime.async_shutdown()

    runtime.recording.async_shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_starts_video_only_recording_with_hard_thirty_minute_limit(
    tmp_path: Path,
) -> None:
    """The switch path is automatic, video-only, and bounded to thirty minutes."""
    hass = SimpleNamespace(
        config=SimpleNamespace(media_dirs={"local": str(tmp_path)}),
        async_add_executor_job=AsyncMock(
            side_effect=lambda target, *args: target(*args)
        ),
    )
    coordinator = SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(entry_id="entry"),
        client=SimpleNamespace(),
        device_info=SimpleNamespace(device_key="test-device"),
        is_pro_hardware=False,
    )
    runtime = NanoKVMMediaRuntime(coordinator, logger=logging.getLogger("test"))
    runtime.recording = SimpleNamespace(
        current_filename=None,
        async_start=AsyncMock(),
        async_stop=AsyncMock(),
    )
    now = datetime.datetime(2026, 7, 19, 0, 3, 0)

    filename = await runtime.async_start_automatic(now=now)

    assert filename == str(
        tmp_path / "nanokvm" / "test-device" / "20260719000300.mp4"
    )
    runtime.recording.async_start.assert_awaited_once_with(
        filename=filename,
        duration=MAX_AUTOMATIC_RECORDING_DURATION_SECONDS,
        include_audio=False,
    )


@pytest.mark.asyncio
async def test_runtime_automatic_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    """Repeated switch commands do not create parallel files or workers."""
    hass = SimpleNamespace(
        config=SimpleNamespace(media_dirs={"local": str(tmp_path)}),
        async_add_executor_job=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(entry_id="entry"),
        client=SimpleNamespace(),
        device_info=SimpleNamespace(device_key="test-device"),
        is_pro_hardware=True,
    )
    runtime = NanoKVMMediaRuntime(coordinator, logger=logging.getLogger("test"))
    runtime.recording = SimpleNamespace(
        current_filename="/media/nanokvm/test-device/active.mp4",
        async_start=AsyncMock(),
        async_stop=AsyncMock(),
    )

    assert await runtime.async_start_automatic() == runtime.recording.current_filename
    runtime.recording.async_start.assert_not_awaited()
    hass.async_add_executor_job.assert_not_awaited()

    await runtime.async_stop_automatic()
    await runtime.async_stop_automatic()
    assert runtime.recording.async_stop.await_count == 2
