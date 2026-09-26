"""Config-entry scoped NanoKVM stream client creation and authentication."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from nanokvm.client import NanoKVMClient

from ..const import CONF_SSL_FINGERPRINT

if TYPE_CHECKING:
    from ..coordinator import NanoKVMDataUpdateCoordinator


class NanoKVMStreamClientProvider:
    """Create short-lived media clients from the coordinator's active transport."""

    def __init__(
        self,
        coordinator: NanoKVMDataUpdateCoordinator,
        *,
        client_factory: Callable[..., NanoKVMClient] = NanoKVMClient,
    ) -> None:
        """Initialize the provider for one config entry."""
        self._coordinator = coordinator
        self._client_factory = client_factory

    def _credentials(self) -> tuple[str, str] | None:
        """Return complete configured media credentials."""
        config_entry = self._coordinator.config_entry
        if not config_entry or not config_entry.data:
            return None

        username = config_entry.data.get(CONF_USERNAME)
        password = config_entry.data.get(CONF_PASSWORD)
        if not username or not password:
            return None
        return username, password

    def create_client(self) -> NanoKVMClient | None:
        """Create a media client using the coordinator's resolved transport."""
        config_entry = self._coordinator.config_entry
        if not config_entry or not config_entry.data:
            return None

        active_url = str(self._coordinator.client.url)
        ssl_fingerprint = (
            config_entry.data.get(CONF_SSL_FINGERPRINT)
            if self._coordinator.client.url.scheme == "https"
            else None
        )
        return self._client_factory(
            active_url,
            token=self._coordinator.client.token,
            ssl_fingerprint=ssl_fingerprint,
        )

    async def async_authenticate(self, client: NanoKVMClient) -> None:
        """Authenticate a media client only when it has no reusable token."""
        credentials = self._credentials()
        if credentials is None:
            raise RuntimeError("Missing NanoKVM stream credentials")
        if client.token:
            return

        await client.authenticate(*credentials)
