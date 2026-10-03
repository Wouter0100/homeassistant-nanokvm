"""Config-entry scoped NanoKVM stream client creation and authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanokvm.client import NanoKVMClient

from ..const import CONF_SSL_FINGERPRINT

if TYPE_CHECKING:
    from ..coordinator import NanoKVMDataUpdateCoordinator


class NanoKVMStreamClientProvider:
    """Create short-lived media clients from the coordinator's active transport."""

    def __init__(self, coordinator: NanoKVMDataUpdateCoordinator) -> None:
        """Initialize the provider for one config entry."""
        self._coordinator = coordinator

    def create_client(self) -> NanoKVMClient:
        """Create a media client using the coordinator's resolved transport."""
        active_client = self._coordinator.client
        return NanoKVMClient(
            str(active_client.url),
            token=active_client.token,
            ssl_fingerprint=(
                self._coordinator.config_entry.data.get(CONF_SSL_FINGERPRINT)
                if active_client.url.scheme == "https"
                else None
            ),
        )

    async def async_authenticate(self, client: NanoKVMClient) -> None:
        """Authenticate a media client only when it has no reusable token."""
        if not client.token:
            await client.authenticate(
                self._coordinator.username, self._coordinator.password
            )
