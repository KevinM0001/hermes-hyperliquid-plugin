"""Chat and screen agree: the tab's routes return the same data as the tools for one snapshot ID,
and no route does blocking work on the event loop."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_desk import runtime as rt_mod, tools
from hl_desk.store import SnapshotStore

from helpers import recorded_replayer, use_standalone_storage

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "dashboard" / "plugin_api.py"


def load_plugin_api():
    name = "hermes_dashboard_plugin_hyperliquid-desk"
    spec = importlib.util.spec_from_file_location(name, API)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # what Hermes' dashboard loader does
    spec.loader.exec_module(mod)
    return mod


class OwnerStandIn:
    """Stands in for this process's running collector, so the runtime answers from its own store."""

    def __init__(self, store):
        self.store = store
        self.desk = types.SimpleNamespace(collector={"state": "connected", "note": "test stand-in"})

    def alive(self):
        return True

    def stop(self, timeout=10.0):
        return True


@pytest.fixture()
def client_and_snaps(tmp_path, monkeypatch):
    use_standalone_storage(monkeypatch, tmp_path)
    rt_mod._slot.reset()
    rt = rt_mod.get_runtime()
    rt.store = SnapshotStore(tmp_path)
    rt.collector = OwnerStandIn(rt.store)
    rp = recorded_replayer()
    snaps = []
    for i, frac in enumerate((0.3, 0.45)):
        rp.run_until(rp.records[int(len(rp.records) * frac)]["recv_mono_ns"])
        s = rp.snapshot(wall_ms=int(time.time() * 1000))     # built "now", as the live collector would
        rt.store.publish(s, frozen=rp.desk.frozen_last)
        snaps.append(s)
    app = FastAPI()
    app.include_router(load_plugin_api().router, prefix="/api/plugins/hyperliquid-desk")
    yield TestClient(app), snaps, rt
    rt_mod._slot.reset()


def test_tab_and_tools_return_the_same_numbers_for_one_snapshot_id(client_and_snaps):
    client, snaps, rt = client_and_snaps
    older = snaps[0]
    sid = older["snapshot_id"]
    tab = client.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": "BTC", "snapshot_id": sid}).json()
    assert tab["ok"] and tab["snapshot_id"] == sid
    assert tab["market"] == json.loads(json.dumps(older["markets"]["BTC"]))
    for name in ("hl_market", "hl_flow", "hl_context", "hl_alerts", "hl_status"):
        via_tab = client.get(f"/api/plugins/hyperliquid-desk/tool/{name}", params={"market": "BTC", "snapshot_id": sid}).json()
        via_chat = json.loads(tools.run_tool(name, {"market": "BTC", "snapshot_id": sid}, rt))
        # `served` and `historical` describe each request (its serving time and age), so they differ by milliseconds.
        strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
        assert strip(via_tab) == strip(via_chat)
        assert via_chat["snapshot_id"] == sid and via_chat["served"]["latest"] is False
    m = json.loads(tools.run_tool("hl_market", {"market": "BTC", "snapshot_id": sid}, rt))
    assert m["top_of_book"] == tab["market"]["book"]["top"]
    f = json.loads(tools.run_tool("hl_flow", {"market": "BTC", "snapshot_id": sid}, rt))
    assert f["values"] == tab["market"]["flow"]["values"]
    a = json.loads(tools.run_tool("hl_alerts", {"snapshot_id": sid}, rt))
    assert a["cards"]["BTC"]["card"] == tab["deck"]["BTC"]["card"]
    w_tab = client.get("/api/plugins/hyperliquid-desk/wallets",
                       params={"market": "BTC", "window_min": 1, "snapshot_id": sid}).json()
    w_chat = json.loads(tools.run_tool("hl_wallets", {"market": "BTC", "window_min": 1, "snapshot_id": sid}, rt))
    strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
    assert strip(w_tab) == strip(w_chat) and w_tab["panel"] == tab["market"]["wallets"]["1m"]
    latest = client.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": "BTC"}).json()
    assert latest["snapshot_id"] == snaps[-1]["snapshot_id"]


def test_unknown_snapshot_and_stopped_collector(client_and_snaps):
    client, snaps, rt = client_and_snaps
    r = client.get("/api/plugins/hyperliquid-desk/snapshot", params={"snapshot_id": "hd-gone-1"}).json()
    assert r["ok"] is False and "no longer retained" in r["error"]
    st = client.get("/api/plugins/hyperliquid-desk/status").json()
    assert st["ok"] and st["runtime"]["collector"]["state"] == "connected"
    rt.collector = None                       # no owner anywhere: nothing is served as current
    r = client.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": "BTC"}).json()
    assert r["ok"] is False and "isn't running" in r["error"]
    st = client.get("/api/plugins/hyperliquid-desk/status").json()
    assert st["runtime"]["collector"]["state"] == "stopped"


BLOCKING = {"run_tool", "start", "stop", "restart", "snapshot", "status", "_read_disk_snapshot", "read_text", "open",
            "urlopen", "post_info", "fetch_wallet_current", "sleep", "import_module", "load_config_readonly",
            "resolve", "_engine", "_loaded", "_apply_settings", "plugin_data_dir"}


def test_routes_do_no_blocking_work_on_the_event_loop():
    tree = ast.parse(API.read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    routes = [n for n in funcs.values() if any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post")
                                               for d in n.decorator_list)]
    assert routes and all(isinstance(r, ast.AsyncFunctionDef) for r in routes)

    def blocking_calls(fn, seen):
        found = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name == "to_thread":
                    continue
                parent_is_thread = False
                if name in BLOCKING and not parent_is_thread:
                    found.append(name)
                if name in funcs and name not in seen and isinstance(funcs[name], ast.FunctionDef):
                    found += blocking_calls(funcs[name], seen | {name})
        return found

    # Calls passed *into* asyncio.to_thread are references, not calls, so they don't appear as Call nodes.
    for r in routes:
        assert blocking_calls(r, {r.name}) == [], r.name
