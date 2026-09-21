"""Public boundary tests for NanoKVM media helpers."""

from custom_components.nanokvm.media.pro_sdp import ProWebRTCOffer
from custom_components.nanokvm.media.client import NanoKVMStreamClientProvider
from custom_components.nanokvm.media.signaling import NanoKVMWebRTCManager


def test_media_package_exposes_signaling_and_sdp_boundaries() -> None:
    """Camera code imports signaling and pure SDP models from focused modules."""
    assert NanoKVMWebRTCManager.__module__.endswith("media.signaling")
    assert ProWebRTCOffer.__module__.endswith("media.pro_sdp")


def test_stream_client_provider_is_an_entry_scoped_boundary() -> None:
    """Camera transport creation remains isolated from entity presentation."""
    assert NanoKVMStreamClientProvider.__module__.endswith("media.client")
