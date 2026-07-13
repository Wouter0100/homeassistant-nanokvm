"""Shared fixtures for platform helper and entity-description tests."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest


@pytest.fixture
def coordinator_state_factory() -> Callable[..., SimpleNamespace]:
    """Build minimal coordinator-shaped state objects for pure helper tests."""

    def build(**values: object) -> SimpleNamespace:
        return SimpleNamespace(**values)

    return build
