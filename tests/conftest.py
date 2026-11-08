"""Test setup: the engine is imported as the top-level package 'hl_desk' from the repo root.

The tests use recorded public data (tests/fixtures/recorded) and synthetic
inputs only. A guard fails any test that tries to reach a non-local address,
so the suite runs with the network off.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_real_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _is_local(addr) -> bool:
    host = addr[0] if isinstance(addr, tuple) else addr
    return isinstance(host, str) and (host in ("127.0.0.1", "::1", "localhost") or host.startswith("/"))


def _guard_connect(self, addr):
    if self.family == socket.AF_UNIX or _is_local(addr):
        return _real_connect(self, addr)
    raise AssertionError(f"test tried to reach the network: {addr!r}")


def _guard_create_connection(addr, *a, **k):
    if _is_local(addr):
        return _real_create_connection(addr, *a, **k)
    raise AssertionError(f"test tried to reach the network: {addr!r}")


@pytest.fixture(autouse=True)
def network_off(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(socket, "create_connection", _guard_create_connection)
    yield
