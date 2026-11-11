"""Taker flow per candle interval, from node fills (Kevin's review of Nov 11, item 3: the chart's Taker flow layer on
every interval, not only 15m).

The order flow map (flow_map.py) keeps eight 15-minute buckets. This module keeps the same counting at one-minute
resolution, a clock-aligned ring of per-minute taker-flow sums, so that any interval of one minute or more (1m, 5m,
15m, 30m, 1h, 4h, 1d) is summed clock-aligned on demand, one bar per candle; the 15 s bars come from the retained
fills themselves (flow.py keeps them 16 min). Per minute: taker buy and sell notional (each node fill once, by tid,
the aggressor (taker) side only, at price x size; buy or sell from the taker's own position effect exactly as
flow_map.classify reads it, never from price), the fill count, the unclassified notional counted apart, and the fill
records that couldn't be read; plus the spans of block time over which node fills were observed, so a bar that
starts before the feed's coverage, spans a gap or is still in progress says how much of it is covered, judged the way
the flow map judges its buckets. Nothing is estimated, scaled or filled in: a bar the fills don't fully cover is
partial (complete false), and a candle the node has no data for gets no bar.

Retention: RETENTION_MS (24 h of block time) of minutes behind the latest block time whose node fills are known. The
accumulator lives on the market partition like the flow map's: dropped with a new producer or replay session, kept
over a gap (the hole is stated as uncovered time).
"""

from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal

from . import flow as flow_mod
from .flow_map import BUY_KINDS, _covered_ms, _holes, _merged, _usd, classify

ZERO = Decimal(0)
MINUTE_MS = 60_000
RETENTION_MS = 24 * 60 * MINUTE_MS           # the ring: 24 h of one-minute sums behind the node's latest known block
FINE_MS = 15_000                             # the 15 s bars, from the retained fills (flow.RETENTION_MS, 16 min)
INTERVAL_MS = {"15s": FINE_MS, "1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS, "30m": 30 * MINUTE_MS,
               "1h": 60 * MINUTE_MS, "4h": 240 * MINUTE_MS, "1d": 1440 * MINUTE_MS}
INTERVALS = tuple(INTERVAL_MS)

COUNTING = ("each node fill once (by tid), the aggressor (taker) side only, at price x size; buy or sell from the taker's own "
            "position effect, never from price, exactly as the order flow map counts it (Open Long, Close Short and a Short > Long "
            "flip are taker buying; Close Long, Open Short and a Long > Short flip are taker selling); net_usd = buy_usd - "
            "sell_usd. A fill whose record is incomplete or conflicting, has no single identified taker or no position effect "
            "is counted apart as unclassified, never guessed")
UNITS_NOTE = ("USD notional (price x size of each fill, the taker side only, each fill once); time_ms is the bar's open in ms of "
              "block time, the candle's own; coverage is in seconds")
COVERAGE_NOTE = ("one bar per candle aligned to the clock, from the candle's open for one interval; coverage_s is the block "
                 "time within the bar over which node fills were observed; a bar is complete only when the whole interval is "
                 "covered, it has ended and every fill record in it was read, and one that starts before the node fills were "
                 "observed, spans a gap or is in progress says so with its coverage; a candle with no covered second and no "
                 "fill has no bar; nothing is scaled or filled in")
RETENTION_NOTE = ("one-minute taker-flow sums are kept for 24 h of block time behind the latest block whose node fills are known while the node feed stays connected (a paused feed or a new producer session starts a new ring; a short drop shows as a gap), "
                  "and every interval of one minute or more is summed from them, clock-aligned; the 15 s bars come from the "
                  "fills themselves, kept 16 min behind the node's last block, so older 15 s bars aren't served")
BEFORE_OBSERVED = "before the node fills were observed"
RING_PRUNED = "older than the one-minute sums are kept (24 h behind the node's last block)"
FILLS_PRUNED = f"node fills older than {flow_mod.RETENTION_MS // MINUTE_MS} min before the node's last block are no longer kept"


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


class Minute:
    """One bucket's sums (a minute of the ring, or any bar summed from minutes or fills)."""
    __slots__ = ("buy", "sell", "fills", "unclassified_usd", "unclassified_fills", "unreadable_fills")

    def __init__(self) -> None:
        self.buy = ZERO
        self.sell = ZERO
        self.fills = 0
        self.unclassified_usd = ZERO
        self.unclassified_fills = 0
        self.unreadable_fills = 0

    def add_exec(self, e) -> bool:
        """Count one execution: True when it was classified (buy or sell), False when counted apart as unclassified."""
        kind, _, _ = classify(e)
        if kind is None:
            self.unclassified_usd += e.notional
            self.unclassified_fills += 1
            return False
        if kind in BUY_KINDS:
            self.buy += e.notional
        else:
            self.sell += e.notional
        self.fills += 1
        return True

    def merge(self, o: "Minute") -> None:
        self.buy += o.buy
        self.sell += o.sell
        self.fills += o.fills
        self.unclassified_usd += o.unclassified_usd
        self.unclassified_fills += o.unclassified_fills
        self.unreadable_fills += o.unreadable_fills

    def empty(self) -> bool:
        return not (self.fills or self.unclassified_fills or self.unreadable_fills)


