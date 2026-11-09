"""Context values are served marked unverified, not withheld (schema 1.3.0).

activeAssetCtx carries no source timestamp, so the ctx family is never fresh. Each context value is served with its
value and {status: unverified, reason, received_age_s} (the receipt age of the last changed context) while a changed
context arrived within the family's max age on a live connection. It is null with the reason when the collector is
stopped, disconnected or warming up, or past the max age: at build time, and again when a latest snapshot is served
(the receipt age grows by the served age), so a dead stream never shows old numbers.
"""

from __future__ import annotations

import json

import pytest

from hl_desk import schema, tools
from hl_desk.serving_public import age_market

from helpers import NS, T0, SnapRT, build, make_desk
from node_helpers import surfaces  # noqa: F401  (fixture)

MS = 1_000_000
CTX = {"markPx": "74990.0", "oraclePx": "74985.5", "midPx": "74991.0", "funding": "0.0000125", "openInterest": "10",
       "dayNtlVlm": "5", "prevDayPx": "74000.0"}
VALUES = ("mark", "oracle", "mid", "funding", "funding_pct_per_hour", "open_interest", "open_interest_usd",
          "day_notional_volume", "prev_day_px")
SERVED = "by the time this was served"


def _desk(market="BTC", ctx=CTX):
    desk = make_desk(markets=(market,))
    ms = desk.markets[market]
    ms.on_connect(1)
    ms.on_ctx({"coin": market, "ctx": dict(ctx)}, 0, T0)
    return desk, ms


def test_values_are_served_marked_unverified_with_the_receipt_age():
    desk, _ = _desk()
    m = build(desk, int(0.4 * NS))["markets"]["BTC"]
    c = m["context"]
    assert c["mark"] == {"value": 74990.0, "unit": "USD", "status": "unverified", "received_age_s": 0.4,
                         "reason": "activeAssetCtx carries no source timestamp; a changed value was received 0.4 s ago"}
    assert c["funding_pct_per_hour"]["value"] == 0.00125 and c["open_interest_usd"]["value"] == 749900.0
    for k in VALUES:
        assert c[k]["value"] is not None and c[k]["status"] == "unverified" and c[k]["received_age_s"] == 0.4, k
        assert "received 0.4 s ago" in c[k]["reason"], k
    assert c["funding"]["unit"] == "fraction per 1h" and c["open_interest"]["unit"] == "coin" and c["units_note"]
    # the status itself is still never fresh, and ctx stays listed among the non-fresh fields
    f = m["freshness"]["ctx"]
    assert f["status"] == "unverified" and "no source timestamp" in f["reason"] and f["progress_age_s"] == 0.4
    assert [u for u in m["unavailable"] if u["field"] == "activeAssetCtx"] == [
        {"field": "activeAssetCtx", "status": "unverified", "reason": f["reason"]}]
    # the mark and oracle price lines come from these values and carry the status
    lines = {ln["kind"]: ln for ln in m["chart"]["price_lines"]}
    assert lines["mark"]["price"] == 74990.0 and lines["mark"]["status"] == "unverified" and lines["mark"]["label"] == "mark (unverified)"
    assert lines["oracle"]["price"] == 74985.5 and lines["oracle"]["status"] == "unverified"


def test_a_missing_field_is_null_with_its_own_reason_beside_shown_values():
    desk, _ = _desk(ctx={k: v for k, v in CTX.items() if k not in ("midPx", "openInterest")})
    c = build(desk, int(0.4 * NS))["markets"]["BTC"]["context"]
    assert c["mark"]["value"] == 74990.0 and c["mark"]["status"] == "unverified"
    assert c["mid"] == {"value": None, "unit": "USD", "reason": "midPx absent"}
    assert c["open_interest"]["value"] is None and c["open_interest_usd"]["value"] is None
    assert c["open_interest_usd"]["reason"] == "needs open interest and mark" and "status" not in c["open_interest_usd"]


@pytest.mark.parametrize("market,limit", [("BTC", 20.0), ("HYPE", 20.0), ("xyz:SP500", 45.0), ("xyz:XYZ100", 45.0)])
def test_null_again_past_the_max_age(market, limit):
    assert limit == schema.by_market(schema.freshness_field("activeAssetCtx")["max_age_s_by_market"], market)
    desk, ms = _desk(market)
    c = build(desk, int(limit * NS))["markets"][market]["context"]          # at the limit: still shown
    assert c["mark"]["value"] == 74990.0 and c["mark"]["received_age_s"] == limit
    m = build(desk, int((limit + 0.001) * NS))["markets"][market]
    for k in VALUES:
        v = m["context"][k]
        assert v["value"] is None and "no changed context for longer than the max age" in v["reason"], k
        assert "status" not in v and "received_age_s" not in v, k
    assert m["context"]["mark"]["unit"] == "USD" and m["context"]["units_note"]
    assert not [ln for ln in m["chart"]["price_lines"] if ln["kind"] in ("mark", "oracle")]
    # an identical context isn't progress; a changed one is, and the values come back at age 0
    ms.on_ctx({"coin": market, "ctx": dict(CTX)}, int((limit + 1) * NS), T0)
    assert build(desk, int((limit + 1) * NS))["markets"][market]["context"]["mark"]["value"] is None
    ms.on_ctx({"coin": market, "ctx": dict(CTX, markPx="75000.0")}, int((limit + 2) * NS), T0)
    c = build(desk, int((limit + 2) * NS))["markets"][market]["context"]
    assert c["mark"]["value"] == 75000.0 and c["mark"]["received_age_s"] == 0.0 and c["mark"]["status"] == "unverified"


