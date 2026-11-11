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
import inspect
import json
import os
import random
import time
import urllib.parse

from .proto import OWNER_POS_SUFFIX

LINE_LIMIT = 64 * 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# A relay accepts at most 30 view switches a minute per connection (profile hl-relay-2): switches are spaced so a
# user clicking through markets never reaches it; the last choice wins.
MIN_SWITCH_INTERVAL_S = 2.5
DEMAND_CHECK_S = 0.25
IDLE_REASON = ("node data paused: no node panel has been on screen for 60 s and nothing else needs it; it resumes from "
               "a fresh snapshot when a node panel is shown again")
# What the relay's refusals and closes mean, in the words the node panels show (the relay's own message is preferred).
REFUSALS = {
    "stopped": "The node-data relay is switched off by its operator. Public data keeps working.",
    "at_capacity": "The node-data relay is full right now. Public data keeps working; try again later.",
    "per_ip_limit": "Too many node-data connections from your network. Close another Hermes window or try later.",
    "rate_limited": "Too many connection attempts. Wait a minute and try again.",
}


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


def source_label(spec) -> str:
    """The feed source as it is served and shown: never a local path, so no screenshot, video or chat answer carries
    the machine's user name or folder layout (the Nov 9 compliance audit). A Unix socket reads "local socket
    (<file name>)", a relay URL "relay <host>"; anything else (a test label) is given back as it is."""
    src = parse_source(str(spec)) if isinstance(spec, str) else None
    if src is None:
        return str(spec)
    if src[0] == "unix":
        return f"local socket ({os.path.basename(src[1]) or 'socket'})"
    from urllib.parse import urlparse
    host = urlparse(src[1]).hostname
    return f"relay {host}" if host else "relay"


def handshake(client, markets: list[str]) -> dict:
    names = list(markets)
    if client.cfg.get("owner_positions", True):
        # The owner position streams (PROTOCOL.md 4.8): named here, or the feed serves none; a producer without them
        # lists the names back as unknown and the plugin says the feed carries no positions.
        names += [m + OWNER_POS_SUFFIX for m in markets]
    hs = {"v": 2, "markets": names, "start": "snapshot"}
    if client.epoch is not None:
        nxt = {m: mf.last_seq + 1 for m, mf in client.markets.items() if mf.last_seq is not None and m in markets}
        for m, mf in client.markets.items():
            if m in markets and mf.opos.offered and mf.opos.last_seq is not None and m + OWNER_POS_SUFFIX in names:
                nxt[m + OWNER_POS_SUFFIX] = mf.opos.last_seq + 1
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
            client.unreadable_line("a feed line that isn't JSON")
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


