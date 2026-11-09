"""The multi-timeframe read of the venue's candles (hl_desk/structure.py and the hl_structure tool): pure functions over
canned candle lists with hand-checked numbers, the interval states, the prior-day cross, and the tool through run_tool
with a stand-in runtime and candle store (as tests/test_candles.py), with a fake clock so the 3 s wait costs nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from helpers import SnapRT, build, make_desk, use_standalone_storage
from test_candles import H, NOW, OPEN, Clock, _Collector, venue

from hl_desk import structure as st
from hl_desk import tools
from hl_desk.candles import INTERVAL_MS, SOURCE, CandleStore

ROOT = Path(__file__).resolve().parent.parent
D, Q, M = INTERVAL_MS["1d"], INTERVAL_MS["4h"], INTERVAL_MS["1m"]
DAY0, Q0, M0 = (NOW // D) * D, (NOW // Q) * Q, (NOW // M) * M          # the candles in progress at NOW
FORBIDDEN = ("bullish", "bearish", "trend", "support", "resistance", "breakout", "oversold", "overbought")

# Six closed hours and the hour in progress, whose extremes (high 120, low 90, volume 999) would change every statistic
# if they were mixed in.
ROWS_1H = [venue(OPEN - 6 * H, 100, 104, 98, 102, "10", 1),
           venue(OPEN - 5 * H, 102, 103, 99, 100, "20", 2),
           venue(OPEN - 4 * H, 100, 110, 95, 105, "30", 3),
           venue(OPEN - 3 * H, 105, 106, 101, 103, "40", 4),
           venue(OPEN - 2 * H, 103, 108, 102, 106, "50", 5),
           venue(OPEN - H, 106, 109, 104, 108, "60", 6),
           venue(OPEN, 108, 120, 90, 111, "999", 7)]
# Two closed days (too few for statistics; enough for the prior day) and the day in progress.
ROWS_1D = [venue(DAY0 - 2 * D, 100, 120, 90, 110, "1000", 100, interval="1d"),
           venue(DAY0 - D, 110, 130, 100, 125, "2000", 200, interval="1d"),
           venue(DAY0, 125, 128, 122, 127, "300", 30, interval="1d")]
ROWS_4H = [venue(Q0 - 3 * Q, 100, 101, 99, 100.5, "1", 1, interval="4h"),
           venue(Q0 - 2 * Q, 100.5, 102, 100, 101.5, "2", 2, interval="4h"),
           venue(Q0 - Q, 101.5, 103, 101, 102.5, "3", 3, interval="4h"),
           venue(Q0, 102.5, 104, 102, 103.5, "4", 4, interval="4h")]
# Seventy closed minutes (more than the 60 used) and the minute in progress.
ROWS_1M = [venue(M0 - i * M, 100 + i / 10, 100.5 + i / 10, 99.5 + i / 10, 100.2 + i / 10, str(i), i, interval="1m")
           for i in range(70, 0, -1)] + [venue(M0, 100.2, 112, 100, 111.5, "5", 5, interval="1m")]


def closed(t, o, h, l, c, v, n=1):
    return {"time": t, "close_time": t + H - 1, "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume_base": float(v), "trades": n, "state": "closed"}


def view_of(candles, cur=None, age=0.5, interval="1h", served_at_ms=NOW):
    """A store view (CandleStore.view) built by hand: closed candles, then the in-progress candle when given."""
    rows = list(candles) + ([dict(cur, state="in_progress")] if cur else [])
    ip = ({"status": "unverified", "reason": f"candle updates carry no source timestamp; received {age:.1f} s ago",
           "received_age_s": age, "from": "websocket", "time": cur["time"], "close_time": cur["close_time"]} if cur
          else {"status": "unavailable", "received_age_s": None, "reason": "no in-progress candle received yet"})
    return {"ok": True, "market": "BTC", "interval": interval, "candles": rows,
            "history": {"count": len(candles), "available": len(candles), "fetched_at_ms": NOW, "fetched_at": "x",
                        "fetches": 1, "refetching": False},
            "in_progress": ip, "source": SOURCE,
            "provenance": {"mode": "LIVE", "served_at_ms": served_at_ms, "websocket": {"messages": 3}}}


def hours():
    """ROWS_1H as the store parses them: six closed candles and the in-progress one."""
    rows = [closed(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"], r["n"]) for r in ROWS_1H[:-1]]
    r = ROWS_1H[-1]
    cur = closed(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"], r["n"])
    return rows, cur


# --- the pure functions ---------------------------------------------------------------------------------------------

def test_interval_block_numbers_hand_checked_and_the_in_progress_candle_excluded():
    rows, cur = hours()
    b = st.interval_block(view_of(rows, cur), 48)
    assert b["state"] == "ready" and b["candles_used"] == 6                      # 48 asked for, 6 held
    assert b["span"] == {"first_ms": OPEN - 6 * H, "first": "2026-11-09T14:00:00.000Z",
                         "last_closed_ms": OPEN - H, "last_closed": "2026-11-09T19:00:00.000Z"}
    assert b["last_closed"] == {"time_ms": OPEN - H, "open": 106.0, "high": 109.0, "low": 104.0, "close": 108.0,
                                "volume_base": 60.0, "trades": 6}
    assert b["in_progress"] == {"time_ms": OPEN, "open": 108.0, "high": 120.0, "low": 90.0, "close": 111.0, "volume_base": 999.0,
                                "status": "unverified", "reason": "candle updates carry no source timestamp; received 0.5 s ago",
                                "received_age_s": 0.5}
    # range over the six closed candles: high 110 and low 95, both in the 10:00 candle; last close 108
    assert b["range"] == {"high": 110.0, "high_ms": OPEN - 4 * H, "high_at": "2026-11-09T16:00:00.000Z",
                          "low": 95.0, "low_ms": OPEN - 4 * H, "low_at": "2026-11-09T16:00:00.000Z",
                          "width_pct": 15.789,              # (110-95)/95
                          "close_from_high_pct": 1.818,     # (110-108)/110
                          "close_from_low_pct": 13.684,     # (108-95)/95
                          "position_pct": 86.667}           # (108-95)/(110-95)
    # closes 102, 100, 105, 103, 106, 108
    assert b["change_pct"] == {"1": 1.887, "3": 2.857, "6": 5.882}            # vs 106, vs 105, vs 102
    assert b["streak"] == {"direction": "up", "bars": 2, "reaches_first_candle": False}   # 103 -> 106 -> 108
    assert b["last_bar"] == {"range_pct": 4.717, "body_pct": 1.887, "direction": "up", "volume_vs_median": 1.71}  # 5/106, 2/106, 60/35
    assert b["largest_bar"] == {"time_ms": OPEN - 4 * H, "time": "2026-11-09T16:00:00.000Z", "range_pct": 15.0, "direction": "up"}
    assert b["volume"] == {"median_base": 35.0, "last_base": 60.0, "sum_base": 210.0}
    assert b["mean_range_pct"] == 6.704                                       # mean of 6, 3.922, 15, 4.762, 5.825, 4.717
    assert set(b) == {"state", "candles_used", "span", "last_closed", "in_progress", "range", "change_pct", "streak",
                      "last_bar", "largest_bar", "volume", "mean_range_pct"}
    assert not any(k.endswith("_reason") for sub in (b["range"], b["change_pct"], b["last_bar"]) for k in sub)
    # without the in-progress candle nothing in the statistics changes
    b2 = st.interval_block(view_of(rows), 48)
    assert b2["in_progress"] is None and {k: v for k, v in b2.items() if k != "in_progress"} == {k: v for k, v in b.items() if k != "in_progress"}


def test_only_the_most_recent_n_closed_candles_are_used():
    rows, cur = hours()
    b = st.interval_block(view_of(rows, cur), 4)
    assert b["candles_used"] == 4 and b["span"]["first_ms"] == OPEN - 4 * H
    assert b["range"]["high"] == 110.0 and b["change_pct"] == {"1": 1.887, "3": 2.857, "4": 2.857}   # 4 bars: vs the 10:00 close too
    assert b["volume"] == {"median_base": 45.0, "last_base": 60.0, "sum_base": 180.0}           # 30, 40, 50, 60


def test_ties_take_the_latest_candle_and_three_candles_have_only_the_1_and_n_keys():
    rows = [closed(OPEN - 3 * H, 10, 12, 9, 10, 1), closed(OPEN - 2 * H, 10, 12, 9, 10, 1), closed(OPEN - H, 10, 12, 9, 10, 1)]
    b = st.interval_block(view_of(rows), 48)
    assert b["range"]["high_ms"] == OPEN - H and b["range"]["low_ms"] == OPEN - H          # the most recent of the tied candles
    assert b["largest_bar"]["time_ms"] == OPEN - H and b["largest_bar"]["range_pct"] == 30.0 and b["largest_bar"]["direction"] == "flat"
    assert b["change_pct"] == {"1": 0.0, "3": 0.0}                                            # "3" is the N key here; no "3_reason"
    assert b["streak"] == {"direction": "flat", "bars": 2, "reaches_first_candle": True}
    assert b["range"]["position_pct"] == 33.333 and b["last_bar"]["direction"] == "flat"
    down = [closed(OPEN - 4 * H, 10, 11, 9, 10, 1), closed(OPEN - 3 * H, 10, 11, 9, 9, 1), closed(OPEN - 2 * H, 9, 11, 8, 8, 1),
            closed(OPEN - H, 8, 11, 7, 7, 1)]
    b = st.interval_block(view_of(down), 48)
    assert b["streak"] == {"direction": "down", "bars": 3, "reaches_first_candle": True}
    assert b["range"]["position_pct"] == 0.0 and b["range"]["close_from_low_pct"] == 0.0 and b["range"]["close_from_high_pct"] == 36.364


def test_values_that_cannot_be_computed_are_null_with_a_reason():
    flat = [closed(OPEN - 3 * H, 10, 10, 10, 10, 0), closed(OPEN - 2 * H, 10, 10, 10, 10, 0), closed(OPEN - H, 10, 10, 10, 10, 0)]
    b = st.interval_block(view_of(flat), 48)
    r = b["range"]
    assert r["width_pct"] == 0.0 and r["position_pct"] is None and "equals the low" in r["position_pct_reason"]
    assert b["last_bar"]["volume_vs_median"] is None and "median base volume" in b["last_bar"]["volume_vs_median_reason"]
    assert b["volume"] == {"median_base": 0.0, "last_base": 0.0, "sum_base": 0.0}
    assert b["mean_range_pct"] == 0.0 and b["largest_bar"]["range_pct"] == 0.0


def test_loading_error_and_insufficient_states():
    loading = {"ok": False, "state": "loading", "market": "BTC", "interval": "4h", "reason": "the first candleSnapshot fetch is running"}
    assert st.interval_block(loading, 42) == {"state": "loading", "reason": "the first candleSnapshot fetch is running"}
    failed = {"ok": False, "state": "error", "market": "BTC", "interval": "4h", "reason": "candleSnapshot failed: x",
              "failed_at": "t", "next_retry_s": 12.5}
    assert st.interval_block(failed, 42) == {"state": "error", "reason": "candleSnapshot failed: x", "next_retry_s": 12.5}
    two = [closed(OPEN - 2 * H, 10, 12, 9, 11, 1), closed(OPEN - H, 11, 13, 10, 12, 2)]
    b = st.interval_block(view_of(two), 48)
    assert b == {"state": "insufficient", "reason": "only 2 closed candles held for this interval; at least 3 are needed",
                 "candles_used": 2, "in_progress": None,
                 "last_closed": {"time_ms": OPEN - H, "open": 11.0, "high": 13.0, "low": 10.0, "close": 12.0, "volume_base": 2.0, "trades": 1}}
    assert st.interval_block(view_of([]), 48)["reason"].startswith("only 0 closed candles")
    assert st.interval_block(view_of([two[0]]), 48)["reason"].startswith("only 1 closed candle held")


def test_cross_block_prior_day_and_last_price_rules():
    rows, cur = hours()
    day = view_of([closed(DAY0 - 2 * D, 100, 120, 90, 110, 1), closed(DAY0 - D, 110, 130, 100, 125, 2)],
                  closed(DAY0, 125, 128, 122, 127, 3), age=0.5, interval="1d")
    blocks = {"1d": st.interval_block(day, 30), "1h": st.interval_block(view_of(rows, cur, age=0.5), 48)}
    c = st.cross_block(blocks)
    assert c["prior_day"] == {"time_ms": DAY0 - D, "time": "2026-11-08T00:00:00.000Z", "open": 110.0, "high": 130.0, "low": 100.0, "close": 125.0}
    # both in-progress candles were received 0.5 s ago: the finer interval wins the tie
    assert c["last_price"] == 111.0 and c["last_price_status"] == "unverified"
    assert c["last_price_from"] == "the in-progress 1h candle's close (unverified; received 0.5 s ago)"
    assert c["above_prior_day_high"] is False and c["below_prior_day_low"] is False
    assert c["position_in_prior_day_range_pct"] == 36.667                      # (111-100)/(130-100)
    assert not any(k.endswith("_reason") for k in c)
    # the most recently received in-progress candle wins, whatever its interval; outside the prior day's range: unclamped
    blocks["1h"] = st.interval_block(view_of(rows, dict(cur, close=131.0), age=2.0), 48)
    c = st.cross_block(blocks)
    assert c["last_price"] == 127.0 and c["last_price_from"].startswith("the in-progress 1d candle's close")
    blocks["1d"] = st.interval_block(view_of(day["candles"][:2], dict(day["candles"][2], close=131.0), age=3.0, interval="1d"), 30)
    c = st.cross_block(blocks)
    assert c["last_price"] == 131.0 and c["above_prior_day_high"] is True and c["position_in_prior_day_range_pct"] == 103.333
    # no in-progress candle anywhere: the last closed close of the finest interval loaded, and said so
    blocks = {"1d": st.interval_block(view_of(day["candles"][:2], interval="1d"), 30),
              "1h": st.interval_block(view_of(rows), 48),
              "5m": st.interval_block(view_of([closed(OPEN - 3 * H, 1, 2, 1, 1.5, 1), closed(OPEN - 2 * H, 1, 2, 1, 1.5, 1),
                                               closed(OPEN - H, 1, 2, 1, 99.0, 1)], interval="5m"), 60),
              "1m": st.interval_block({"ok": False, "state": "loading", "reason": "running"}, 60)}
    c = st.cross_block(blocks)
    assert c["last_price"] == 99.0 and c["last_price_status"] == "closed"
    assert c["last_price_from"] == "the last closed 5m candle's close (opened 2026-11-09T19:00:00.000Z)"
    assert c["below_prior_day_low"] is True and c["position_in_prior_day_range_pct"] == -3.333
    # no 1d interval, or a 1d still loading: the prior day is null with the reason, and so is the comparison
    c = st.cross_block({"1h": st.interval_block(view_of(rows, cur), 48)})
    assert c["prior_day"] is None and c["prior_day_reason"] == "the 1d interval wasn't requested"
    assert c["last_price"] == 111.0 and c["above_prior_day_high"] is None and c["position_in_prior_day_range_pct"] is None
    assert c["comparison_reason"] == "the 1d interval wasn't requested"
    c = st.cross_block({"1d": st.interval_block({"ok": False, "state": "loading", "reason": "running"}, 30)})
    assert c["prior_day_reason"] == "the 1d interval is loading: running" and c["last_price"] is None
    assert c["last_price_reason"] == "no interval has a candle yet" and c["comparison_reason"] == "the 1d interval is loading: running"


def test_structure_answer_shape_notes_and_provenance():
    rows, cur = hours()
    views = {"1h": view_of(rows, cur, served_at_ms=NOW + 700),
             "4h": {"ok": False, "state": "loading", "market": "BTC", "interval": "4h", "reason": "running"}}
    s = st.structure("BTC", ["4h", "1h"], views)
    assert s["ok"] is True and s["market"] == "BTC" and s["as_of_ms"] == NOW + 700 and s["as_of"] == "2026-11-09T20:13:26.417Z"
    assert list(s["intervals"]) == ["4h", "1h"] and s["intervals"]["4h"] == {"state": "loading", "reason": "running"}
    assert s["intervals"]["1h"]["state"] == "ready" and s["cross"]["last_price"] == 111.0
    assert s["source"] == SOURCE and s["provenance"]["source"] == SOURCE and s["provenance"]["mode"] == "LIVE"
    assert s["provenance"]["intervals"] == {"4h": {"state": "loading", "reason": "running"},
                                            "1h": {"state": "ready", "candles_held": 6, "fetched_at_ms": NOW, "fetched_at": "x", "fetches": 1,
                                                   "refetching": False, "websocket_messages": 3, "in_progress_from": "websocket"}}
    assert s["notes"][:2] == ["closed candles only in the statistics; the in-progress candle is reported separately and is unverified",
                              "percentages are of the venue's prices; nothing here is a forecast or a signal"]
    assert s["notes"][-1] == "interval 4h still loading: the first candleSnapshot fetch is running; call again in a second for them"
    assert set(s) == {"ok", "market", "as_of_ms", "as_of", "intervals", "cross", "source", "provenance", "notes"}
    text = json.dumps(s).lower() + json.dumps(tools.SCHEMAS["hl_structure"]).lower()
    assert not any(re.search(rf"\b{w}", text) for w in FORBIDDEN), text
    assert st.structure("BTC", ["4h"], {"4h": views["4h"]})["provenance"]["mode"] is None     # nothing loaded: no mode claimed


# --- the tool ----------------------------------------------------------------------------------------------------------

class Jobs:
    """The stand-in executor of tests/test_candles.py, running the queued fetches of chosen intervals only."""

    def __init__(self):
        self.queued = []

    def submit(self, fn, *args):
        self.queued.append((fn, args))

    def run(self, only=None):
        keep, run = [], []
        for fn, args in self.queued:
            (run if only is None or args[1] in only else keep).append((fn, args))
        self.queued = keep
        for fn, args in run:
            fn(*args)
        return len(run)


class FakeTime:
    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 1000.0, [], on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s
        if self.on_sleep:
            self.on_sleep()


ANSWERS = {"1h": ROWS_1H, "1d": ROWS_1D, "4h": ROWS_4H, "1m": ROWS_1M}


@pytest.fixture
def desk_rt(tmp_path, monkeypatch):
    """A runtime owning a stand-in collector with a real CandleStore over canned venue answers, and a published
    snapshot for run_tool to resolve (as tests/test_candles.py's tool test)."""
    from hl_desk import runtime as rt_mod
    from hl_desk.store import SnapshotStore

    use_standalone_storage(monkeypatch, tmp_path / "data")
    rt_mod._slot.reset()
    rt = rt_mod.get_runtime()
    clock, jobs = Clock(), Jobs()
    calls = []

    def fetch(market, interval, start_ms, end_ms):
        calls.append((market, interval))
        return ANSWERS.get(interval, [])

    store = CandleStore("http://127.0.0.1:9/info", fetch=fetch, executor=jobs, now=clock.now)
    rt.collector = _Collector(store, ["BTC", "xyz:SP500"])
    rt.store = SnapshotStore(tmp_path / "snaps")
    desk = make_desk()
    snap = build(desk, 1)
    rt.store.publish(snap, frozen=desk.frozen_last)
    yield rt, store, jobs, clock, calls, snap
    rt_mod._slot.reset()


def run(rt, args):
    return json.loads(tools.run_tool("hl_structure", args, rt))


def test_tool_waits_for_first_fetches_reports_loading_intervals_and_answers_on_the_next_call(desk_rt, monkeypatch):
    rt, store, jobs, clock, calls, snap = desk_rt
    ft = FakeTime(on_sleep=lambda: (jobs.run(only={"1h", "1d", "1m"}), clock.advance(250)))
    monkeypatch.setattr(tools, "_sleep", ft.sleep)
    monkeypatch.setattr(tools, "_monotonic", ft.monotonic)
    args = {"market": "BTC", "intervals": ["1d", "4h", "1h", "1m"], "snapshot_id": snap["snapshot_id"]}
    r = run(rt, args)
    # the 4h fetch never lands: the tool polled every 0.25 s for 3 s, then answered with what it had
    assert ft.sleeps == [0.25] * 12 and sorted(set(calls)) == [("BTC", "1d"), ("BTC", "1h"), ("BTC", "1m")]
    assert r["ok"] is True and r["tool"] == "hl_structure" and r["market"] == "BTC" and r["disclaimer"] == tools.DISCLAIMER
    assert list(r["intervals"]) == ["1d", "4h", "1h", "1m"]
    assert r["intervals"]["4h"] == {"state": "loading", "reason": r["intervals"]["4h"]["reason"]} and "running" in r["intervals"]["4h"]["reason"]
    h1 = r["intervals"]["1h"]
    assert h1["state"] == "ready" and h1["candles_used"] == 6 and h1["range"]["high"] == 110.0 and h1["change_pct"] == {"1": 1.887, "3": 2.857, "6": 5.882}
    assert h1["in_progress"]["close"] == 111.0 and h1["in_progress"]["status"] == "unverified" and h1["in_progress"]["received_age_s"] == 3.0
    assert h1["in_progress"]["reason"] == "candle updates carry no source timestamp; received 3.0 s ago"
    d1 = r["intervals"]["1d"]
    assert d1["state"] == "insufficient" and d1["candles_used"] == 2 and d1["last_closed"]["high"] == 130.0
    m1 = r["intervals"]["1m"]
    assert m1["state"] == "ready" and m1["candles_used"] == 60 and m1["span"]["first_ms"] == M0 - 60 * M     # 70 held, 60 used
    assert m1["in_progress"]["close"] == 111.5
    c = r["cross"]
    assert c["prior_day"] == {"time_ms": DAY0 - D, "time": "2026-11-08T00:00:00.000Z", "open": 110.0, "high": 130.0, "low": 100.0, "close": 125.0}
    assert c["last_price"] == 111.5 and c["last_price_from"] == "the in-progress 1m candle's close (unverified; received 3.0 s ago)"
    assert c["above_prior_day_high"] is False and c["below_prior_day_low"] is False and c["position_in_prior_day_range_pct"] == 38.333
    assert r["as_of_ms"] == clock.ms and r["provenance"]["intervals"]["4h"]["state"] == "loading"
    assert "4h still loading" in r["notes"][-1] and "calling again in a second" in r["note"]
    assert r["snapshot_id"] == snap["snapshot_id"] and r["served"]["latest"] is False and r["historical"]["as_of"] == snap["built_at"]
    assert r["source"] == SOURCE and r["schema"] == snap["schema"]
    text = json.dumps(r).lower()
    assert not any(re.search(rf"\b{w}", text) for w in FORBIDDEN), text
    # the next call: the fetch has landed; nothing to wait for
    jobs.run()
    ft.sleeps.clear()
    r = run(rt, args)
    assert ft.sleeps == [] and r["intervals"]["4h"]["state"] == "ready" and r["intervals"]["4h"]["candles_used"] == 3
    assert r["intervals"]["4h"]["change_pct"] == {"1": 0.985, "3": 1.99} and r["intervals"]["4h"]["streak"] == {"direction": "up", "bars": 2, "reaches_first_candle": True}
    assert "loading" not in json.dumps(r["intervals"]) and not any("still loading" in n for n in r["notes"])
    # the default intervals, in order; a comma-separated string is accepted too
    r = run(rt, {"market": "BTC", "snapshot_id": snap["snapshot_id"]})
    assert list(r["intervals"]) == ["1d", "4h", "1h", "30m", "5m", "1m"] and r["intervals"]["30m"]["state"] == "loading"
    r = run(rt, {"market": "BTC", "intervals": "1h, 1d", "snapshot_id": snap["snapshot_id"]})
    assert list(r["intervals"]) == ["1h", "1d"]


def test_tool_errors_match_hl_candles(desk_rt, monkeypatch):
    rt, store, jobs, clock, calls, snap = desk_rt
    monkeypatch.setattr(tools, "STRUCTURE_WAIT_S", 0.0)
    sid = snap["snapshot_id"]
    for bad, why in (({"market": "ETH"}, "market 'ETH' isn't followed"),
                     ({}, "market is required"),
                     ({"market": "BTC", "intervals": ["2h"]}, "intervals must be a list of distinct intervals from 1m, 5m, 15m, 30m, 1h, 4h, 1d"),
                     ({"market": "BTC", "intervals": ["1h", "1h"]}, "intervals must be a list of distinct intervals"),
                     ({"market": "BTC", "intervals": []}, "intervals must be a list of distinct intervals")):
        e = run(rt, dict(bad, snapshot_id=sid))
        assert e["ok"] is False and e["tool"] == "hl_structure" and why in e["error"] and e["provenance"] is None, e
    assert calls == []                                                 # no bad request touched the venue
    # a failed fetch is that interval's error state, not the tool's
    def failing(market, interval, start_ms, end_ms):
        if interval == "1d":
            raise ConnectionError("venue down")
        return ANSWERS.get(interval, [])
    store._fetch = failing
    run(rt, {"market": "BTC", "intervals": ["1d", "1h"], "snapshot_id": sid})
    jobs.run()
    r = run(rt, {"market": "BTC", "intervals": ["1d", "1h"], "snapshot_id": sid})
    assert r["ok"] is True and r["intervals"]["1d"] == {"state": "error", "reason": "candleSnapshot failed: ConnectionError: venue down", "next_retry_s": 30.0}
    assert r["intervals"]["1h"]["state"] == "ready" and r["cross"]["prior_day"] is None
    assert r["cross"]["prior_day_reason"] == "the 1d interval is error: candleSnapshot failed: ConnectionError: venue down"
    # the collector isn't running (what the runtime answers for a dead collector, locally or from the owner socket):
    # the error form with the served labels, like hl_candles
    monkeypatch.setattr(rt, "candles", lambda market, interval, limit=None: {
        "ok": False, "state": "error", "market": market, "interval": interval,
        "reason": "the collector isn't running for this profile (start it with /hl start)"})
    e = run(rt, {"market": "BTC", "snapshot_id": sid})
    assert e == {"tool": "hl_structure", "ok": False, "state": "error", "market": "BTC", "reason": e["reason"], "snapshot_id": sid,
                 "snapshot_time": snap["built_at"], "served": e["served"], "historical": e["historical"], "schema": snap["schema"],
                 "disclaimer": tools.DISCLAIMER, "note": e["note"]}
    assert "isn't running" in e["reason"] and e["served"]["latest"] is False
    # a runtime without a candle store (a stand-in): the same form
    from hl_desk.runtime import _served
    served = _served(snap, snap["built_at_ms"] + 120_000, latest=False)
    e = json.loads(tools.run_tool("hl_structure", {"market": "BTC", "snapshot_id": sid}, SnapRT(served)))
    assert e["ok"] is False and e["state"] == "error" and "candle store" in e["reason"] and e["served"]["age_s"] == 120.0


def test_hl_structure_is_registered_everywhere_hl_candles_is():
    assert tools.SCHEMAS["hl_structure"]["parameters"]["required"] == ["market"]
    assert tools.SCHEMAS["hl_structure"]["parameters"]["properties"]["intervals"]["items"]["enum"] == ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    assert tools.HANDLERS["hl_structure"] is tools.hl_structure
    assert st.DEFAULT_INTERVALS == ("1d", "4h", "1h", "30m", "5m", "1m")
    assert st.CANDLES_USED == {"1d": 30, "4h": 42, "1h": 48, "30m": 48, "15m": 48, "5m": 60, "1m": 60}
    for rel in ("plugin.yaml", "catalog/hyperliquid-desk.yaml", "README.md", "docs/SNAPSHOT.md"):
        assert "hl_structure" in (ROOT / rel).read_text(), rel
    assert "Nine agent tools" in (ROOT / "README.md").read_text()
    assert (tools.STRUCTURE_WAIT_S, tools.STRUCTURE_POLL_S) == (3.0, 0.25)
