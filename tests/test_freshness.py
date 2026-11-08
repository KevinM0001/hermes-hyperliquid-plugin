"""Freshness thresholds, frozen sources, quiet markets, disconnects and clock skew.

Each threshold comes from schema/metrics.v1.json and is tested at, just
inside and just past its limit, on the monotonic clock.
"""

from __future__ import annotations

import pytest

from hl_desk import schema

from helpers import A, B, NS, T0, book_msg, make_desk, open_segment, trade, build

FAST = [(100.0 - i * 0.1, 1.0, 1) for i in range(5)]
FAST_ASK = [(100.1 + i * 0.1, 1.0, 1) for i in range(5)]


def status(desk, now_ns, field, market="BTC"):
    snap = build(desk, now_ns)
    return snap, snap["markets"][market]["freshness"][field]


def test_thresholds_are_the_schema_values():
    l2 = schema.freshness_field("l2Book")["subscriptions"]
    assert l2["fast"]["max_age_s"] == 3.0 and l2["full"]["max_age_s"] == 15.0
    assert schema.freshness_field("trades")["max_age_s_by_market"]["BTC"] == 30.0
    assert schema.freshness_field("activeAssetCtx")["max_age_s_by_market"]["BTC"] == 20.0
    assert schema.freshness_field("bbo")["progress_signal"] == "none"


@pytest.mark.parametrize("age_s,expected", [(0.0, "fresh"), (3.0, "fresh"), (3.001, "stale")])
def test_fast_book_max_age(age_s, expected):
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 5 * NS, T0)
    snap, f = status(desk, 5 * NS + int(age_s * NS), "book_fast")
    assert f["status"] == expected
    top = snap["markets"]["BTC"]["book"]["top"]
    if expected == "fresh":
        assert top["best_bid"]["value"] == 100.0
    else:
        assert top["best_bid"]["value"] is None and "stale" in top["best_bid"]["reason"]


def test_frozen_source_behind_a_live_connection_turns_stale():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    # The socket keeps delivering pushes, but their source time never advances.
    for k in range(1, 20):
        ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), int(k * 0.5 * NS), T0)
    snap, f = status(desk, int(9.5 * NS), "book_fast")
    assert f["status"] == "stale"
    assert f["messages_without_progress"] == 19
    assert f["last_message_age_s"] == 0.0
    assert snap["markets"]["BTC"]["book"]["top"]["spread"]["value"] is None


def test_rechecked_without_any_new_message():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    assert status(desk, 1 * NS, "book_fast")[1]["status"] == "fresh"
    assert status(desk, 4 * NS, "book_fast")[1]["status"] == "stale"   # same state, later clock


def test_quiet_market_values_are_null_not_zero():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, T0 + 1000)
    snap, f = status(desk, 30 * NS, "trades")
    assert f["status"] == "fresh"
    snap, f = status(desk, int(30.001 * NS), "trades")
    assert f["status"] == "unverified"
    assert "quiet market" in f["reason"]
    vals = snap["markets"]["BTC"]["flow"]["values"]
    assert all(v["value"] is None for v in vals.values())
    assert snap["markets"]["BTC"]["wallets"]["5m"]["buyers"] is None


def test_quiet_window_with_real_zero_selling_is_a_real_zero():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, T0 + 1000)
    snap, _ = status(desk, 1 * NS, "trades")
    vals = snap["markets"]["BTC"]["flow"]["values"]
    assert vals["sell_aggressive_notional"]["value"] == 0.0        # complete, fresh window with no selling
    assert vals["buy_aggressive_notional"]["value"] == 100.0


def test_bbo_is_never_fresh():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_bbo({"coin": "BTC", "time": T0, "bbo": [{"px": "100.0", "sz": "1", "n": 1}, {"px": "100.1", "sz": "1", "n": 1}]}, 0, T0)
    snap, f = status(desk, 1, "bbo")
    assert f["status"] == "unverified"
    bbo = snap["markets"]["BTC"]["book"]["bbo"]
    assert bbo["value"] is None and "sent only if the bbo changes" in bbo["reason"]


def test_context_is_never_fresh_without_a_source_timestamp():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ctx = {"markPx": "100.0", "oraclePx": "100.1", "funding": "0.0000125", "openInterest": "10",
           "dayNtlVlm": "5", "prevDayPx": "99", "midPx": "100.0"}
    ms.on_ctx({"coin": "BTC", "ctx": ctx}, 0, T0)
    for k in range(1, 30):                       # identical contexts once a second: not even receipt progress
        ms.on_ctx({"coin": "BTC", "ctx": dict(ctx)}, k * NS, T0)
    snap, f = status(desk, 1 * NS, "ctx")
    assert f["status"] == "unverified" and "no source timestamp" in f["reason"] and "1.0 s ago" in f["reason"]
    c = snap["markets"]["BTC"]["context"]
    assert c["mark"]["value"] is None and c["funding"]["value"] is None and c["funding_pct_per_hour"]["value"] is None
    assert c["funding"]["unit"] == "fraction per 1h" and c["open_interest"]["unit"] == "coin"
    snap, f = status(desk, int(20.001 * NS), "ctx")
    assert f["status"] == "unverified" and "no changed context" in f["reason"]
    ms.on_ctx({"coin": "BTC", "ctx": dict(ctx, markPx="100.5")}, 21 * NS, T0)
    snap, f = status(desk, 21 * NS, "ctx")
    assert f["status"] == "unverified" and "no source timestamp" in f["reason"]   # a change is receipt recency only