# --- a hosted relay (profile hl-relay-2: handoff 1d step 7) ------------------------------------------------------
def check_relay_url(url: str) -> str | None:
    """Why a relay URL can't be used, or None. The relay is bound to its own HTTPS/WSS origin: wss:// with the
    system's certificate checks (ws:// only to this machine), no user name or password in the URL."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return "the relay URL can't be parsed"
    if u.scheme not in ("wss", "ws") or not u.hostname:
        return "the relay URL must be wss://<host>/..."
    if u.username is not None or u.password is not None:
        return "a relay URL with a user name or password isn't accepted (an access key goes in the node_key setting)"
    if u.scheme == "ws" and u.hostname not in LOOPBACK_HOSTS:
        return "ws:// is accepted only for a relay on this machine; use wss:// (TLS with certificate checks)"
    if u.hostname.endswith(".invalid"):
        return ("the hosted relay's address isn't set in this version yet; public mode keeps working (set node_feed "
                "to a relay URL of your own, or wait for an update)")
    return None


def relay_handshake(client, full: str | None, execs: list[str]) -> dict:
    """The handshake for a relay view: `markets` = the one market in full, `execs` = the markets whose executions are
    wanted. A market is resumed only where the client's state can continue in the view it asks for: a full market
    only if it was received in full with a valid book (progress made through filtered ranges never stands in for
    full data, so without a resume the relay starts it from a snapshot); an executions market wherever its sequence
    is known (the relay honours it only if it still holds everything since)."""
    streams = streams_on(client)
    hs = {"v": 2, "markets": full_names(full, streams), "execs": list(execs), "start": "snapshot"}
    if client.epoch is not None:
        nxt = {}
        for m, mf in client.markets.items():
            if m == full and streams and client.views.get(m + OWNER_POS_SUFFIX) == "full" \
                    and mf.opos.offered and mf.opos.last_seq is not None:
                nxt[m + OWNER_POS_SUFFIX] = mf.opos.last_seq + 1       # the stream resumes on its own sequence
            if mf.last_seq is None:
                continue
            if m == full:
                if client.views.get(m) == "full" and mf.fam["book"].valid:
                    nxt[m] = mf.last_seq + 1
            elif m in execs:
                nxt[m] = mf.last_seq + 1
        if nxt:
            hs["resume"] = {"epoch": client.epoch, "next": nxt}
    return hs


def streams_on(client) -> bool:
    """Whether to ask a relay for the open market's position stream (PROTOCOL.md 4.8): the owner_positions setting
    (on by default), as for a local socket."""
    return bool(client.cfg.get("owner_positions", True))


def full_names(full: str | None, streams: bool) -> list[str]:
    """The `markets` of a relay handshake or switch: the open market and, with streams, its position stream (which
    the relay serves only with its market's full data, counted with it as one full market)."""
    if not full:
        return []
    return [full, full + OWNER_POS_SUFFIX] if streams else [full]


def switch_message(full: str | None, execs: list[str], streams: bool = False) -> dict:
    return {"v": 2, "switch": {"markets": full_names(full, streams), "execs": list(execs)}}


class RelayEnded(Exception):
    """The relay closed the connection normally."""


class FixedDemand:
    """A demand that doesn't change (a tool call's own short subscription)."""

    def __init__(self, full: str | None, execs: list[str]) -> None:
        self.value = (full, list(execs))

    def want(self, now: float):
        return self.value


def _connect_relay(url: str, key: str | None):
    """A WebSocket connection to the relay URL as given: certificate and host name checked by the system's trust
    store, no proxy taken from the environment, and no redirect ever followed (so an access key is only ever sent to
    the configured origin)."""
    from websockets.asyncio.client import connect

    class _NoRedirect(connect):
        def process_redirect(self, exc):          # websockets >= 13: never follow a redirect
            return exc

    kw = {"additional_headers": {"Authorization": f"Bearer {key}"} if key else None, "max_size": LINE_LIMIT,
          "open_timeout": 15, "ping_interval": 20, "ping_timeout": 20, "compression": "deflate"}
    if "proxy" in inspect.signature(connect.__init__).parameters:    # websockets >= 15
        kw["proxy"] = None
    return _NoRedirect(url, **kw)


def _refusal(exc) -> tuple[dict, float | None]:
    """The connection state for a relay's HTTP refusal, with its Retry-After (seconds) when given."""
    resp = exc.response
    code, message = None, None
    try:
        body = json.loads(resp.body.decode("utf-8", "replace")) if resp.body else {}
        if isinstance(body, dict):
            code = body.get("refused") if isinstance(body.get("refused"), str) else None
            message = body.get("message") if isinstance(body.get("message"), str) else None
    except (ValueError, AttributeError):
        pass
    code = code or resp.headers.get("X-Relay-Refusal")
    if 300 <= resp.status_code < 400:
        reason = (f"the relay answered with a redirect (HTTP {resp.status_code}); node mode never follows one, so "
                  "check the node_feed setting")
    else:
        reason = (message or REFUSALS.get(code or "") or f"refused by the relay (HTTP {resp.status_code})")[:300]
    retry = None
    try:
        retry = float(resp.headers.get("Retry-After")) if resp.headers.get("Retry-After") else None
    except ValueError:
        retry = None
    state = "stopped" if code == "stopped" else ("full" if code == "at_capacity" else "refused")
    return {"state": state, "since_wall_ms": int(time.time() * 1000), "reason": reason, "refused": code,
            "http_status": resp.status_code}, retry


def _closed(client, exc) -> str:
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    text = (getattr(rcvd, "reason", "") or "")[:200]
    if code == 4001:
        relay = client.relay or {}
        msg = relay.get("message") if isinstance(relay.get("message"), str) else REFUSALS["stopped"]
        client.connection = {"state": "stopped", "since_wall_ms": int(time.time() * 1000), "reason": msg,
                             "close_code": code}
        return msg
    if code == 4008:
        return "dropped by the relay as a slow subscriber; reconnecting with a resume"
    if code == 1001:
        return "the relay is restarting"
    if code is None:
        return f"the connection to the relay was lost ({type(exc).__name__})"
    return f"the relay closed the connection (code {code}{': ' + text if text else ''})"


async def run_relay(client, url: str, demand, stop: asyncio.Event, key: str | None = None,
                    clock=time.monotonic) -> None:
    """Keep one relay subscription matching the demand: connect while node data is wanted, switch the view when
    it changes (spaced by MIN_SWITCH_INTERVAL_S), close when nothing wants it (the next connection then starts every
    market from a fresh snapshot), reconnect with backoff after a loss or a refusal (the relay's reason stays shown)."""
    bad = check_relay_url(url)
    if bad:
        client.connection = {"state": "not configured", "since_wall_ms": None, "reason": bad}
        return
    backoff = 1.0
    while not stop.is_set():
        want = demand.want(clock())
        if want is None:
            if client.connection.get("state") != "idle":
                if client.epoch is not None or client.markets:
                    client.reset("node data paused (nothing used it)")
                client.connection = {"state": "idle", "since_wall_ms": int(time.time() * 1000), "reason": IDLE_REASON}
            try:
                await asyncio.wait_for(stop.wait(), timeout=DEMAND_CHECK_S)
            except asyncio.TimeoutError:
                pass
            continue
        client.connection = {"state": "connecting", "since_wall_ms": int(time.time() * 1000), "reason": None}
        retry_after = None
        try:
            ended = await _relay_session(client, url, demand, stop, key, want, clock)
            backoff = 1.0
            if ended == "idle":
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed, InvalidStatus
            if isinstance(exc, InvalidStatus):
                state, retry_after = _refusal(exc)
                client.on_disconnect(state["reason"], int(time.time() * 1000))
                client.connection = state                    # keep the relay's own reason visible
            elif isinstance(exc, ConnectionClosed):
                reason = _closed(client, exc)
                if client.connection.get("state") == "stopped":
                    state = dict(client.connection)
                    client.on_disconnect(reason, int(time.time() * 1000))
                    client.connection = state
                else:
                    client.on_disconnect(reason, int(time.time() * 1000))
            elif isinstance(exc, RelayEnded):
                client.on_disconnect(str(exc), int(time.time() * 1000))
            else:
                client.on_disconnect(f"can't reach the relay ({type(exc).__name__}: {exc})"[:300],
                                     int(time.time() * 1000))
        if stop.is_set():
            break
        delay = backoff * (0.5 + random.random())
        if client.connection.get("state") in ("stopped", "full", "refused"):
            delay = max(delay, 15.0)
        if retry_after is not None:
            delay = max(delay, min(retry_after, 300.0))
        client.connection["retry_in_s"] = round(delay, 1)
        deadline = clock() + delay
        while not stop.is_set() and clock() < deadline:
            if demand.want(clock()) is None:
                break                                    # nothing wants node data now: go idle, don't retry
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(DEMAND_CHECK_S, max(0.0, deadline - clock())))
            except asyncio.TimeoutError:
                pass
        backoff = min(backoff * 2, 60.0)


