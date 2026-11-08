"""Node executions: one stream per metric, activity buckets, wallet splits, liquidations (handoff steps 13-15).

Totals are checked against values calculated here, independently, from the raw feed JSON (exact decimals),
never against the module's own helpers.
"""

from __future__ import annotations

import copy
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest

from hl_desk.node.flow import NodeFlow, activity_buckets, liquidations, parse_exec, wallet_splits

from node_helpers import EXECS, client, read_recording, surfaces

A = "0x" + "a" * 40
B = "0x" + "b" * 40
W = "0x" + "c" * 40


def _exec(tid, px, sz, t, aggressor, parts, liquidation=None):
    """One execution in the feed's own format (PROTOCOL.md 4.6)."""
    e = {"tid": tid, "px": px, "sz": sz, "time_ms": t, "aggressor": aggressor, "complete": True, "participants": parts}
    if liquidation:
        e["liquidation"] = liquidation
    return e


def _p(user, side, role, effect=None, unavailable=None, liquidated=False, oid=1):
    p = {"user": user, "side": side, "role": role, "oid": oid}
    if effect is not None:
        p["effect"] = effect
    if unavailable is not None:
        p["effect_unavailable"] = unavailable
    if liquidated:
        p["liquidated"] = True
    return p


def _flow_with(*bodies, t0=1_000_000):
    f = NodeFlow("test")
    f.on_watermark(1, t0)                     # the window opens after this watermark
    for i, body in enumerate(bodies):
        f.on_execs(body, 2 + i, t0 + 1 + i)
        f.on_watermark(2 + i, t0 + 1 + i)
    return f


def _flip_buy(order="taker_first"):
    """The handoff's flip: a taker buys 4 at 100 from a short of 3, against a maker selling 4 from a long of 4."""
    taker = _p(A, "B", "taker", {"cover": "3", "new_long": "1"})
    maker = _p(B, "A", "maker", {"exit": "4", "new_short": "0"})
    parts = [taker, maker] if order == "taker_first" else [maker, taker]
    return _exec(1, "100", "4", 1_000_001, "B", parts)


# --- the same cases through the plugin: feed lines in, the Flow Deck card and the wallet panel out --------------

SP = "xyz:SP500"
T0 = 1_800_000_000_000                       # the public segment opens here
NOW = T0 + 111_200                           # snapshots are built here: card window [T0+15 s, T0+105 s]
SESSION = "0" * 31 + "1"


def _plugin_run(surfaces, bodies, drop=None, desk_cfg=None, t_first=T0 + 60_000, history=None, client_cfg=None):
    """Feed exec bodies to the plugin's live node client as protocol v2 lines (each followed by a watermark), next
    to a complete public Flow Deck card and a wallet panel listing W over the same span. After every line, read
    the card's activity buckets (hl_alerts) and the wallet panel's node split (hl_wallets), through chat and the
    tab. Until the fills are processed through the window's end both stay unavailable with a reason, never
    partial. drop: the index of a body whose execs line is lost (a real sequence gap). Returns the final reads."""
    from helpers import NS, open_segment, trade
    plugin = surfaces([SP], **(desk_cfg or {}))
    ms = plugin.desk.markets[SP]
    open_segment(ms, T0)
    for k in range(1, 112):                  # public trades: W buys every second (so the wallet panel lists W)
        t = T0 + k * 1000
        ms.on_trades([trade(SP, t, 6800.0, 0.01, "B", W, B, k)], k * NS, t)
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c = plugin.attach(client(cfg=client_cfg))
    c.on_line({"v": 2, "control": "hello", "epoch": 7, "session": SESSION, "started_ms": T0 - 20_000,
               "pub_ms": T0 - 20_000, "mode": "LIVE", "markets": {SP: {"start_seq": 1, "snapshot": "pending"}}},
              T0 - 19_950, 0)
    lines, seq, times = [], 0, {}

    def line(kind, body, block, bt):
        nonlocal seq
        seq += 1
        return {"v": 2, "epoch": 7, "session": SESSION, "market": SP, "seq": seq, "kind": kind, "block": block,
                "block_time_ms": bt, "pub_ms": bt + 10, "body": body}
    lines.append(line("watermark", {"through_seq": 0}, 1, T0 - 5_000))
    for i, body in enumerate(bodies):
        bt = t_first + i * 1_000
        b = copy.deepcopy(body)
        for e in b["execs"]:
            e["time_ms"] = times.setdefault(e["tid"], bt)       # a retried execution keeps its own time
        ex = line("execs", b, 10 + i, bt)
        if i != drop:
            lines.append(ex)
        lines.append(line("watermark", {"through_seq": seq}, 10 + i, bt))
    lines.append(line("watermark", {"through_seq": seq}, 99, T0 + 111_000))
    reads = []
    for m in lines:
        rx = m["block_time_ms"] + 50
        c.on_line(m, rx, (rx - T0) * 10 ** 6)
        plugin.read(SP, NOW, (NOW - T0) * 10 ** 6)
        card, wal = plugin.last["card_buckets"], plugin.last["wallet_split"]
        reads.append((card, wal))
        if history is not None:
            history.append((m, plugin.last))
        if m is not lines[-1]:
            assert card["available"] is False and card["reason"], card
            assert all(card[k]["value"] is None and card[k]["reason"] for k in ("new_long", "cover", "new_short", "exit"))
            assert wal["available"] is False and wal["reason"], wal
        assert card["stream"] == "node fills" and card["provenance"]["source"] == "test-socket"
        assert card["freshness"]["family"] == "flow"
    return plugin, reads[-1]


