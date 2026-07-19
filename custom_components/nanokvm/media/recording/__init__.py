"""HDMI recording lifecycle and media backends."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError

RECORDING_START_TIMEOUT_SECONDS = 20


def temporary_recording_path(output_path: Path) -> Path:
    """Return an extension-preserving temporary path for PyAV."""
    return output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")


async def async_wait_for_result(
    result: asyncio.Future[str],
    error: asyncio.Future[None],
    *,
    description: str,
) -> str:
    """Wait for a startup result while giving signaling failures precedence."""
    try:
        async with asyncio.timeout(RECORDING_START_TIMEOUT_SECONDS):
            done, _pending = await asyncio.wait(
                {result, error}, return_when=asyncio.FIRST_COMPLETED
            )
    except TimeoutError as err:
        raise HomeAssistantError(f"Timed out waiting for {description}") from err

    if error in done:
        raise error.exception()  # type: ignore[misc]
    return result.result()


async def async_wait_for_event(
    event: asyncio.Event,
    error: asyncio.Future[None],
    *,
    description: str,
) -> None:
    """Wait for a startup event unless signaling fails first."""
    event_task = asyncio.create_task(event.wait())
    try:
        try:
            async with asyncio.timeout(RECORDING_START_TIMEOUT_SECONDS):
                done, _pending = await asyncio.wait(
                    {event_task, error}, return_when=asyncio.FIRST_COMPLETED
                )
        except TimeoutError as err:
            raise HomeAssistantError(f"Timed out waiting for {description}") from err

        if event_task not in done:
            raise error.exception()  # type: ignore[misc]
    finally:
        event_task.cancel()
        with suppress(asyncio.CancelledError):
            await event_task
