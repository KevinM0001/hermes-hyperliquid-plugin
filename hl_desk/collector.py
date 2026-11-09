"""The live collector: one bounded worker thread on Hyperliquid's public websocket.

Started only by hl_desk.runtime.start() (the /hl start command or the tab's
Start button), never on import, registration, doctor or validate. It runs
its own asyncio loop in its own thread, so nothing here touches the gateway
event loop. Bounded: one thread, one websocket connection, at most 20
subscriptions for the 4 default markets, 16 minutes of trades per market,
a fixed snapshot ring. Reconnects back off from 1 s to 30 s.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time

from . import info_client
from .config import DeskConfig
from .feed import dispatch, dispatch_depth, subscriptions
from .markets import MarketRegistry
from .state import DeskState
from .store import SnapshotStore
from .util import dex_of, iso_ms

THREAD_NAME = "hyperliquid-desk-collector"
BACKOFF_S = (1, 2, 4, 8, 16, 30)
PING_EVERY_S = 30.0


def _make_thread(collector: "Collector") -> threading.Thread:
    """The worker thread. Inside Hermes it is created with agent.memory_provider.spawn_context_thread,
    the helper the Hermes docs name for background threads (developer-guide/gateway-internals.md
    'Background threads'; memory-provider-plugin.md: 'never a bare threading.Thread'), so it keeps
    the starting profile's context under a multi-profile gateway. Outside Hermes (tests) a plain
    daemon thread is used. The thread carries its collector and its profile's data directory, so a
    reloaded copy of this module in the same process can find it, but only for the same profile."""
    try:
        from agent.memory_provider import spawn_context_thread
        t = spawn_context_thread(collector._run, name=THREAD_NAME, daemon=True)
    except ImportError:
        t = threading.Thread(target=collector._run, name=THREAD_NAME, daemon=True)
    t.collector = collector
    t.hl_desk_data_dir = str(collector.store.data_dir)
    return t


class Collector:
    def __init__(self, cfg: DeskConfig, store: SnapshotStore, owner_api=None) -> None:
        self.cfg = cfg
        self.store = store
        self.owner_api = owner_api      # answers other processes of this profile (hl_desk.owner)
        self.owner_server = None
        self.desk = DeskState(cfg, mode="LIVE")
        from .config import node_key
        from .node.manager import NodeManager
        self.node = NodeManager(cfg, key_getter=node_key)     # node feed (optional) and explicit replays
        self.desk.node = self.node
        self.thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._stop_requested = threading.Event()   # set by stop() from any thread, even before the loop exists
        self._reconnect_now = False
        self.conn = 0
        self.reconnects = 0
        self.started_ms: int | None = None

    # --- control (called from any thread) -----------------------------------
    def start(self) -> None:
        self.started_ms = int(time.time() * 1000)
        self._set_state("starting")
        self.thread = _make_thread(self)
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop_requested.set()
        loop, ev = self._loop, self._stop
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        if self.thread is not None:
            self.thread.join(timeout)
            return not self.thread.is_alive()
        return True

    def request_reconnect(self) -> None:
        self._reconnect_now = True

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # --- worker ------------------------------------------------------------
    def _set_state(self, state: str, **extra) -> None:
        info = {"state": state, "since": iso_ms(int(time.time() * 1000)), "conn": self.conn,
                "reconnects": self.reconnects, "started": iso_ms(self.started_ms), "thread": THREAD_NAME,
                "run_id": self.desk.run_id}
        if self.owner_server is not None and self.owner_server.error:
            info["owner_service_error"] = (f"{self.owner_server.error}; other Hermes processes of this profile can't "
                                           "reach this collector")
        info.update(extra)
        self.desk.collector = info
        self.desk.link = {"connected": "connected", "stopped": "stopped"}.get(state, "disconnected")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            self._set_state("stopped")
            self._publish()
            loop.close()
            self._loop = None

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        if self._stop_requested.is_set():
            return
        if self.owner_api is not None and self.store.data_dir is not None:
            from .owner import OwnerServer, sock_path
            self.owner_server = OwnerServer(sock_path(self.store.data_dir), self.owner_api)
            await self.owner_server.start()
        try:
            await self._run_feeds()
        finally:
            if self.owner_server is not None:
                await self.owner_server.close()

    async def _run_feeds(self) -> None:
        # Node tasks (a configured node feed, deadline checks, an explicitly started replay) run from the start,
        # on this loop; public metadata is loaded alongside.
        node_tasks = self.node.tasks(self._stop)
        if not await self._load_metadata():
            for t in node_tasks:
                t.cancel()
            await asyncio.gather(*node_tasks, return_exceptions=True)
            return
        tasks = node_tasks + [asyncio.create_task(self._ws_loop()), asyncio.create_task(self._snapshot_loop()),
                              asyncio.create_task(self._depth_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ms in self.desk.markets.values():
            ms.on_disconnect("collector stopped", int(time.time() * 1000))

    async def _load_metadata(self) -> bool:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._set_state("loading metadata")
                dexes = {dex_of(m) for m in self.cfg.markets}
                md = await asyncio.to_thread(info_client.fetch_metadata, self.cfg.info_url, dexes)
                reg = MarketRegistry()
                reg.load_perp_dexs(md["perpDexs"])
                for dex, meta in md["meta"].items():
                    reg.load_meta(dex, meta)
                self.desk.set_markets([reg.check(m) for m in self.cfg.markets])
                self.desk.metadata = {"fetched": iso_ms(md["fetched_ms"]), "dexes": reg.dexes,
                                      "source": "info perpDexs + meta per dex"}
                return True
            except Exception as exc:  # network errors are reported, then retried with backoff
                delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                attempt += 1
                self._set_state("disconnected", last_error=f"metadata: {exc!r}", next_retry_s=delay)
                self._publish()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        return False

    async def _ws_loop(self) -> None:
        from websockets.asyncio.client import connect

        attempt = 0
        while not self._stop.is_set():
            self.conn += 1
            connected_at = None
            try:
                self._set_state("connecting")
                async with connect(self.cfg.ws_url, max_size=2**23, open_timeout=15, close_timeout=5,
                                   ping_interval=20, ping_timeout=20) as ws:
                    connected_at = time.monotonic()
                    for ms in self.desk.markets.values():
                        ms.on_connect(self.conn)
                    for sub in subscriptions(list(self.desk.markets)):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    self._set_state("connected")
                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        if self._reconnect_now:
                            self._reconnect_now = False
                            raise ConnectionResetError("restart requested")
                        if self._stalled():
                            raise TimeoutError("every fast book stale for the stall limit while connected")
                        if time.monotonic() - last_ping > PING_EVERY_S:
                            await ws.send(json.dumps({"method": "ping"}))   # keeps the server from idling us out
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        dispatch(self.desk, msg, time.monotonic_ns(), int(time.time() * 1000))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                for ms in self.desk.markets.values():
                    ms.on_disconnect(reason, int(time.time() * 1000))
                if connected_at is not None and time.monotonic() - connected_at > 60:
                    attempt = 0
                delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] * (0.8 + 0.4 * random.random())
                attempt += 1
                self.reconnects += 1
                self._set_state("disconnected", last_error=reason, next_retry_s=round(delay, 1))
                self._publish()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def _stalled(self) -> bool:
        limit = self.cfg.stall_reconnect_s()
        now = time.monotonic_ns()
        stale = []
        for ms in self.desk.markets.values():
            tr = ms.trackers["book_fast"]
            if tr.progress_mono_ns is None:
                return False   # still warming up
            stale.append((now - tr.progress_mono_ns) / 1e9 > limit)
        return bool(stale) and all(stale)

    async def _depth_loop(self) -> None:
        while not self._stop.is_set():
            if self.desk.link == "connected":
                for coin in list(self.desk.markets):
                    for nsf in (4, 3, 2):
                        body = {"type": "l2Book", "coin": coin, "nSigFigs": nsf}
                        try:
                            resp = await asyncio.to_thread(info_client.post_info, self.cfg.info_url, body, 10.0)
                        except Exception:
                            continue   # the book's own freshness turns stale; nothing else to do
                        dispatch_depth(self.desk, body, resp, time.monotonic_ns(), int(time.time() * 1000))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.depth_poll_s)
            except asyncio.TimeoutError:
                pass

    async def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            self._publish()   # built on this loop, the only thread that mutates the state
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.snapshot_interval_s)
            except asyncio.TimeoutError:
                pass

    def _publish(self) -> dict:
        snap = self.desk.build_snapshot(time.monotonic_ns(), int(time.time() * 1000))
        self.store.publish(snap, frozen=self.desk.frozen_last)
        return snap