def _ref_upper_tail(market, side, total):
    """Counted here from the shipped reference file: count(sums >= total) / count."""
    import gzip
    from hl_desk.node import calibration
    ref = json.loads(gzip.decompress((calibration.HERE / calibration.load().data["reference_file"]).read_bytes()))
    sums = ref["markets"][market][f"{side}_sums_usd_sorted"]
    return sum(1 for x in sums if x >= total) / len(sums)


def test_node_burst_through_the_plugin(surfaces):
    """Node mode (Kevin's choice A): the burst comes from node fills, one evaluation per node block, at the manual
    threshold; it carries its threshold's label, its rank against the reference and its own side's buckets, with
    provenance and freshness, the same through chat and the tab. Public-trades bursts aren't shown in node mode,
    although W's public buying (about $4,080 a minute) is over its burst_min_usd here. The buckets are withheld
    while the node fills aren't fresh at the build; after a producer restart the earlier stream's burst is gone."""
    buy_flip = _flip_buy()
    sell = _exec(31, "100", "2", 0, "A", [_p(A, "A", "taker", {"exit": "2", "new_short": "0"}),
                                          _p(B, "B", "maker", {"cover": "0", "new_long": "2"})])
    hist = []
    plugin, _ = _plugin_run(surfaces, [{"execs": [buy_flip]}, {"execs": [sell]}],
                            desk_cfg={"node_burst_min_usd": {SP: 400.0}, "burst_min_usd": {SP: 1_000.0}}, history=hist)
    fired = False
    for m, last in hist:
        bursts = [e for e in last["events"] if e.get("type") == "burst"]
        tab = [e for e in last["tab"]["deck"]["events"] if e.get("type") == "burst"]
        assert bursts == tab and all(b["stream"] == "node fills" for b in bursts)
        info = last["tab"]["deck"]["bursts"]
        assert info["mode"] == "node" and info["threshold"]["source"] == "manual" and info["threshold"]["usd"] == 400
        if m["seq"] < 2:                                    # the burst's block hasn't arrived yet
            assert bursts == []
            continue
        (b,) = bursts
        fired = True
        assert b["side"] == "buying" and b["at_ms"] == T0 + 60_000 and b["aggressive_notional_usd"] == 400
        assert b["threshold"] == {"source": "manual", "label": "manual: $400 (setting node_burst_min_usd)", "usd": 400.0}
        r = b["rank"]
        assert r["available"] and r["upper_tail"] == _ref_upper_tail(SP, "buying", 400.0)
        assert r["label"].endswith("minute, Nov 3 to 7") and r["label"].startswith("a top ")
        assert [x["address"] for x in b["wallets"]["listed"]] == [A]
        na = b["node_activity"]
        assert na["stream"] == "node fills" and na["provenance"]["source"] == "test-socket"
        assert na["freshness"]["family"] == "flow"
        assert not {"new_short", "exit", "new_short_pct", "exit_pct"} & set(na)       # its own side only
        if na["freshness"]["state"] == "LIVE":
            assert na["available"] is True
            assert _v(na["cover"]) == 300 and _v(na["new_long"]) == 100
            assert _v(na["cover_pct"]) == 75 and _v(na["buying_aggressive_usd"]) == 400
        else:
            assert na["available"] is False and "node fills" in na["reason"] and na["new_long"]["value"] is None
    assert fired and hist[-1][1]["events"][0]["node_activity"]["available"] is True
    c = plugin.mgr.live                                  # a producer restart: a new epoch, nothing carried over
    c.on_line({"v": 2, "control": "hello", "epoch": 8, "session": "0" * 31 + "2", "started_ms": NOW, "pub_ms": NOW,
               "mode": "LIVE", "markets": {SP: {"start_seq": 1, "snapshot": "pending"}}}, NOW, (NOW - T0) * 10 ** 6)
    plugin.read(SP, NOW, (NOW - T0) * 10 ** 6)
    assert [e for e in plugin.last["events"] if e.get("type") == "burst"] == []


def test_public_mode_keeps_its_public_trades_bursts(surfaces):
    """Without a node feed the public-trades rule is unchanged: W's buying crosses burst_min_usd and the burst says
    its node buckets need node mode; no node calibration is used."""
    from helpers import NS, open_segment, trade
    plugin = surfaces([SP], burst_min_usd={SP: 4_000.0}, node_burst_min_usd={SP: 1.0})
    ms = plugin.desk.markets[SP]
    open_segment(ms, T0)
    for k in range(1, 112):
        t = T0 + k * 1000
        ms.on_trades([trade(SP, t, 6800.0, 0.01, "B", W, B, k)], k * NS, t)
    snap = plugin.desk.build_snapshot(112 * NS, T0 + 112_000)
    deck = snap["markets"][SP]["deck"]
    bursts = [e for e in deck["events"] if e.get("type") == "burst"]
    assert bursts and all("stream" not in e and "rank" not in e for e in bursts)
    assert bursts[-1]["threshold_usd"] == 4_000.0
    assert bursts[-1]["node_activity"] == {"available": False, "reason": "needs node mode: no node feed configured"}
    assert deck["bursts"]["mode"] == "public" and deck["bursts"]["threshold_usd"] == 4_000.0


