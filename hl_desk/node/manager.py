"""Owns the node feed clients of one plugin runtime: one LIVE client (a socket or relay) and, only when
the user starts it, one REPLAY client playing a recording. Both run on the collector's event loop, the
loop that builds snapshots, so a snapshot never sees a half-applied message.

Live and replay never mix: each has its own client, partitioned by its own source, mode, producer session
and replay session. Replay views are labelled REPLAY and kept apart from live prices, current metrics and
commentary; a replay is never started automatically and never stands in for a live source.
"""

from __future__ import annotations

import asyncio
import time

from . import calibration as calibration_mod
from . import panels
from .bursts import NodeBurstRule
from .client import FeedClient
from .demand import Demand
from .replay import Recording, ReplayPlayer
from .transport import FixedDemand, parse_source, run_feed, run_relay

DEADLINE_CHECK_S = 0.25
LEASE_MAX_S = 60.0          # a tool call waits at most 20 s for data: no lease outlives this
REPLAY_TICK_S = 0.05


class NodeManager:
    def __init__(self, cfg, key_getter=None) -> None:
        self.cfg = cfg
        self.key_getter = key_getter or (lambda: None)
        self.markets = list(cfg.markets)
        self.opts = {"liq_burst_count": cfg.liq_burst_count, "liq_burst_usd": cfg.liq_burst_usd}
        src = parse_source(cfg.node_feed or "")
        self.live: FeedClient | None = FeedClient(cfg.node_feed, "LIVE") if src else None
        if self.live is not None:
            self.live.listeners.append(lambda ev: panels.on_client_event(ev, self.opts))
            self.attach_bursts(self.live)
        # A relay (a ws(s):// URL, the hosted default included) is subscribed to only while node data is used: the
        # demand (panels on screen, the 60 s linger, background alerts) drives one subscription, and a tool call that
        # needs another market in full gets its own short one (lease/release). A local socket serves every market.
        self.relay_mode = src is not None and src[0] == "ws"
        self.demand: Demand | None = (Demand(self.markets, alerts_in_background=getattr(cfg, "node_alerts", False))
                                      if self.relay_mode else None)
        self.tool_clients: dict[str, dict] = {}
        self.leases: dict[int, str | None] = {}
        self.lease_expiry: dict[int, float] = {}     # a backstop: a lease never released ends on its own
        self._lease_seq = 0
        self._key: str | None = None
        self.replay_client: FeedClient | None = None
        self.player: ReplayPlayer | None = None
        self.replay_error: str | None = None
        self.key_error: str | None = None          # the error type only: never the message or the key
        self._stop: asyncio.Event | None = None
        self._tasks: list = []
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach_bursts(self, client: FeedClient) -> FeedClient:
        """Node-mode bursts for every market of this client: the calibration's rule, with each market's manual
        threshold (node_burst_min_usd) or its automatic one, ranked against the calibration's reference. The
        calibration is loaded here, on first use by a node source; public mode never loads it."""
        cal, cfg = calibration_mod.load(), self.cfg

        def factory(market, c):
            return NodeBurstRule(market, cal.threshold(market, cfg.node_burst_min_usd), ranker=cal.rank,
                                 provenance=lambda: panels.provenance(c, market),
                                 target_pct=cfg.wallet_target_share_pct, cap=cfg.wallet_cap)
        client.burst_rule_factory = factory
        for m, mf in client.markets.items():
            if mf.flow.bursts is None:
                mf.flow.bursts = factory(m, client)
        return client

    # --- lifecycle (on the collector loop) ---------------------------------------
    def tasks(self, stop: asyncio.Event) -> list:
        self._stop = stop
        self.loop = asyncio.get_running_loop()
        out = [asyncio.create_task(self._deadlines(stop)), asyncio.create_task(self._replay_loop(stop))]
        if self.live is not None:
            key = None
            if parse_source(self.cfg.node_feed)[0] == "ws":
                key = self._read_key()
            self._key = key
            if self.relay_mode:
                out.append(asyncio.create_task(run_relay(self.live, self.cfg.node_feed, self.demand, stop, key)))
            else:
                out.append(asyncio.create_task(run_feed(self.live, self.cfg.node_feed, self.markets, stop, key)))
        self._tasks = out
        return out

    async def _deadlines(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                now = time.monotonic_ns()
                for c in (self.live, self.replay_client, *(t["client"] for t in self.tool_clients.values())):
                    if c is not None:
                        c.check_deadlines(now)
                for lid in [k for k, t in self.lease_expiry.items() if t < time.monotonic()]:
                    self.release(lid)                  # a call that never released (an error, a timeout) lets go here
                try:
                    await asyncio.wait_for(stop.wait(), timeout=DEADLINE_CHECK_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in self.tool_clients.values():       # a tool's own subscription ends with the collector
                t["stop"].set()

    # --- demand (handoff 1d step 7) ------------------------------------------------
    def panel_seen(self, market: str | None) -> None:
        """A node panel is on screen with this market open (from the dashboard's poll; any thread)."""
        if self.demand is not None:
            self.demand.panel(market)

    def lease(self, market: str | None) -> dict | None:
        """A tool call needs this market's node data now (on the loop). Served by the panel subscription when that
        carries the market in full; otherwise by a short subscription of its own, released by release()."""
        if not self.relay_mode or self.live is None or market not in self.markets:
            return None
        self._lease_seq += 1
        lid = self._lease_seq
        self.lease_expiry[lid] = time.monotonic() + LEASE_MAX_S
        if self.live.views.get(market) == "full" and self.demand.want(time.monotonic()) is not None:
            self.leases[lid] = None
            return {"id": lid, "via": "the panel's subscription"}
        tc = self.tool_clients.get(market)
        if tc is None:
            client = FeedClient(self.cfg.node_feed, "LIVE")
            client.listeners.append(lambda ev: panels.on_client_event(ev, self.opts))
            self.attach_bursts(client)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(client, self.cfg.node_feed, FixedDemand(market, []), stop, self._key))
            tc = self.tool_clients[market] = {"client": client, "stop": stop, "task": task, "leases": 0}
        tc["leases"] += 1
        self.leases[lid] = market
        return {"id": lid, "via": "a subscription of its own"}

    def release(self, lid) -> None:
        """The tool call has answered: its own subscription ends when no other call holds it (on the loop)."""
        self.lease_expiry.pop(lid, None)
        market = self.leases.pop(lid, None) if isinstance(lid, int) else None
        tc = self.tool_clients.get(market) if market is not None else None
        if tc is None:
            return
        tc["leases"] -= 1
        if tc["leases"] <= 0:
            tc["stop"].set()
            del self.tool_clients[market]

    def node_bursts_for(self, market: str) -> bool:
        """Whether node-mode bursts replace public-trades bursts for this market now. A local socket (Part B): whenever
        it is configured. A relay: only while it is connected and the market's node fills are valid, so a paused,
        unreachable, full or stopped relay leaves the public-trades bursts running."""
        if self.live is None:
            return False
        if not self.relay_mode:
            return True
        mf = self.live.markets.get(market)
        return (self.live.connection.get("state") == "connected" and mf is not None and mf.fam["flow"].valid)

    def client_for(self, market: str):
        """The live client whose state answers for this market: a tool's own subscription while the panel
        subscription doesn't carry the market in full."""
        if self.relay_mode and self.live is not None and self.live.views.get(market) != "full":
            tc = self.tool_clients.get(market)
            if tc is not None:
                return tc["client"]
        return self.live

    async def _replay_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.player is not None:
                try:
                    self.player.tick()
                except Exception as exc:          # a broken recording stops the replay, visibly
                    self.replay_error = f"replay stopped: {type(exc).__name__}: {exc}"
                    self.player.state = "ended"
            try:
                await asyncio.wait_for(stop.wait(), timeout=REPLAY_TICK_S)
            except asyncio.TimeoutError:
                pass

    # --- replay control (call on the loop; see call()) -----------------------------
    def replay_command(self, op: str, arg=None) -> dict:
        if op == "start":
            path = arg or self.cfg.node_replay_file
            if not path:
                return {"ok": False, "error": "no recording: set node_replay_file or pass a path"}
            try:
                rec = Recording.load(path)
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"can't read the recording: {exc}"}
            self.replay_client = FeedClient(f"recording:{rec.header.get('recording_id')}", "REPLAY")
            self.replay_client.listeners.append(lambda ev: panels.on_client_event(ev, self.opts))
            self.attach_bursts(self.replay_client)
            self.player = ReplayPlayer(rec, self.replay_client)
            self.replay_error = None
            self.player.start()
        elif self.player is None:
            return {"ok": False, "error": "no replay is loaded; start one first"}
        elif op == "restart":
            self.player.start()
        elif op == "pause":
            self.player.pause()
        elif op == "resume":
            self.player.resume()
        elif op == "seek":
            self.player.seek(int(arg))
        elif op == "speed":
            self.player.set_speed(float(arg))
        elif op == "stop":
            self.player = None
            self.replay_client = None
            return {"ok": True, "replay": None}
        else:
            return {"ok": False, "error": f"unknown replay command {op!r}"}
        return {"ok": True, "replay": self.player.status()}

    def call(self, fn, timeout: float = 10.0):
        """Run fn() on the collector loop from another thread and return its result."""
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError("the collector isn't running")
        fut = asyncio.run_coroutine_threadsafe(_wrap(fn), self.loop)
        return fut.result(timeout)

    # --- views (on the collector loop) ---------------------------------------------
    def live_client(self):
        """The live client, only while its source really is live (a configured feed can turn out to be a replayer)."""
        return self.live if (self.live is not None and self.live.mode == "LIVE") else None

    def views(self, now_wall_ms: int, now_mono_ns: int) -> dict:
        out = {}
        feed_is_replay = self.live is not None and self.live.mode == "REPLAY"
        for m in self.markets:
            live = (panels.market_view(self.client_for(m), m, self.opts, now_wall_ms, now_mono_ns)
                    if self.live is not None and not feed_is_replay else None)
            if feed_is_replay:
                live = {"available": False, "reason": "the configured node_feed serves a REPLAY, shown in the replay area "
                                                      "and never as live data"}
            rep_client = self.replay_client or (self.live if feed_is_replay else None)
            rep = (panels.market_view(rep_client, m, self.opts, now_wall_ms, now_mono_ns)
                   if rep_client is not None else None)
            out[m] = {"live": live, "replay": rep}
        return out

    def relay_status(self) -> dict | None:
        if not self.relay_mode or self.live is None:
            return None
        hb = self.live.relay or {}
        return {"hosted_default": bool(getattr(self.cfg, "node_feed_is_hosted_default", False)),
                "views": dict(self.live.views),
                "relay_heartbeat": {k: hb.get(k) for k in ("state", "publisher", "message", "recv_wall_ms") if k in hb}
                                   or None,
                "demand": self.demand.status(time.monotonic()),
                "tool_subscriptions": {m: {"connection": dict(t["client"].connection), "leases": t["leases"]}
                                       for m, t in self.tool_clients.items()}}

    def status(self) -> dict:
        return {"live": None if self.live is None else {
                    "source": self.live.source, "connection": dict(self.live.connection),
                    "epoch": self.live.epoch, "session": self.live.session,
                    "counters": dict(self.live.counters), "skew": dict(self.live.skew),
                    "relay": self.relay_status()},
                "replay": None if self.player is None else self.player.status() | {"error": self.replay_error},
                "configured": {"node_feed": self.cfg.node_feed or None,
                               "node_replay_file": self.cfg.node_replay_file or None,
                               "key": self._key_state()},
                "bursts": self.burst_config()}

    def burst_config(self) -> dict:
        """Node-mode burst thresholds per market (manual or automatic) and the calibration they come from."""
        if self.live is None and self.replay_client is None:
            return {"mode": "public", "note": "bursts come from public trades (burst_min_usd); node-mode bursts and "
                                              "their calibration are used only with a node feed or a replay"}
        cal = calibration_mod.load()
        return {"mode": "node", "calibration": None if cal.data is None else {
                    "version": cal.data["version"], "dated": cal.data["dated"], "measured": cal.measured,
                    "measured_utc": cal.data["measured_utc"], "target_per_day": cal.data["target_per_day"],
                    "file": cal.path.name, "reference_loaded": cal.reference is not None},
                "calibration_error": cal.error, "reference_error": cal.reference_error,
                "thresholds": {m: cal.threshold(m, self.cfg.node_burst_min_usd) for m in self.markets},
                "manual_setting": "node_burst_min_usd"}

    def _read_key(self) -> str | None:
        try:
            key = self.key_getter()
        except Exception as exc:                   # e.g. Hermes's UnscopedSecretError: shown, never swallowed
            self.key_error = type(exc).__name__
            return None
        self.key_error = None
        return key

    def _key_state(self) -> str:
        if self._read_key():
            return "set"
        return f"unreadable ({self.key_error}); connecting without it" if self.key_error else "not set"


async def _wrap(fn):
    return fn()