async def _relay_session(client, url, demand, stop, key, want, clock) -> str:
    conn = await _connect_relay(url, key)
    client.on_connect(int(time.time() * 1000))
    reader = None
    try:
        current = want
        await conn.send(json.dumps(relay_handshake(client, *current)))
        last_switch = clock()

        async def read():
            async for msg in conn:
                _feed(client, msg)

        reader = asyncio.create_task(read())
        while True:
            done, _ = await asyncio.wait({reader}, timeout=DEMAND_CHECK_S)
            if reader in done:
                reader.result()                          # raises ConnectionClosed with the relay's code
                code = conn.close_code                   # a normal close (1000, 1001) ends the loop quietly
                raise RelayEnded("the relay is restarting" if code == 1001 else
                                 f"the relay closed the connection (code {code})")
            if stop.is_set():
                return "stopped"
            want = demand.want(clock())
            if want is None:
                client.reset("node data paused (nothing used it)")
                client.connection = {"state": "idle", "since_wall_ms": int(time.time() * 1000), "reason": IDLE_REASON}
                return "idle"
            if want != current and clock() - last_switch >= MIN_SWITCH_INTERVAL_S:
                await conn.send(json.dumps(switch_message(*want, streams=streams_on(client))))
                current, last_switch = want, clock()
    finally:
        if reader is not None and not reader.done():
            reader.cancel()
        await conn.close()
