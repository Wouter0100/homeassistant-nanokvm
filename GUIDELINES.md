# Project Guidelines

This document outlines the purpose, standards, and practices for the Sipeed NanoKVM Home Assistant integration project. Adhering to these guidelines ensures consistency, quality, and ease of maintenance.

## Project Purpose

The primary goal of this project is to provide a seamless integration for the [Sipeed NanoKVM](https://github.com/sipeed/NanoKVM) device within Home Assistant. This allows users to monitor and control their NanoKVM's functionalities directly from their Home Assistant dashboard, enabling automation and remote management of the connected computer.

## Core Principles

- **Home Assistant Centric:** The integration must follow the development guidelines and best practices for creating Home Assistant custom components. This includes adhering to the prescribed file structure, configuration flow, and entity models.
- **User-Friendly:** The integration should be easy for users to install, configure, and use. Configuration should be handled through the Home Assistant UI (Config Flow), and entities should be clearly named and categorized.
- **Reliable and Robust:** The integration should be stable and handle potential errors gracefully. This includes managing connection issues with the NanoKVM device and providing informative feedback to the user.

## Development Standards

### Code Style and Structure

- **Python:** The project follows the standard Python coding style (PEP 8).
- **Home Assistant Conventions:** All code must adhere to the Home Assistant developer documentation and conventions for custom components. This includes:
    - **Directory Structure:** The component is located in `custom_components/nanokvm/`.
    - **`manifest.json`:** This file defines the integration's metadata, dependencies, and other essential information.
    - **`const.py`:** All constants, such as domain names, configuration keys, and service names, are defined in this file.
    - **`config_flow.py`:** The configuration process is managed through a config flow, enabling UI-based setup.
    - **Entity Platforms:** Entities are organized into their respective platforms (e.g., `binary_sensor.py`, `sensor.py`, `switch.py`, `button.py`).
- **Dependencies:** Project dependencies are managed through the `requirements` key in `manifest.json`. The primary dependency is the `python-nanokvm` library.

### Naming Conventions

- **Entities:** Entity IDs should be descriptive and follow the format `platform.nanokvm_description` (e.g., `binary_sensor.nanokvm_power_led`).
- **Services:** Service names are defined in `const.py` and `services.yaml`.

### Localization

- The integration supports localization. All user-facing strings should be defined in `strings.json` and translated in the `translations` directory.

### Versioning

- The project version is defined in `manifest.json` and should follow semantic versioning (SemVer).

### Error Handling and Reconnection

- The integration should be resilient to the NanoKVM device being temporarily unavailable (e.g., due to a power cycle).
- The `NanoKVMDataUpdateCoordinator` is responsible for managing the connection and data updates.
- When a `NanoKVMError` occurs, the coordinator will attempt to re-authenticate with the device and then retry fetching the data. This ensures that the connection can be re-established automatically when the device comes back online.
- Standard network errors (e.g., `aiohttp.ClientError`, `asyncio.TimeoutError`) are treated as transient, and Home Assistant's DataUpdateCoordinator will handle the retry logic.

## Continuous Improvement

This `GUIDELINES.md` file is a living document. It should be continuously updated by the AI assistant whenever changes are made to the codebase that introduce new standards, best practices, or important information that should be remembered for future development.

## Contribution Guidelines

- **Issues:** Before starting work on a new feature or bug fix, please check the issue tracker to see if it has already been discussed.
- **Pull Requests:**
    - Create a separate branch for each new feature or bug fix.
    - Ensure your code adheres to the project's standards and conventions.
    - Update the documentation if you are adding or changing functionality.
    - Add or update tests for your changes.
    - Ensure all tests pass before submitting a pull request.

By following these guidelines, we can maintain a high-quality and user-friendly integration for the Sipeed NanoKVM community.
