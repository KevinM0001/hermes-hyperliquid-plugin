"""Regression tests for Codex review 3 (commit 556b093), one per finding, from its reproductions."""

from __future__ import annotations

import json
from pathlib import Path

from hl_desk import book as bk, feed, info_client, owner, schema, tools

from helpers import A, B, NS, T0, SnapRT, book_msg, build, make_desk, open_segment, trade

ROOT = Path(__file__).resolve().parent.parent


def _flow_values_null(m):
    v = m["flow"]["values"]
    return all(v[k]["value"] is None and v[k].get("reason") for k in ("buy_aggressive_notional", "sell_aggressive_notional", "net", "volume", "trade_count"))


# --- BLOCKING 2: right after a malformed-trade gap, nothing is fresh until a valid trade arrives ---------

def test_malformed_gap_restarts_freshness_before_any_new_trade():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 1 * NS, T0 + 1000)
    bad = trade("BTC", T0 + 2000, 100.0, 1.0, "A", A, B, 2)
    bad.pop("side")
    ms.on_trades([bad], 2 * NS, T0 + 2000)
    m = build(desk, 2 * NS + NS // 5, wall_ms=T0 + 2200)["markets"]["BTC"]
    fr = m["freshness"]["trades"]
    assert fr["status"] == "warming_up" and "coverage gap" in fr["reason"] and "side" in fr["reason"]
    assert _flow_values_null(m)
    assert m["wallets"]["1m"]["buyers"] is None and m["wallets"]["1m"]["reason"].startswith("trades warming_up")
    assert m["deck"]["card"]["regime"]["value"] is None
    # A valid trade after the gap makes progress again; the window starts at the gap.
    ms.on_trades([trade("BTC", T0 + 3000, 100.0, 2.0, "B", A, B, 3)], 3 * NS, T0 + 3000)
    m = build(desk, 3 * NS + NS // 5, wall_ms=T0 + 3200)["markets"]["BTC"]
    assert m["freshness"]["trades"]["status"] == "fresh"
    assert m["flow"]["values"]["volume"]["value"] == 200.0


# --- BLOCKING 3: the dispatcher never drops a message whose first record lacks its coin -----------------

def test_dispatcher_routes_a_message_whose_first_record_has_no_coin():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    feed.dispatch(desk, {"channel": "trades", "data": [trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)]}, 1 * NS, T0 + 1000)
    first = trade("BTC", T0 + 2000, 100.0, 1.0, "B", A, B, 2)
    first.pop("coin")
    feed.dispatch(desk, {"channel": "trades", "data": [first, trade("BTC", T0 + 2000, 100.0, 2.0, "B", A, B, 3)]},
                  2 * NS, T0 + 2000)
    assert ms.store.counters["malformed"] == 1
    assert ms.store.gaps and "coin" in ms.store.gaps[-1]["reason"] and ms.store.gaps[-1]["end_ms"] == T0 + 2000
    feed.dispatch(desk, {"channel": "trades", "data": [trade("BTC", T0 + 3000, 100.0, 1.0, "B", A, B, 4)]}, 3 * NS, T0 + 3000)
    m = build(desk, 3 * NS + NS // 5, wall_ms=T0 + 3200)["markets"]["BTC"]
    assert m["flow"]["window"]["start_exclusive_ms"] == T0 + 2000
    assert m["flow"]["values"]["volume"]["value"] == 100.0          # only the trade after the bad message


def test_unattributable_trades_message_ends_every_markets_coverage():
    desk = make_desk(markets=("BTC", "HYPE"))
    for i, name in enumerate(("BTC", "HYPE")):
        open_segment(desk.markets[name], T0, conn=1)
    nocoin = trade("BTC", T0 + 2000, 100.0, 1.0, "B", A, B, 7)
    nocoin.pop("coin")
    feed.dispatch(desk, {"channel": "trades", "data": [nocoin]}, 2 * NS, T0 + 2000)
    for name in ("BTC", "HYPE"):
        st = desk.markets[name].store
        assert st.counters["unattributed_messages"] == 1
        assert st.gaps and "no attributable coin" in st.gaps[-1]["reason"]
        assert desk.markets[name].trackers["trades"].restart_reason


def test_record_naming_another_coin_is_invalid_for_the_subscription():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    feed.dispatch(desk, {"channel": "trades", "data": [trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1),
                                                       trade("ETH", T0 + 1000, 3000.0, 1.0, "B", A, B, 2)]},
                  1 * NS, T0 + 1000)
    assert ms.store.counters["malformed"] == 1 and "coin" in ms.store.gaps[-1]["reason"]


# --- BLOCKING 4: account age is judged when both answers are in hand -----------------------------------

def test_slow_account_requests_count_as_age(monkeypatch):
    clock = {"ms": T0}
    monkeypatch.setattr(info_client.time, "time", lambda: clock["ms"] / 1000)
    # the answer is 105 s old at the request: within the 120 s limit when the state answer lands (114 s), past it
    # once both answers are in hand (123 s), which is when the age is judged
    state = {"time": T0 - 105_000, "marginSummary": {"accountValue": "700", "totalNtlPos": "700", "totalMarginUsed": "70"},
             "assetPositions": []}

    def slow_post(url, body):
        clock["ms"] += 9000                         # each request takes 9 s (within its 10 s timeout)
        return state if body["type"] == "clearinghouseState" else []

    monkeypatch.setattr(info_client, "post_info", slow_post)
    cur = info_client.fetch_wallet_current("http://info.invalid", A, "")
    assert cur["requested_ms"] == T0 and cur["state_received_ms"] == T0 + 9000 and cur["returned_ms"] == T0 + 18000
    acc = tools.current_account(cur, "", (0.05, "fixed for tests"))["account"]
    assert acc["freshness"]["source_age_s"] == 123.0 and acc["freshness"]["status"] == "delayed"
    assert acc["freshness"]["source_max_age_s"] == 120.0 and "limit 120.0 s" in acc["freshness"]["reason"]
    assert acc["margin_summary"]["account_value_usd"]["value"] is None


def test_account_without_a_serving_time_is_unverified():
    cur = {"clearinghouseState": {"time": T0, "marginSummary": {"accountValue": "1"}, "assetPositions": []},
           "frontendOpenOrders": []}
    acc = tools.current_account(cur, "", (0.05, "fixed"))["account"]
    assert acc["freshness"]["status"] == "unverified" and acc["positions"]["value"] is None


# --- DEFERRED 1: impossible book numbers are never summed -------------------------------------------

def test_negative_or_zero_sizes_and_fractional_counts_are_rejected():
    levels, problems = bk.parse_levels([{"px": "99.9", "sz": "-2", "n": 1}, {"px": "99", "sz": "1", "n": 2.5},
                                        {"px": "0", "sz": "1", "n": 1}, {"px": "98", "sz": "0", "n": 1}])
    assert levels == [(99.0, 1.0, None)]                 # fractional n is not truncated to 2
    assert [p["index"] for p in problems] == [0, 2, 3]
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, [(99.9, -2, 1), (99, 1, 1)], [(100.1, 1, 1), (101, 1, 1)]), 0, T0)
    m = build(desk, 1)["markets"]["BTC"]
    assert m["book"]["top"]["best_bid"]["value"] is None
    for band in m["book"]["depth"]["bands"]:
        assert band["exact"]["bid"]["notional_usd"]["value"] is None