def test_null_when_disconnected_stopped_or_warming_up():
    desk, ms = _desk()
    assert build(desk, int(0.4 * NS))["markets"]["BTC"]["context"]["mark"]["value"] == 74990.0
    desk.link = "disconnected"
    c = build(desk, int(0.5 * NS))["markets"]["BTC"]["context"]
    assert all(c[k]["value"] is None and "disconnected" in c[k]["reason"] and "status" not in c[k] for k in VALUES)
    desk.link = "stopped"
    c = build(desk, int(0.6 * NS))["markets"]["BTC"]["context"]
    assert all(c[k]["value"] is None and "collector is not running" in c[k]["reason"] for k in VALUES)
    desk.link = "connected"
    ms.on_connect(2)                                   # a new connection: nothing received on it yet
    assert ms.ctx is None
    c = build(desk, int(0.7 * NS))["markets"]["BTC"]["context"]
    assert all(c[k]["value"] is None and "warming_up" in c[k]["reason"] and "status" not in c[k] for k in VALUES)


def test_serving_grows_the_receipt_age_and_withholds_past_the_max_age():
    desk, _ = _desk()
    m = build(desk, int(0.5 * NS))["markets"]["BTC"]
    assert age_market(m, 0, SERVED) is m
    aged = age_market(m, 1.0, SERVED)
    for k in VALUES:
        v = aged["context"][k]
        assert v["value"] == m["context"][k]["value"] and v["received_age_s"] == 1.5 and v["status"] == "unverified", k
        assert "received 1.5 s ago" in v["reason"], k
    f = aged["freshness"]["ctx"]
    assert f["status"] == "unverified" and "received 1.5 s ago" in f["reason"] and f["progress_age_s"] == 1.5
    assert [u for u in aged["unavailable"] if u["field"] == "activeAssetCtx"] == [
        {"field": "activeAssetCtx", "status": "unverified", "reason": f["reason"]}]
    assert {ln["kind"] for ln in aged["chart"]["price_lines"]} >= {"mark", "oracle"}
    exact = age_market(m, 19.5, SERVED)                 # 0.5 + 19.5 = 20.0: at the limit, still shown
    assert exact["context"]["mark"]["value"] == 74990.0 and exact["context"]["mark"]["received_age_s"] == 20.0
    late = age_market(m, 19.51, SERVED)
    for k in VALUES:
        v = late["context"][k]
        assert v["value"] is None and "no changed context for longer than the max age" in v["reason"], k
        assert SERVED in v["reason"] and "status" not in v and "received_age_s" not in v, k
        assert v["unit"] == m["context"][k]["unit"]
    f = late["freshness"]["ctx"]
    assert f["status"] == "unverified" and "no changed context" in f["reason"] and SERVED in f["reason"]
    assert [u for u in late["unavailable"] if u["field"] == "activeAssetCtx"] == [
        {"field": "activeAssetCtx", "status": "unverified", "reason": f["reason"]}]
    assert not [ln for ln in late["chart"]["price_lines"] if ln["kind"] in ("mark", "oracle")]
    assert {w["kind"] for w in late["chart"]["price_lines_withheld"]} == {"mark", "oracle"}
    assert late["context"]["units_note"] == m["context"]["units_note"]
    # a context already withheld at build time is left alone by the gate
    gone = build(desk, int(20.001 * NS))["markets"]["BTC"]
    assert age_market(gone, 1.0, SERVED)["context"] == gone["context"]


def test_hl_context_ages_the_values_it_serves():
    desk, _ = _desk()
    snap = build(desk, int(0.5 * NS))
    r = json.loads(tools.run_tool("hl_context", {"market": "BTC"}, SnapRT(snap)))
    assert r["ok"] and r["context"]["oracle"]["value"] == 74985.5 and r["context"]["oracle"]["received_age_s"] == 0.5
    assert r["status"]["overall"] == "unverified" and any("marked unverified" in n for n in r["notes"])
    served = dict(snap, served={"latest": True, "age_s": 2.0, "served_at": "t", "note": "n"})
    r = json.loads(tools.run_tool("hl_context", {"market": "BTC"}, SnapRT(served)))
    assert r["context"]["oracle"]["value"] == 74985.5 and r["context"]["oracle"]["received_age_s"] == 2.5
    assert "received 2.5 s ago" in r["status"]["fields"]["ctx"]["reason"]
    served = dict(snap, served={"latest": True, "age_s": 19.6, "served_at": "t", "note": "n"})
    r = json.loads(tools.run_tool("hl_context", {"market": "BTC"}, SnapRT(served)))
    assert all(r["context"][k]["value"] is None and SERVED in r["context"][k]["reason"] for k in VALUES)
    assert r["unavailable"] == [{"field": "activeAssetCtx", "status": "unverified", "reason": r["status"]["fields"]["ctx"]["reason"]}]