def test_latest_reads_withhold_node_fill_results_once_their_flow_lapses(surfaces, monkeypatch):
    """Review 7, B1, for results built from node fills, with Kevin's Nov 8 delay rule: the Flow Deck card's buckets
    (hl_alerts) and the wallet split (hl_wallets) are available in the latest snapshot when built, with the node
    fills LIVE; read 2.5 s later, past the stale limit by their delay bound (set here 2 s above the delay they had
    when built), they are withheld with the reason, through chat and the tab's tool route."""
    from hl_desk import runtime, tools
    e = _flip_buy()
    _, (probe, _) = _plugin_run(surfaces, [{"execs": [e]}])
    d = probe["freshness"]["delay_ms"]                  # this feed's delay when built (it was silent ~50 s before)
    plugin, (card, wal) = _plugin_run(surfaces, [{"execs": [e]}],
                                      client_cfg={"delayed_after_ms": d + 1_000, "stale_after_ms": d + 2_000})
    assert card["available"] and card["freshness"]["state"] == "LIVE" and wal["available"]
    assert card["freshness"]["delay_ms"] == d

    def latest(tool, extra=None):
        monkeypatch.setattr(runtime.time, "time", lambda: (NOW + 2_500) / 1000)
        monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: (NOW - T0 + 2_500) * 10 ** 6)
        via_chat = json.loads(tools.run_tool(tool, {"market": SP, **(extra or {})}, plugin.rt))
        if tool == "hl_alerts":
            via_tab = plugin.http.get(f"/api/plugins/hyperliquid-desk/tool/{tool}", params={"market": SP}).json()
            strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
            assert strip(via_chat) == strip(via_tab)
        return via_chat
    ab = latest("hl_alerts")["cards"][SP]["card"]["activity_buckets"]
    assert ab["available"] is False and "node fills STALE" in ab["reason"] and "by the time this was served" in ab["reason"]
    assert all(ab[k]["value"] is None for k in ("new_long", "cover", "new_short", "exit"))
    assert not any(k.endswith("_pct") for k in ab)
    split = latest("hl_wallets", {"window_min": 5})["panel"]["node_activity_split"]
    assert split["available"] is False and "node fills STALE" in split["reason"] and "wallets" not in split


def _v(metric):
    return None if metric["value"] is None else Decimal(str(metric["value"]))


@pytest.mark.parametrize("order", ["taker_first", "maker_first"])
def test_flip_example_through_the_plugins_card(surfaces, order):
    e = _flip_buy(order)
    plugin, (card, wal) = _plugin_run(surfaces, [{"execs": [e]}, {"execs": [e]}, {"execs": [e, e]}])
    assert card["available"] and _v(card["cover"]) == 300 and _v(card["new_long"]) == 100
    assert _v(card["cover_pct"]) == 75 and _v(card["new_long_pct"]) == 25 and _v(card["buying_aggressive_usd"]) == 400
    assert card["new_short_pct"]["value"] is None and "no aggressive notional" in card["new_short_pct"]["reason"]
    assert plugin.mgr.live.markets[SP].flow.counters["duplicates"] == 3


def test_selling_flip_and_different_prices_through_the_plugins_card(surfaces):
    sell_flip = _exec(2, "101", "5", 0, "A", [_p(A, "A", "taker", {"exit": "2", "new_short": "3"}),
                                               _p(B, "B", "maker", {"cover": "0", "new_long": "5"})])
    buy_lo = _exec(3, "99.5", "2", 0, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "2"}),
                                             _p(B, "A", "maker", {"exit": "2", "new_short": "0"})])
    _, (card, _) = _plugin_run(surfaces, [{"execs": [sell_flip]}, {"execs": [buy_lo]}])
    assert _v(card["exit"]) == 202 and _v(card["new_short"]) == 303 and _v(card["new_long"]) == Decimal("199.0")
    assert _v(card["cover"]) == 0 and _v(card["selling_aggressive_usd"]) == 505
    assert _v(card["exit_pct"]) == round(Decimal(202) / Decimal(505) * 100, 4)


def test_missing_position_effect_through_the_plugins_card(surfaces):
    ok = _exec(4, "100", "1", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "1"}), _p(B, "A", "maker", {"exit": "1", "new_short": "0"})])
    missing = _exec(5, "100", "3", 0, "B", [_p(W, "B", "taker", unavailable="startPosition absent"),
                                             _p(B, "A", "maker", {"exit": "3", "new_short": "0"})])
    _, (card, _) = _plugin_run(surfaces, [{"execs": [ok, missing]}])
    for k in ("new_long", "cover", "new_long_pct", "cover_pct"):
        assert card[k]["value"] is None and "startPosition absent" in card[k]["reason"], k    # never scaled
    assert _v(card["buying_unclassified_usd"]) == 300