# --- DEFERRED 2: the skill no longer calls public PRESSURE node-only ---------------------------------

def test_skill_does_not_call_pressure_node_only():
    text = (ROOT / "skills" / "hyperliquid-commentary" / "SKILL.md").read_text()
    assert not any("PRESSURE" in ln and "needs node mode" in ln and "available on public data" not in ln
                   for ln in text.splitlines())


# --- DEFERRED 3: the CVD rule and the clock note match the implementation -----------------------------

def test_cvd_rule_and_clock_note_are_current():
    c = schema.load()["metrics"]["flow.cvd_bucket"]["completeness_rule"]
    assert "cumulative origin" in c and "first complete bucket shown" not in c
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_book(book_msg("BTC", T0, [(99.9, 1, 1)], [(100.1, 1, 1)]), 0, T0)
    note = build(desk, 1)["markets"]["BTC"]["clock"]["note"]
    assert "doesn't change freshness" not in note and "withholds freshness" in note


# --- DEFERRED 4: the lifecycle test accepts the owner-socket fallback ---------------------------------

def test_lifecycle_expects_the_path_sock_path_returns():
    # sock_path only computes a path; fixed paths keep this independent of the test's temp directory.
    assert str(owner.sock_path(Path("/" + "y" * 120))).endswith(".sock")
    assert not str(owner.sock_path(Path("/" + "y" * 120))).endswith("owner.sock")
    assert str(owner.sock_path(Path("/hd"))) == "/hd/owner.sock"
