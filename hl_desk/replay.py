"""Replays recorded public data into a DeskState labeled REPLAY (or SYNTHETIC for constructed data).

Each record carries its recorded monotonic receive time, which the replay
uses as 'now', so freshness is judged exactly as it was live. Replay is
never an automatic fallback for live data: it runs only when called.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DeskConfig
from .feed import dispatch, dispatch_depth
from .markets import MarketRegistry
from .state import DeskState


def registry_from_fixtures(info_dir: Path) -> MarketRegistry:
    reg = MarketRegistry()
    reg.load_perp_dexs(json.loads((info_dir / "perpDexs.json").read_text())["response"])
    for f in sorted(info_dir.glob("meta_*.json")):
        rec = json.loads(f.read_text())
        reg.load_meta(rec["request"].get("dex", ""), rec["response"])
    return reg


class Replayer:
    def __init__(self, records: list[dict], cfg: DeskConfig, registry: MarketRegistry, mode: str = "REPLAY") -> None:
        self.records = records
        self.desk = DeskState(cfg, mode=mode)
        self.desk.set_markets([registry.check(m) for m in cfg.markets])
        self.desk.metadata = {"source": "recorded info perpDexs + meta", "dexes": registry.dexes}
        self.desk.collector = {"state": "replay", "note": "recorded data, not live"}
        self.desk.link = "stopped"
        self.pos = 0
        self.now_ns = None
        self.wall_ms = None

    @classmethod
    def from_file(cls, path: Path, cfg: DeskConfig, registry: MarketRegistry, mode: str = "REPLAY") -> "Replayer":
        with Path(path).open() as fh:
            return cls([json.loads(line) for line in fh if line.strip()], cfg, registry, mode)

    def step(self, rec: dict) -> None:
        self.now_ns, self.wall_ms = rec["recv_mono_ns"], rec["recv_wall_ms"]
        ev = rec.get("event")
        if ev == "connect":
            for ms in self.desk.markets.values():
                ms.on_connect(rec["conn"])
            self.desk.link = "connected"
            self.desk.collector = {"state": "connected", "conn": rec["conn"], "note": "replay"}
        elif ev == "disconnect":
            for ms in self.desk.markets.values():
                ms.on_disconnect(rec.get("reason", "recorded disconnect"), rec["recv_wall_ms"])
            self.desk.link = "disconnected"
            self.desk.collector = {"state": "disconnected", "conn": rec["conn"], "note": "replay"}
        elif rec.get("http") == "l2Book":
            dispatch_depth(self.desk, rec["request"], rec["response"], rec["recv_mono_ns"], rec["recv_wall_ms"])
        elif "msg" in rec:
            dispatch(self.desk, rec["msg"], rec["recv_mono_ns"], rec["recv_wall_ms"])

    def run_until(self, mono_ns: int | None = None) -> None:
        while self.pos < len(self.records):
            rec = self.records[self.pos]
            if mono_ns is not None and rec["recv_mono_ns"] > mono_ns:
                break
            self.step(rec)
            self.pos += 1
        if mono_ns is not None:
            self.now_ns = mono_ns

    def snapshot(self, now_ns: int | None = None, wall_ms: int | None = None) -> dict:
        n = now_ns if now_ns is not None else self.now_ns
        w = wall_ms if wall_ms is not None else self.wall_ms
        return self.desk.build_snapshot(n, w)

    def run_all_with_snapshots(self, every_s: float = 1.0):
        """Yield a snapshot every `every_s` of recorded time, like the live collector."""
        if not self.records:
            return
        t = self.records[0]["recv_mono_ns"]
        end = self.records[-1]["recv_mono_ns"]
        step = int(every_s * 1e9)
        while t <= end:
            self.run_until(t)
            yield self.snapshot(t, self.wall_ms)
            t += step
