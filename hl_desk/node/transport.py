"""Connections to a node feed: a local Unix socket, or a relay's WebSocket URL (handoff step 11).

Both carry protocol v2 lines unchanged (PROTOCOL.md 1; a relay passes the producer's lines byte for byte).
The client writes one handshake line, then only reads. After a reconnect it asks to resume each market
from the next sequence it needs, in the same producer session; anything the producer can't honour comes
back as replay_unavailable or a new session, and the client invalidates accordingly.

The optional access key is read only through Hermes (agent.secret_scope.get_secret) by the caller and
passed in; it is sent only to a WebSocket URL, as an Authorization: Bearer header, never in the URL and
never to a local socket. Runs on the collector's own event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

LINE_LIMIT = 64 * 1024 * 1024


def parse_source(spec: str) -> tuple[str, str] | None:
    """('unix', path) or ('ws', url) from the node_feed setting; None when empty or not understood."""
    s = (spec or "").strip()
    if not s:
        return None
    if s.startswith("ws://") or s.startswith("wss://"):
        return ("ws", s)
    if s.startswith("unix:"):
        s = s[5:]
    if s.startswith("~"):
        s = os.path.expanduser(s)
    if s.startswith("/"):
        return ("unix", s)
    return None


def handshake(client, markets: list[str]) -> dict:
    hs = {"v": 2, "markets": list(markets), "start": "snapshot"}
    if client.epoch is not None:
        nxt = {m: mf.last_seq + 1 for m, mf in client.markets.items() if mf.last_seq is not None and m in markets}
        if nxt:
            hs["resume"] = {"epoch": client.epoch, "next": nxt}
    return hs


async def run_feed(client, spec: str, markets: list[str], stop: asyncio.Event, key: str | None = None,
                   on_status=None) -> None:
    """Connect, read, reconnect with backoff until stop is set."""
    src = parse_source(spec)
    if src is None:
        client.connection = {"state": "not configured", "since_wall_ms": None,
                             "reason": f"node_feed {spec!r} is neither a Unix socket path nor a ws(s):// URL"}
        return
    backoff = 1.0
    while not stop.is_set():
        client.connection = {"state": "connecting", "since_wall_ms": int(time.time() * 1000), "reason": None}
        try:
            if src[0] == "unix":
                await _unix(client, src[1], markets, stop)
            else:
                await _ws(client, src[1], markets, stop, key)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:     # connection refused, closed, TLS, refused by the relay
            if client.connection.get("state") == "refused":
                client.on_disconnect(client.connection["reason"], int(time.time() * 1000))
                client.connection["state"] = "refused"          # keep the relay's own reason visible
            else:
                client.on_disconnect(f"{type(exc).__name__}: {exc}", int(time.time() * 1000))
        if stop.is_set():
            break
        delay = backoff * (0.5 + random.random())
        client.connection["retry_in_s"] = round(delay, 1)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 30.0)


def _feed(client, raw: bytes | str) -> None:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    for piece in text.split("\n"):
        if not piece.strip():
            continue
        try:
            obj = json.loads(piece)
        except ValueError:
            client.counters["malformed"] += 1
            continue
        client.on_line(obj, int(time.time() * 1000), time.monotonic_ns())


async def _unix(client, path: str, markets: list[str], stop: asyncio.Event) -> None:
    reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
    client.on_connect(int(time.time() * 1000))
    try:
        writer.write((json.dumps(handshake(client, markets)) + "\n").encode())
        await writer.drain()
        while not stop.is_set():
            line = await reader.readline()
            if not line:
                client.on_disconnect("the producer closed the connection", int(time.time() * 1000))
                return
            _feed(client, line)
    finally:
        writer.close()


async def _ws(client, url: str, markets: list[str], stop: asyncio.Event, key: str | None) -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    headers = {"Authorization": f"Bearer {key}"} if key else None
    try:
        conn = await connect(url, additional_headers=headers, max_size=LINE_LIMIT, open_timeout=15,
                             ping_interval=20, ping_timeout=20)
    except InvalidStatus as exc:      # the relay refused (full, stopped, rate-limited): show its reason
        resp = exc.response
        reason = ""
        try:
            reason = resp.body.decode("utf-8", "replace").strip()[:300] if resp.body else ""
        except Exception:
            pass
        client.connection = {"state": "refused", "since_wall_ms": int(time.time() * 1000),
                             "reason": f"HTTP {resp.status_code}: {reason or 'refused by the relay'}"}
        raise
    client.on_connect(int(time.time() * 1000))
    try:
        await conn.send(json.dumps(handshake(client, markets)))
        while not stop.is_set():
            msg = await conn.recv()
            _feed(client, msg)
    finally:
        await conn.close()
