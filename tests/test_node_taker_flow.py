"""The chart's Taker flow layer per candle interval, from node fills (hl_desk/node/taker_flow.py; Kevin's review of Nov 11,
item 3: every interval, not only 15m).

The one-minute ring is checked against a count made here, independently, from the raw feed JSON (exact decimals, each
tid once, the taker side only, the side from the taker's own effect), and every interval's bars against the minute
sums, clock-aligned (30m, 1h, 4h and 1d boundaries); partial bars where the coverage starts mid-bucket or a line is
lost; the 15 s bars from the retained fills only; the candles route's answer (the 15m bars equal the flow map's
buckets); the snapshot's chart.taker_flow beside its 15 s candles and its serving-time aging. The tab's mirror of it is
in test_tab_taker_flow.py.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from hl_desk.node import panels
from hl_desk.node.flow import RETENTION_MS as FILLS_RETENTION_MS
from hl_desk.node.serving import age_snapshot_for_serving, aged_taker_flow
from hl_desk.node.taker_flow import BEFORE_OBSERVED, FILLS_PRUNED, INTERVAL_MS, MINUTE_MS, RETENTION_MS, RING_PRUNED
from hl_desk.node.transport import source_label

from node_helpers import EXECS, OPTS, client, read_recording, surfaces  # noqa: F401  (fixture)
from test_node_flow_map import A, B, SP, T0, Feed, _client_from, _eight_buckets, _fill

H = 3_600_000
M = MINUTE_MS
BAR_KEYS = {"time_ms", "end_ms", "buy_usd", "sell_usd", "net_usd", "fills", "unclassified_usd", "unclassified_fills",
            "unreadable_fills", "coverage_s", "complete", "in_progress", "reason"}
BLOCK_KEYS = {"available", "stream", "interval", "bucket_s", "bars", "coverage", "retention", "counting", "units_note",
              "counters", "provenance", "freshness", "as_of_note"}


def _r(d) -> float:
    return round(float(d), 2)


def _independent_bars(msgs, market, step):
    """Straight from the raw JSON: per clock-aligned bucket of `step` ms, the taker buy and sell notional (each tid once,
    blocks after the market's first watermark, the taker side only, the side from the taker's own effect) and the fills."""
    first_wm = next(m["block"] for _, m in msgs if m["market"] == market and m["kind"] == "watermark")
    seen, out = set(), {}
    for _, m in msgs:
        if m["market"] != market or m["kind"] != "execs" or m["block"] <= first_wm:
            continue
        for e in m["body"]["execs"]:
            if e["tid"] in seen:
                continue
            seen.add(e["tid"])
            t = e.get("time_ms", m["block_time_ms"])
            tk = [p for p in e["participants"] if p["role"] == "taker"]
            assert len(tk) == 1 and e["complete"] is True and tk[0]["side"] == e["aggressor"]
            eff = {k: Decimal(v) for k, v in tk[0]["effect"].items()}
            assert sum(eff.values()) > 0                              # every fill in the recording is classifiable
            row = out.setdefault(t - t % step, {"buy": Decimal(0), "sell": Decimal(0), "fills": 0})
            row["buy" if tk[0]["side"] == "B" else "sell"] += Decimal(e["px"]) * Decimal(e["sz"])
            row["fills"] += 1
    return out


def _bars(c, market, iv, times, now_ms):
    tf = panels.taker_flow(c, market, iv, times, now_ms, now_ms * 10 ** 6)
    assert tf["available"] is True and set(tf) == BLOCK_KEYS, tf
    assert all(set(b) == BAR_KEYS for b in tf["bars"])
    return {b["time_ms"]: b for b in tf["bars"]}, tf


# --- the recorded window -----------------------------------------------------------------------------------

@pytest.mark.parametrize("market", ["BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"])
def test_recorded_minutes_equal_an_independent_count_and_every_interval_sums_them(market):
    """The 120 s recording: the ring's three minutes hold exactly the fills counted here from the raw JSON, and on every
    interval (15 s from the retained fills, the rest from the ring) each bar is that count over its own clock-aligned
    span, with its coverage (the first bar partial, the last in progress) and a bar only where the node has data. The
    15m bar is the flow map's in-progress bucket, number for number."""
    _, msgs = read_recording(EXECS)
    c = _client_from(msgs)
    last_rx = msgs[-1][0]
    st = c.markets[market].taker_flow
    first_bt = next(m["block_time_ms"] for _, m in msgs if m["market"] == market and m["kind"] == "watermark")
    last_bt = max(m["block_time_ms"] for _, m in msgs if m["market"] == market)
    assert st.through_ms == last_bt and st.segments == [[first_bt, last_bt, None]]
    want = _independent_bars(msgs, market, M)
    assert set(st.minutes) == set(want) and len(want) == 3
    for t, row in want.items():
        mb = st.minutes[t]
        assert (mb.buy, mb.sell, mb.fills) == (row["buy"], row["sell"], row["fills"])     # exact decimals
        assert mb.unclassified_fills == 0 and mb.unreadable_fills == 0 and mb.unclassified_usd == 0
    assert st.counters == {"fills": sum(r["fills"] for r in want.values()), "late_dropped": 0}
    for iv, step in INTERVAL_MS.items():
        t_end = last_bt - last_bt % step
        times = [t_end - k * step for k in range(60)]
        bars, tf = _bars(c, market, iv, times, last_rx)
        assert tf["stream"] == "node fills" and tf["interval"] == iv and tf["bucket_s"] == step // 1000
        assert tf["provenance"]["mode"] == "LIVE" and tf["freshness"]["state"] == "LIVE" and tf["as_of_note"] is None
        assert set(bars) == {t for t in times if t + step > first_bt and t < last_bt}, iv
        exp = _independent_bars(msgs, market, step)
        for t, b in bars.items():
            e = exp.get(t, {"buy": Decimal(0), "sell": Decimal(0), "fills": 0})
            assert (b["buy_usd"], b["sell_usd"], b["net_usd"], b["fills"]) == (_r(e["buy"]), _r(e["sell"]), _r(e["buy"] - e["sell"]), e["fills"]), (iv, t)
            exact = min(t + step, last_bt) - max(t, first_bt)
            assert b["end_ms"] == t + step and b["in_progress"] is (t + step > last_bt)
            assert b["coverage_s"] == round(exact / 1000, 1)
            assert b["complete"] is (exact == step and not b["in_progress"])
            assert (b["reason"] is None) is b["complete"]
            if t < first_bt:
                assert f"{(first_bt - t) / 1000:.0f} s of it not covered by node fills: {BEFORE_OBSERVED}" in b["reason"]
            assert b["unclassified_usd"] == 0 and b["unclassified_fills"] == 0 and b["unreadable_fills"] == 0
        assert sum(e["fills"] for e in exp.values()) == sum(b["fills"] for b in bars.values())
        cov = tf["coverage"]
        assert cov["end_ms"] == last_bt and cov["coverage_s"] == round((last_bt - first_bt) / 1000, 1) == 119.9
        assert cov["complete"] is False and cov["start_ms"] == min(times)
        kept = FILLS_RETENTION_MS if iv == "15s" else RETENTION_MS
        kept_from = last_bt - kept
        assert tf["retention"] == {"kept_from_ms": kept_from, "through_ms": last_bt, "minutes_kept": kept // M,
                                   "note": tf["retention"]["note"]}
        # the holes: the candles asked for before what is kept, then the span before the feed was observed
        pruned = ([{"start_ms": min(times), "end_ms": kept_from, "why": FILLS_PRUNED if iv == "15s" else RING_PRUNED}]
                  if min(times) < kept_from else [])
        assert cov["uncovered"] == pruned + [{"start_ms": max(min(times), kept_from), "end_ms": first_bt, "why": BEFORE_OBSERVED}]
        assert bool(pruned) == (iv in ("30m", "1h", "4h", "1d"))              # 60 candles back: past 24 h from 30m up
        json.dumps(tf)
    fm = panels.market_view(c, market, OPTS, last_rx, last_rx * 10 ** 6)["flow_map"]
    bars, _ = _bars(c, market, "15m", [b["start_ms"] for b in fm["buckets"]], last_rx)
    last = fm["buckets"][-1]
    assert list(bars) == [last["start_ms"]] and last["in_progress"] is True
    for k in ("buy_usd", "sell_usd", "net_usd", "fills", "coverage_s", "complete", "in_progress", "unclassified_usd",
              "unclassified_fills", "unreadable_fills", "reason"):
        assert bars[last["start_ms"]][k] == last[k], k