def minute_start(t_ms: int) -> int:
    return t_ms - t_ms % MINUTE_MS


class TakerFlowState:
    """The ring of one-minute sums and the observed spans of block time, for one market partition."""

    def __init__(self) -> None:
        self.minutes: dict[int, Minute] = {}
        self.segments: list[list] = []          # [start_ms, end_ms, why the hole before it] of observed block time
        self.through_ms: int | None = None      # the latest block time whose node fills are known
        self.seen_malformed = 0
        self.counters = {"fills": 0, "late_dropped": 0}

    def cut_ms(self) -> int | None:
        # minute-aligned, so the oldest minute kept is whole: a bar never claims seconds whose fills were pruned
        return None if self.through_ms is None else minute_start(self.through_ms - RETENTION_MS)

    def observe(self, flow: flow_mod.NodeFlow, block_time_ms) -> None:
        """Coverage: node fills are known over (flow.since_ms, block_time_ms] of the current observed window; called at
        every watermark and executions message, as the flow map's."""
        if flow.restart_pending or flow.since_ms is None or not _is_int(block_time_ms):
            return
        since = flow.since_ms
        t = max(since, block_time_ms)
        if self.segments and self.segments[-1][0] == since:
            if t > self.segments[-1][1]:
                self.segments[-1][1] = t
        else:
            why = None
            if self.segments:
                why = (flow.windows[-1].get("ended_by") if flow.windows else None) or "node fills not observed"
            self.segments.append([since, t, why])
        if self.through_ms is None or t > self.through_ms:
            self.through_ms = t
            self._prune()

    def add(self, flow: flow_mod.NodeFlow, execs: list, block_time_ms) -> None:
        """The executions newly counted by the flow (once by tid, inside its observed window), each into the minute of
        its own time; executions the flow couldn't read are counted per minute as unreadable."""
        malformed = flow.counters.get("malformed", 0)
        if malformed > self.seen_malformed:
            cut = self.cut_ms()
            if _is_int(block_time_ms) and (cut is None or block_time_ms >= cut):
                self._minute(block_time_ms).unreadable_fills += malformed - self.seen_malformed
            self.seen_malformed = malformed
        cut = self.cut_ms()
        for e in execs:
            if cut is not None and e.time_ms < cut:
                self.counters["late_dropped"] += 1
                continue
            if self._minute(e.time_ms).add_exec(e):
                self.counters["fills"] += 1

    def _minute(self, t_ms: int) -> Minute:
        s = minute_start(t_ms)
        m = self.minutes.get(s)
        if m is None:
            m = self.minutes[s] = Minute()
        return m

    def _prune(self) -> None:
        cut = self.cut_ms()
        if cut is None:
            return
        for s in [s for s in self.minutes if s < cut]:
            del self.minutes[s]
        self.segments = [s for s in self.segments if s[1] > cut]


# --- the bars ------------------------------------------------------------------------------------------------

def _clip(segments: list[list], cut: int, why: str) -> list[list]:
    """The merged observed spans, clipped at the retention cut: what lies before it is no longer held, so it is not
    covered, and the hole before the clipped span says why."""
    out = []
    for s in _merged(segments):
        if s[1] <= cut:
            continue
        out.append([cut, s[1], why] if s[0] < cut else s)
    return out


def _bar(t: int, be: int, m: Minute, segs: list[list], end: int, step: int) -> dict:
    hi = min(be, end)
    cov = _covered_ms(segs, t, hi)
    in_progress = be > end
    reasons = []
    if in_progress:
        reasons.append(f"in progress: {max(0, hi - t) / 1000:.0f} s of its {step // 1000} s have passed")
    if cov < hi - t:
        holes = _holes(segs, t, hi)
        reasons.append(f"{(hi - t - cov) / 1000:.0f} s of it not covered by node fills: "
                       + "; ".join(h["why"] for h in holes))
    if m.unreadable_fills:
        reasons.append(f"{m.unreadable_fills} fill record(s) in it couldn't be read, so its totals are unknown")
    return {"time_ms": t, "end_ms": be, "buy_usd": _usd(m.buy), "sell_usd": _usd(m.sell), "net_usd": _usd(m.buy - m.sell),
            "fills": m.fills, "unclassified_usd": _usd(m.unclassified_usd), "unclassified_fills": m.unclassified_fills,
            "unreadable_fills": m.unreadable_fills, "coverage_s": round(cov / 1000, 1),
            "complete": (not in_progress) and cov == step and m.unreadable_fills == 0,
            "in_progress": in_progress, "reason": "; ".join(reasons) or None}


