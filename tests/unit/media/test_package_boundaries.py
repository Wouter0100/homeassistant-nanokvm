"""Public boundary tests for NanoKVM media helpers."""

from custom_components.nanokvm.media.pro_sdp import ProWebRTCOffer
from custom_components.nanokvm.media.recording.controller import (
    NanoKVMRecordingController,
)
from custom_components.nanokvm.media.recording.direct_h264 import (
    DirectH264RecordingBackend,
)
from custom_components.nanokvm.media.recording.webrtc import WebRTCRecordingBackend
from custom_components.nanokvm.media.client import NanoKVMStreamClientProvider
from custom_components.nanokvm.media.runtime import NanoKVMMediaRuntime
from custom_components.nanokvm.media.signaling import NanoKVMWebRTCManager


def test_media_package_exposes_signaling_and_sdp_boundaries() -> None:
    """Camera code imports signaling and pure SDP models from focused modules."""
    assert NanoKVMWebRTCManager.__module__.endswith("media.signaling")
    assert ProWebRTCOffer.__module__.endswith("media.pro_sdp")


def test_recording_package_separates_lifecycle_from_media_backends() -> None:
    """Recording lifecycle and transport implementations have clear boundaries."""
    assert NanoKVMRecordingController.__module__.endswith("recording.controller")
    assert DirectH264RecordingBackend.__module__.endswith("recording.direct_h264")
    assert WebRTCRecordingBackend.__module__.endswith("recording.webrtc")


def test_media_runtime_and_client_provider_are_entry_scoped_boundaries() -> None:
    """Shared media ownership is separate from camera entity presentation."""
    assert NanoKVMStreamClientProvider.__module__.endswith("media.client")
    assert NanoKVMMediaRuntime.__module__.endswith("media.runtime")
