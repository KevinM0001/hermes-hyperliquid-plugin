"""Regression tests for Codex review 2 (commit 16cb0e3), one per finding, from its reproductions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hl_desk import owner, schema, tools
from hl_desk.runtime import _served

from helpers import A, B, NS, T0, SnapRT, build, make_desk, open_segment, trade

ROOT = Path(__file__).resolve().parent.parent


# --- BLOCKING 2: a malformed best level makes the mid, and everything that depends on it, unknown -------

def test_malformed_best_bid_never_yields_a_covered_band():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    raw = {"coin": "BTC", "time": T0, "levels": [
        [{"px": "99.9", "n": 1}, {"px": "98", "sz": "1", "n": 1}],
        [{"px": "100.1", "sz": "1", "n": 1}, {"px": "100.2", "sz": "1", "n": 1}, {"px": "101", "sz": "1", "n": 1}]]}
    ms.on_book(raw, 0, T0)          # thin: both the fast and the full book carry it
    m = build(desk, 1)["markets"]["BTC"]
    assert m["book"]["top"]["mid"]["value"] is None
    for band in m["book"]["depth"]["bands"]:
        for side in ("bid", "ask"):
            ex = band["exact"][side]
            assert ex["status"] == "unavailable" and ex["notional_usd"]["value"] is None
            assert "mid" in ex["notional_usd"]["reason"] and "unknown" in ex["notional_usd"]["reason"]
    obs = m["book"]["depth"]["observed"]["full precision"]
    assert "unknown" in obs["reason"]


def test_malformed_best_level_blocks_older_mids_for_later_depth_books():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    good = {"coin": "BTC", "time": T0, "levels": [[{"px": "99.9", "sz": "1", "n": 1}] * 1 + [{"px": str(99.8 - i / 10), "sz": "1", "n": 1} for i in range(4)],
                                                    [{"px": str(100.1 + i / 10), "sz": "1", "n": 1} for i in range(5)]]}
    bad = {"coin": "BTC", "time": T0 + 500, "levels": [[{"px": "99.9", "n": 1}] + good["levels"][0][1:], good["levels"][1]]}
    ms.on_book(good, 0, T0)
    ms.on_book(bad, NS // 2, T0 + 500)
    deep = {"coin": "BTC", "time": T0 + 900, "levels": [[{"px": str(99.9 - i * 0.5), "sz": "1", "n": 1} for i in range(20)],
                                                         [{"px": str(100.1 + i * 0.5), "sz": "1", "n": 1} for i in range(20)]]}
    ms.on_depth_poll(4, deep, NS, T0 + 900)
    obs = build(desk, NS)["markets"]["BTC"]["book"]["depth"]["observed"]["nSigFigs=4"]
    assert "unknown" in obs["reason"]          # the older valid mid (T0) is not borrowed across the bad one


# --- BLOCKING 3: invalid trades are never zero-filled or attributed to invented wallets ---------------

@pytest.mark.parametrize("drop", ["side", "users", "tid"])
def test_trade_missing_an_essential_field_is_a_gap_not_a_zero(drop):
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, T0)
    bad = trade("BTC", T0 + 2000, 100.0, 1.0, "A", A, B, 2)
    bad.pop(drop)
    ms.on_trades([bad, trade("BTC", T0 + 2000, 100.0, 3.0, "B", A, B, 3)], NS, T0)
    assert ms.store.counters["malformed"] == 1
    gap = ms.store.gaps[-1]
    assert drop in gap["reason"] and gap["end_ms"] == T0 + 2000
    ms.on_trades([trade("BTC", T0 + 3000, 100.0, 2.0, "B", A, B, 4)], NS, T0)
    m = build(desk, NS)["markets"]["BTC"]
    flow = m["flow"]
    assert flow["window"]["start_exclusive_ms"] == T0 + 2000          # the window starts after the bad message
    assert flow["values"]["volume"]["value"] == 200.0                  # only the trade after it
    w = m["wallets"]["1m"]
    assert "None" not in json.dumps(w) and w["unique_execution_notional_usd"] == 200.0
    assert any(not t["counted"] for t in flow["tape"] if t["tid"] == 3)   # the valid trade in the bad message: tape only


def test_trade_with_a_bad_side_or_address_is_rejected():
    from hl_desk.trades import parse_trade, trade_problems
    ok = trade("BTC", T0, 100.0, 1.0, "B", A, B, 1)
    assert trade_problems(ok) == [] and parse_trade(ok, 1) is not None
    assert "side" in trade_problems(dict(ok, side="X"))
    assert "users" in trade_problems(dict(ok, users=["None", B]))
    assert "users" in trade_problems(dict(ok, users=[A]))
    assert "sz" in trade_problems(dict(ok, sz="0"))


# --- BLOCKING 4: every tool carries the historical-snapshot label ------------------------------------

def test_every_tool_keeps_the_served_age_and_historical_label():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, T0)
    snap = build(desk, 1)
    served = _served(snap, snap["built_at_ms"] + 120_000, latest=False)
    for name in tools.HANDLERS:
        r = json.loads(tools.run_tool(name, {"market": "BTC", "snapshot_id": served["snapshot_id"]}, SnapRT(served)))
        assert r["served"]["latest"] is False and r["served"]["age_s"] == 120.0, name
        assert r["historical"]["as_of"] == snap["built_at"] and "build time" in r["historical"]["note"], name


# --- BLOCKING 5: wallet account data obeys the freshness and null rules -------------------------------

def _drill(state, orders=None, fetched_offset_ms=0):
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, T0)
    snap = build(desk, 1)
    returned = T0 + 2000 + fetched_offset_ms
    cur = lambda user, dex: {"requested_ms": returned - 200, "state_received_ms": returned - 100,
                             "orders_received_ms": returned, "returned_ms": returned,
                             "clearinghouseState": state, "frontendOpenOrders": orders or []}
    out = json.loads(tools.run_tool("hl_wallets", {"market": "BTC", "window_min": 1, "address": A}, SnapRT(snap, desk, cur)))
    return out["wallet"]["current"]


def _state(time_ms, positions=True):
    s = {"time": time_ms, "marginSummary": {"accountValue": "700", "totalNtlPos": "700", "totalMarginUsed": "70"}}
    if positions:
        s["assetPositions"] = [{"type": "oneWay", "position": {"coin": "BTC", "szi": "7", "entryPx": "100", "positionValue": "700",
                                                                "unrealizedPnl": "0", "marginUsed": "70", "liquidationPx": None,
                                                                "leverage": {"type": "cross", "value": 10}}}]
    return s


def test_hour_old_account_state_is_delayed_and_null():
    cur = _drill(_state(T0 + 2000 - 3_600_000))
    acc = cur["account"]
    assert acc["freshness"]["status"] == "delayed" and acc["freshness"]["source_time_ms"] == T0 + 2000 - 3_600_000
    assert acc["margin_summary"]["account_value_usd"]["value"] is None
    assert acc["positions"]["value"] is None and "delayed" in acc["positions"]["reason"]


def test_fresh_account_state_shows_values_and_missing_positions_are_unknown_not_empty():
    cur = _drill(_state(T0 + 1500))
    acc = cur["account"]
    assert acc["freshness"]["status"] == "fresh"
    assert acc["margin_summary"]["account_value_usd"] == {"value": 700.0, "unit": "USD"}
    p = acc["positions"]["value"][0]
    assert p["size_coin"]["value"] == 7.0 and p["leverage"]["value"] == 10.0
    assert p["liquidation_px"]["value"] is None and "no liquidation price" in p["liquidation_px"]["reason"]
    cur2 = _drill(_state(T0 + 1500, positions=False))
    assert cur2["account"]["positions"]["value"] is None and "unknown (not none)" in cur2["account"]["positions"]["reason"]
    cur3 = _drill({k: v for k, v in _state(T0 + 1500).items() if k != "time"})
    assert cur3["account"]["freshness"]["status"] == "unverified" and cur3["account"]["positions"]["value"] is None


def test_open_orders_are_never_presented_as_current():
    cur = _drill(_state(T0 + 1500), orders=[{"coin": "BTC", "side": "B", "limitPx": "99", "sz": "1", "timestamp": T0 + 1999}])
    oo = cur["open_orders"]
    assert oo["freshness"]["status"] == "unverified" and oo["orders"]["value"] is None and oo["count"]["value"] is None
    assert "placement time" in oo["orders"]["reason"]


# --- BLOCKING 6: the schema describes what is published -------------------------------------------

def test_schema_describes_exact_approximate_cvd_slice_and_account_metrics():
    s = schema.load()
    m = s["metrics"]
    assert "exact" in m["book.depth_band"]["outputs"] and "approximate" in m["book.depth_band"]["outputs"]
    assert "range_usd" in m["book.depth_band_approximate"]["formula"]
    assert "cumulative_from" in m["flow.cvd_bucket"]["formula"] and "cumulative_base_usd" in m["flow.cvd_bucket"]["slice"]
    for k in ("wallet.current_account_value", "wallet.current_total_position_notional", "wallet.current_margin_used",
              "wallet.current_position", "wallet.current_open_orders"):
        assert k in m, k
    assert s["freshness"]["fields"]["clearinghouseState"]["source_max_age_s"] == 10.0
    assert s["freshness"]["fields"]["frontendOpenOrders"]["never_fresh"] is True
    assert "first bucket shown" not in json.dumps(m["flow.cvd_bucket"]).replace("not from the first bucket shown", "")


# --- DEFERRED 1: PRESSURE is no longer declared node-only ------------------------------------------

def test_pressure_is_not_declared_node_only():
    from hl_desk.state import NODE_PANELS
    assert not any("PRESSURE" in p for p in NODE_PANELS)
    assert "PRESSURE" not in (ROOT / "catalog" / "hyperliquid-desk.yaml").read_text()


# --- DEFERRED 2: a long profile path still gets a working owner socket ---------------------------

def test_long_data_dir_gets_a_short_private_socket(tmp_path, monkeypatch):
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="hd"))      # stands in for the system temp folder; removed below
    fallback = short / f"hl-desk-{os.getuid()}"
    monkeypatch.setattr(owner, "_fallback_dir", lambda: fallback)
    long_dir = tmp_path / ("x" * 120)
    long_dir.mkdir()
    p = owner.sock_path(long_dir)
    assert len(str(p)) <= owner.MAX_SOCK_PATH and p.parent == fallback
    assert owner.sock_path(long_dir) == p          # the same for every process

    import asyncio

    async def serve_once():
        srv = owner.OwnerServer(p, lambda req: {"ok": True, "echo": req.get("op")})
        await srv.start()
        assert srv.error is None and (p.parent.stat().st_mode & 0o777) == 0o700
        reply = await asyncio.to_thread(owner.request, p, {"op": "ping"})
        await srv.close()
        return reply

    try:
        assert asyncio.run(serve_once()) == {"ok": True, "echo": "ping"}
        assert not p.exists()                       # removed on close
    finally:
        fallback.rmdir()
        short.rmdir()


# --- DEFERRED 3: the README no longer describes a snapshot file ---------------------------------

def test_readme_describes_in_memory_snapshots_only():
    readme = (ROOT / "README.md").read_text()
    assert "snapshot file" not in readme and "latest_snapshot.json" not in readme