def test_executions_without_a_single_taker_or_unreadable_leave_both_sides_unavailable(surfaces):
    """An execution whose aggressive side can't be told, or that can't be read at all, could belong to either
    side: no bucket is shown as complete."""
    ok = _exec(11, "100", "1", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "1"}), _p(B, "A", "maker", {"exit": "1", "new_short": "0"})])
    no_taker = _exec(12, "100", "2", 0, "B", [_p(A, "B", "maker", {"cover": "0", "new_long": "2"}),
                                               _p(B, "A", "maker", {"exit": "2", "new_short": "0"})])
    _, (card, _) = _plugin_run(surfaces, [{"execs": [ok, no_taker]}])
    assert card["available"] is False and "no single identified taker" in card["reason"]
    for k in ("new_long", "cover", "new_short", "exit"):
        assert card[k]["value"] is None and "no single identified taker" in card[k]["reason"], k
    assert _v(card["unclassified_usd"]) == 200
    unreadable = {"tid": 13, "px": "100", "time_ms": 0, "aggressor": "B", "complete": True, "participants": []}  # no sz
    _, (card, _) = _plugin_run(surfaces, [{"execs": [ok]}, {"execs": [unreadable]}])
    assert card["available"] is False and "couldn't be read" in card["reason"]


def test_an_incomplete_execution_never_gives_a_split(surfaces):
    """Review 4, B4. The producer marks an execution incomplete when its participants' records disagree. Here the
    buy-maker record is 4 at 100 (the execution's size) while the sell taker's own effect is exit 2, new short 8
    (a size of 10): split as known, the selling percentages would add up to 250%. The card, its burst and the
    wallet split are unavailable, with the reason and the unclassified notional."""
    e = dict(_exec(91, "100", "4", 0, "A", [_p(W, "B", "maker", {"cover": "0", "new_long": "4"}),
                                            _p(B, "A", "taker", {"exit": "2", "new_short": "8"})]), complete=False)
    hist = []
    plugin, (card, wal) = _plugin_run(surfaces, [{"execs": [e]}], desk_cfg={"node_burst_min_usd": {SP: 400.0}},
                                      history=hist)
    assert card["available"] is False and "incomplete or conflicting" in card["reason"]
    assert _v(card["unclassified_usd"]) == 400
    assert all(card[k]["value"] is None for k in ("new_long", "cover", "new_short", "exit"))
    assert not any(k.endswith("_pct") for k in card)
    # Its $400 meets the node burst threshold, but a rejected record makes the window incomplete: no burst.
    assert not [ev for ev in hist[-1][1]["events"] if ev.get("type") == "burst"]
    counters = hist[-1][1]["node_status"]["markets"][SP]["bursts"]["counters"]
    assert counters["fired"] == 0 and counters["incomplete_windows"] >= 1
    assert counters["rejected"] == {"an execution whose participants' records are missing or disagree (complete false)": 1}
    assert wal["wallets"][W]["full"]["available"] is False
    conflicting = dict(_exec(92, "100", "4", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "4"}),
                                                      _p(B, "A", "maker", {"exit": "4", "new_short": "0"})]), conflict=True)
    _, (card, _) = _plugin_run(surfaces, [{"execs": [conflicting]}])
    assert card["available"] is False and "incomplete or conflicting" in card["reason"]


def test_a_sequence_gap_between_contributing_fills_through_the_plugins_card(surfaces):
    """A real gap (the line holding the second fill is lost): every family is invalid, the flow window restarts at
    the next watermark, and the card's buckets are unavailable rather than missing a fill."""
    first = _exec(20, "100", "1", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "1"}), _p(B, "A", "maker", {"exit": "1", "new_short": "0"})])
    lost = _exec(21, "100", "2", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "2"}), _p(B, "A", "maker", {"exit": "2", "new_short": "0"})])
    after = _exec(22, "100", "4", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "4"}), _p(B, "A", "maker", {"exit": "4", "new_short": "0"})])
    plugin, (card, wal) = _plugin_run(surfaces, [{"execs": [first]}, {"execs": [lost]}, {"execs": [after]}], drop=1)
    assert card["available"] is False and "observed only since" in card["reason"]
    assert all(card[k]["value"] is None for k in ("new_long", "cover", "new_short", "exit"))
    assert wal["available"] is False and "observed only since" in wal["reason"]
    flow = plugin.mgr.live.markets[SP].flow
    assert 21 not in {e.tid for e in flow.execs.values()} and flow.coverage()["earlier_windows"][-1]["ended_by"]
    assert plugin.mgr.live.markets[SP].counters["gaps"] == 1


def test_wallet_half_maker_half_taker_through_the_plugins_wallet_panel(surfaces):
    maker_buy = _exec(6, "10", "5", 0, "A", [_p(W, "B", "maker", {"cover": "0", "new_long": "5"}),
                                              _p(B, "A", "taker", {"exit": "5", "new_short": "0"})])
    taker_buy = _exec(7, "10", "5", 0, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "5"}),
                                              _p(B, "A", "maker", {"exit": "5", "new_short": "0"})])
    _, (_, wal) = _plugin_run(surfaces, [{"execs": [maker_buy, taker_buy]}])
    s = wal["wallets"][W]
    assert wal["available"] and wal["provenance"]["mode"] == "LIVE" and wal["freshness"]["state"] == "LIVE"
    assert Decimal(str(s["full"]["bought_usd"])) == 100 and Decimal(str(s["full"]["pct"]["new_long"])) == 100   # never 200%
    assert Decimal(str(s["aggressive"]["bought_usd"])) == 50 and Decimal(str(s["aggressive"]["pct"]["new_long"])) == 100


