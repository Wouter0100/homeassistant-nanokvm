"""Config-entry scoped media runtime shared by NanoKVM entities and services."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .client import NanoKVMStreamClientProvider
from .recording.controller import NanoKVMRecordingController

if TYPE_CHECKING:
    from ..coordinator import NanoKVMDataUpdateCoordinator

MAX_AUTOMATIC_RECORDING_DURATION_SECONDS = 30 * 60
_DEFAULT_MEDIA_ROOT = Path("/media")
_UNSAFE_PATH_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]+")


def automatic_recording_path(
    media_root: Path,
    device_key: str,
    now: datetime.datetime,
) -> Path:
    """Create the device directory and return a collision-free timestamp path."""
    safe_device_key = _UNSAFE_PATH_CHARACTER.sub("-", device_key).strip("-.")
    if not safe_device_key:
        safe_device_key = "device"
    output_directory = media_root / "nanokvm" / safe_device_key
    output_directory.mkdir(parents=True, exist_ok=True)

    stem = now.strftime("%Y%m%d%H%M%S")
    candidate = output_directory / f"{stem}.mp4"
    collision = 1
    while candidate.exists():
        candidate = output_directory / f"{stem}-{collision:02d}.mp4"
        collision += 1
    return candidate


class NanoKVMMediaRuntime:
    """Own media clients and the single recording controller for one device."""

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        *,
        logger: logging.Logger,
    ) -> None:
        """Initialize entry-scoped media resources."""
        self._coordinator = coordinator
        self.client_provider = NanoKVMStreamClientProvider(coordinator)
        self.recording = NanoKVMRecordingController(
            logger=logger,
            hass_provider=lambda: coordinator.hass,
            config_entry_provider=lambda: coordinator.config_entry,
            client_factory=self.client_provider.create_client,
            authenticate_client=self.client_provider.async_authenticate,
            use_direct_stream=not coordinator.is_pro_hardware,
        )

    async def async_shutdown(self) -> None:
        """Stop all entry-scoped media work."""
        await self.recording.async_shutdown()

    async def async_start_automatic(
        self, *, now: datetime.datetime | None = None
    ) -> str:
        """Start one bounded video-only recording with an automatic filename."""
        if self.recording.current_filename is not None:
            return self.recording.current_filename

        hass = self._coordinator.hass
        media_dirs = getattr(hass.config, "media_dirs", {})
        media_root = Path(media_dirs.get("local", _DEFAULT_MEDIA_ROOT))
        device_key = getattr(
            self._coordinator.device_info,
            "device_key",
            self._coordinator.config_entry.entry_id,
        )
        output_path = await hass.async_add_executor_job(
            automatic_recording_path,
            media_root,
            device_key,
            now or dt_util.now(),
        )
        filename = str(output_path)
        await self.recording.async_start(
            filename=filename,
            duration=MAX_AUTOMATIC_RECORDING_DURATION_SECONDS,
            include_audio=False,
        )
        return filename

    async def async_stop_automatic(self) -> None:
        """Stop the shared recording controller idempotently."""
        await self.recording.async_stop()