# --- synthetic fills at chosen block times -----------------------------------------------------------------

def test_interval_bars_are_clock_aligned_sums_of_the_minutes_across_a_day_boundary():
    """Fills 90, 30 and 1 minutes before a UTC midnight and 1 minute, 3 h 59 min and 4 h 1 min after it, observed from
    2 h before through 5 h after: every interval's bars are the sums of the minute bars inside them, aligned to the
    clock (30m, 1h, 4h and 1d boundaries), the 1d bar before midnight partial (2 h covered), the one after in progress."""
    D0 = T0 - T0 % INTERVAL_MS["1d"] + INTERVAL_MS["1d"]
    c = client()
    f = Feed(c, SP)
    f.hello()
    f.wm(D0 - 2 * H)
    fills = [(D0 - 90 * M, "open_long", "1"), (D0 - 30 * M, "close_long", "2"), (D0 - M, "open_long", "3"),
             (D0 + M, "open_short", "4"), (D0 + 239 * M, "close_short", "5"), (D0 + 241 * M, "open_long", "6")]
    for i, (t, kind, sz) in enumerate(fills):
        f.execs(t, [_fill(i + 1, kind, t, sz=sz, user=A, maker=B)])
        f.wm(t + 1000)
    end = D0 + 5 * H
    f.wm(end)
    st = c.markets[SP].taker_flow
    assert sorted(st.minutes) == [t for t, _, _ in fills]
    now = end + 100
    one, _ = _bars(c, SP, "1m", list(range(D0 - 2 * H, end, M)), now)
    assert set(one) == set(range(D0 - 2 * H, end, M))                  # every observed minute has a bar, fills or not
    assert all(b["complete"] is True and b["coverage_s"] == 60 for b in one.values())
    assert [one[t]["fills"] for t, _, _ in fills] == [1] * 6
    assert [(one[t]["buy_usd"], one[t]["sell_usd"]) for t, _, _ in fills] == [(100.0, 0.0), (0.0, 200.0), (300.0, 0.0), (0.0, 400.0), (500.0, 0.0), (600.0, 0.0)]
    expect_fills = {"30m": {D0 - 90 * M: 1, D0 - 60 * M: 0, D0 - 30 * M: 2, D0: 1, D0 + 210 * M: 1, D0 + 240 * M: 1},
                    "1h": {D0 - 2 * H: 1, D0 - H: 2, D0: 1, D0 + 3 * H: 1, D0 + 4 * H: 1},
                    "4h": {D0 - 4 * H: 3, D0: 2, D0 + 4 * H: 1},
                    "1d": {D0 - 24 * H: 3, D0: 3}}
    for iv in ("5m", "15m", "30m", "1h", "4h", "1d"):
        step = INTERVAL_MS[iv]
        got, tf = _bars(c, SP, iv, list(range(D0 - 2 * INTERVAL_MS["1d"], end + step, step)), now)
        assert set(got) == {t for t in range(D0 - 2 * INTERVAL_MS["1d"], end + step, step) if t + step > D0 - 2 * H and t < end}
        for t, b in got.items():
            assert t % step == 0
            inside = [one[m] for m in range(t, t + step, M) if m in one]
            assert b["buy_usd"] == pytest.approx(sum(x["buy_usd"] for x in inside))
            assert b["sell_usd"] == pytest.approx(sum(x["sell_usd"] for x in inside))
            assert b["net_usd"] == pytest.approx(b["buy_usd"] - b["sell_usd"])
            assert b["fills"] == sum(x["fills"] for x in inside)
            assert b["coverage_s"] == pytest.approx(sum(x["coverage_s"] for x in inside))
            assert b["in_progress"] is (t + step > end)
            assert b["complete"] is (b["coverage_s"] == step / 1000 and not b["in_progress"])
        if iv in expect_fills:
            assert {t: b["fills"] for t, b in got.items() if b["fills"]} == {t: n for t, n in expect_fills[iv].items() if n}
            for t, n in expect_fills[iv].items():
                assert got[t]["fills"] == n, (iv, t)
        assert sum(b["fills"] for b in got.values()) == 6 and tf["counters"]["fills"] == 6
    day, _ = _bars(c, SP, "1d", [D0 - 24 * H, D0], now)
    before, after = day[D0 - 24 * H], day[D0]
    assert before["coverage_s"] == 7200 and before["complete"] is False and before["in_progress"] is False
    assert before["reason"] == f"79200 s of it not covered by node fills: {BEFORE_OBSERVED}"
    assert (before["buy_usd"], before["sell_usd"], before["net_usd"]) == (400.0, 200.0, 200.0)
    assert after["coverage_s"] == 18000 and after["in_progress"] is True and after["complete"] is False
    assert after["reason"] == "in progress: 18000 s of its 86400 s have passed"
    assert (after["buy_usd"], after["sell_usd"], after["net_usd"]) == (1100.0, 400.0, 700.0)