def test_disconnect_makes_every_field_disconnected():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    desk.link = "disconnected"
    snap, f = status(desk, 1, "book_fast")
    assert all(v["status"] == "disconnected" for v in snap["markets"]["BTC"]["freshness"].values())
    assert snap["markets"]["BTC"]["book"]["top"]["best_bid"]["value"] is None


def test_warming_up_before_the_first_message():
    desk = make_desk()
    desk.markets["BTC"].on_connect(1)
    snap, f = status(desk, 1, "book_fast")
    assert f["status"] == "warming_up"
    assert snap["markets"]["BTC"]["deck"]["card"]["regime"]["value"] is None


def test_clock_skew_withholds_freshness_instead_of_being_ignored():
    for skew_ms, expected in ((0, "fresh"), (3_600_000, "delayed")):
        desk = make_desk()
        ms = desk.markets["BTC"]
        ms.on_connect(1)
        for k in range(5):     # local wall clock = source time + 200 ms + skew
            ms.on_book(book_msg("BTC", T0 + k * 500, FAST, FAST_ASK), k * NS // 2, T0 + k * 500 + 200 + skew_ms)
        snap = desk.build_snapshot(int(2.5 * NS), T0 + 2000 + 200 + skew_ms)
        m = snap["markets"]["BTC"]
        assert m["freshness"]["book_fast"]["status"] == expected
        assert m["clock"]["offset_ms"] == 200 + skew_ms
        assert m["clock"]["clock_skew_suspected"] is (skew_ms != 0)


def test_delayed_backlog_advancing_an_hour_behind_is_never_fresh():
    """GPT review R11 finding 7: source times advance once per second, one message per second, an hour behind."""
    hour = 3_600_000
    for reconnect in (False, True):
        desk = make_desk()
        ms = desk.markets["BTC"]
        open_segment(ms, T0 - hour, mono_ns=0)
        if reconnect:
            ms.on_disconnect("test", T0)
            open_segment(ms, T0 - hour + 30_000, mono_ns=0, conn=2)
        base = T0 - hour + 60_000
        for k in range(1, 40):
            ms.on_book(book_msg("BTC", base + k * 1000, FAST, FAST_ASK), k * NS, T0 + k * 1000)
            ms.on_trades([trade("BTC", base + k * 1000, 100.0, 1.0, "B", A, B, k)], k * NS, T0 + k * 1000)
        snap = desk.build_snapshot(39 * NS, T0 + 39_000)
        m = snap["markets"]["BTC"]
        assert m["freshness"]["book_fast"]["status"] == "delayed"
        assert m["freshness"]["trades"]["status"] == "delayed"
        assert m["book"]["top"]["best_bid"]["value"] is None
        assert m["flow"]["values"]["volume"]["value"] is None
        assert m["deck"]["card"]["regime"]["value"] is None
        assert m["wallets"]["1m"]["buyers"] is None


@pytest.mark.parametrize("age_ms,expected", [(4950, "fresh"), (5000, "unverified"), (5051, "delayed")])
def test_source_age_limit_with_the_clock_bound(age_ms, expected):
    # fast book: source-age limit 5.0 s; test clock bound 0.05 s
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    assert desk.build_snapshot(1, T0 + age_ms)["markets"]["BTC"]["freshness"]["book_fast"]["status"] == expected


def test_unknown_clock_bound_means_unverified(monkeypatch):
    from hl_desk import clock
    monkeypatch.setattr(clock, "os_bound", lambda: (None, "the system clock isn't synchronized (adjtimex)"))
    desk = make_desk(clock_bound_s=None)
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    f = desk.build_snapshot(1, T0 + 200)["markets"]["BTC"]["freshness"]["book_fast"]
    assert f["status"] == "unverified" and "isn't synchronized" in f["reason"]


def test_source_time_ahead_of_local_time_breaks_the_clock_relationship():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0 + 1100, FAST, FAST_ASK), 0, T0)
    f = desk.build_snapshot(1, T0)["markets"]["BTC"]["freshness"]["book_fast"]
    assert f["status"] == "unverified" and "ahead of local time" in f["reason"]
