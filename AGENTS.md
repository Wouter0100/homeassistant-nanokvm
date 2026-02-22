# Agent Project Overview: Sipeed NanoKVM Home Assistant Integration

**Developer Note:** This document is for all contributors and coding agents
working on this integration. Keep it up-to-date with current architecture and
implementation details.

This document provides a developer-focused overview of the Sipeed NanoKVM
Home Assistant integration, detailing its architecture and code structure.

## Project Overview

This project is a custom integration for [Home Assistant][ha-url], allowing
users to control and monitor a [Sipeed NanoKVM][nanokvm-url] device.
It communicates with the NanoKVM HTTP API to expose features as entities
and services in Home Assistant.

The integration is built on the `nanokvm` Python library, which handles
the low-level API communication.

## Code Structure

The integration follows the standard structure for a Home Assistant
`custom_component`.

- `custom_components/nanokvm/`: Root directory for the integration.

### Core Files

- **`__init__.py`**: Main entry point.
  - **`async_setup_entry`**: Initializes the integration from a config entry.
    Creates/authenticates the client, fetches initial device info, creates the
    coordinator, forwards setup to platform files, and registers services.
  - **`async_unload_entry`**: Unloads platforms, disconnects SSH collector, and
    unregisters services when the last entry is removed.

- **`coordinator.py`**: Hosts `NanoKVMDataUpdateCoordinator`.
  - Central polling logic (`_async_update_data`) and API fetch helpers.
  - Handles reauthentication, storage-state fetches, and SSH metric refresh.

- **`entity.py`**: Defines `NanoKVMEntity` base class.
  - Shared entity behavior (`unique_id`, `device_info`) for all platforms.

- **`services.py`**: Service schemas, registration, and handlers.
  - Implements all `nanokvm.*` service behavior and unregister logic.

- **`config_flow.py`**: Manages the user configuration flow in Home Assistant.
  - Implements `ConfigFlow` for manual setup and zeroconf discovery.
  - Includes `validate_input` to verify connectivity and authentication before
    creating the config entry.
  - Handles auth step, static-host option, and legacy/new unique-id matching.

- **`const.py`**: Central repository for shared constants (domain, service
  names, attributes, defaults, icons, and signal names).

- **`utils.py`**: Shared helpers for host normalization and SSH host extraction.

- **`ssh_metrics.py`**: SSH metrics collection implementation used by the
  coordinator.

- **`manifest.json`**: Integration metadata.
  - Domain, name, version, dependencies (including `zeroconf`).
  - PyPI requirement (`nanokvm`).
  - `iot_class` as `local_polling`.
  - `zeroconf` discovery trigger.

### Entity Platforms

The integration is split into platform files, each responsible for one Home
Assistant entity type:

- `binary_sensor.py`
- `button.py`
- `camera.py`
- `camera_webrtc.py` (WebRTC helper used by the camera platform)
- `select.py`
- `sensor.py`
- `switch.py`

Each platform follows a similar pattern:

1. **Entity descriptions**:
   A tuple of dataclass instances (for example,
   `NanoKVMSwitchEntityDescription`) declaratively defines entities.
2. **`value_fn`**:
   The description includes a lambda/function that reads entity state from
   coordinator data.
3. **Action functions**:
   For actionable entities (`SwitchEntity`, `ButtonEntity`, etc.), descriptions
   include callables like `turn_on_fn` or `press_fn`.
4. **`async_setup_entry`**:
   Platform setup iterates entity descriptions and creates entity instances.
5. **Entity class**:
   Entity classes inherit a Home Assistant base class plus `NanoKVMEntity`.
   Action methods wrap client calls in `async with self.coordinator.client:`
   for correct session handling.

### Services

- **`services.yaml`**:
  Defines integration services, descriptions, and fields for Home Assistant UI.
  Service implementations are in `services.py`.

## Key Concepts

- **`NanoKVMClient` lifecycle management**:
  `NanoKVMClient` must be used as an async context manager so internal
  `aiohttp.ClientSession` handling is correct.
- **Coordinator pattern**:
  `DataUpdateCoordinator` provides one polling path and shared state for all
  entities.
- **Declarative entities**:
  Dataclass-based entity descriptions keep entity definitions compact.
- **`nanokvm` library boundary**:
  API transport/parsing logic lives in the external library, not this
  integration.
- **Zeroconf discovery**:
  The integration supports local-network discovery via mDNS/zeroconf.

## Local CI/CD (Pre-Push)

Run these checks locally before pushing:

1. Python lint:
   - `ruff check custom_components/nanokvm`
   - If needed: `.\venv\Scripts\python -m ruff check custom_components/nanokvm`
2. Validate metadata JSON:
   - `Get-Content hacs.json | ConvertFrom-Json > $null`
   - `Get-Content custom_components/nanokvm/manifest.json | ConvertFrom-Json > $null`
3. Local Home Assistant smoke test:
   - `docker compose up -d --build`
   - `docker compose logs --tail=200`
   - `docker compose down`

## Required GitHub Workflows

- HACS: `.github/workflows/hacs.yaml`
- Hassfest: `.github/workflows/hassfest.yaml`

Both should be green on the PR branch before merge/release.

[ha-url]: https://www.home-assistant.io/
[nanokvm-url]: https://github.com/sipeed/NanoKVM