def test_partial_bars_where_the_coverage_starts_mid_bucket_and_a_line_is_lost():
    """The feed is first observed 5 minutes into a quarter hour; a line is lost 100 s into the next one. The 15m bar
    of the first quarter hour is partial with the seconds not covered and why; while the gap stands the block is
    withheld like the tape; after the window restarts the second quarter hour's bar counts the fills before the gap
    and after the restart, never the ones in between, and names the hole with the gap's own reason; the minute bars
    inside the hole don't exist, the one the gap cut is partial."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    B0, B1 = T0, T0 + INTERVAL_MS["15m"]
    f.wm(B0 + 300_000)
    f.execs(B0 + 400_000, [_fill(1, "open_long", B0 + 400_000, sz="10", user=A)])
    f.wm(B0 + 500_000)
    f.execs(B1 + 60_000, [_fill(2, "open_long", B1 + 60_000, sz="5", user=A)])
    f.wm(B1 + 100_000)
    f.skip()
    gap_line = f.execs(B1 + 200_000, [_fill(3, "close_long", B1 + 200_000, sz="7", user=B)])
    withheld = panels.taker_flow(c, SP, "15m", [B0, B1], B1 + 200_100, (B1 + 200_100) * 10 ** 6)
    assert withheld["available"] is False and "node fills INVALID" in withheld["reason"] and "sequence gap" in withheld["reason"]
    assert set(withheld) == {"available", "reason", "stream", "interval", "bucket_s", "provenance", "freshness"}
    f.execs(B1 + 250_000, [_fill(4, "open_short", B1 + 250_000, sz="3", user=B)])     # still before the next watermark
    f.wm(B1 + 300_000)
    f.execs(B1 + 400_000, [_fill(5, "close_long", B1 + 400_000, sz="2", user=B)])
    f.wm(B1 + 500_000)
    end = B1 + 600_000
    f.wm(end)
    gap_reason = f"sequence gap: expected {gap_line['seq'] - 1}, got {gap_line['seq']}"
    q, tf = _bars(c, SP, "15m", [B0 - INTERVAL_MS["15m"], B0, B1], end + 100)
    assert set(q) == {B0, B1}
    assert q[B0]["coverage_s"] == 600 and q[B0]["complete"] is False and q[B0]["in_progress"] is False
    assert q[B0]["reason"] == f"300 s of it not covered by node fills: {BEFORE_OBSERVED}"
    assert (q[B0]["buy_usd"], q[B0]["fills"]) == (1000.0, 1)
    assert q[B1]["coverage_s"] == 400 and q[B1]["in_progress"] is True
    assert q[B1]["reason"] == f"in progress: 600 s of its 900 s have passed; 200 s of it not covered by node fills: {gap_reason}"
    assert (q[B1]["buy_usd"], q[B1]["sell_usd"], q[B1]["fills"]) == (500.0, 200.0, 2)      # 700 and 300 in the hole: never counted
    assert tf["coverage"]["uncovered"] == [{"start_ms": B0 - INTERVAL_MS["15m"], "end_ms": B0 + 300_000, "why": BEFORE_OBSERVED},
                                           {"start_ms": B1 + 100_000, "end_ms": B1 + 300_000, "why": gap_reason}]
    assert tf["coverage"]["coverage_s"] == 1000 and tf["coverage"]["complete"] is False
    one, _ = _bars(c, SP, "1m", list(range(B0, end, M)), end + 100)
    assert set(one) == set(range(B0 + 300_000, B1 + 120_000, M)) | set(range(B1 + 300_000, end, M))
    cut = one[B1 + 60_000]
    assert cut["coverage_s"] == 40 and cut["complete"] is False and cut["fills"] == 1
    assert cut["reason"] == f"20 s of it not covered by node fills: {gap_reason}"
    assert c.markets[SP].flow.counters["before_window"] == 2


def test_15s_bars_come_from_the_retained_fills_only_and_never_bridge_a_restart():
    """One fill at second 30 of every minute for 20 minutes. The 15 s bars are served only for the span the fills are
    still kept (16 min behind the node's last block), each bar holding its own fill, and they sum to the minute bars
    where both exist; the minute bars (the ring) go back further. After a lost line the window restarts: the 15 s bars
    before the restart aren't served (their fills were dropped) and the hole is stated with the gap's reason, while the
    ring keeps the minutes before it."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    for k in range(20):
        t = T0 + k * M + 30_000
        f.execs(t, [_fill(k + 1, "open_long" if k % 2 else "close_long", t, sz=str(k + 1), user=A)])
        f.wm(T0 + (k + 1) * M)
    end = T0 + 20 * M
    now = end + 100
    cut = end - FILLS_RETENTION_MS
    assert cut == T0 + 4 * M
    slots = list(range(T0, end, 15_000))
    fine, tf = _bars(c, SP, "15s", slots, now)
    assert set(fine) == {t for t in slots if t >= cut}
    assert tf["retention"]["kept_from_ms"] == cut and tf["retention"]["minutes_kept"] == 16
    assert tf["coverage"] == {"start_ms": T0, "end_ms": end, "coverage_s": 960.0, "complete": False,
                              "uncovered": [{"start_ms": T0, "end_ms": cut, "why": FILLS_PRUNED}], "note": tf["coverage"]["note"]}
    one, _ = _bars(c, SP, "1m", list(range(T0, end, M)), now)
    assert set(one) == set(range(T0, end, M))                                # the ring reaches back to the start
    for k in range(20):
        t = T0 + k * M
        assert one[t]["fills"] == 1 and one[t]["complete"] is True
        if t < cut:
            assert not any(s in fine for s in range(t, t + M, 15_000))
            continue
        quarter = [fine[s] for s in range(t, t + M, 15_000)]
        assert [q["fills"] for q in quarter] == [0, 0, 1, 0] and all(q["complete"] for q in quarter)
        assert sum(q["buy_usd"] for q in quarter) == one[t]["buy_usd"] and sum(q["sell_usd"] for q in quarter) == one[t]["sell_usd"]
    # a lost line: the window restarts at the next watermark
    f.skip()
    gap_line = f.execs(end + 30_000, [_fill(21, "open_long", end + 30_000, user=A)])
    f.wm(end + M)
    f.execs(end + M + 30_000, [_fill(22, "open_long", end + M + 30_000, sz="4", user=A)])
    end2 = end + 2 * M
    f.wm(end2)
    gap_reason = f"sequence gap: expected {gap_line['seq'] - 1}, got {gap_line['seq']}"
    slots = list(range(T0, end2, 15_000))
    fine, tf = _bars(c, SP, "15s", slots, end2 + 100)
    assert set(fine) == set(range(end + M, end2, 15_000))                   # only the restarted window's fills are kept
    assert [fine[s]["fills"] for s in sorted(fine)] == [0, 0, 1, 0] and fine[end + M + 30_000]["buy_usd"] == 400.0
    assert tf["coverage"]["uncovered"] == [{"start_ms": T0, "end_ms": end2 - FILLS_RETENTION_MS, "why": FILLS_PRUNED},
                                           {"start_ms": end2 - FILLS_RETENTION_MS, "end_ms": end + M, "why": gap_reason}]
    one, tf1 = _bars(c, SP, "1m", list(range(T0, end2, M)), end2 + 100)
    assert set(one) == set(range(T0, end, M)) | {end + M}                   # the ring keeps the minutes before the gap
    assert one[end + M]["fills"] == 1 and one[end + M]["buy_usd"] == 400.0
    assert tf1["coverage"]["uncovered"] == [{"start_ms": end, "end_ms": end + M, "why": gap_reason}]
    assert c.markets[SP].flow.counters["before_window"] == 1


def test_withheld_while_the_flow_family_is_not_usable_and_the_fills_pruned_reason():
    c = client()
    f = Feed(c, SP)
    f.hello()
    tf = panels.taker_flow(c, SP, "1m", [T0], T0, T0 * 10 ** 6)
    assert tf == {"stream": "node fills", "interval": "1m", "bucket_s": 60, "available": False,
                  "reason": "node fills INVALID: no watermark yet", "provenance": tf["provenance"], "freshness": tf["freshness"]}
    assert tf["freshness"]["family"] == "flow" and tf["provenance"]["mode"] == "LIVE"
    none = panels.taker_flow(c, "BTC", "1m", [T0], T0, T0 * 10 ** 6)
    assert none["available"] is False and none["reason"] == "no node data for this market from this source yet"
    f.wm(T0)
    assert panels.taker_flow(c, SP, "2h", [T0], T0 + 100, (T0 + 100) * 10 ** 6)["reason"].startswith("interval '2h' isn't supported")
    empty = panels.taker_flow(c, SP, "1m", [], T0 + 100, (T0 + 100) * 10 ** 6)
    assert empty["available"] is True and empty["bars"] == [] and empty["coverage"]["start_ms"] == T0 - RETENTION_MS
    # a window older than the fills are kept: the 15 s bars' hole says the fills are pruned, not that nothing was observed
    for k in range(1, 20):
        f.wm(T0 + k * M)
    fine = panels.taker_flow(c, SP, "15s", [T0], T0 + 19 * M + 100, (T0 + 19 * M + 100) * 10 ** 6)
    assert fine["bars"] == [] and fine["retention"]["kept_from_ms"] == T0 + 3 * M
    assert fine["coverage"] == {"start_ms": T0, "end_ms": T0 + 19 * M, "coverage_s": 960.0, "complete": False,
                                "uncovered": [{"start_ms": T0, "end_ms": T0 + 3 * M, "why": FILLS_PRUNED}], "note": fine["coverage"]["note"]}


# --- the candles route, the snapshot and serving time ---------------------------------------------------------

CANDLES_URL = "/api/plugins/hyperliquid-desk/candles"


def _candle_store(rows_by_call, now_ms):
    from test_candles import Clock, Jobs, Venue
    from hl_desk.candles import CandleStore
    clock = Clock(now_ms)
    jobs, src = Jobs(), Venue(rows_by_call)
    return CandleStore("http://127.0.0.1:9/info", fetch=src, executor=jobs, now=clock.now), jobs


def _venue(t, step, coin, interval):
    return {"t": t, "T": t + step - 1, "s": coin, "i": interval, "o": "100", "h": "101", "l": "99", "c": "100", "v": "1", "n": 1}


def test_candles_route_and_hl_candles_carry_the_bars_aligned_to_the_candles_served(surfaces, monkeypatch):
    """The route's answer carries taker_flow for the candles it returns: with the eight-bucket feed, the 15m bars equal
    the flow map's buckets number for number (plus the pruned ninth quarter hour, which the ring still holds), a candle
    before the feed gets no bar, and the hl_candles tool answers the same; a followed market the feed doesn't carry says
    so."""
    from hl_desk import tools
    plugin = surfaces([SP, "BTC"])
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c, f, end = _eight_buckets(plugin.attach(client()))
    step = INTERVAL_MS["15m"]
    starts = [T0 - step] + [T0 + k * step for k in range(9)]
    store, jobs = _candle_store([[_venue(t, step, SP, "15m") for t in starts], [_venue(T0, step, "BTC", "15m")]], end)
    plugin.rt.collector.candles = store
    plugin.rt.collector.node = plugin.mgr
    plugin.rt.collector.desk = plugin.desk
    view = plugin.read(SP, end + 100)
    fm = view["flow_map"]
    monkeypatch.setattr(time, "time", lambda: (end + 100) / 1000)
    monkeypatch.setattr(time, "monotonic_ns", lambda: (end + 100) * 10 ** 6)
    assert plugin.http.get(CANDLES_URL, params={"market": SP, "interval": "15m"}).json()["state"] == "loading"
    jobs.run()
    body = plugin.http.get(CANDLES_URL, params={"market": SP, "interval": "15m"}).json()
    assert body["ok"] is True and [k["time"] for k in body["candles"]] == starts
    tf = body["taker_flow"]
    assert tf["available"] is True and set(tf) == BLOCK_KEYS and tf["interval"] == "15m" and tf["bucket_s"] == 900
    assert tf["provenance"]["source"] == source_label(c.source) and tf["freshness"]["state"] == "LIVE" and tf["as_of_note"] is None
    bars = {b["time_ms"]: b for b in tf["bars"]}
    assert set(bars) == set(starts[1:])                                       # no bar for the candle before the feed
    for bucket in fm["buckets"]:                                              # the map's eight: the same numbers
        b = bars[bucket["start_ms"]]
        for k in ("buy_usd", "sell_usd", "net_usd", "fills", "coverage_s", "complete", "in_progress", "reason"):
            assert b[k] == bucket[k], (bucket["start_ms"], k)
    first = bars[T0]                                                          # pruned from the map, kept by the ring
    assert first["complete"] is True and first["fills"] == 2 and (first["buy_usd"], first["sell_usd"]) == (100.0, 200.0)
    assert tf["coverage"]["uncovered"] == [{"start_ms": T0 - step, "end_ms": T0, "why": BEFORE_OBSERVED}]
    sid = plugin.last["snapshot"]["snapshot_id"]
    via_chat = json.loads(tools.run_tool("hl_candles", {"market": SP, "interval": "15m", "snapshot_id": sid}, plugin.rt))
    assert via_chat["ok"] is True and via_chat["taker_flow"] == tf
    assert "taker_flow" in tools.SCHEMAS["hl_candles"]["description"]
    assert plugin.http.get(CANDLES_URL, params={"market": "BTC", "interval": "15m"}).json()["state"] == "loading"
    jobs.run()
    other = plugin.http.get(CANDLES_URL, params={"market": "BTC", "interval": "15m"}).json()["taker_flow"]
    assert other["available"] is False and other["reason"] == "no node data for this market from this source yet"
    assert other["stream"] == "node fills" and other["interval"] == "15m" and other["provenance"]["source"] == source_label(c.source)


def _public_candles(plugin, market, from_ms, n):
    from helpers import open_segment, trade
    ms = plugin.desk.markets[market]
    open_segment(ms, from_ms)
    for k in range(1, n + 1):
        t = from_ms + k * 15_000
        ms.on_trades([trade(market, t, 100.0, 1.0, "B", A, B, k)], (t - from_ms) * 1_000_000, t)
    return ms


def test_snapshot_carries_the_15s_bars_under_its_candles_and_ages_them_at_serving_time(surfaces):
    """chart.taker_flow: one bar per 15 s candle of the snapshot, from the node's retained fills, judged at the build;
    the tab's snapshot route shows the same. Served 3 s later past the delayed limit (2 s here) the bars stay, the
    freshness is the flow family's aged status and the "as of" note states the block time and the delay, as the flow
    map's; within the limit it stays LIVE with the served age; a historical read is unchanged; an unknown served age
    reads STALE and keeps the bars."""
    from hl_desk.util import iso_ms
    plugin = surfaces([SP])
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c = plugin.attach(client(cfg={"delayed_after_ms": 2_000, "stale_after_ms": 4_000}))
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    _public_candles(plugin, SP, T0, 8)                                       # candles at T0 + 15 s ... T0 + 120 s
    fills = {T0 + 20_000: ("open_long", "1"), T0 + 50_000: ("close_long", "2"), T0 + 65_000: ("open_long", "3")}
    for k in range(1, 127):
        t = T0 + 1000 * k
        if t in fills:
            f.execs(t, [_fill(k, fills[t][0], t, sz=fills[t][1], user=A)])
        f.wm(t)
    built = T0 + 126_100
    view = plugin.read(SP, built)
    assert view["families"]["flow"]["state"] == "LIVE"
    snap = plugin.last["snapshot"]
    chart = snap["markets"][SP]["chart"]
    tf = chart["taker_flow"]
    assert tf["available"] is True and set(tf) == BLOCK_KEYS and tf["interval"] == "15s" and tf["bucket_s"] == 15
    assert tf["freshness"] == view["families"]["flow"] and tf["as_of_note"] is None and tf["provenance"]["source"] == source_label(c.source)
    times = [k["t"] for k in chart["candles"]]
    assert times and all(t % 15_000 == 0 for t in times)
    bars = {b["time_ms"]: b for b in tf["bars"]}
    assert set(bars) == set(times)                                            # every candle lies inside the observed span
    want = {T0 + 15_000: (100.0, 0.0, 1), T0 + 45_000: (0.0, 200.0, 1), T0 + 60_000: (300.0, 0.0, 1)}
    for t, b in bars.items():
        assert (b["buy_usd"], b["sell_usd"], b["fills"]) == want.get(t, (0.0, 0.0, 0)), t
        assert b["net_usd"] == b["buy_usd"] - b["sell_usd"]
        if t + 15_000 <= T0 + 126_000:
            assert b["complete"] is True and b["coverage_s"] == 15 and b["reason"] is None
        else:
            assert b["in_progress"] is True and b["complete"] is False
    assert plugin.last["tab"]["chart"]["taker_flow"] == json.loads(json.dumps(tf, default=str))
    words = ("freshness", "as_of_note")
    same = lambda a, b: {k: a[k] for k in a if k not in words} == {k: b[k] for k in b if k not in words}
    late = age_snapshot_for_serving(dict(snap, served={"latest": True, "age_s": 3.0}))["markets"][SP]
    fl = late["node"]["live"]["families"]["flow"]
    assert fl["state"] == "BEHIND" and fl["aged_by_ms"] == 3_000
    w = late["chart"]["taker_flow"]
    assert w["available"] is True and w["freshness"] == fl and same(w, tf) and w["bars"] == tf["bars"]
    assert w["as_of_note"] == f"as of the node's fills at {iso_ms(fl['watermark_block_time_ms'])}, {fl['delay_ms'] / 1000:.1f} s behind"
    assert w["as_of_note"] == late["node"]["live"]["flow_map"]["as_of_note"]
    assert late["chart"]["candles"] == chart["candles"]                      # the public candles aren't touched
    soon = age_snapshot_for_serving(dict(snap, served={"latest": True, "age_s": 0.5}))["markets"][SP]["chart"]["taker_flow"]
    assert soon["freshness"]["state"] == "LIVE" and soon["freshness"]["aged_by_ms"] == 500 and soon["as_of_note"] is None and soon["bars"] == tf["bars"]
    hist = age_snapshot_for_serving(dict(snap, served={"latest": False, "age_s": 9.0}))["markets"][SP]["chart"]["taker_flow"]
    assert hist == tf
    unknown = aged_taker_flow(tf, None)
    assert unknown["available"] is True and unknown["freshness"]["state"] == "STALE" and unknown["bars"] == tf["bars"]
    assert unknown["as_of_note"].startswith("as of the node's fills at ")
    # a block built without node mode, or withheld, is served as built
    off = {"stream": "node fills", "interval": "15s", "bucket_s": 15, "available": False, "reason": "needs node mode"}
    assert aged_taker_flow(off, 3_000) is off
    gone = aged_taker_flow(dict(tf, freshness=dict(tf["freshness"], state="UNKNOWN", reason="clock skew")), 3_000)
    assert gone is not tf and gone["available"] is True                      # not LIVE: aged_status leaves it as judged


def test_without_node_mode_the_snapshot_block_says_so():
    from helpers import build, make_desk
    desk = make_desk()
    snap = build(desk, 1)
    assert snap["markets"]["BTC"]["chart"]["taker_flow"] == {
        "stream": "node fills", "interval": "15s", "bucket_s": 15, "available": False,
        "reason": "needs node mode: no node feed configured (setting node_feed)"}
