"""The owner service: how other Hermes processes of the same profile reach the state owner.

Hermes can serve one profile from several processes (the gateway or dashboard
process and a plugin host). Only one of them owns the collector (it holds
collector.lock). That process also listens on a Unix socket, owner.sock, in
the plugin's profile-scoped data directory, created with mode 0600 so only
the same OS user can connect. Any other process asks the owner for a
snapshot by ID, the owner's status, a wallet's executions from a retained
snapshot, or a stop or restart. There is no network listener and no copy of
the snapshots on disk, so a process can never mistake an old copy for
current data: when no owner answers, nothing is running.

Protocol: one JSON object per line in each direction, one request per
connection. Requests: {"op": "ping"|"status"|"snapshot"|"wallet_trades"|
"candles"|"replay"|"stop"|"restart", ...}. Replies are JSON objects with "ok".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path

SOCK_NAME = "owner.sock"
MAX_LINE = 64 * 1024 * 1024
CLIENT_TIMEOUT_S = 5.0
MAX_SOCK_PATH = 100          # Unix socket paths are limited to about 104-108 bytes


def _fallback_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"hl-desk-{os.getuid()}"


def sock_path(data_dir: Path) -> Path:
    """owner.sock in the profile's data directory, or, when that path is too long for a Unix socket, a short
    per-profile name in a private per-user directory under the system temp folder (the same for every process)."""
    p = Path(data_dir) / SOCK_NAME
    if len(str(p)) <= MAX_SOCK_PATH:
        return p
    digest = hashlib.sha256(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:20]
    return _fallback_dir() / f"{digest}.sock"


def _secure_fallback_dir(d: Path) -> str | None:
    """Create the fallback directory 0700 and check it belongs to this user; an error message otherwise."""
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = d.stat()
    except OSError as exc:
        return f"can't create the owner socket directory {d}: {exc}"
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        return f"the owner socket directory {d} isn't private to this user"
    return None


class OwnerServer:
    """Runs on the collector's own event loop; `api(request) -> reply` must be quick and non-blocking."""

    def __init__(self, path: Path, api) -> None:
        self.path = Path(path)
        self.api = api
        self.server: asyncio.AbstractServer | None = None
        self.error: str | None = None

    async def start(self) -> None:
        if len(str(self.path)) > MAX_SOCK_PATH:
            self.error = (f"owner socket path is too long for a Unix socket ({len(str(self.path))} characters) even "
                          "under the system temp folder; set TMPDIR to a shorter directory")
            return
        if self.path.parent == _fallback_dir():
            self.error = _secure_fallback_dir(self.path.parent)
            if self.error:
                return
        try:
            self.path.unlink()   # a socket left by a dead owner; this process holds the lock now
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), CLIENT_TIMEOUT_S)
            try:
                req = json.loads(line or b"{}")
                reply = self.api(req if isinstance(req, dict) else {})
            except Exception as exc:
                reply = {"ok": False, "error": f"owner error: {type(exc).__name__}: {exc}"}
            writer.write(json.dumps(reply, separators=(",", ":"), default=str).encode("utf-8") + b"\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def request(path: Path, req: dict, timeout: float = CLIENT_TIMEOUT_S) -> dict | None:
    """Ask the owner. Returns None when no owner is listening. Blocking: call off the event loop."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            return None
        s.sendall(json.dumps(req).encode("utf-8") + b"\n")
        chunks, total = [], 0
        while True:
            b = s.recv(1 << 20)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if b.endswith(b"\n") or total > MAX_LINE:
                break
        data = b"".join(chunks)
        if not data:
            return None
        return json.loads(data)
    except (socket.timeout, ValueError, OSError) as exc:
        return {"ok": False, "error": f"the owner didn't answer: {type(exc).__name__}: {exc}"}
    finally:
        s.close()