def test_wallet_with_only_maker_buys_through_the_plugins_wallet_panel(surfaces):
    maker_buy = _exec(8, "10", "5", 0, "A", [_p(W, "B", "maker", {"cover": "0", "new_long": "5"}),
                                              _p(B, "A", "taker", {"exit": "5", "new_short": "0"})])
    _, (_, wal) = _plugin_run(surfaces, [{"execs": [maker_buy]}])
    s = wal["wallets"][W]
    assert Decimal(str(s["full"]["pct"]["new_long"])) == 100
    assert s["aggressive"]["pct"]["new_long"] is None and "percentage unavailable" in s["aggressive"]["pct_reason"]["buying"]


@pytest.mark.parametrize("order", ["taker_first", "maker_first"])
def test_flip_example_in_either_order_and_with_retries(order):
    e = _flip_buy(order)
    f = _flow_with({"execs": [e]}, {"execs": [e]}, {"execs": [e, e]})      # retried in later messages and twice in one
    execs, cov = f.window(1_000_000, 1_000_010)
    assert len(execs) == 1 and f.counters["duplicates"] == 3
    a = activity_buckets(execs)
    assert a["buying"]["buckets"] == {"new_long": Decimal(100), "cover": Decimal(300)}
    assert a["buying"]["pct"] == {"new_long": Decimal(25), "cover": Decimal(75)}
    assert a["buying"]["aggressive_usd"] == Decimal(400)
    assert a["selling"]["aggressive_usd"] == 0 and a["selling"]["reason"] == "no aggressive notional on this side"


def test_selling_flip_and_executions_at_different_prices():
    sell_flip = _exec(2, "101", "5", 1_000_001, "A", [_p(A, "A", "taker", {"exit": "2", "new_short": "3"}),
                                                       _p(B, "B", "maker", {"cover": "0", "new_long": "5"})])
    buy_lo = _exec(3, "99.5", "2", 1_000_002, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "2"}),
                                                     _p(B, "A", "maker", {"exit": "2", "new_short": "0"})])
    f = _flow_with({"execs": [sell_flip]}, {"execs": [buy_lo]})
    a = activity_buckets(f.window(1_000_000, 1_000_010)[0])
    assert a["selling"]["buckets"] == {"exit": Decimal("202"), "new_short": Decimal("303")}      # each at its own price
    assert a["buying"]["buckets"] == {"new_long": Decimal("199.0"), "cover": Decimal(0)}
    assert a["selling"]["pct"]["exit"] == Decimal(202) / Decimal(505) * 100


def test_missing_position_effect_is_unavailable_never_scaled():
    ok = _exec(4, "100", "1", 1_000_001, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "1"}), _p(B, "A", "maker", {"exit": "1", "new_short": "0"})])
    missing = _exec(5, "100", "3", 1_000_002, "B", [_p(W, "B", "taker", unavailable="startPosition absent"),
                                                     _p(B, "A", "maker", {"exit": "3", "new_short": "0"})])
    f = _flow_with({"execs": [ok, missing]})
    a = activity_buckets(f.window(1_000_000, 1_000_010)[0])
    assert a["buying"]["available"] is False and a["buying"]["unclassified_usd"] == Decimal(300)
    assert a["buying"]["buckets"] == {"new_long": None, "cover": None}
    assert "startPosition absent" in a["buying"]["reason"]


def test_a_gap_between_contributing_fills_restarts_the_window():
    c = client()
    f = NodeFlow("test")
    f.on_watermark(10, 1_000_000)
    f.on_execs({"execs": [_flip_buy()]}, 11, 1_000_010)
    f.on_watermark(11, 1_000_010)
    f.boundary("sequence gap")
    f.on_execs({"execs": [_exec(9, "100", "1", 1_000_020, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "1"})])]},
               12, 1_000_020)                            # before the first watermark after the gap: not counted
    f.on_watermark(13, 1_000_030)
    execs, cov = f.window(1_000_000, 1_000_040)
    assert execs == [] and cov["covers"] is False and "observed only since" in cov["reason"]
    assert f.counters["before_window"] == 1 and cov["earlier_windows"][-1]["ended_by"] == "sequence gap"


def test_wallet_buying_100_half_maker_half_taker_all_opening_longs():
    maker_buy = _exec(6, "10", "5", 1_000_001, "A", [_p(W, "B", "maker", {"cover": "0", "new_long": "5"}),
                                                      _p(B, "A", "taker", {"exit": "5", "new_short": "0"})])
    taker_buy = _exec(7, "10", "5", 1_000_002, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "5"}),
                                                      _p(B, "A", "maker", {"exit": "5", "new_short": "0"})])
    s = wallet_splits(_flow_with({"execs": [maker_buy, taker_buy]}).window(1_000_000, 1_000_010)[0], {W})[W]
    assert s["full"]["bought_usd"] == 100 and s["full"]["pct"]["new_long"] == 100       # never 200%
    assert s["aggressive"]["bought_usd"] == 50 and s["aggressive"]["pct"]["new_long"] == 100


