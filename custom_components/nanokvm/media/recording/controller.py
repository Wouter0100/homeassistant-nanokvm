"""Shared lifecycle controller for NanoKVM HDMI recordings."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..signaling import AuthenticateClientCallable, StreamClientFactory
from .direct_h264 import DirectH264RecordingBackend
from .webrtc import WebRTCRecordingBackend


@dataclass(slots=True)
class _RecordingControl:
    """Mutable ownership state for one recording worker."""

    stop_event: asyncio.Event
    started: asyncio.Future[None]
    task: asyncio.Task[None] | None = None
    active: bool = False


class NanoKVMRecordingController:
    """Own one background HDMI recording for a NanoKVM config entry."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        hass_provider: Callable[[], HomeAssistant | None],
        config_entry_provider: Callable[[], ConfigEntry | None],
        client_factory: StreamClientFactory,
        authenticate_client: AuthenticateClientCallable,
        state_callback: Callable[[bool], None] | None = None,
        use_direct_stream: bool = False,
    ) -> None:
        """Initialize the recording controller and its media backends."""
        self._logger = logger
        self._hass_provider = hass_provider
        self._config_entry_provider = config_entry_provider
        self._state_listeners: set[Callable[[bool], None]] = set()
        if state_callback is not None:
            self._state_listeners.add(state_callback)
        self._use_direct_stream = use_direct_stream
        self.direct_backend = DirectH264RecordingBackend(
            client_factory=client_factory,
            authenticate_client=authenticate_client,
        )
        self.webrtc_backend = WebRTCRecordingBackend(
            logger=logger,
            hass_provider=hass_provider,
            client_factory=client_factory,
            authenticate_client=authenticate_client,
        )
        self._session: _RecordingControl | None = None
        self._session_lock = asyncio.Lock()
        self._is_recording = False
        self._current_filename: str | None = None

    @property
    def is_recording(self) -> bool:
        """Return whether the active worker has captured its first frame."""
        return self._is_recording

    @property
    def current_filename(self) -> str | None:
        """Return the active or starting recording output path."""
        return self._current_filename

    def async_add_state_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Subscribe a camera or switch to shared recording state changes."""
        self._state_listeners.add(listener)

        def remove_listener() -> None:
            self._state_listeners.discard(listener)

        return remove_listener

    def _set_recording_state(self, recording: bool) -> None:
        """Update and publish recording state to all entry consumers."""
        if self._is_recording == recording:
            return
        self._is_recording = recording
        for listener in tuple(self._state_listeners):
            try:
                listener(recording)
            except Exception:
                self._logger.exception("NanoKVM recording state listener failed")

    async def async_start(
        self, *, filename: str, duration: int, include_audio: bool
    ) -> None:
        """Start recording and wait until the first video frame is captured."""
        hass = self._hass_provider()
        config_entry = self._config_entry_provider()
        if hass is None or config_entry is None:
            raise HomeAssistantError("Home Assistant is not ready for HDMI recording")

        async with self._session_lock:
            if self._session is not None:
                raise HomeAssistantError("An HDMI recording is already in progress")

            control = _RecordingControl(
                stop_event=asyncio.Event(),
                started=asyncio.get_running_loop().create_future(),
            )
            self._session = control
            self._current_filename = filename
            worker = self._async_run(
                control,
                filename=filename,
                duration=duration,
                include_audio=include_audio,
            )
            try:
                control.task = config_entry.async_create_background_task(
                    hass,
                    worker,
                    "NanoKVM HDMI recording",
                    eager_start=False,
                )
            except Exception as err:
                worker.close()
                self._session = None
                self._current_filename = None
                raise HomeAssistantError(
                    f"Unable to schedule HDMI recording: {err}"
                ) from err
            control.task.add_done_callback(
                lambda task: self._handle_worker_done(control, task)
            )

        try:
            await control.started
        except BaseException:
            async with self._session_lock:
                if self._session is control and control.task.done():
                    self._session = None
                    self._current_filename = None
            raise

    def _handle_worker_done(
        self, control: _RecordingControl, task: asyncio.Task[None]
    ) -> None:
        """Settle startup if config-entry teardown cancels an unstarted worker."""
        if task.cancelled() and not control.started.done():
            control.started.set_exception(
                HomeAssistantError(
                    "HDMI recording was stopped before it became active"
                )
            )

    async def async_stop(self) -> None:
        """Stop recording, if active."""
        async with self._session_lock:
            control = self._session

        if control is None or control.task is None:
            return

        if control.started.done() and not control.started.cancelled():
            control.stop_event.set()
        else:
            control.task.cancel()

        with suppress(asyncio.CancelledError):
            await control.task

    async def async_shutdown(self) -> None:
        """Stop recording during config-entry shutdown."""
        await self.async_stop()

    async def _async_run(
        self,
        control: _RecordingControl,
        *,
        filename: str,
        duration: int,
        include_audio: bool,
    ) -> None:
        """Run a recording while translating worker failures into entity state."""

        def on_started() -> None:
            if control.started.done():
                return
            control.active = True
            self._set_recording_state(True)
            control.started.set_result(None)

        try:
            await self._async_record(
                filename,
                duration,
                include_audio,
                control.stop_event,
                on_started,
            )
            if not control.started.done():
                control.started.set_exception(
                    HomeAssistantError(
                        "HDMI recording ended before it became active"
                    )
                )
        except asyncio.CancelledError:
            if not control.started.done():
                control.started.set_exception(
                    HomeAssistantError(
                        "HDMI recording was stopped before it became active"
                    )
                )
            raise
        except Exception as err:
            if not control.started.done():
                control.started.set_exception(
                    HomeAssistantError(f"Unable to start HDMI recording: {err}")
                )
            else:
                self._logger.error("HDMI recording ended unexpectedly: %s", err)
        finally:
            try:
                if control.active:
                    self._set_recording_state(False)
            finally:
                async with self._session_lock:
                    if self._session is control:
                        self._session = None
                        self._current_filename = None

    async def _async_record(
        self,
        filename: str,
        duration: int,
        include_audio: bool,
        stop_event: asyncio.Event,
        on_started: Callable[[], None],
    ) -> None:
        """Select the low-overhead direct stream when available."""
        if self._use_direct_stream and not include_audio:
            await self.direct_backend.async_record(
                filename, duration, stop_event, on_started
            )
            return

        await self.webrtc_backend.async_record(
            filename, duration, include_audio, stop_event, on_started
        )
