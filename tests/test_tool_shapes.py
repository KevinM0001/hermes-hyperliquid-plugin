"""Every tool returns JSON with provenance, freshness status and the schema reference, and never raises."""

from __future__ import annotations

import json

import pytest

from hl_desk import tools

from helpers import SnapRT, recorded_replayer

PROV_KEYS = {"mode", "source", "market", "snapshot_id", "coverage"}


@pytest.fixture(scope="module")
def replay_state():
    rp = recorded_replayer()
    rp.run_until(rp.records[int(len(rp.records) * 0.45)]["recv_mono_ns"])
    return rp, rp.snapshot()


def call(name, args, snap, desk=None, current=None):
    raw = tools.run_tool(name, args, SnapRT(snap, desk, current))
    assert isinstance(raw, str)
    return json.loads(raw)


@pytest.mark.parametrize("name", ["hl_market", "hl_flow", "hl_context", "hl_wallets"])
def test_market_tools_shape(replay_state, name):
    rp, snap = replay_state
    for market in snap["markets"]:
        r = call(name, {"market": market}, snap)
        assert r["ok"] is True and r["tool"] == name
        assert r["snapshot_id"] == snap["snapshot_id"] and r["mode"] == "REPLAY"
        assert PROV_KEYS <= set(r["provenance"]) and r["provenance"]["market"] == market
        assert r["status"]["overall"] in {"fresh", "partial", "stale", "unverified", "warming_up", "disconnected", "stopped"}
        for f in r["status"]["fields"].values():
            assert {"status", "reason", "source_time", "max_age_s"} <= set(f)
        assert isinstance(r["unavailable"], list)
        assert r["schema"]["version"] == "1.3.0"
        assert "not a trade recommendation" in r["disclaimer"]


def test_market_tool_specifics(replay_state):
    rp, snap = replay_state
    m = call("hl_market", {"market": "BTC"}, snap)
    assert {"best_bid", "best_ask", "mid", "spread", "spread_bps"} <= set(m["top_of_book"])
    assert all("api_count" in lv for lv in m["levels"]["bids"])
    assert [b["band_pct"] for b in m["depth"]["bands"]] == [0.25, 0.5, 1.0]
    assert m["bbo"]["value"] is None
    f = call("hl_flow", {"market": "BTC"}, snap)
    assert {"buy_aggressive_notional", "sell_aggressive_notional", "net", "volume", "trade_count"} <= set(f["values"])
    assert "start" in f["window"] and "end" in f["window"]
    c = call("hl_context", {"market": "BTC"}, snap)
    assert c["context"]["funding"]["unit"] == "fraction per 1h"
    w = call("hl_wallets", {"market": "BTC", "window_min": 1}, snap)
    assert w["panel"]["window"]["requested_min"] == 1


def test_status_and_alerts_shape(replay_state):
    rp, snap = replay_state
    s = call("hl_status", {}, snap)
    assert s["ok"] and set(s["markets"]) == set(snap["markets"])
    for m in s["markets"].values():
        assert PROV_KEYS <= set(m["provenance"])
        assert set(m["freshness"]) == {"book_fast", "book_full", "book_sf4", "book_sf3", "book_sf2", "trades", "bbo", "ctx"}
        assert m["panels_needing_node_mode"]
    a = call("hl_alerts", {}, snap)
    assert a["ok"] and set(a["cards"]) == set(snap["markets"])
    for card in a["cards"].values():
        assert PROV_KEYS <= set(card["provenance"])
        assert "regime" in card["card"] and "numbers" in card["card"]
        assert card["card"]["numbers"]["pressure"]["value"] is None
    assert "regime_order" in a["definitions"]


def test_wallet_drilldown_shape_and_totals(replay_state):
    rp, snap = replay_state
    panel = snap["markets"]["BTC"]["wallets"]["1m"]
    addr = panel["buyers"]["listed"][0]["address"]
    current = lambda user, dex: {"returned_ms": 1, "clearinghouseState": {"assetPositions": [], "marginSummary": {}},
                                 "frontendOpenOrders": []}
    r = call("hl_wallets", {"market": "BTC", "window_min": 1, "address": addr}, snap, rp.desk, current)
    w = r["wallet"]
    assert w["trades"]["totals"]["bought_usd"] == panel["wallets"][addr]["bought_usd"]
    assert w["trades"]["totals"]["sold_usd"] == panel["wallets"][addr]["sold_usd"]
    assert w["current"]["label"] == "current account state, not part of the snapshot"
    # An answer with no time field can't be shown as current: unverified, every value null with a reason.
    assert w["current"]["account"]["freshness"]["status"] == "unverified"
    assert w["current"]["account"]["positions"]["value"] is None
    assert w["current"]["coverage"] == "this answer covers only the default perp dex, not HIP-3 dexes"
    x = call("hl_wallets", {"market": "xyz:SP500", "window_min": 1, "address": addr}, snap, rp.desk, current)
    assert x["wallet"]["current"]["dex"] == "xyz" and "only the 'xyz' dex" in x["wallet"]["current"]["coverage"]


@pytest.mark.parametrize("name,args,needle", [
    ("hl_market", {}, "market is required"),
    ("hl_market", {"market": "NOTACOIN"}, "unsupported"),
    ("hl_flow", {"market": None}, "market is required"),
    ("hl_wallets", {"market": "BTC", "window_min": 7}, "window_min must be 1, 5 or 15"),
    ("hl_wallets", {"market": "BTC", "window_min": "x"}, "window_min must be 1, 5 or 15"),
    ("hl_context", {"market": "BTC", "snapshot_id": "hd-nope-1"}, "no longer retained"),
    ("hl_alerts", {"market": "zzz:BTC"}, "unsupported"),
])
def test_bad_input_returns_json_error_never_raises(replay_state, name, args, needle):
    rp, snap = replay_state
    r = call(name, args, snap)
    assert r["ok"] is False and needle in r["error"]


def test_bad_address_and_no_snapshot(replay_state):
    rp, snap = replay_state
    r = call("hl_wallets", {"market": "BTC", "address": "0x123"}, snap)
    assert r["ok"] is True and "address must be" in r["wallet"]["error"]

    class Empty(SnapRT):
        def snapshot(self, sid):
            return None, "no snapshot yet: the collector isn't running (start it with /hl start)"

    out = json.loads(tools.run_tool("hl_market", {"market": "BTC"}, Empty(None)))
    assert out["ok"] is False and "/hl start" in out["error"]

    class Broken(SnapRT):
        def snapshot(self, sid):
            raise RuntimeError("boom")

    out = json.loads(tools.run_tool("hl_status", {}, Broken(None)))
    assert out["ok"] is False and "internal error" in out["error"]


def test_tool_schemas_are_openai_style():
    for name, s in tools.SCHEMAS.items():
        assert s["name"] == name and s["parameters"]["type"] == "object"
        assert len(s["description"]) > 80