def test_wallet_with_only_maker_buys_has_no_aggressive_percentage():
    maker_buy = _exec(8, "10", "5", 1_000_001, "A", [_p(W, "B", "maker", {"cover": "0", "new_long": "5"}),
                                                      _p(B, "A", "taker", {"exit": "5", "new_short": "0"})])
    s = wallet_splits(_flow_with({"execs": [maker_buy]}).window(1_000_000, 1_000_010)[0], {W})[W]
    assert s["full"]["pct"]["new_long"] == 100
    assert s["aggressive"]["pct"]["new_long"] is None and "percentage unavailable" in s["aggressive"]["pct_reason"]["buying"]


def test_wallet_split_is_unavailable_while_an_execution_misses_a_record_or_address(surfaces):
    """Step 14: a missing identity makes the split unavailable. The second execution has only the taker's record
    (complete: false), so the maker could have been W: W's split isn't shown from the fills it can see."""
    buy = _exec(40, "10", "5", 0, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "5"}),
                                         _p(B, "A", "maker", {"exit": "5", "new_short": "0"})])
    partial = dict(_exec(41, "10", "3", 0, "B", [_p(A, "B", "taker", {"cover": "0", "new_long": "3"})]), complete=False)
    _, (_, wal) = _plugin_run(surfaces, [{"execs": [buy]}, {"execs": [partial]}])
    s = wal["wallets"][W]
    for part in ("full", "aggressive"):
        assert s[part]["available"] is False and "missing a participant's record or address" in s[part]["reason"]
        assert "pct" not in s[part]
    _, (_, wal) = _plugin_run(surfaces, [{"execs": [buy]}])          # without it, the same wallet's split is shown
    assert wal["wallets"][W]["full"]["available"] is True


def test_one_address_on_both_sides_is_two_participants():
    self_trade = _exec(10, "100", "2", 1_000_001, "B", [_p(W, "B", "taker", {"cover": "0", "new_long": "2"}),
                                                        _p(W, "A", "maker", {"exit": "2", "new_short": "0"})])
    s = wallet_splits(_flow_with({"execs": [self_trade]}).window(1_000_000, 1_000_010)[0], {W})[W]
    assert s["full"]["bought_usd"] == 200 and s["full"]["sold_usd"] == 200
    assert s["full"]["usd"]["new_long"] == 200 and s["full"]["usd"]["exit"] == 200


# --- the recorded window ---------------------------------------------------------------------

def _independent_totals(msgs, market):
    """Executions counted once by tid, straight from the raw JSON, with the handoff's bucket rule."""
    seen, buy, sell = set(), {"new_long": Decimal(0), "cover": Decimal(0)}, {"new_short": Decimal(0), "exit": Decimal(0)}
    vol = Decimal(0)
    for _, m in msgs:
        if m.get("market") != market or m["kind"] != "execs":
            continue
        for e in m["body"]["execs"]:
            if e["tid"] in seen:
                continue
            seen.add(e["tid"])
            px, sz = Decimal(e["px"]), Decimal(e["sz"])
            vol += px * sz
            takers = [p for p in e["participants"] if p["role"] == "taker"]
            if len(takers) != 1:
                continue
            eff = takers[0]["effect"]
            if takers[0]["side"] == "B":
                buy["new_long"] += Decimal(eff["new_long"]) * px
                buy["cover"] += Decimal(eff["cover"]) * px
            else:
                sell["new_short"] += Decimal(eff["new_short"]) * px
                sell["exit"] += Decimal(eff["exit"]) * px
    return seen, buy, sell, vol


def _client_from(msgs, source="sock-a"):
    h, _ = read_recording(EXECS)
    c = client(source)
    p = h["producer"]
    c.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
               "markets": {}}, p["started_ms"], 0)
    for rx, m in msgs:
        mf = c._market(m["market"])
        mf.last_seq = m["seq"] - 1                      # this fixture keeps only execs and watermarks: treat as contiguous
        c.on_line(m, rx, rx * 10 ** 6)
    return c


def test_recorded_window_totals_once_per_execution_against_an_independent_count():
    _, msgs = read_recording(EXECS)
    for market in ("BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"):
        c = _client_from(msgs)
        mf = c.markets[market]
        first_wm = next(m["block"] for _, m in msgs if m["market"] == market and m["kind"] == "watermark")
        counted = [(rx, m) for rx, m in msgs if m["market"] == market and m["block"] > first_wm]
        seen, buy, sell, vol = _independent_totals(counted, market)
        assert set(mf.flow.execs) == seen
        a = activity_buckets(list(mf.flow.execs.values()))
        assert a["buying"]["buckets"] == buy and a["selling"]["buckets"] == sell
        assert sum((e.notional for e in mf.flow.execs.values()), Decimal(0)) == vol


