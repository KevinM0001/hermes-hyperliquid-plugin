"""Wallet ranks and shares (Handoff 1 step 5b), checked against values calculated independently."""

from __future__ import annotations

from fractions import Fraction

from hl_desk import wallets
from hl_desk.trades import Trade

from helpers import A, B, C, T0, make_desk, open_segment, trade, build


def T(t, px, sz, side, buyer, seller, tid):
    return Trade("BTC", t, px, sz, side, tid, f"h{tid}", buyer, seller, tid)


def handoff_case():
    # 100: A buys from B, A is the taker (side B: the buyer aggressed)
    # 60:  B buys from A, A is the taker (side A: the seller aggressed)
    # 40:  A on both sides, the buy side taking (side B)
    return [T(T0 + 1, 100.0, 1.0, "B", A, B, 1),
            T(T0 + 2, 60.0, 1.0, "A", B, A, 2),
            T(T0 + 3, 40.0, 1.0, "B", A, A, 3)]


def test_handoff_example_shares_and_net():
    trades = handoff_case()
    p = wallets.panel(trades, {"complete": True})
    assert p["unique_execution_notional_usd"] == 200.0
    a = p["wallets"][A]
    assert a["bought_usd"] == 140.0 and a["sold_usd"] == 100.0
    assert a["buy_share_pct"]["value"] == 70.0
    assert a["sell_share_pct"]["value"] == 50.0
    assert a["net_bought_usd"] == 40.0
    assert a["aggressive_bought_usd"] == 140.0      # 100 + 40, A took both buys
    assert a["aggressive_sold_usd"] == 60.0         # A was the aggressive seller of the 60
    assert a["participation_usd"] == 240.0          # participation, not volume (volume is 200)
    assert B not in p["wallets"]          # rows are kept only for listed wallets; B's comes from the tally
    b = wallets.wallet_row(B, wallets.tally(trades)[B], 200.0, 200.0)
    assert b["buy_share_pct"]["value"] == 30.0 and b["sell_share_pct"]["value"] == 50.0
    assert b["bought_usd"] == 60.0 and b["sold_usd"] == 100.0
    assert b["aggressive_bought_usd"] == 0.0 and b["aggressive_sold_usd"] == 0.0
    # Independent totals: every buyer's bought notional sums to the unique notional, same for sellers.
    assert p["buyers"]["total_usd"]["value"] == 200.0
    assert p["sellers"]["total_usd"]["value"] == 200.0


def test_handoff_example_ranks_and_target():
    p = wallets.panel(handoff_case(), {"complete": True})
    buyers = p["buyers"]
    assert [r["address"] for r in buyers["listed"]] == [A]      # A alone reaches 70% >= 50%
    assert buyers["stopped_by"] == "target"
    assert buyers["share_reached_pct"]["value"] == 70.0
    sellers = p["sellers"]
    # A and B tie at 100 each: the tie breaks by address, and A (0xaaa…) alone reaches 50%.
    assert [r["address"] for r in sellers["listed"]] == [A]
    assert sellers["share_reached_pct"]["value"] == 50.0


def test_two_hundred_equal_wallets_hit_the_cap():
    sellers = [f"0x{i:040x}" for i in range(1, 201)]
    trades = [T(T0 + i, 10.0, 1.0, "B", s, A, i) for i, s in enumerate(sellers, start=1)]
    p = wallets.panel(trades, {"complete": True})
    buyers = p["buyers"]
    assert len(buyers["listed"]) == 50
    assert buyers["stopped_by"] == "cap"
    assert buyers["share_reached_pct"]["value"] == 25.0
    assert buyers["summary"] == "Top 50 make up 25% of buying; the 50% target isn't reached."
    # Ties at every rank break by address, ascending.
    assert [r["address"] for r in buyers["listed"]] == sorted(sellers)[:50]


def test_ties_at_the_cutoff_break_by_address():
    # Shares 30, 20, 20, 20, 10: target 50% is reached at the 2nd wallet; the three 20s tie.
    w1, w2, w3, w4, w5 = (f"0x{c * 40}" for c in "19253")
    trades = [T(T0 + 1, 30, 1, "B", w1, A, 1), T(T0 + 2, 20, 1, "B", w2, A, 2),
              T(T0 + 3, 20, 1, "B", w3, A, 3), T(T0 + 4, 20, 1, "B", w4, A, 4),
              T(T0 + 5, 10, 1, "B", w5, A, 5)]
    r = wallets.panel(trades, {})["buyers"]
    assert [x["address"] for x in r["listed"]] == [w1, w3]     # 0x222… beats 0x555… and 0x999…
    assert r["share_reached_pct"]["value"] == 50.0
    assert r["stopped_by"] == "target"


