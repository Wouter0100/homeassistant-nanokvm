"""Shared utility helpers for the Sipeed NanoKVM integration."""
from __future__ import annotations


def normalize_host(host: str) -> str:
    """Normalize a host value to an API base URL."""
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"

    if not host.endswith("/api/"):
        host = f"{host}api/" if host.endswith("/") else f"{host}/api/"

    return host


def normalize_mdns(mdns: str) -> str:
    """Normalize mDNS hostnames to include a trailing dot."""
    return mdns if mdns.endswith(".") else f"{mdns}."


def extract_ssh_host(host: str) -> str:
    """Extract SSH host value from integration host configuration."""
    return host.replace("/api/", "").replace("http://", "").replace("https://", "")