def test_duplicate_reversed_and_retried_arrival_and_a_source_switch():
    _, msgs = read_recording(EXECS)
    hype = [(rx, m) for rx, m in msgs if m["market"] == "HYPE"]
    f = NodeFlow("relay")
    wm0 = hype[0][1]
    first = next(m for _, m in hype if m["kind"] == "watermark")
    f.on_watermark(first["block"], first["block_time_ms"])
    bodies = [m for _, m in hype if m["kind"] == "execs" and m["block"] > first["block"]]
    for m in reversed(bodies):                           # reversed arrival
        f.on_execs(m["body"], m["block"], m["block_time_ms"])
    for m in bodies[:10]:                                # retries of messages already counted
        f.on_execs(m["body"], m["block"], m["block_time_ms"])
    seen, buy, sell, vol = _independent_totals([(0, m) for m in bodies], "HYPE")
    assert set(f.execs) == seen
    assert sum((e.notional for e in f.execs.values()), Decimal(0)) == vol
    # A source switch (another client: a different input stream) starts its own labelled window; nothing is summed.
    c1 = _client_from(hype, source="unix:/run/feed.sock")
    c2 = _client_from(hype[len(hype) // 2:], source="wss://relay.example/v1/feed")
    t1, t2 = c1.markets["HYPE"].flow, c2.markets["HYPE"].flow
    assert t1.stream != t2.stream and t2.since_block > t1.since_block
    # Each window holds exactly the executions after its own first watermark, counted independently: nothing of
    # the first source is carried into the second, and nothing is summed across them.
    for t, part in ((t1, hype), (t2, hype[len(hype) // 2:])):
        wm = next(m["block"] for _, m in part if m["kind"] == "watermark")
        seen, buy, sell, vol = _independent_totals([(rx, m) for rx, m in part if m["block"] > wm], "HYPE")
        assert set(t.execs) == seen and sum((e.notional for e in t.execs.values()), Decimal(0)) == vol
        a = activity_buckets(list(t.execs.values()))
        assert a["buying"]["buckets"] == buy and a["selling"]["buckets"] == sell


def test_recorded_liquidations_counted_once_for_the_liquidated_trader():
    """21 liquidation executions in the window are 3 liquidations; the counterparty's record is present in each."""
    _, msgs = read_recording(EXECS)
    ex = []
    for rx, m in msgs:
        if m["kind"] == "execs":
            for e in m["body"]["execs"]:
                pe = parse_exec(e, m["block"], m["block_time_ms"])
                if pe.liquidation is not None:
                    assert len(pe.participants) == 2 and sum(p.liquidated for p in pe.participants) == 1
                    ex.append(pe)
    assert len(ex) == 21
    liqs = liquidations(ex)
    assert len(liqs) == 3 and all(g["position"] == "long" for g in liqs)
    by_raw = {}
    for e in ex:
        v = [p for p in e.participants if p.liquidated][0]
        by_raw.setdefault((e.liquidation["liquidated_user"], v.oid), Decimal(0))
        by_raw[(e.liquidation["liquidated_user"], v.oid)] += e.px * e.sz
    assert {(g["liquidated_user"], g["oid"]): g["notional"] for g in liqs} == by_raw
    # Each group is one liquidation order sweeping several makers: its fills share one block, one mark price
    # and one method, at several prices.
    shape = {}
    for e in ex:
        v = [p for p in e.participants if p.liquidated][0]
        s = shape.setdefault((e.liquidation["liquidated_user"], v.oid), {"blocks": set(), "marks": set(), "methods": set(), "px": set()})
        s["blocks"].add(e.block)
        s["marks"].add(e.liquidation["mark_px"])
        s["methods"].add(e.liquidation["method"])
        s["px"].add(e.px)
    assert all(len(s["blocks"]) == len(s["marks"]) == len(s["methods"]) == 1 for s in shape.values())
    assert all(len(s["px"]) >= 2 for s in shape.values())


def test_liquidation_burst_alert_through_the_plugin(surfaces):
    """The recorded HYPE burst reaches the user through hl_alerts and the tab's events, from node fills, with its
    provenance and freshness; BTC's single liquidation never alerts. Read at every watermark."""
    h, msgs = read_recording(EXECS)
    plugin = surfaces(["BTC", "HYPE"])
    c = plugin.attach(client("sock-a"))
    p = h["producer"]
    c.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
               "markets": {}}, p["started_ms"], 0)
    shown = None
    for rx, m in msgs:
        if m["market"] not in ("BTC", "HYPE"):
            continue
        c._market(m["market"]).last_seq = m["seq"] - 1      # this fixture keeps only execs and watermarks
        c.on_line(m, rx, rx * 10 ** 6)
        if m["kind"] != "watermark":
            continue
        for mk in ("BTC", "HYPE"):
            plugin.read(mk, rx)
            liq = [e for e in plugin.last["events"] if e.get("type") == "liquidation_burst"]
            tab_liq = [e for e in plugin.last["tab"]["deck"]["events"] if e.get("type") == "liquidation_burst"]
            assert liq == tab_liq
            if mk == "BTC":
                assert liq == []                           # one liquidation: below the 2-in-60 s rule
            elif liq:
                assert len(liq) == 1 and (shown is None or liq[0]["at_ms"] == shown["at_ms"])
                shown = liq[0]
    assert shown is not None
    assert shown["liquidations"] == 2 and shown["longs_liquidated_usd"] == shown["notional_usd"]
    assert shown["shorts_liquidated_usd"] == 0 and shown["source"] == "node fills"
    assert shown["provenance"]["source"] == "sock-a" and shown["freshness"]["family"] == "flow"


def test_liquidation_burst_alert_from_the_recorded_window():
    _, msgs = read_recording(EXECS)
    c = _client_from(msgs)
    events = c.markets["HYPE"].panels.liq_events
    assert len(events) == 1
    ev = events[0]
    assert ev["liquidations"] == 2 and ev["longs_liquidated_usd"] == ev["notional_usd"] and ev["shorts_liquidated_usd"] == 0
    assert len(c.markets["BTC"].panels.liq_events) == 0          # BTC had one liquidation: below the 2-in-60 s rule


# --- step 15: Flow Deck buckets against hl_deck.py's definitions ----------------------------

HL_DECK = Path(os.environ.get("HL_DECK_SOURCE") or "/nonexistent/hl_deck.py")


@pytest.mark.skipif(not HL_DECK.exists(), reason="needs HL_DECK_SOURCE: the hl_deck.py reference (Kevin's terminal deck)")
def test_buckets_follow_hl_decks_definitions():
    """hl_deck counts aggressive (crossed) fills into Open Long / Close Short (buying) and Open Short / Close Long
    (selling) by the node's dir text, at px*sz. The feed has no dir text: its buckets come from each taker's
    signed start position (handoff step 7). On every non-flip execution the two definitions pick the same bucket
    and amount; a flip, which hl_deck drops, fills both buckets here (handoff steps 7 and 14)."""
    src = HL_DECK.read_text()
    assert re.search(r'nl=d\.get\("Open Long"', src) and re.search(r'sc=d\.get\("Close Short"', src)
    assert re.search(r'ns=d\.get\("Open Short"', src) and re.search(r'le=d\.get\("Close Long"', src)
    assert "r[5]" in src and "crossed" in src                 # aggressive (crossed) fills only
    assert not re.search(r'd\.get\("Long > Short"|d\.get\("Short > Long"', src)   # hl_deck counts no flip
    _, msgs = read_recording(EXECS)
    hl_deck_side = {"Open Long": "buy", "Close Short": "buy", "Open Short": "sell", "Close Long": "sell"}
    plugin_bucket = {"Open Long": "new_long", "Close Short": "cover", "Open Short": "new_short", "Close Long": "exit"}
    flips = per_label = 0
    for _, m in msgs:
        if m["kind"] != "execs":
            continue
        for e in m["body"]["execs"]:
            tk = [p for p in e["participants"] if p["role"] == "taker"][0]
            eff = {k: Decimal(v) for k, v in tk["effect"].items()}
            nonzero = [k for k, v in eff.items() if v > 0]
            if len(nonzero) == 2:
                # A flip: hl_deck drops it; here it fills both buckets of its side, which add up to the fill size.
                assert sum(eff.values()) == Decimal(e["sz"])
                assert set(nonzero) in ({"cover", "new_long"}, {"exit", "new_short"})
                flips += 1
                continue
            # The dir text hl_deck would read for this fill follows from the taker's side and start position:
            # a buy from flat or long opens a long, a buy from short covers; a sell from flat or short opens a short,
            # a sell from long exits.
            label = {("B", "new_long"): "Open Long", ("B", "cover"): "Close Short",
                     ("A", "new_short"): "Open Short", ("A", "exit"): "Close Long"}[(tk["side"], nonzero[0])]
            assert hl_deck_side[label] == ("buy" if tk["side"] == "B" else "sell")
            assert plugin_bucket[label] == nonzero[0] and eff[nonzero[0]] == Decimal(e["sz"])
            one = activity_buckets([parse_exec(e, m["block"], m["block_time_ms"])])
            side = "buying" if tk["side"] == "B" else "selling"
            assert one[side]["buckets"][plugin_bucket[label]] == Decimal(e["px"]) * Decimal(e["sz"])   # hl_deck's px*sz
            per_label += 1
    assert per_label == 1465 and flips == 8       # measured on this window (counted independently)


DECK_LOG = os.environ.get("HL_DECK_LIVE_LOG")


@pytest.mark.skipif(not DECK_LOG or not Path(DECK_LOG).exists(), reason="needs HL_DECK_LIVE_LOG (Kevin's deck frames)")
def test_deck_live_frames_use_the_same_bucket_vocabulary_compatibility_only():
    """Compatibility only: hl_deck's frames put new-long and cover on the buy line and new-short and exit on the
    sell line, as the plugin does. The frames are from other hours, so no number is compared."""
    buy_lines = sell_lines = 0
    with open(DECK_LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("buy ") and "new-long" in s:
                assert "cover" in s and "exit" not in s and "new-short" not in s
                buy_lines += 1
            elif s.startswith("sell ") and "new-short" in s:
                assert "exit" in s and "cover" not in s and "new-long" not in s
                sell_lines += 1
    assert buy_lines > 0 and sell_lines > 0
