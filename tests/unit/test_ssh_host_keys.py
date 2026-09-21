"""Tests for SSH host-key probing and persistence."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import custom_components.nanokvm.ssh_host_keys as host_keys_module
from custom_components.nanokvm.ssh_host_keys import (
    SSHHostKey,
    async_probe_host_key,
    async_write_known_hosts,
    known_hosts_path,
)


class _FakeKey:
    """Minimal Paramiko public-key boundary."""

    def __init__(self, key_type: str = "ssh-ed25519", raw: bytes = b"key") -> None:
        self.key_type = key_type
        self.raw = raw

    def get_name(self) -> str:
        """Return the OpenSSH key type."""
        return self.key_type

    def get_base64(self) -> str:
        """Return the OpenSSH public-key payload."""
        return base64.b64encode(self.raw).decode()

    def asbytes(self) -> bytes:
        """Return the canonical key bytes used for fingerprints."""
        return self.raw


class _FakeTransport:
    """Handshake-only transport that records authentication boundaries."""

    instances: list[_FakeTransport] = []

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.start_client_calls = 0
        self.closed = False
        self.key = _FakeKey(raw=b"known-public-key")
        self.__class__.instances.append(self)

    def start_client(self, *, timeout: float) -> None:
        """Record the unauthenticated SSH handshake."""
        self.start_client_calls += 1
        assert timeout == 10

    def get_remote_server_key(self) -> _FakeKey:
        """Return the server key negotiated by the handshake."""
        return self.key

    def close(self) -> None:
        """Record transport cleanup."""
        self.closed = True


@pytest.mark.asyncio
async def test_probe_host_key_only_performs_unauthenticated_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe reads a key without exposing a password-authentication path."""
    _FakeTransport.instances = []
    monkeypatch.setattr(host_keys_module.paramiko, "Transport", _FakeTransport)

    result = await async_probe_host_key("nanokvm.local")

    assert result == SSHHostKey(
        host="nanokvm.local",
        known_hosts_line="nanokvm.local ssh-ed25519 a25vd24tcHVibGljLWtleQ==",
        fingerprint=(
            "SHA256:"
            + base64.b64encode(hashlib.sha256(b"known-public-key").digest())
            .decode()
            .rstrip("=")
        ),
    )
    transport = _FakeTransport.instances[0]
    assert transport.address == ("nanokvm.local", 22)
    assert transport.start_client_calls == 1
    assert transport.closed is True


def test_known_hosts_path_is_scoped_to_the_entry(tmp_path: Path) -> None:
    """Each config entry receives a separate trust file."""
    assert known_hosts_path(tmp_path, "entry-1") == (
        tmp_path / "nanokvm" / "ssh" / "entry-1.known_hosts"
    )
    assert known_hosts_path(tmp_path, "entry-2") != known_hosts_path(tmp_path, "entry-1")


def test_write_known_hosts_rejects_multiline_content(tmp_path: Path) -> None:
    """Host-key storage cannot be used to inject extra known-hosts entries."""
    with pytest.raises(ValueError, match="exactly one non-empty line"):
        host_keys_module._write_known_hosts(
            tmp_path / "known_hosts",
            "host ssh-ed25519 key\nother-host ssh-ed25519 key",
        )


@pytest.mark.asyncio
async def test_write_known_hosts_creates_owner_only_file(tmp_path: Path) -> None:
    """Persisting a public key creates a private filesystem entry safely."""
    hass = MagicMock()
    hass.config.path.return_value = str(tmp_path)
    key = SSHHostKey(
        host="nanokvm.local",
        known_hosts_line="nanokvm.local ssh-ed25519 ZHVtbXk=",
        fingerprint="SHA256:dummy",
    )

    path = await async_write_known_hosts(hass, "entry-1", key.known_hosts_line)

    assert path == tmp_path / "nanokvm" / "ssh" / "entry-1.known_hosts"
    assert path.read_text() == key.known_hosts_line + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
