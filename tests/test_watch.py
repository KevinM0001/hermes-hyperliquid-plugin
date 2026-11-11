"""Kevin's swing-context page as the watch block (hl_desk/watch.py, deck.<market>.watch, the hl_watch tool): offline, canned
trades at known minutes and canned candles, every number checked by hand; the coverage rules (a gap inside a window, the
segment start inside a window), the build-time and serving-time withholding, and the tool through run_tool with a
stand-in runtime (as tests/test_candles.py and tests/test_structure.py).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from helpers import A, B, C, NS, T0, SnapRT, book_msg, build, make_desk, open_segment, trade
from test_candles import Clock, Jobs, venue

from hl_desk import schema, tools, watch
from hl_desk.candles import INTERVAL_MS, CandleStore
from hl_desk.runtime import _served
from hl_desk.serving_public import age_market
from hl_desk.trades import Trade

ROOT = Path(__file__).resolve().parent.parent
M = watch.MINUTE_MS
H, D = INTERVAL_MS["1h"], INTERVAL_MS["1d"]
SERVED = "by the time this was served"


def minute(k: int) -> int:
    return T0 + k * M


def tr(k, offset_s, px, sz, side, taker, tid, maker=B):
    """A counted Trade at minute k + offset_s; the aggressor is the buyer for side B and the seller for side A."""
    buyer, seller = (taker, maker) if side == "B" else (maker, taker)
    return Trade("BTC", minute(k) + offset_s * 1000, float(px), float(sz), side, tid, f"0x{tid:x}", buyer, seller, tid)


class Store(types.SimpleNamespace):
    """What the watch block reads of a TradeStore: the current segment, the closed segments and the declared gaps."""

    def __init__(self, segment_start_ms, watermark_ms, segments=(), gaps=()):
        super().__init__(segment_start_ms=segment_start_ms, watermark_ms=watermark_ms, segments=list(segments), gaps=list(gaps))


def fifteen():
    """Minutes 1 to 15 of a segment that began at T0: each minute one aggressive buy of 100*k by A and one aggressive
    sell of 450 by C (px 100); the watermark trade is in minute 16. Totals: buy 12,000, sell 6,750, 30 trades."""
    bars, tid = watch.MinuteBars(), 0
    for k in range(1, 16):
        tid += 1
        bars.add([tr(k, 10, 100, k, "B", A, tid)])
        tid += 1
        bars.add([tr(k, 20, 100, 4.5, "A", C, tid)])
    bars.add([tr(16, 5, 100, 0.01, "B", A, 999)])
    return bars, Store(T0, minute(16) + 5000)


def halves(n_first, n_second, first=(300, 100), second=(100, 100)):
    """n_first minutes of (buy, sell) = first, then n_second of second (px 1, so sizes are notionals), the watermark
    trade in the minute after; the segment began at T0."""
    bars, tid = watch.MinuteBars(), 0
    for k in range(1, n_first + n_second + 1):
        buy, sell = first if k <= n_first else second
        tid += 1
        bars.add([tr(k, 10, 1, buy, "B", A, tid)])
        tid += 1
        bars.add([tr(k, 20, 1, sell, "A", C, tid)])
    last = n_first + n_second + 1
    bars.add([tr(last, 1, 1, 0.01, "B", A, 9999)])
    store = Store(T0, minute(last) + 1000)
    return watch.window_minutes(bars, minute(last), 240, watch.coverage_intervals(store))


# --- minute bars, coverage and the flow windows ----------------------------------------------------------------------------

def test_flow_windows_hand_checked_with_the_segment_start_inside_the_longer_ones():
    bars, store = fifteen()
    end = minute(16)                                            # the watermark's minute: windows end there (exclusive)
    intervals = watch.coverage_intervals(store)
    assert intervals == [(T0, minute(16) + 5000)]
    assert not watch.minute_covered(minute(0), intervals)       # starts at the segment start: trades at the start aren't counted
    assert watch.minute_covered(minute(1), intervals) and watch.minute_covered(minute(15), intervals)
    assert not watch.minute_covered(minute(16), intervals)      # in progress: ends after the watermark
    f15 = watch.flow_window(watch.window_minutes(bars, end, 15, intervals), store, end - 15 * M, end)
    assert f15 == {"start_ms": minute(1), "start": "2027-01-15T08:01:00.000Z", "end_ms": minute(16), "end": "2027-01-15T08:16:00.000Z",
                   "minutes_covered": 15, "minutes_wanted": 15, "complete": True, "gap_inside": False,
                   "buy_usd": 12000.0, "sell_usd": 6750.0, "trades": 30, "imbalance": 0.28}      # (12000 - 6750) / 18750
    f1h = watch.flow_window(watch.window_minutes(bars, end, 60, intervals), store, end - 60 * M, end)
    assert f1h["start_ms"] == minute(-44) and f1h["minutes_covered"] == 15 and f1h["minutes_wanted"] == 60
    assert f1h["complete"] is False and f1h["gap_inside"] is False
    assert (f1h["buy_usd"], f1h["sell_usd"], f1h["imbalance"], f1h["trades"]) == (12000.0, 6750.0, 0.28, 30)
    assert f1h["note"] == ("incomplete: 15 of 60 minutes covered; the coverage segment began at 2027-01-15T08:00:00.000Z, inside the "
                           "window, so earlier minutes aren't covered")
    f4h = watch.flow_window(watch.window_minutes(bars, end, 240, intervals), store, end - 240 * M, end)
    assert (f4h["minutes_covered"], f4h["minutes_wanted"], f4h["complete"]) == (15, 240, False)
    # no covered minute at all: null values with the reason, never zeros
    empty = watch.flow_window(watch.window_minutes(bars, minute(-100), 15, intervals), store, minute(-115), minute(-100))
    assert empty["buy_usd"] is None and empty["imbalance"] is None and empty["trades"] is None
    assert empty["reason"] == "no covered minute in the window" and empty["minutes_covered"] == 0
    # covered minutes without trades: zero notional is a true zero, the imbalance undefined
    quiet = watch.flow_window(watch.window_minutes(watch.MinuteBars(), end, 15, intervals), store, end - 15 * M, end)
    assert (quiet["buy_usd"], quiet["sell_usd"], quiet["trades"], quiet["imbalance"]) == (0.0, 0.0, 0, None)
    assert quiet["imbalance_reason"] == "no trades in the covered minutes" and quiet["complete"] is True


def test_a_gap_inside_a_window_drops_its_minutes_and_is_declared():
    bars, _ = fifteen()
    gap = {"start_ms": minute(8) + 30_000, "end_ms": minute(10) + 10_000, "reason": "malformed trade (missing or invalid side)"}
    store = Store(minute(10) + 10_000, minute(16) + 5000,
                  segments=[{"conn": 1, "start_ms": T0, "end_ms": minute(8) + 30_000, "end_reason": gap["reason"]}], gaps=[gap])
    intervals = watch.coverage_intervals(store)
    assert intervals == [(T0, minute(8) + 30_000), (minute(10) + 10_000, minute(16) + 5000)]
    assert [k for k in range(1, 16) if watch.minute_covered(minute(k), intervals)] == [1, 2, 3, 4, 5, 6, 7, 11, 12, 13, 14, 15]
    end = minute(16)
    mins = watch.window_minutes(bars, end, 15, intervals)
    f = watch.flow_window(mins, store, end - 15 * M, end)
    # minutes 8, 9 and 10 hold counted trades, but the gap makes them uncovered: they are left out, never averaged over
    assert (f["buy_usd"], f["sell_usd"], f["trades"]) == (9300.0, 5400.0, 24)       # 100*(1..7 + 11..15) = 9300; 12 * 450
    assert f["imbalance"] == 0.2653 and f["minutes_covered"] == 12 and f["complete"] is False and f["gap_inside"] is True
    assert f["note"] == ("incomplete: 12 of 15 minutes covered; a coverage gap inside the window (malformed trade (missing or invalid "
                         "side), from 2027-01-15T08:08:30.000Z to 2027-01-15T08:10:10.000Z)")
    tk, why = watch.taker_per_hour(mins)
    assert why is None and tk["per_hour_usd"] == 73500.0 and tk["hours_covered"] == 0.2 and tk["minutes_covered"] == 12   # 14700 / 0.2
    # a window entirely after the gap doesn't report it
    later = watch.flow_window(watch.window_minutes(bars, end, 5, intervals), store, end - 5 * M, end)
    assert later["gap_inside"] is False and later["complete"] is True and later["minutes_covered"] == 5


def test_minute_bars_keep_240_minutes_and_the_wallet_sums_of_the_latest_hour():
    bars = watch.MinuteBars()
    for k in range(250):
        bars.add([tr(k, 0, 100, 1, "B", A, k + 1)])
    assert len(bars.bars) == 240 and min(bars.bars) == minute(10) and max(bars.bars) == minute(249)
    assert bars.bars[minute(249)]["wallets"] == {A: 100.0} and bars.bars[minute(189)]["wallets"] == {A: 100.0}   # 60 minutes back: kept
    assert bars.bars[minute(188)]["wallets"] is None                                                          # older: dropped
    assert bars.last == (minute(249), 250, 100.0) and bars.latest_ms == minute(249)
    b = bars.bars[minute(249)]
    assert (b["buy"], b["sell"], b["n"], b["high"], b["low"]) == (100.0, 0.0, 1, 100.0, 100.0)
    bars.add([tr(249, 30, 101, 2, "A", C, 777), tr(249, 15, 99, 1, "B", A, 778)])      # a late trade inside the minute
    b = bars.bars[minute(249)]
    assert (b["buy"], b["sell"], b["n"], b["high"], b["low"], b["last"][2]) == (199.0, 202.0, 3, 101.0, 99.0, 101.0)
    assert b["wallets"] == {A: 199.0, C: 202.0}


# --- wave, taker per hour, top wallet ------------------------------------------------------------------------------------------

def test_wave_halves_states_and_the_more_than_10_minutes_rule():
    w, why = watch.wave(halves(11, 11), False)                  # first half 300/100 (0.5), second 100/100 (0): delta -0.5
    assert why is None and w["state"] == "decelerating" and w["delta"] == -0.5
    assert w["first_half"] == {"minutes": 11, "start_ms": minute(1), "end_ms": minute(12), "buy_usd": 3300.0, "sell_usd": 1100.0,
                               "trades": 22, "imbalance": 0.5}
    assert w["second_half"] == {"minutes": 11, "start_ms": minute(12), "end_ms": minute(23), "buy_usd": 1100.0, "sell_usd": 1100.0,
                                "trades": 22, "imbalance": 0.0}
    assert w["gap_inside"] is False and "halves by count" in w["rule"] and "note" not in w
    w, why = watch.wave(halves(10, 11), False)                  # 21 covered minutes: halves of 10 and 11; 10 isn't more than 10
    assert w is None and why == "needs more than 10 covered minutes in each half of the 4h window; 10 and 11 covered"
    w, why = watch.wave(halves(11, 12), False)                  # 23 covered: the second half holds the extra minute
    assert why is None and (w["first_half"]["minutes"], w["second_half"]["minutes"]) == (11, 12)
    w, _ = watch.wave(halves(11, 11, first=(100, 100), second=(300, 100)), False)
    assert w["state"] == "accelerating" and w["delta"] == 0.5
    w, _ = watch.wave(halves(11, 11, first=(300, 100), second=(300, 100)), True)
    assert w["state"] == "flat" and w["delta"] == 0.0 and w["gap_inside"] is True and "spans a coverage gap" in w["note"]
    w, _ = watch.wave(halves(11, 11, first=(105, 95), second=(100, 100)), False)      # |0| - |0.05| = -0.05: not below -0.05
    assert w["state"] == "flat" and w["delta"] == -0.05
    w, _ = watch.wave(halves(11, 11, first=(106, 94), second=(100, 100)), False)      # -0.06: below
    assert w["state"] == "decelerating" and w["delta"] == -0.06
    w, _ = watch.wave(halves(11, 11, first=(100, 100), second=(94, 106)), False)      # |-0.06| - 0 = +0.06: the sign of the push doesn't matter
    assert w["state"] == "accelerating" and w["delta"] == 0.06
    bars, store = fifteen()
    w, why = watch.wave(watch.window_minutes(bars, minute(16), 240, watch.coverage_intervals(store)), False)
    assert w is None and why.endswith("7 and 8 covered")


def test_taker_per_hour_scales_the_covered_minutes_not_four_hours():
    bars, store = fifteen()
    mins = watch.window_minutes(bars, minute(16), 240, watch.coverage_intervals(store))
    tk, why = watch.taker_per_hour(mins)
    assert why is None
    assert tk == {"per_hour_usd": 75000.0, "notional_usd": 18750.0, "minutes_covered": 15, "hours_covered": 0.25, "method": tk["method"]}
    assert "divided by the covered hours" in tk["method"] and "the 4h total divided by 4" in tk["method"]
    tk, why = watch.taker_per_hour(watch.window_minutes(bars, minute(-100), 240, watch.coverage_intervals(store)))
    assert tk is None and why == "no covered minute in the 4h window"


def test_top_wallet_1h_both_readings():
    bars, store = fifteen()
    mins = watch.window_minutes(bars, minute(16), 60, watch.coverage_intervals(store))
    tw, why = watch.top_wallet_1h(mins)
    assert why is None
    # minute tops: C's 450 in minutes 1 to 4 (A had 100 to 400), then A's 100*k in minutes 5 to 15: 1800 + 11000 = 12800 of 18750
    assert tw["share_pct"] == 68.2667 and tw["minute_tops_usd"] == 12800.0 and tw["total_usd"] == 18750.0
    assert tw["minutes_covered"] == 15 and tw["minutes_with_trades"] == 15
    assert (tw["address"], tw["notional_usd"], tw["minute_ms"]) == (A, 1500.0, minute(15))       # the largest minute top
    assert tw["hour_top"] == {"address": A, "notional_usd": 12000.0, "share_pct": 64.0}            # the hour-wide top aggressor
    assert "minute's top aggressor" in tw["label"]
    # ties inside a minute go to the lowest address; between minute tops to the latest minute
    bars2 = watch.MinuteBars()
    bars2.add([tr(1, 0, 1, 50, "B", C, 1), tr(1, 1, 1, 50, "A", A, 2), tr(2, 0, 1, 50, "B", B, 3)])
    bars2.add([tr(3, 0, 1, 0.5, "B", A, 4)])
    st = Store(T0, minute(3) + 1)
    tw, _ = watch.top_wallet_1h(watch.window_minutes(bars2, minute(3), 60, watch.coverage_intervals(st)))
    assert (tw["address"], tw["minute_ms"], tw["notional_usd"]) == (B, minute(2), 50.0) and tw["share_pct"] == 66.6667
    assert tw["hour_top"] == {"address": A, "notional_usd": 50.0, "share_pct": 33.3333}
    tw, why = watch.top_wallet_1h(watch.window_minutes(watch.MinuteBars(), minute(3), 60, watch.coverage_intervals(st)))
    assert tw is None and "share unavailable, not 0%" in why


# --- ranges from closed candles ----------------------------------------------------------------------------------------------

def candle(t, o, h, l, c, state="closed", interval="1h"):
    return {"time": t, "close_time": t + INTERVAL_MS[interval] - 1, "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "volume_base": 1.0, "trades": 1, "state": state}


def view_of(candles, interval="1h", fetched_at_ms=T0):
    closed = [c for c in candles if c["state"] == "closed"]
    return {"ok": True, "market": "BTC", "interval": interval, "candles": list(candles),
            "history": {"count": len(closed), "available": len(closed), "fetched_at_ms": fetched_at_ms},
            "in_progress": {"status": "unverified"}, "source": "x", "provenance": {}}


def hours(n_closed=25):
    """n_closed closed hours before T0 and the hour in progress at T0. The last 24 closed: high 102 (twice, the later wins),
    low 90; the older ones and the candle in progress carry extremes that must stay out."""
    rows = []
    for i in range(n_closed):
        t = T0 - (n_closed - i) * H
        old = i < n_closed - 24
        rows.append(candle(t, 100, 500 if old else (102 if i in (n_closed - 19, n_closed - 15) else 100),
                           1 if old else (90 if i == n_closed - 4 else 95), 100 + i))
    rows.append(candle(T0, 100, 200, 10, 150, state="in_progress"))
    return rows


def test_range_24h_from_the_last_24_closed_hours_with_the_position_unclamped():
    r = watch.range_block(view_of(hours()), "1h", 24, 100.0, None)
    assert r["state"] == "ready" and r["interval"] == "1h" and r["candles_used"] == 24 and r["candles_wanted"] == 24 and r["complete"] is True
    assert (r["high"], r["high_ms"], r["low"], r["low_ms"]) == (102.0, T0 - 15 * H, 90.0, T0 - 4 * H)      # the later of the tied highs (i=10)
    assert (r["first_ms"], r["last_closed_ms"], r["last_close"], r["fetched_at_ms"]) == (T0 - 24 * H, T0 - H, 124.0, T0)
    assert r["high_at"] == "2027-01-14T17:00:00.000Z" and r["low_at"] == "2027-01-15T04:00:00.000Z"
    assert (r["position_pct"], r["outside"], r["zone"], r["middle_band_pct"]) == (83.333, None, "high", [35.0, 65.0])    # (100-90)/12
    assert "note" not in r and "position_reason" not in r
    above = watch.range_block(view_of(hours()), "1h", 24, 103.0, None)
    assert (above["position_pct"], above["outside"], above["zone"]) == (108.333, "above", "high")
    below = watch.range_block(view_of(hours()), "1h", 24, 89.0, None)
    assert (below["position_pct"], below["outside"], below["zone"]) == (-8.333, "below", "low")
    mid = watch.range_block(view_of(hours()), "1h", 24, 96.0, None)
    assert (mid["position_pct"], mid["zone"]) == (50.0, "middle")
    assert watch.range_block(view_of(hours()), "1h", 24, 94.2, None)["zone"] == "middle"       # 35.0: the band's edge is middle
    assert watch.range_block(view_of(hours()), "1h", 24, 94.19, None)["zone"] == "low"
    assert watch.range_block(view_of(hours()), "1h", 24, 97.8, None)["zone"] == "middle"       # 65.0
    assert watch.range_block(view_of(hours()), "1h", 24, 97.81, None)["zone"] == "high"
    # fewer closed candles than wanted: the range over them, said so
    few = watch.range_block(view_of(hours(5)[-6:]), "1h", 24, 100.0, None)
    assert few["candles_used"] == 5 and few["complete"] is False and few["note"] == "only 5 closed 1h candles held; the range covers them"
    assert (few["high"], few["low"], few["position_pct"]) == (100.0, 90.0, 100.0)      # highs 100, the one low of 90
    # no price: the range stays, the position is null with the price's reason
    nop = watch.range_block(view_of(hours()), "1h", 24, None, "no fast book mid (x); no counted trade yet")
    assert nop["high"] == 102.0 and nop["position_pct"] is None and nop["zone"] is None and nop["outside"] is None
    assert nop["position_reason"] == "no fast book mid (x); no counted trade yet"
    flat = watch.range_block(view_of([candle(T0 - H, 10, 10, 10, 10)]), "1h", 24, 10.0, None)
    assert flat["position_pct"] is None and "equals the low" in flat["position_reason"]


def test_range_states_while_candles_are_missing():
    assert watch.range_block(None, "1h", 24, 100.0, None) == {"state": "unavailable", "interval": "1h",
                                                               "reason": "no candle store: the collector isn't running (replay or synthetic data)"}
    loading = {"ok": False, "state": "loading", "market": "BTC", "interval": "1h", "reason": "the first candleSnapshot fetch ... is running"}
    assert watch.range_block(loading, "1h", 24, 100.0, None) == {"state": "loading", "interval": "1h", "reason": loading["reason"]}
    failed = {"ok": False, "state": "error", "reason": "candleSnapshot failed: x", "failed_at": "t", "next_retry_s": 12.5}
    assert watch.range_block(failed, "1h", 24, 100.0, None) == {"state": "error", "interval": "1h", "reason": "candleSnapshot failed: x", "next_retry_s": 12.5}
    assert watch.range_block(view_of([candle(T0, 1, 1, 1, 1, state="in_progress")]), "1h", 24, 100.0, None) == {
        "state": "insufficient", "interval": "1h", "candles_wanted": 24, "reason": "no closed 1h candle held yet"}


def days(n_closed):
    day0 = T0 // D * D
    rows = []
    for j in range(n_closed):
        t = day0 - (n_closed - j) * D
        old = j < n_closed - 7
        rows.append(candle(t, 100, 900 if old else (112 if j == n_closed - 5 else 110), 2 if old else (80 if j == n_closed - 2 else 85), 100,
                           interval="1d"))
    rows.append(candle(day0, 100, 999, 1, 100, state="in_progress", interval="1d"))
    return rows


def test_range_7d_from_seven_days_or_the_4h_fallback_saying_which():
    r = watch.range_7d({"1d": view_of(days(9), "1d")}, 100.0, None)
    assert r["from"] == "1d" and r["interval"] == "1d" and r["candles_used"] == 7 and r["complete"] is True
    assert (r["high"], r["low"], r["position_pct"], r["zone"]) == (112.0, 80.0, 62.5, "middle")
    assert "fallback_reason" not in r
    # fewer than 7 days: the 4h candles stand in, and the block says so; until they arrive it is loading
    three = view_of(days(3), "1d")
    pending = watch.range_7d({"1d": three}, 100.0, None)
    assert pending["state"] == "loading" and pending["from"] is None and pending["interval"] == "4h"
    assert pending["reason"].startswith("only 3 closed 1d candles held (fewer than 7 days), so the last 42 closed 4h candles stand in")
    q = INTERVAL_MS["4h"]
    quarters = [candle(T0 - (50 - i) * q, 100, 300 if i < 8 else (104 if i == 20 else 100), 50 if i < 8 else (88 if i == 40 else 95), 100,
                       interval="4h") for i in range(50)]
    r = watch.range_7d({"1d": three, "4h": view_of(quarters, "4h")}, 100.0, None)
    assert r["from"] == "4h" and r["interval"] == "4h" and r["candles_used"] == 42 and r["complete"] is True
    assert (r["high"], r["low"], r["position_pct"], r["zone"]) == (104.0, 88.0, 75.0, "high")
    assert r["fallback_reason"].startswith("only 3 closed 1d candles held")
    short = watch.range_7d({"1d": three, "4h": view_of(quarters[-30:], "4h")}, 100.0, None)
    assert short["from"] == "4h" and short["candles_used"] == 30 and short["complete"] is False and short["candles_wanted"] == 42
    one = watch.range_7d({"1d": view_of(days(1), "1d"), "4h": view_of(quarters, "4h")}, 100.0, None)
    assert one["fallback_reason"].startswith("only 1 closed 1d candle held")
    loading = watch.range_7d({"1d": {"ok": False, "state": "loading", "reason": "running"}}, 100.0, None)
    assert loading == {"state": "loading", "interval": "1d", "reason": "running", "from": None}
    assert watch.range_7d(None, 100.0, None)["state"] == "unavailable"


# --- verdict ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pos,delta,expected", [
    (35.0, -0.5, ("MIDDLE", "warning", "mid-range: neither the low nor the high of the range")),
    (50.0, None, ("MIDDLE", "warning", "mid-range: neither the low nor the high of the range")),
    (65.0, 0.5, ("MIDDLE", "warning", "mid-range: neither the low nor the high of the range")),
    (34.999, -0.0501, ("LOW + FADING", "good", "at the low of range and the 4h wave is decelerating")),
    (10.0, 0.0501, ("LOW + BUILDING", "critical", "at the low of range but the wave is still accelerating")),
    (10.0, -0.05, ("LOW", "serious", "at the low of range, wave flat")),
    (10.0, 0.05, ("LOW", "serious", "at the low of range, wave flat")),
    (-20.0, None, ("LOW", "serious", "at the low of range, wave not measurable yet")),
    (65.001, -0.17, ("HIGH + FADING", "good", "at the high of range and the 4h wave is decelerating")),
    (97.2, 0.28, ("HIGH + BUILDING", "critical", "at the high of range but the wave is still accelerating")),
    (120.0, 0.0, ("HIGH", "serious", "at the high of range, wave flat")),
    (90.0, None, ("HIGH", "serious", "at the high of range, wave not measurable yet")),
])
def test_verdict_every_branch_at_its_thresholds(pos, delta, expected):
    assert watch.verdict(pos, delta) == expected


# --- the block, built and withheld -------------------------------------------------------------------------------------------

def test_block_shape_and_the_same_withholding_at_build_and_serving_time():
    bars, store = fifteen()
    views = {"1h": view_of(hours()), "1d": view_of(days(9), "1d")}
    w = watch.block("BTC", bars, store, views, mid=100.0, mid_time_ms=minute(16) + 5000)
    assert set(w) == {"market", "price", "price_from", "price_time_ms", "price_label", "as_of_ms", "as_of", "flow", "wave", "wave_reason",
                      "taker_per_hour", "top_wallet_1h", "range_24h", "range_7d", "verdict", "coverage", "notes"}
    assert (w["price"], w["price_from"], w["price_time_ms"], w["as_of_ms"]) == (100.0, "book_fast", minute(16) + 5000, minute(16) + 5000)
    assert list(w["flow"]) == ["15m", "1h", "4h"] and w["flow"]["15m"]["imbalance"] == 0.28 and w["flow"]["4h"]["minutes_covered"] == 15
    assert w["wave"] is None and w["wave_reason"].endswith("7 and 8 covered")
    assert w["taker_per_hour"]["per_hour_usd"] == 75000.0 and w["top_wallet_1h"]["share_pct"] == 68.2667
    assert w["range_24h"]["position_pct"] == 83.333 and w["range_7d"]["position_pct"] == 62.5 and w["range_7d"]["from"] == "1d"
    assert w["verdict"] == {"label": "HIGH", "tone": "serious", "text": "at the high of range, wave not measurable yet", "position_pct": 83.333,
                            "wave_delta": None, "wave_state": None, "rule": watch.VERDICT_RULE, "wave_reason": w["wave_reason"],
                            "kind": "a description by a fixed rule, not a signal or a forecast"}
    assert w["coverage"]["windows_end_ms"] == minute(16) and w["coverage"]["minutes_retained"] == 240 and w["coverage"]["gaps_in_4h"] == []
    assert w["notes"][0].startswith("Not a forecast") and any("loss-maker" in n for n in w["notes"])
    assert json.loads(json.dumps(w)) == w
    # trades not fresh: the trade parts are null with the reason and the verdict loses the wave; the same whether the build
    # withholds (its reason) or the serving gate does (the lapse reason)
    built = watch.block("BTC", bars, store, views, mid=100.0, mid_time_ms=minute(16) + 5000, trades_reason="trades unverified: R")
    served = watch.withhold(w, trades_reason="trades unverified: R")
    assert built == served
    for k in ("flow", "wave", "taker_per_hour", "top_wallet_1h"):
        assert built[k] is None and built[k + "_reason"] == "trades unverified: R"
    assert built["price"] == 100.0 and built["range_24h"]["position_pct"] == 83.333
    assert built["verdict"]["label"] == "HIGH" and built["verdict"]["wave_reason"] == "trades unverified: R"
    # the fast book not fresh: the price is the last trade (said so), and it follows trades from then on
    lt = watch.block("BTC", bars, store, views, mid=None, mid_reason="l2Book fast (5 levels) stale: x", book_reason="l2Book fast (5 levels) stale: x")
    assert (lt["price"], lt["price_from"], lt["price_time_ms"]) == (100.0, "last_trade", minute(16) + 5000)
    assert lt["price_label"].startswith("the last counted public trade") and lt["range_24h"]["position_pct"] == 83.333
    assert watch.withhold(lt, book_reason="l2Book fast (5 levels) stale: later") == lt            # not built on the fast book
    gone = watch.withhold(lt, trades_reason="trades stale: S")
    assert gone["price"] is None and gone["price_reason"] == "trades stale: S" and gone["price_from"] == "last_trade"
    for rk in ("range_24h", "range_7d"):
        assert gone[rk]["high"] == lt[rk]["high"] and gone[rk]["position_pct"] is None and gone[rk]["zone"] is None
        assert gone[rk]["outside"] is None and gone[rk]["position_reason"] == "trades stale: S"
    assert gone["verdict"] is None and gone["verdict_reason"] == "needs the 24h position: trades stale: S"
    # neither: no price at all, with both reasons
    none = watch.block("BTC", bars, store, views, mid=None, mid_reason="empty bid side", book_reason=None, trades_reason="trades stale: S")
    assert none["price"] is None and none["price_from"] is None and none["price_reason"] == "no fast book mid (empty bid side); trades stale: S"
    assert none["verdict"] is None and none["range_24h"]["position_reason"] == none["price_reason"]
    # the fast book lapsing on a price that came from it: price, positions and verdict withheld, the trade parts kept
    bk = watch.withhold(w, book_reason="l2Book fast (5 levels) stale: B")
    assert bk["price"] is None and bk["price_reason"] == "l2Book fast (5 levels) stale: B" and bk["flow"] == w["flow"]
    assert bk["range_24h"]["position_pct"] is None and bk["range_24h"]["high"] == 102.0 and bk["verdict"] is None
    assert bk["verdict_reason"] == "needs the 24h position: l2Book fast (5 levels) stale: B"
    # MIDDLE needs no wave: it survives a trades lapse unchanged in label
    midw = watch.withhold(watch.block("BTC", bars, store, views, mid=96.0, mid_time_ms=1), trades_reason="trades stale: S")
    assert midw["verdict"]["label"] == "MIDDLE" and midw["verdict"]["wave_reason"] == "trades stale: S"
    # no coverage segment yet
    fresh = watch.block("BTC", watch.MinuteBars(), Store(None, None), views, mid=100.0, mid_time_ms=1)
    assert fresh["flow"] is None and fresh["flow_reason"] == "no coverage segment yet" and fresh["as_of_ms"] is None
    assert fresh["coverage"]["reason"] == "no coverage segment yet" and fresh["verdict"]["label"] == "HIGH"
    # no candle store: the ranges say so and the verdict can't be formed
    nostore = watch.block("BTC", bars, store, None, mid=100.0, mid_time_ms=1)
    assert nostore["range_24h"]["state"] == "unavailable" and nostore["range_7d"]["state"] == "unavailable"
    assert nostore["verdict"] is None and "no candle store" in nostore["verdict_reason"]


# --- through the desk: the snapshot, the candle requests, serving-time aging and the tool -----------------------------------

NOWC = T0 + 25 * M + 5000                     # the watermark, and the candle store's clock
ROWS_1H = ([venue(T0 - 25 * H, 100, 500, 1, 100, "1", 1)]
           + [venue(T0 - (25 - i) * H, 100, 102 if i in (6, 10) else 100, 90 if i == 21 else 95, 100 + i, "1", 1) for i in range(1, 25)]
           + [venue(T0, 100, 200, 10, 150, "1", 1)])
DAY0 = NOWC // D * D
ROWS_1D = ([venue(DAY0 - 8 * D, 100, 900, 2, 100, "1", 1, interval="1d")]
           + [venue(DAY0 - (8 - j) * D, 100, 112 if j == 3 else 110, 80 if j == 6 else 85, 100, "1", 1, interval="1d") for j in range(1, 8)]
           + [venue(DAY0, 100, 999, 1, 100, "1", 1, interval="1d")])


def feed(ms, n_first=12, n_second=12):
    """A segment from T0 with n_first minutes of buy 100 / sell 100 then n_second of buy 300 / sell 100 (A buys, C sells,
    px 100), the watermark trade in the minute after, and a fresh fast book with mid 100 at the watermark."""
    open_segment(ms, T0)
    tid = 0
    for k in range(1, n_first + n_second + 1):
        buy = 1.0 if k <= n_first else 3.0
        tid += 1
        ms.on_trades([trade("BTC", minute(k) + 10_000, 100.0, buy, "B", A, B, tid)], 0, T0)
        tid += 1
        ms.on_trades([trade("BTC", minute(k) + 20_000, 100.0, 1.0, "A", B, C, tid)], 0, T0)
    wm = minute(n_first + n_second + 1) + 5000
    ms.on_trades([trade("BTC", wm, 100.0, 0.01, "B", A, B, 9999)], 0, T0)
    ms.on_book(book_msg("BTC", wm, [(99.9 - i / 10, 1, 1) for i in range(5)], [(100.1 + i / 10, 1, 1) for i in range(5)]), 0, wm)
    return wm


def desk_with_candles():
    desk = make_desk()
    jobs, clock = Jobs(), Clock(NOWC)
    rows = {"1h": ROWS_1H, "1d": ROWS_1D}
    store = CandleStore("http://127.0.0.1:9/info", fetch=lambda m, iv, s, e: rows.get(iv, []), executor=jobs, now=clock.now)
    desk.candles = store
    return desk, store, jobs


def test_the_snapshot_carries_the_block_requests_the_candles_at_the_first_build_and_ages_at_serving_time():
    desk, store, jobs = desk_with_candles()
    ms = desk.markets["BTC"]
    wm = feed(ms)
    first = build(desk, 1)["markets"]["BTC"]
    assert store.take_pending() == [{"type": "candle", "coin": "BTC", "interval": "1h"}, {"type": "candle", "coin": "BTC", "interval": "1d"}]
    assert len(jobs.queued) == 2                                 # the 1h and 1d fetches, scheduled by the first build
    w0 = first["deck"]["watch"]
    assert w0["range_24h"]["state"] == "loading" and "running" in w0["range_24h"]["reason"] and w0["range_7d"]["state"] == "loading"
    assert w0["verdict"] is None and "loading" in w0["verdict_reason"] and w0["price"] == 100.0       # the price doesn't wait for candles
    assert w0["flow"]["15m"]["imbalance"] == 0.4444
    jobs.run()
    snap = build(desk, 2)
    m = snap["markets"]["BTC"]
    w = m["deck"]["watch"]
    assert m["freshness"]["trades"]["status"] == "fresh" and m["freshness"]["book_fast"]["status"] == "fresh"
    assert (w["price"], w["price_from"], w["price_time_ms"], w["as_of_ms"]) == (100.0, "book_fast", wm, wm)
    f = w["flow"]
    assert (f["15m"]["buy_usd"], f["15m"]["sell_usd"], f["15m"]["imbalance"], f["15m"]["trades"]) == (3900.0, 1500.0, 0.4444, 30)   # minutes 10 to 24
    assert f["15m"]["complete"] is True and f["15m"]["start_ms"] == minute(10) and f["15m"]["end_ms"] == minute(25)
    assert (f["1h"]["buy_usd"], f["1h"]["sell_usd"], f["1h"]["imbalance"], f["1h"]["minutes_covered"]) == (4800.0, 2400.0, 0.3333, 24)
    assert f["1h"]["complete"] is False and "began at 2027-01-15T08:00:00.000Z" in f["1h"]["note"]
    assert (f["4h"]["minutes_covered"], f["4h"]["minutes_wanted"], f["4h"]["gap_inside"]) == (24, 240, False)
    assert w["wave"]["state"] == "accelerating" and w["wave"]["delta"] == 0.5          # 0 over minutes 1-12, 0.5 over 13-24
    assert (w["wave"]["first_half"]["minutes"], w["wave"]["first_half"]["imbalance"]) == (12, 0.0)
    assert (w["wave"]["second_half"]["minutes"], w["wave"]["second_half"]["imbalance"]) == (12, 0.5)
    assert w["taker_per_hour"] == {"per_hour_usd": 18000.0, "notional_usd": 7200.0, "minutes_covered": 24, "hours_covered": 0.4,
                                   "method": w["taker_per_hour"]["method"]}
    tw = w["top_wallet_1h"]
    assert (tw["share_pct"], tw["minute_tops_usd"], tw["total_usd"]) == (66.6667, 4800.0, 7200.0)   # A ties C at 100 (lowest address), then 300
    assert (tw["address"], tw["notional_usd"], tw["minute_ms"]) == (A, 300.0, minute(24))
    assert tw["hour_top"] == {"address": A, "notional_usd": 4800.0, "share_pct": 66.6667}
    r = w["range_24h"]
    assert (r["state"], r["high"], r["high_ms"], r["low"], r["low_ms"], r["candles_used"]) == ("ready", 102.0, T0 - 15 * H, 90.0, T0 - 4 * H, 24)
    assert (r["position_pct"], r["zone"], r["fetched_at_ms"]) == (83.333, "high", NOWC)
    assert (w["range_7d"]["from"], w["range_7d"]["high"], w["range_7d"]["low"], w["range_7d"]["position_pct"], w["range_7d"]["zone"]) == (
        "1d", 112.0, 80.0, 62.5, "middle")
    assert w["verdict"]["label"] == "HIGH + BUILDING" and w["verdict"]["tone"] == "critical"
    assert w["verdict"]["text"] == "at the high of range but the wave is still accelerating"
    assert (w["verdict"]["position_pct"], w["verdict"]["wave_delta"], w["verdict"]["wave_state"]) == (83.333, 0.5, "accelerating")
    assert json.loads(json.dumps(snap))["markets"]["BTC"]["deck"]["watch"] == w
    assert not jobs.queued                                        # the second build asked again; the store had no reason to refetch
    # serving-time aging: nothing at 2 s; the fast book lapses at 3 s (price, positions, verdict); trades at 30 s (the trade parts)
    assert age_market(m, 2.0, SERVED)["deck"]["watch"] == w
    bk = age_market(m, 3.5, SERVED)["deck"]["watch"]
    assert bk["price"] is None and bk["price_from"] == "book_fast" and bk["price_reason"].startswith("l2Book fast (5 levels) stale:")
    assert SERVED in bk["price_reason"]
    for rk in ("range_24h", "range_7d"):
        assert bk[rk]["position_pct"] is None and bk[rk]["zone"] is None and bk[rk]["position_reason"] == bk["price_reason"]
        assert bk[rk]["high"] == w[rk]["high"] and bk[rk]["low"] == w[rk]["low"]
    assert bk["verdict"] is None and bk["verdict_reason"] == "needs the 24h position: " + bk["price_reason"]
    assert bk["flow"] == w["flow"] and bk["wave"] == w["wave"] and bk["taker_per_hour"] == w["taker_per_hour"] and bk["top_wallet_1h"] == w["top_wallet_1h"]
    late = age_market(m, 31.0, SERVED)["deck"]["watch"]
    for k in ("flow", "wave", "taker_per_hour", "top_wallet_1h"):
        assert late[k] is None and late[k + "_reason"].startswith("trades stale:") and SERVED in late[k + "_reason"]
    assert late["price"] is None and late["verdict"] is None
    assert late == watch.withhold(w, late["flow_reason"], late["price_reason"])          # the build's own rule, at the served age
    aged = age_market(m, 31.0, SERVED)
    assert aged["deck"]["card"]["regime"]["value"] is None and aged["deck"]["events"] == m["deck"]["events"]   # the rest of the deck as before


def test_build_time_withholding_when_trades_are_not_fresh_but_the_fast_book_is():
    desk, store, jobs = desk_with_candles()
    ms = desk.markets["BTC"]
    wm = feed(ms)
    build(desk, 1)                                               # the first build requests the candles
    jobs.run()
    ms.on_book(book_msg("BTC", wm + 30_500, [(99.9 - i / 10, 1, 1) for i in range(5)], [(100.1 + i / 10, 1, 1) for i in range(5)]),
               int(30.5 * NS), wm + 30_500)
    m = build(desk, 31 * NS)["markets"]["BTC"]
    assert m["freshness"]["trades"]["status"] == "unverified" and m["freshness"]["book_fast"]["status"] == "fresh"
    w = m["deck"]["watch"]
    r = f"trades unverified: {m['freshness']['trades']['reason']}"
    for k in ("flow", "wave", "taker_per_hour", "top_wallet_1h"):
        assert w[k] is None and w[k + "_reason"] == r
    assert w["price"] == 100.0 and w["price_from"] == "book_fast" and w["range_24h"]["position_pct"] == 83.333
    assert w["verdict"] == {"label": "HIGH", "tone": "serious", "text": "at the high of range, wave not measurable yet", "position_pct": 83.333,
                            "wave_delta": None, "wave_state": None, "rule": watch.VERDICT_RULE, "wave_reason": r,
                            "kind": "a description by a fixed rule, not a signal or a forecast"}
    assert m["deck"]["card"]["regime"]["value"] is None                   # the card is withheld by the same field


def test_without_a_candle_store_and_before_any_trade():
    desk = make_desk(markets=("BTC", "HYPE"))
    feed(desk.markets["BTC"])
    snap = build(desk, 1)
    w = snap["markets"]["BTC"]["deck"]["watch"]
    assert w["range_24h"] == {"state": "unavailable", "interval": "1h", "reason": "no candle store: the collector isn't running (replay or synthetic data)"}
    assert w["range_7d"]["state"] == "unavailable" and w["verdict"] is None and "no candle store" in w["verdict_reason"]
    assert w["flow"]["15m"]["imbalance"] == 0.4444 and w["wave"]["state"] == "accelerating"
    h = snap["markets"]["HYPE"]["deck"]["watch"]
    assert h["market"] == "HYPE" and h["price"] is None and h["price_from"] is None and h["as_of_ms"] is None
    assert h["flow"] is None and h["flow_reason"].startswith("trades warming_up:")
    assert h["price_reason"].startswith("no fast book mid (l2Book fast (5 levels) warming_up:")
    assert h["verdict"] is None and h["coverage"]["reason"] == "no coverage segment yet"
    assert snap["markets"]["BTC"]["chart"]["intervals"] == {}        # nothing else changed


def test_hl_watch_tool_answers_the_snapshots_blocks_in_the_tool_envelope():
    desk, store, jobs = desk_with_candles()
    feed(desk.markets["BTC"])
    build(desk, 1)
    jobs.run()
    snap = build(desk, 2)
    sid = snap["snapshot_id"]
    r = json.loads(tools.run_tool("hl_watch", {"market": "BTC", "snapshot_id": sid}, SnapRT(snap)))
    assert r["ok"] is True and r["tool"] == "hl_watch" and r["snapshot_id"] == sid and r["mode"] == "SYNTHETIC"
    assert list(r["markets"]) == ["BTC"]
    bt = r["markets"]["BTC"]
    assert bt["watch"] == json.loads(json.dumps(snap["markets"]["BTC"]["deck"]["watch"]))
    assert bt["provenance"]["market"] == "BTC" and set(bt["status"]["fields"]) == {"trades", "book_fast"} and bt["status"]["overall"] == "fresh"
    assert set(r["definitions"]) == {"formula", "thresholds", "window", "completeness_rule", "source"}
    assert r["definitions"]["thresholds"]["middle_band_pct"] == [35, 65] and r["definitions"]["thresholds"]["wave_delta"] == 0.05
    assert r["disclaimer"] == tools.DISCLAIMER and "not a forecast" in r["note"] and r["schema"] == snap["schema"]
    assert set(r) == {"tool", "ok", "snapshot_id", "snapshot_time", "mode", "served", "markets", "definitions", "schema", "disclaimer", "note"}
    every = json.loads(tools.run_tool("hl_watch", {"snapshot_id": sid}, SnapRT(snap)))
    assert list(every["markets"]) == sorted(snap["markets"]) == ["BTC"]
    e = json.loads(tools.run_tool("hl_watch", {"market": "ETH", "snapshot_id": sid}, SnapRT(snap)))
    assert e["ok"] is False and "market 'ETH' is unsupported" in e["error"] and e["provenance"] is None
    # a historical read keeps its labels; a latest read is aged: at 31 s the trade parts and the fast-book price are withheld
    hist = json.loads(tools.run_tool("hl_watch", {"snapshot_id": sid}, SnapRT(_served(snap, snap["built_at_ms"] + 120_000, latest=False))))
    assert hist["served"]["latest"] is False and hist["historical"]["as_of"] == snap["built_at"]
    assert hist["markets"]["BTC"]["watch"]["flow"]["15m"]["imbalance"] == 0.4444
    latest = json.loads(tools.run_tool("hl_watch", {"snapshot_id": sid}, SnapRT(_served(snap, snap["built_at_ms"] + 31_000, latest=True, age_s=31.0))))
    lw = latest["markets"]["BTC"]["watch"]
    assert lw["flow"] is None and lw["flow_reason"].startswith("trades stale:") and lw["price"] is None and lw["verdict"] is None
    assert latest["markets"]["BTC"]["status"]["fields"]["trades"]["status"] == "stale"


def test_hl_watch_is_registered_everywhere_and_the_schema_defines_it():
    assert tools.HANDLERS["hl_watch"] is tools.hl_watch and tools.SCHEMAS["hl_watch"]["name"] == "hl_watch"
    assert "required" not in tools.SCHEMAS["hl_watch"]["parameters"] and set(tools.SCHEMAS["hl_watch"]["parameters"]["properties"]) == {"market", "snapshot_id"}
    for rel in ("plugin.yaml", "catalog/hyperliquid-desk.yaml", "README.md", "docs/SNAPSHOT.md", "docs/DEMO.md"):
        assert "hl_watch" in (ROOT / rel).read_text(), rel
    assert "deck.watch" in (ROOT / "docs" / "SNAPSHOT.md").read_text()
    s = schema.load()
    m = s["metrics"]["deck.watch"]
    assert s["version"] == "1.3.0" and any("deck.watch" in c for c in s["changes"] if c.startswith("1.3.0"))
    assert m["source"] == "watch" and "watch" in s["sources"]
    assert m["thresholds"] == {"middle_band_pct": [35, 65], "wave_delta": 0.05, "wave_min_minutes_per_half": "more than 10 covered minutes",
                               "range_24h_candles": "24 closed 1h", "range_7d_candles": "7 closed 1d, else 42 closed 4h",
                               "windows_min": {"15m": 15, "1h": 60, "4h": 240}, "minutes_retained": 240}
    for phrase in ("(buy - sell) / (buy + sell)", "|imbalance(second half)| - |imbalance(first half)|", "hours_covered", "minute's top aggressor",
                   "last 24 closed 1h", "42 closed 4h", "not clamped", "MIDDLE", "+ FADING", "+ BUILDING", "wave not measurable yet"):
        assert phrase in m["formula"], phrase
    for phrase in ("wholly inside one coverage segment", "minutes_covered", "gap_inside", "more than 10", "serving_public", "loading", "not a forecast"):
        assert phrase in m["completeness_rule"], phrase
    assert (watch.MIDDLE_PCT, watch.WAVE_DELTA, watch.WAVE_MIN_MINUTES_PER_HALF, watch.RETAIN_MINUTES) == ((35.0, 65.0), 0.05, 10, 240)
    assert watch.START_INTERVALS == ("1h", "1d") and watch.TRADES_PARTS == ("flow", "wave", "taker_per_hour", "top_wallet_1h")