def _group(items, wanted: list[int], step: int, key, into) -> dict:
    """Each item into the requested bar [t, t + step) that holds its time (bars are the candles' own open times)."""
    acc: dict[int, Minute] = {}
    for it in items:
        t_ms = key(it)
        i = bisect_right(wanted, t_ms) - 1
        if i < 0:
            continue
        t = wanted[i]
        if t_ms >= t + step:
            continue
        m = acc.get(t)
        if m is None:
            m = acc[t] = Minute()
        into(m, it)
    return acc


def _ring_bars(st: TakerFlowState, step: int, times) -> tuple[list[dict], list[list], int, int]:
    """Bars of one minute or more: the ring's minutes summed clock-aligned into the requested candle times."""
    end, cut = st.through_ms, st.cut_ms()
    wanted = sorted({t for t in times if _is_int(t)})
    acc = _group(st.minutes.items(), wanted, step, key=lambda kv: kv[0], into=lambda m, kv: m.merge(kv[1]))
    segs = _clip(st.segments, cut, RING_PRUNED)
    out = []
    for t in wanted:
        be = t + step
        m = acc.get(t)
        if _covered_ms(segs, t, min(be, end)) <= 0 and m is None:
            continue                                   # nothing known about this candle's span: no bar
        out.append(_bar(t, be, m or Minute(), segs, end, step))
    return out, segs, end, cut


def _fine_bars(st: TakerFlowState, flow: flow_mod.NodeFlow, times) -> tuple[list[dict], list[list], int, int]:
    """The 15 s bars, from the fills the flow retains (its current observed window, 16 min behind the node's last
    block): a bar is served only when it lies wholly inside the retained span, so its fills can't have been pruned."""
    end = st.through_ms
    cut = end - flow_mod.RETENTION_MS
    if flow.restart_pending or flow.since_ms is None:
        return [], [], end, cut
    lo = max(flow.since_ms, cut)
    if flow.since_ms < cut:                      # the window reaches further back than the fills are kept
        why = FILLS_PRUNED
    else:                                        # the hole before the window: the boundary that ended the last one
        why = (flow.windows[-1].get("ended_by") if flow.windows else None) or BEFORE_OBSERVED
    segs = [[lo, max(lo, end), why]]
    wanted = sorted({t for t in times if _is_int(t) and t >= cut})
    acc = _group(flow.execs.values(), wanted, FINE_MS, key=lambda e: e.time_ms, into=lambda m, e: m.add_exec(e))
    for t_bad in list(flow.unreadable):
        i = bisect_right(wanted, t_bad) - 1
        if i >= 0 and t_bad < wanted[i] + FINE_MS:
            m = acc.get(wanted[i])
            if m is None:
                m = acc[wanted[i]] = Minute()
            m.unreadable_fills += 1
    out = []
    for t in wanted:
        be = t + FINE_MS
        m = acc.get(t)
        if _covered_ms(segs, t, min(be, end)) <= 0 and m is None:
            continue
        out.append(_bar(t, be, m or Minute(), segs, end, FINE_MS))
    return out, segs, end, cut


def build(st: TakerFlowState | None, flow: flow_mod.NodeFlow, interval: str, times) -> dict:
    """The bars for the candle open times `times` (ms) at `interval`, with the span covered and the retention
    (provenance and freshness are added by the panel). A candle the node has no data for (no covered second, no fill)
    gets no bar; a bar the fills don't fully cover is partial, as the flow map's buckets are."""
    step = INTERVAL_MS.get(interval)
    if step is None:
        return {"available": False, "reason": f"interval {interval!r} isn't supported; one of {', '.join(INTERVALS)}"}
    if st is None or st.through_ms is None:
        return {"available": False, "reason": "no node fills observed for this market yet"}
    if step == FINE_MS:
        bars, segs, end, cut = _fine_bars(st, flow, times)
    else:
        bars, segs, end, cut = _ring_bars(st, step, times)
    # The span covered, over the candles asked for: what lies before the retention isn't held (a hole with that reason),
    # then the holes in what is held, with theirs.
    asked = [t for t in times if _is_int(t)]
    lo = min(asked) if asked else cut
    kept_lo = max(lo, cut)
    cov_all = _covered_ms(segs, kept_lo, end) if end > kept_lo else 0
    holes = [{"start_ms": lo, "end_ms": min(cut, end), "why": FILLS_PRUNED if step == FINE_MS else RING_PRUNED}] if lo < cut else []
    holes += _holes(segs, kept_lo, end) if end > kept_lo else []
    unread = sum(b["unreadable_fills"] for b in bars)
    return {"available": True, "bars": bars,
            "coverage": {"start_ms": lo, "end_ms": end, "coverage_s": round(cov_all / 1000, 1),
                         "complete": cov_all == end - lo and unread == 0, "uncovered": holes, "note": COVERAGE_NOTE},
            "retention": {"kept_from_ms": cut, "through_ms": end,
                          "minutes_kept": (flow_mod.RETENTION_MS if step == FINE_MS else RETENTION_MS) // MINUTE_MS,
                          "note": RETENTION_NOTE},
            "counting": COUNTING, "units_note": UNITS_NOTE, "counters": dict(st.counters)}
