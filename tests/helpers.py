"""Builders for synthetic test inputs (mode SYNTHETIC) and access to the recorded fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from hl_desk.config import DeskConfig
from hl_desk.replay import Replayer, registry_from_fixtures
from hl_desk.state import DeskState

FIX = Path(__file__).resolve().parent / "fixtures" / "recorded"
INFO = FIX / "info"
WS = FIX / "ws" / "session.jsonl"

T0 = 1_800_000_000_000          # ms, a multiple of 15 s
NS = 1_000_000_000
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def use_standalone_storage(monkeypatch, path):
    """Run the plugin's storage as it runs outside Hermes, even when the tests themselves run in Hermes's
    environment: Hermes's storage helper is made unimportable, so HL_DESK_DATA_DIR applies (runtime.default_data_dir)."""
    import sys
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    monkeypatch.setenv("HL_DESK_DATA_DIR", str(path))


def registry():
    return registry_from_fixtures(INFO)


CLOCK_BOUND_S = 0.05     # a fixed, known local clock error bound for deterministic tests


def make_desk(markets=("BTC",), mode="SYNTHETIC", **cfg) -> DeskState:
    cfg.setdefault("clock_bound_s", CLOCK_BOUND_S)
    desk = DeskState(DeskConfig(markets=list(markets), **cfg), mode=mode)
    desk.set_markets([registry().check(m) for m in markets])
    desk.link = "connected"
    return desk


def latest_source_ms(desk) -> int:
    """The latest source time any tracker has seen (or T0): synthetic tests build snapshots 'just after' it."""
    times = [tr.source_ms for ms in desk.markets.values() for tr in ms.trackers.values() if tr.source_ms is not None]
    return max(times) if times else T0


def build(desk, now_ns, wall_ms=None, after_ms=200):
    """Build a snapshot with a realistic wall clock: after_ms past the latest source time unless given."""
    return desk.build_snapshot(now_ns, wall_ms if wall_ms is not None else latest_source_ms(desk) + after_ms)


def trade(coin, t_ms, px, sz, side, buyer, seller, tid):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "time": t_ms,
            "hash": f"0x{tid:064x}", "tid": tid, "users": [buyer, seller]}


def open_segment(ms, start_ms, mono_ns=0, conn=1):
    """Connect and deliver a subscription snapshot whose latest trade is at start_ms."""
    ms.on_connect(conn)
    ms.on_trades([trade(ms.market, start_ms, 100.0, 0.001, "B", C, C, 10_000_000 + conn)], mono_ns, start_ms)


def book_msg(coin, t_ms, bids, asks):
    return {"coin": coin, "time": t_ms,
            "levels": [[{"px": str(p), "sz": str(s), "n": n} for p, s, n in bids],
                       [{"px": str(p), "sz": str(s), "n": n} for p, s, n in asks]]}


def recorded_replayer(markets=None) -> Replayer:
    cfg = DeskConfig(markets=list(markets), clock_bound_s=CLOCK_BOUND_S) if markets else DeskConfig(clock_bound_s=CLOCK_BOUND_S)
    return Replayer.from_file(WS, cfg, registry())


def recorded_records() -> list[dict]:
    with WS.open() as fh:
        return [json.loads(line) for line in fh]


class SnapRT:
    """A stand-in runtime for tools: serves one snapshot and the desk's market states."""

    def __init__(self, snap, desk=None, current=None):
        self.snap, self.desk, self.current = snap, desk, current

    def snapshot(self, sid):
        if sid and sid != self.snap["snapshot_id"]:
            return None, f"snapshot {sid} is no longer retained"
        return self.snap, None

    def status(self):
        return {"collector": {"state": "replay"}}

    def wallet_trades(self, snapshot_id, market, window_min, address):
        """Same drill-down as the real runtime: the snapshot's frozen execution set."""
        from hl_desk.tools import drilldown
        frozen = self.desk.frozen_last if (self.desk is not None and snapshot_id == self.snap["snapshot_id"]) else None
        return drilldown(self.snap, frozen, market, window_min, address)

    def clock_bound(self):
        return CLOCK_BOUND_S, "fixed for tests"

    def fetch_wallet_current(self, user, dex):
        if self.current is None:
            raise RuntimeError("offline")
        return self.current(user, dex)
