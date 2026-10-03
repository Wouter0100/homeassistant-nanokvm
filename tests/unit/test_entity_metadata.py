"""Tests that every entity gets its name and icon from the metadata files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.nanokvm import (
    binary_sensor,
    button,
    number,
    select,
    sensor,
    switch,
)
from custom_components.nanokvm.camera import NanoKVMCamera
from custom_components.nanokvm.update import NanoKVMUpdate

INTEGRATION = Path(binary_sensor.__file__).parent

DESCRIPTIONS = [
    (platform, description.translation_key)
    for platform, groups in {
        "binary_sensor": (
            binary_sensor.BINARY_SENSORS,
            binary_sensor.MEDIA_BINARY_SENSORS,
        ),
        "button": (button.BUTTONS,),
        "camera": ((NanoKVMCamera.entity_description,),),
        "number": (number.NUMBERS,),
        "select": (select.SELECTS,),
        "sensor": (
            sensor.SENSORS,
            sensor.NETWORK_SENSORS,
            sensor.MEDIA_SENSORS,
            sensor.SSH_SENSORS,
        ),
        "switch": (switch.SWITCHES, switch.SSH_SWITCHES),
        "update": ((NanoKVMUpdate.entity_description,),),
    }.items()
    for group in groups
    for description in group
]


def _entities(relative_path: str) -> dict[str, dict[str, dict[str, object]]]:
    return json.loads((INTEGRATION / relative_path).read_text())["entity"]


@pytest.mark.parametrize(
    "translation", ["translations/en.json", "translations/fr.json", "translations/pt-BR.json"]
)
def test_every_entity_has_a_translated_name(translation: str) -> None:
    """Descriptions carry no inline name, so each needs a translation."""
    entities = _entities(translation)

    assert [
        (platform, key)
        for platform, key in DESCRIPTIONS
        if not entities.get(platform, {}).get(key, {}).get("name")
    ] == []


def test_every_entity_has_an_icon() -> None:
    """Descriptions carry no inline icon, so each needs an icons.json entry."""
    icons = _entities("icons.json")

    assert [
        (platform, key)
        for platform, key in DESCRIPTIONS
        if not str(icons.get(platform, {}).get(key, {}).get("default", "")).startswith(
            "mdi:"
        )
    ] == []
