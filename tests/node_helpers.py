"""Shared helpers for the node-mode tests (Handoff 1c Part B).

The fixtures under fixtures/node are cut from Kevin's recorded node feed (engine recorder, protocol v2) and
from the Part A follow-test taps, every line byte for byte (MANIFEST.json gives the sources and filters).
Tests feed those real lines to the client with their recorded receipt times; the cases that need a fault
(a gap, a crossed book, a late snapshot) make it by dropping, repeating or reordering real lines. Where the
handoff specifies a numeric case (the flip example, the wallet cases, a missing position effect) or step 14
names an input the window doesn't contain (an execution without a single taker), that execution is built in
the feed's own format.

Surfaces runs a client through the plugin itself: every read builds and publishes the collector's snapshot and
compares the tab's routes and the node tools (chat and tab), so the tests check each intermediate tool and tab
result, not just the final state.
"""

from __future__ import annotations

import copy
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

import hl_desk
from hl_desk.node import panels
from hl_desk.node.client import FeedClient

ROOT = Path(hl_desk.__file__).resolve().parent.parent

FIX = Path(__file__).resolve().parent / "fixtures" / "node"
XYZ = FIX / "xyz-2markets-66s.jsonl.gz"
EXECS = FIX / "execs-4markets-120s.jsonl.gz"
RESTART = FIX / "restart-xyz-XYZ100.ndjson.gz"
OPTS = {"liq_burst_count": 2, "liq_burst_usd": 1_000_000}
NO_AGE = {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10 ** 15}


def read_recording(path: Path) -> tuple[dict, list[tuple[int, dict]]]:
    """(header, [(rx_ms, msg), ...]) from a recording fixture."""
    header, out = None, []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for raw in fh:
            o = json.loads(raw)
            if "recording" in o:
                header = o["recording"]
            elif "msg" in o:
                out.append((o["rx_ms"], o["msg"]))
    return header, out