# --- through chat and the tab's routes ------------------------------------------------------------

def _publish(plugin, at_ms):
    snap = plugin.desk.build_snapshot(at_ms * MS, T0 + at_ms)
    plugin.rt.store.publish(snap, frozen=plugin.desk.frozen_last)
    return snap


def _at(monkeypatch, at_ms):
    from hl_desk import runtime
    monkeypatch.setattr(runtime.time, "time", lambda: (T0 + at_ms) / 1000)
    monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: at_ms * MS)


def _read(plugin, monkeypatch, tool, at_ms, **args):
    """The tool through chat and the tab's tool route, at at_ms on both clocks (as the snapshots were stamped)."""
    _at(monkeypatch, at_ms)
    args = {"market": "BTC", **args}
    via_chat = json.loads(tools.run_tool(tool, args, plugin.rt))
    via_tab = plugin.http.get(f"/api/plugins/hyperliquid-desk/tool/{tool}", params=args).json()
    strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
    assert strip(via_chat) == strip(via_tab)
    return via_chat


def test_hl_context_through_chat_and_the_tab(surfaces, monkeypatch):
    plugin = surfaces(["BTC"])
    ms = plugin.desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_ctx({"coin": "BTC", "ctx": dict(CTX)}, 1000 * MS, T0 + 1000)
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    snap = _publish(plugin, 1500)                                    # built 0.5 s after the context
    built = snap["markets"]["BTC"]["context"]["mark"]
    assert built["value"] == 74990.0 and built["status"] == "unverified" and built["received_age_s"] == 0.5
    _at(monkeypatch, 2500)
    tab = plugin.http.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": "BTC"}).json()
    # the snapshot route serves the build's shape with the served age; the tab adds that age itself
    assert tab["ok"] and tab["served"]["latest"] is True and abs(tab["served"]["age_s"] - 1.0) < 1e-6
    assert tab["market"]["context"]["mark"] == built
    r = _read(plugin, monkeypatch, "hl_context", 2500)                # the tools age it: 1 s later
    assert r["ok"] and r["served"]["latest"] is True and abs(r["served"]["age_s"] - 1.0) < 1e-6 and "historical" not in r
    assert r["context"]["mark"]["value"] == 74990.0 and r["context"]["mark"]["received_age_s"] == 1.5
    assert r["context"]["mark"]["status"] == "unverified" and "received 1.5 s ago" in r["context"]["mark"]["reason"]
    assert r["status"]["fields"]["ctx"]["status"] == "unverified" and "received 1.5 s ago" in r["status"]["fields"]["ctx"]["reason"]
    st = _read(plugin, monkeypatch, "hl_status", 2500)
    assert st["markets"]["BTC"]["freshness"]["ctx"]["status"] == "unverified"
    # built 19.5 s after the context and served 1.001 s later: past the max age by then, so withheld
    snap2 = _publish(plugin, 20_500)
    assert snap2["markets"]["BTC"]["context"]["mark"]["received_age_s"] == 19.5
    late = _read(plugin, monkeypatch, "hl_context", 21_501)
    assert late["served"]["latest"] is True
    for k in VALUES:
        assert late["context"][k]["value"] is None and SERVED in late["context"][k]["reason"], k
    assert "no changed context" in late["status"]["fields"]["ctx"]["reason"]
    hist = _read(plugin, monkeypatch, "hl_context", 21_501, snapshot_id=snap2["snapshot_id"])
    assert hist["historical"] and hist["context"]["mark"]["value"] == 74990.0 and hist["context"]["mark"]["received_age_s"] == 19.5


def test_schema_1_3_0_states_the_rule():
    s = schema.load()
    assert s["version"] == "1.3.0"
    assert any(c.startswith("1.3.0") and "context values served marked unverified" in c for c in s["changes"])
    ctx = s["freshness"]["fields"]["activeAssetCtx"]
    assert ctx["never_fresh"] is True and "received_age_s" in ctx["served"] and "null" in ctx["reason_when_exceeded"]
    for k in ("context.mark", "context.oracle", "context.mid", "context.funding", "context.open_interest",
              "context.day_notional_volume", "context.prev_day_px"):
        assert "unverified" in s["metrics"][k]["completeness_rule"], k
    assert "received_age_s" in s["conventions"]["null_rule"]
