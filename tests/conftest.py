"""Shared pytest fixtures for Home Assistant integration-boundary tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest


@pytest.fixture
def hass_mock() -> MagicMock:
    """Return a Home Assistant mock with integration storage initialized."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {}
    return hass


@pytest.fixture
def config_entry_mock() -> MagicMock:
    """Return a complete NanoKVM config-entry mock."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test-entry"
    entry.unique_id = "test-device"
    entry.data = {
        CONF_HOST: "nanokvm.local",
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "password",
    }
    return entry