def read_tap(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def hello_from(header: dict, mode: str = "LIVE") -> dict:
    p = header["producer"]
    return {"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "started_ms": p["started_ms"],
            "pub_ms": p["started_ms"], "mode": mode, "limits": header["limits"], "labels": header.get("labels", {}),
            "markets": {m: {"start_seq": None, "snapshot": "in_stream"} for m in header["markets"]}}


def client(source: str = "test-socket", cfg: dict | None = None, mode: str = "LIVE") -> FeedClient:
    c = FeedClient(source, mode, NO_AGE | (cfg or {}))
    c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
    return c


def feed(c: FeedClient, msgs, start_mono_ns: int = 0):
    """Feed (rx_ms, msg) pairs as received; the monotonic clock follows the recorded receipt time."""
    for rx, m in msgs:
        c.on_line(m, rx, start_mono_ns + rx * 1_000_000)


def by_market(msgs, market):
    return [(rx, m) for rx, m in msgs if m.get("market") == market]


def d(x) -> Decimal:
    return Decimal(str(x))


def mutate(msg: dict, **changes) -> dict:
    out = copy.deepcopy(msg)
    out.update(changes)
    return out


def check_view(view: dict, client_obj, market: str) -> None:
    """Invariants every intermediate node view (and so every node tool result) must hold."""
    assert view["provenance"]["mode"] == client_obj.mode
    assert view["provenance"]["epoch"] == client_obj.epoch
    fam = view["families"]
    for f, st in fam.items():
        assert st["state"] in ("LIVE", "STALE", "BEHIND", "UNKNOWN", "INVALID", "REPLAY"), (f, st)
        if client_obj.mode == "REPLAY":
            assert st["state"] in ("REPLAY", "INVALID"), (f, st)     # a replay is never LIVE, STALE or BEHIND
        if st["state"] == "INVALID":
            assert st.get("reason"), (f, st)
    if "book" not in view:                     # no data for this market from this source yet
        return
    if fam["book"]["state"] == "INVALID":
        assert view["book"]["valid"] is False
        for p in ("ladder", "walls", "thinning"):
            assert view[p].get("available") is False and view[p].get("reason"), p
    if view["book"]["valid"]:
        lad = view["ladder"]
        if lad["bids"] and lad["asks"]:
            assert Decimal(lad["bids"][0]["px"]) < Decimal(lad["asks"][0]["px"])   # never shows a crossed book as valid
        for r in lad["bids"] + lad["asks"]:
            assert r["orders"] > 0 and r["total_sz"] > 0
            assert r["largest_order_sz"] <= r["total_sz"]
    if fam["triggers"]["state"] in ("INVALID", "STALE", "BEHIND", "UNKNOWN"):
        assert view["stops"].get("available") is False
    if fam["flow"]["state"] not in ("LIVE", "REPLAY"):
        assert view["tape"].get("available") is False


# --- the plugin's own outputs: snapshot, tab routes and tools, read at every intermediate step ----------------

def jsonable(x):
    """A value as the tools and the tab serve it (JSON, decimals as strings)."""
    return json.loads(json.dumps(x, default=str))


def load_plugin_api():
    import importlib.util
    import sys
    name = "hermes_dashboard_plugin_hyperliquid-desk"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class OwnerStandIn:
    def __init__(self, store):
        import types
        self.store = store
        self.desk = types.SimpleNamespace(collector={"state": "connected", "note": "test stand-in"})

    def alive(self):
        return True

    def stop(self, timeout=10.0):
        return True


NODE_TOOLS = ("node_status", "node_book", "node_stops")


class Surfaces:
    """The plugin's outputs for a node client, as the user reads them: the snapshot the collector builds and
    publishes, the tab's snapshot route, and the node_status, node_book and node_stops tools both through chat and
    through the tab's tool route (hl_alerts and hl_wallets too, for the node buckets on the Flow Deck card, its
    events and the wallet panels). Every read checks that all of them show the same thing and that the node
    results hold check_view's invariants, then returns the node view the tests assert on."""

    def __init__(self, tmp_path, monkeypatch, markets, **desk_cfg):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from hl_desk import runtime as rt_mod
        from hl_desk.node.manager import NodeManager
        from hl_desk.store import SnapshotStore

        from helpers import make_desk, use_standalone_storage

        use_standalone_storage(monkeypatch, tmp_path)
        rt_mod._slot.reset()
        self.rt_mod = rt_mod
        self.rt = rt_mod.get_runtime()
        self.rt.store = SnapshotStore(tmp_path)
        self.rt.collector = OwnerStandIn(self.rt.store)
        app = FastAPI()
        app.include_router(load_plugin_api().router, prefix="/api/plugins/hyperliquid-desk")
        self.http = TestClient(app)
        self.markets = list(markets)
        self.desk = make_desk(markets=tuple(markets), **desk_cfg)
        self.mgr = NodeManager(self.desk.cfg)
        self.desk.node = self.mgr
        self.reads = 0
        self.last: dict = {}

    def attach(self, c: FeedClient) -> FeedClient:
        """Make c the plugin's live feed (a LIVE client) or its running replay (a REPLAY client); both can run.
        Either gets node-mode bursts, as the plugin attaches them (NodeManager.attach_bursts)."""
        if c.mode == "REPLAY":
            self.mgr.replay_client = c
        else:
            self.mgr.live = c
        return self.mgr.attach_bursts(c)

    def close(self):
        self.rt_mod._slot.reset()

    def _get(self, path, **params):
        return self.http.get(f"/api/plugins/hyperliquid-desk/{path}", params={k: v for k, v in params.items() if v}).json()

    def read(self, market: str, now_wall_ms: int, now_mono_ns: int | None = None, slot: str | None = None) -> dict:
        slot = slot or ("live" if self.mgr.live is not None else "replay")
        c = self.mgr.live if slot == "live" else self.mgr.replay_client
        now_mono_ns = now_wall_ms * 1_000_000 if now_mono_ns is None else now_mono_ns
        from hl_desk import tools
        snap = self.desk.build_snapshot(now_mono_ns, now_wall_ms)
        self.rt.store.publish(snap, frozen=self.desk.frozen_last)
        sid = snap["snapshot_id"]
        view = snap["markets"][market]["node"][slot]
        tab = self._get("snapshot", market=market, snapshot_id=sid)
        assert tab["ok"] and tab["market"]["node"][slot] == jsonable(view)
        out = {"snapshot": snap, "tab": tab["market"]}
        strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
        for name in NODE_TOOLS:
            args = {"market": market, "snapshot_id": sid, "source": slot}
            via_chat = json.loads(tools.run_tool(name, args, self.rt))
            via_tab = self._get(f"tool/{name}", **args)
            assert via_chat["ok"] and via_chat["snapshot_id"] == sid, via_chat
            assert strip(via_chat) == strip(via_tab), name
            out[name] = via_chat
        st = out["node_status"]["markets"][market]
        assert st["families"] == jsonable(view.get("families")) and st["provenance"] == jsonable(view.get("provenance"))
        nb, ns = out["node_book"], out["node_stops"]
        if view.get("available") is False:
            assert nb["available"] is False and ns["available"] is False and nb["reason"] == view["reason"]
        elif "book" in view:
            assert nb["families"] == jsonable(view["families"])
            for k in ("book", "ladder", "walls", "thinning", "tape"):
                assert nb[k] == jsonable(view[k]), k
            assert ns["stops"] == jsonable(view["stops"])
            check_view(view, c, market)
            for fam, s in view["families"].items():          # a family that isn't usable never serves its values
                if s["state"] == "INVALID":
                    assert s["reason"]
            if view["families"]["book"]["state"] not in ("LIVE", "REPLAY"):
                for k in ("ladder", "walls", "thinning"):
                    assert nb[k].get("available") is False and nb[k].get("reason"), k
        if slot == "live":
            alerts = json.loads(tools.run_tool("hl_alerts", {"market": market, "snapshot_id": sid}, self.rt))
            assert alerts["ok"] and strip(alerts) == strip(self._get("tool/hl_alerts", market=market, snapshot_id=sid))
            card = alerts["cards"][market]["card"]
            assert card == tab["market"]["deck"]["card"]
            out["card_buckets"] = card["activity_buckets"]
            out["events"] = alerts["events"]
            wal = json.loads(tools.run_tool("hl_wallets", {"market": market, "snapshot_id": sid, "window_min": 5}, self.rt))
            assert wal["ok"] and strip(wal) == strip(self._get("wallets", market=market, snapshot_id=sid, window_min=5))
            out["wallet_split"] = wal["panel"].get("node_activity_split")
            # every node-fill result names its source and the flow family's freshness, available or not
            cb = out["card_buckets"]
            assert cb["stream"] == "node fills" and cb["provenance"]["source"] == c.source, cb
            assert cb["freshness"].get("family") == "flow" or cb["freshness"].get("state") == "INVALID", cb
            for ev in out["events"]:
                if ev.get("type") == "burst":
                    assert ev["node_activity"]["provenance"]["source"] == c.source and "freshness" in ev["node_activity"]
                elif ev.get("type") == "liquidation_burst":
                    assert ev["provenance"]["source"] == c.source and ev["freshness"]["family"] == "flow"
            if out["wallet_split"] is not None:
                assert out["wallet_split"]["provenance"]["source"] == c.source and "freshness" in out["wallet_split"]
        self.reads += 1
        self.last = out
        return view


@pytest.fixture()
def surfaces(tmp_path, monkeypatch):
    """surfaces(markets) -> a Surfaces for those markets; torn down after the test."""
    made = []

    def make(markets, **desk_cfg):
        d = tmp_path / f"plugin{len(made)}"
        d.mkdir()
        made.append(Surfaces(d, monkeypatch, markets, **desk_cfg))
        return made[-1]
    yield make
    for s in made:
        s.close()