def test_empty_side_share_is_unavailable_not_zero():
    r = wallets.ranked_list({}, 0.0, 50.0, 50, "buying")
    assert r["share_reached_pct"]["value"] is None
    assert r["share_reached_pct"]["reason"] == "no buying in window"
    row = wallets.wallet_row(A, {"bought": 0.0, "sold": 5.0, "aggressive_bought": 0.0,
                                 "aggressive_sold": 0.0, "executions": 1}, 0.0, 5.0)
    assert row["buy_share_pct"]["value"] is None and row["buy_share_pct"]["reason"] == "no buying in window"
    assert row["sell_share_pct"]["value"] == 100.0


def test_duplicated_execution_counts_once_through_the_store():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    t1 = trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)
    ms.on_trades([t1], 1, T0 + 1000)
    ms.on_trades([t1], 2, T0 + 1000)                 # the same (coin, time, tid) again
    ms.on_trades([t1, trade("BTC", T0 + 2000, 60.0, 1.0, "A", B, A, 2)], 3, T0 + 2000)
    assert ms.store.counters["duplicates"] == 2
    execs = ms.store.window(T0, T0 + 2000, start_inclusive=False)
    assert len(execs) == 2
    p = wallets.panel(execs, {})
    assert p["unique_execution_notional_usd"] == 160.0


def test_wallet_drilldown_trades_add_up_to_totals():
    trades = handoff_case()
    p = wallets.panel(trades, {})
    d = wallets.wallet_trades(trades, A)
    assert d["totals"]["bought_usd"] == p["wallets"][A]["bought_usd"]
    assert d["totals"]["sold_usd"] == p["wallets"][A]["sold_usd"]
    assert d["totals"]["aggressive_bought_usd"] == p["wallets"][A]["aggressive_bought_usd"]
    assert d["totals"]["aggressive_sold_usd"] == p["wallets"][A]["aggressive_sold_usd"]
    assert [e["role"] for e in d["executions"]] == ["buyer", "seller", "both"]
    assert sum(e["notional_usd"] for e in d["executions"]) == 200.0   # each execution listed once


def test_shares_match_an_independent_calculation_on_recorded_trades():
    from helpers import recorded_replayer
    rp = recorded_replayer(["BTC"])
    rp.run_until(rp.records[len(rp.records) // 3]["recv_mono_ns"])
    snap = rp.snapshot()
    ms = rp.desk.markets["BTC"]
    panel = snap["markets"]["BTC"]["wallets"]["1m"]
    win = panel["window"]
    execs = [t for t in ms.store.trades if win["start_exclusive_ms"] < t.time <= win["end_ms"]]
    bought, sold = {}, {}
    for t in execs:
        n = Fraction(str(t.px)) * Fraction(str(t.sz))
        bought[t.buyer] = bought.get(t.buyer, 0) + n
        sold[t.seller] = sold.get(t.seller, 0) + n
    tb, ts = sum(bought.values()), sum(sold.values())
    assert tb == ts == sum(Fraction(str(t.px)) * Fraction(str(t.sz)) for t in execs)
    expect_rank = sorted(bought, key=lambda a: (-bought[a], a))
    listed = panel["buyers"]["listed"]
    assert [r["address"] for r in listed] == expect_rank[:len(listed)]
    for r in listed:
        assert abs(r["share_pct"] - float(bought[r["address"]] / tb * 100)) < 1e-4   # shares are rounded to 4 dp
    cum = sum(bought[a] for a in expect_rank[:len(listed)]) / tb * 100
    assert abs(panel["buyers"]["share_reached_pct"]["value"] - float(cum)) < 1e-4
    assert float(cum) >= 50.0 or len(listed) == 50
    if len(listed) > 1:   # the list is the shortest prefix that reaches the target
        assert float(sum(bought[a] for a in expect_rank[:len(listed) - 1]) / tb * 100) < 50.0


def test_window_never_spans_a_gap_and_reports_completeness():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i in range(1, 6):
        ms.on_trades([trade("BTC", T0 + i * 10_000, 100.0, 1.0, "B", A, B, i)], i, T0 + i * 10_000)
    ms.on_disconnect("test gap", T0 + 60_000)
    open_segment(ms, T0 + 120_000, conn=2)
    ms.on_trades([trade("BTC", T0 + 130_000, 100.0, 2.0, "A", B, C, 99)], 10 * 10**9, T0 + 130_000)
    snap = build(desk, 10 * 10**9)
    p = snap["markets"]["BTC"]["wallets"]["5m"]
    assert p["window"]["complete"] is False
    assert p["window"]["start_exclusive_ms"] == T0 + 120_000      # the new segment's start, not before the gap
    assert p["executions"] == 1
    assert p["unique_execution_notional_usd"] == 200.0
    assert "incomplete" in p["window"]["completeness"]
