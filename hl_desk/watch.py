"""Kevin's swing-context page as a per-market block (deck.<market>.watch): where the price sits in the 24h and 7d
ranges, taker flow imbalance over 15m, 1h and 4h, the 4h wave, taker notional per hour, the hour's top wallet and a
verdict by his own rule. The definitions are the page's; the data is the plugin's own: minute bars built from the
collector's counted public trades (4 hours per market, with each minute's aggressor notional per wallet for the latest
hour) under the plugin's coverage rules, and ranges from the venue's closed candles (hl_desk.candles). Nothing here is
a forecast or a signal: the verdict is a description by the page's rule, and a value that can't be computed is null
with a reason beside it (<name>_reason). The parts built on trades follow the trades field, the price and the positions
the field the price came from, both at build time and when a snapshot is served or shown (withhold).
"""

from __future__ import annotations

from .util import iso_ms, rnd

MINUTE_MS = 60_000
RETAIN_MINUTES = 240                      # 4 hours of minute bars per market
WALLET_MINUTES = 60                       # per-wallet minute sums are kept for the latest hour (the 1h top wallet)
WINDOWS = (("15m", 15), ("1h", 60), ("4h", 240))
WAVE_MIN_MINUTES_PER_HALF = 10            # each half needs more than this many covered minutes
WAVE_DELTA = 0.05
MIDDLE_PCT = (35.0, 65.0)                 # the hatched band of the page's range track, the "loss-maker" condition
RANGE_24H = ("1h", 24)
RANGE_7D = ("1d", 7)
RANGE_7D_FALLBACK = ("4h", 42)
START_INTERVALS = ("1h", "1d")            # requested for every followed market when the collector starts
TRADES_PARTS = ("flow", "wave", "taker_per_hour", "top_wallet_1h")
VERDICT_RULE = ("MIDDLE when the 24h position is 35 to 65%; else LOW below 35 or HIGH above 65, + FADING when the 4h wave "
                "delta < -0.05, + BUILDING when it is > 0.05, otherwise flat; with no wave, the edge alone. The rule: "
                "a description, not a signal")
WAVE_RULE = ("the covered minutes of the 4h window split into halves by count (the second half holds the extra minute); "
             "delta = |imbalance(second half)| - |imbalance(first half)|; decelerating below -0.05, accelerating above "
             "0.05, else flat; each half needs more than 10 covered minutes")
NOTES = ("Not a forecast: these are descriptions by a fixed rule, not signals",
         "Range position is where the price sits between the 24h low and high; the hatched band is the middle 30%, the "
         "condition you identified as your loss-maker",
         "Flow is (taker buys - taker sells) / total, so +1 is all buying",
         "4h wave compares the second half of the last four hours with the first: negative means the push is losing force",
         "Top wallet is that address's share of all aggressive flow in the hour (the page's definition: the sum of each "
         "minute's top aggressor; hour_top is the hour-wide top aggressor)",
         "every window counts only minutes wholly inside one coverage segment and says how many it covers; the ranges "
         "use closed candles only, never the candle in progress")


# --- minute bars -----------------------------------------------------------------------------------------------------

class MinuteBars:
    """Per UTC minute, one market's counted public trades (the trade store's execution sets, every segment): taker buy
    and sell notional, trade count, high, low and last price, and each aggressor's notional in the minute (kept for the
    latest hour). The newest 240 minutes are retained; coverage is judged at read time from the store's segments."""

    def __init__(self) -> None:
        self.bars: dict[int, dict] = {}
        self.latest_ms: int | None = None       # the newest minute start seen
        self.last: tuple | None = None          # (time, seq, px) of the latest counted trade

    def add(self, trades) -> None:
        for t in trades:
            m = t.time // MINUTE_MS * MINUTE_MS
            b = self.bars.get(m)
            if b is None:
                b = self.bars[m] = {"buy": 0.0, "sell": 0.0, "n": 0, "high": t.px, "low": t.px,
                                    "last": (t.time, t.seq, t.px), "wallets": {}}
            n = t.notional
            if t.side == "B":
                b["buy"] += n
            elif t.side == "A":
                b["sell"] += n
            b["n"] += 1
            b["high"] = max(b["high"], t.px)
            b["low"] = min(b["low"], t.px)
            if (t.time, t.seq) > b["last"][:2]:
                b["last"] = (t.time, t.seq, t.px)
            tk = t.taker
            if tk is not None and b["wallets"] is not None:
                b["wallets"][tk] = b["wallets"].get(tk, 0.0) + n
            if self.last is None or (t.time, t.seq) > self.last[:2]:
                self.last = (t.time, t.seq, t.px)
            if self.latest_ms is None or m > self.latest_ms:
                self.latest_ms = m
        if trades:
            self._prune()

    def _prune(self) -> None:
        cut = self.latest_ms - (RETAIN_MINUTES - 1) * MINUTE_MS
        wallet_cut = self.latest_ms - WALLET_MINUTES * MINUTE_MS
        for m in [m for m in self.bars if m < cut]:
            del self.bars[m]
        for m, b in self.bars.items():
            if m < wallet_cut and b["wallets"] is not None:
                b["wallets"] = None


def coverage_intervals(store) -> list[tuple[int, int]]:
    """(start, end] of every retained coverage segment: the closed ones and the current one up to its watermark."""
    out = [(s["start_ms"], s["end_ms"]) for s in getattr(store, "segments", ())
           if s.get("start_ms") is not None and s.get("end_ms") is not None]
    if store.segment_start_ms is not None and store.watermark_ms is not None:
        out.append((store.segment_start_ms, store.watermark_ms))
    return out


def minute_covered(m: int, intervals) -> bool:
    """A minute is covered only when it lies wholly inside one segment: it starts after the segment start (trades at
    the start aren't counted) and ends at or before that segment's watermark (the chart candles' completeness rule)."""
    return any(m > s and m + MINUTE_MS - 1 <= e for s, e in intervals)


def window_minutes(bars: MinuteBars, end_ms: int, n: int, intervals) -> list[dict]:
    """The n UTC minutes before end_ms (a minute boundary), oldest first: {start_ms, covered, bar}."""
    return [{"start_ms": m, "covered": minute_covered(m, intervals), "bar": bars.bars.get(m)}
            for m in range(end_ms - n * MINUTE_MS, end_ms, MINUTE_MS)]


def _imbalance(buy: float, sell: float):
    vol = buy + sell
    return (buy - sell) / vol if vol > 0 else None


def _sums(mins) -> tuple[float, float, int]:
    buy = sell = 0.0
    n = 0
    for x in mins:
        b = x["bar"]
        if b is not None:
            buy += b["buy"]
            sell += b["sell"]
            n += b["n"]
    return buy, sell, n


def gaps_inside(store, start_ms: int, end_ms: int) -> list[dict]:
    """The store's declared gaps that overlap [start_ms, end_ms)."""
    return [g for g in getattr(store, "gaps", ()) if g.get("start_ms") is not None and g["start_ms"] < end_ms
            and (g.get("end_ms") is None or g["end_ms"] > start_ms)]


def _why_incomplete(store, start_ms: int, end_ms: int, gaps: list[dict], covered: int, wanted: int) -> str:
    head = f"incomplete: {covered} of {wanted} minutes covered"
    if gaps:
        g = gaps[0]
        return (f"{head}; a coverage gap inside the window ({g.get('reason')}, from {iso_ms(g['start_ms'])} to "
                f"{iso_ms(g['end_ms']) if g.get('end_ms') is not None else 'open'})")
    seg = store.segment_start_ms
    if seg is not None and seg >= start_ms:
        return f"{head}; the coverage segment began at {iso_ms(seg)}, inside the window, so earlier minutes aren't covered"
    return f"{head}; minutes not covered by a retained coverage segment"


def flow_window(mins: list[dict], store, start_ms: int, end_ms: int) -> dict:
    """One flow window over its covered minutes: buy_usd, sell_usd, imbalance = (buy - sell) / (buy + sell), trades,
    with minutes_covered, minutes_wanted, complete and gap_inside; never averaged across a gap as if covered."""
    cov = [x for x in mins if x["covered"]]
    gaps = gaps_inside(store, start_ms, end_ms)
    out = {"start_ms": start_ms, "start": iso_ms(start_ms), "end_ms": end_ms, "end": iso_ms(end_ms),
           "minutes_covered": len(cov), "minutes_wanted": len(mins), "complete": len(cov) == len(mins),
           "gap_inside": bool(gaps)}
    if not cov:
        why = "no covered minute in the window"
        out.update(buy_usd=None, sell_usd=None, imbalance=None, trades=None, reason=why)
    else:
        buy, sell, n = _sums(cov)
        imb = _imbalance(buy, sell)
        out.update(buy_usd=rnd(buy, 2), sell_usd=rnd(sell, 2), trades=n, imbalance=rnd(imb, 4))
        if imb is None:
            out["imbalance_reason"] = "no trades in the covered minutes"
    if not out["complete"]:
        out["note"] = _why_incomplete(store, start_ms, end_ms, gaps, len(cov), len(mins))
    return out


def _half(mins: list[dict]) -> dict:
    buy, sell, n = _sums(mins)
    return {"minutes": len(mins), "start_ms": mins[0]["start_ms"], "end_ms": mins[-1]["start_ms"] + MINUTE_MS,
            "buy_usd": rnd(buy, 2), "sell_usd": rnd(sell, 2), "trades": n, "imbalance": rnd(_imbalance(buy, sell), 4),
            "_imb": _imbalance(buy, sell)}


def wave(mins4h: list[dict], gap_inside: bool) -> tuple[dict | None, str | None]:
    """The page's 4h wave: the covered minutes of the 4h window split into halves by count, delta = |imbalance of the
    second half| - |imbalance of the first|; decelerating below -0.05, accelerating above 0.05, else flat."""
    cov = [x for x in mins4h if x["covered"]]
    h = len(cov) // 2
    first, second = cov[:h], cov[h:]
    if len(first) <= WAVE_MIN_MINUTES_PER_HALF or len(second) <= WAVE_MIN_MINUTES_PER_HALF:
        return None, (f"needs more than {WAVE_MIN_MINUTES_PER_HALF} covered minutes in each half of the 4h window; "
                      f"{len(first)} and {len(second)} covered")
    a, b = _half(first), _half(second)
    if a["_imb"] is None or b["_imb"] is None:
        side = "first" if a["_imb"] is None else "second"
        return None, f"no trades in the {side} half's covered minutes, so its imbalance is undefined"
    delta = abs(b["_imb"]) - abs(a["_imb"])
    state = "decelerating" if delta < -WAVE_DELTA else ("accelerating" if delta > WAVE_DELTA else "flat")
    a.pop("_imb")
    b.pop("_imb")
    out = {"state": state, "delta": rnd(delta, 4), "first_half": a, "second_half": b, "gap_inside": gap_inside,
           "rule": WAVE_RULE}
    if gap_inside:
        out["note"] = "the 4h window spans a coverage gap; the halves hold only its covered minutes"
    return out, None


def taker_per_hour(mins4h: list[dict]) -> tuple[dict | None, str | None]:
    """Taker notional over the covered minutes of the 4h window scaled to one hour: notional / hours_covered (the page
    divides the 4h total by 4; the plugin divides by the hours it covered, and says so)."""
    cov = [x for x in mins4h if x["covered"]]
    if not cov:
        return None, "no covered minute in the 4h window"
    buy, sell, _ = _sums(cov)
    hours = len(cov) / 60
    return {"per_hour_usd": rnd((buy + sell) / hours, 2), "notional_usd": rnd(buy + sell, 2),
            "minutes_covered": len(cov), "hours_covered": rnd(hours, 4),
            "method": "taker notional over the covered minutes of the 4h window divided by the covered hours "
                      "(the 4h total divided by 4)"}, None


def top_wallet_1h(mins1h: list[dict]) -> tuple[dict | None, str | None]:
    """The page's top wallet: share_pct = the sum over the hour's covered minutes of each minute's top aggressor
    notional / the hour's taker notional; address is the minute-top wallet with the largest notional. hour_top is the
    hour-wide top aggressor. Ties: the lowest address in a minute; the latest minute between minute tops."""
    cov = [x for x in mins1h if x["covered"]]
    if not cov:
        return None, "no covered minute in the 1h window"
    buy, sell, _ = _sums(cov)
    total = buy + sell
    if total <= 0:
        return None, "no taker notional in the hour's covered minutes (share unavailable, not 0%)"
    tops, hour = [], {}
    for x in cov:
        b = x["bar"]
        if b is None or not b.get("wallets"):
            continue
        addr, v = min(b["wallets"].items(), key=lambda kv: (-kv[1], kv[0]))
        tops.append((v, x["start_ms"], addr))
        for a2, v2 in b["wallets"].items():
            hour[a2] = hour.get(a2, 0.0) + v2
    if not tops:
        return None, "no aggressor attributed in the hour's covered minutes"
    minute_tops = sum(v for v, _, _ in tops)
    v, m, addr = max(tops, key=lambda t: (t[0], t[1]))          # the largest minute top; the latest minute on ties
    ha, hv = min(hour.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"share_pct": rnd(minute_tops / total * 100, 4), "minute_tops_usd": rnd(minute_tops, 2),
            "total_usd": rnd(total, 2), "minutes_covered": len(cov), "minutes_with_trades": len(tops),
            "address": addr, "notional_usd": rnd(v, 2), "minute_ms": m, "minute": iso_ms(m),
            "hour_top": {"address": ha, "notional_usd": rnd(hv, 2), "share_pct": rnd(hv / total * 100, 4)},
            "label": "share_pct is the sum of each minute's top aggressor notional over the hour's taker notional (the page's "
                     "definition); address is the minute-top wallet with the largest notional; hour_top is the hour-wide "
                     "top aggressor; addresses, not identified owners"}, None


# --- ranges from the venue's closed candles -----------------------------------------------------------------------------

def candle_views(store, market: str) -> dict:
    """Request and read the candles the ranges need (1h and 1d; 4h once 1d is loaded with fewer than 7 closed days),
    through the store (hl_desk.candles): a request starts a fetch only when the store's policy allows one."""
    views = {}
    for iv, n in (RANGE_24H, RANGE_7D):
        store.request(market, iv)
        views[iv] = store.view(market, iv, n + 1)
    d = views[RANGE_7D[0]]
    if d.get("ok") and sum(1 for c in d.get("candles") or [] if c.get("state") == "closed") < RANGE_7D[1]:
        iv, n = RANGE_7D_FALLBACK
        store.request(market, iv)
        views[iv] = store.view(market, iv, n + 1)
    return views


def zone_of(position_pct: float) -> str:
    lo, hi = MIDDLE_PCT
    return "low" if position_pct < lo else ("high" if position_pct > hi else "middle")


def _position(out: dict, price, price_reason: str | None) -> dict:
    out["middle_band_pct"] = list(MIDDLE_PCT)
    width = out["high"] - out["low"]
    if price is None:
        out.update(position_pct=None, position_reason=price_reason or "no price", outside=None, zone=None)
    elif width <= 0:
        out.update(position_pct=None, position_reason="the high equals the low over the candles used", outside=None, zone=None)
    else:
        pos = (price - out["low"]) / width * 100.0
        out.update(position_pct=round(pos, 3), outside="above" if pos > 100 else ("below" if pos < 0 else None),
                   zone=zone_of(pos))
    return out


def range_block(view: dict | None, interval: str, n: int, price, price_reason: str | None) -> dict:
    """The range of the most recent n closed candles of one interval (high, high_ms, low, low_ms; the most recent
    candle on ties) with the price's unclamped position in it; or the loading / error / unavailable form."""
    if view is None:
        return {"state": "unavailable", "interval": interval,
                "reason": "no candle store: the collector isn't running (replay or synthetic data)"}
    if not view.get("ok"):
        out = {"state": view.get("state") or "error", "interval": interval,
               "reason": view.get("reason") or view.get("error") or "the candle store gave no answer"}
        if view.get("next_retry_s") is not None:
            out["next_retry_s"] = view["next_retry_s"]
        return out
    closed = [c for c in view.get("candles") or [] if c.get("state") == "closed"][-n:]
    if not closed:
        return {"state": "insufficient", "interval": interval, "candles_wanted": n,
                "reason": f"no closed {interval} candle held yet"}
    hi = [c for c in closed if c["high"] == max(c["high"] for c in closed)][-1]
    lo = [c for c in closed if c["low"] == min(c["low"] for c in closed)][-1]
    h = view.get("history") or {}
    out = {"state": "ready", "interval": interval, "candles_used": len(closed), "candles_wanted": n,
           "complete": len(closed) == n,
           "high": hi["high"], "high_ms": hi["time"], "high_at": iso_ms(hi["time"]),
           "low": lo["low"], "low_ms": lo["time"], "low_at": iso_ms(lo["time"]),
           "first_ms": closed[0]["time"], "last_closed_ms": closed[-1]["time"], "last_close": closed[-1]["close"],
           "fetched_at_ms": h.get("fetched_at_ms"), "fetched_at": iso_ms(h.get("fetched_at_ms"))}
    if not out["complete"]:
        out["note"] = f"only {len(closed)} closed {interval} candles held; the range covers them"
    return _position(out, price, price_reason)


def range_7d(views: dict | None, price, price_reason: str | None) -> dict:
    """The last 7 closed 1d candles, or the last 42 closed 4h candles when fewer than 7 days exist (from says which)."""
    if views is None:
        return range_block(None, RANGE_7D[0], RANGE_7D[1], price, price_reason) | {"from": None}
    d = views.get(RANGE_7D[0])
    if d is None or not d.get("ok"):
        return range_block(d, RANGE_7D[0], RANGE_7D[1], price, price_reason) | {"from": None}
    days = sum(1 for c in d.get("candles") or [] if c.get("state") == "closed")
    if days >= RANGE_7D[1]:
        return range_block(d, RANGE_7D[0], RANGE_7D[1], price, price_reason) | {"from": RANGE_7D[0]}
    why = f"only {days} closed 1d candle{'s' if days != 1 else ''} held (fewer than 7 days), so the last 42 closed 4h candles stand in"
    fb = views.get(RANGE_7D_FALLBACK[0])
    if fb is None:
        return {"state": "loading", "interval": RANGE_7D_FALLBACK[0], "from": None,
                "reason": why + "; they are being requested, ask again in a second"}
    return range_block(fb, *RANGE_7D_FALLBACK, price, price_reason) | {"from": RANGE_7D_FALLBACK[0], "fallback_reason": why}


# --- verdict ------------------------------------------------------------------------------------------------------------

def verdict(position_pct: float, wave_delta: float | None) -> tuple[str, str, str]:
    """The page's verdict(): (label, tone, text) from the 24h position and the 4h wave delta."""
    lo, hi = MIDDLE_PCT
    if lo <= position_pct <= hi:
        return "MIDDLE", "warning", "mid-range: neither the low nor the high of the range"
    edge = "low" if position_pct < lo else "high"
    big = edge.upper()
    if wave_delta is None:
        return big, "serious", f"at the {edge} of range, wave not measurable yet"
    if wave_delta < -WAVE_DELTA:
        return f"{big} + FADING", "good", f"at the {edge} of range and the 4h wave is decelerating"
    if wave_delta > WAVE_DELTA:
        return f"{big} + BUILDING", "critical", f"at the {edge} of range but the wave is still accelerating"
    return big, "serious", f"at the {edge} of range, wave flat"


def _with_verdict(w: dict) -> dict:
    r24 = w.get("range_24h")
    pos = r24.get("position_pct") if isinstance(r24, dict) else None
    if pos is None:
        if isinstance(r24, dict) and r24.get("state") not in (None, "ready"):
            why = f"the 24h range is {r24['state']}: {r24.get('reason')}"
        else:
            why = (r24.get("position_reason") if isinstance(r24, dict) else None) or "no 24h range"
        w["verdict"] = None
        w["verdict_reason"] = f"needs the 24h position: {why}"
        return w
    wv = w.get("wave")
    delta = wv.get("delta") if isinstance(wv, dict) else None
    label, tone, text = verdict(pos, delta)
    out = {"label": label, "tone": tone, "text": text, "position_pct": pos, "wave_delta": delta,
           "wave_state": wv.get("state") if isinstance(wv, dict) else None, "rule": VERDICT_RULE,
           "kind": "a description by a fixed rule, not a signal or a forecast"}
    if delta is None:
        out["wave_reason"] = w.get("wave_reason") or "no wave"
    w["verdict"] = out
    w.pop("verdict_reason", None)
    return w


def withhold(w: dict, trades_reason: str | None = None, book_reason: str | None = None) -> dict:
    """The block with the parts built on a field that isn't fresh withheld, null with the reason: the flow, wave, taker
    and top-wallet parts follow trades; the price (and with it the positions and the verdict) follows the field it came
    from (book_fast, or trades for the last trade). The build applies it with its own reasons and serving_public
    applies it again at the served age, so both give the same shape; the verdict is recomputed from what is left."""
    out = dict(w)
    if trades_reason:
        for k in TRADES_PARTS:
            out[k] = None
            out[k + "_reason"] = trades_reason
    src = out.get("price_from")
    price_reason = (book_reason if (book_reason and src == "book_fast") else
                    trades_reason if (trades_reason and src == "last_trade") else None)
    if price_reason and out.get("price") is not None:
        out["price"] = None
        out["price_reason"] = price_reason
        for rk in ("range_24h", "range_7d"):
            r = out.get(rk)
            if isinstance(r, dict) and r.get("state") == "ready":
                out[rk] = dict(r, position_pct=None, position_reason=price_reason, outside=None, zone=None)
    return _with_verdict(out)


# --- the block ----------------------------------------------------------------------------------------------------------

def block(market: str, bars: MinuteBars, store, views: dict | None, mid=None, mid_time_ms=None, mid_reason=None,
          trades_reason: str | None = None, book_reason: str | None = None) -> dict:
    """deck.<market>.watch for one snapshot: the trades-built parts from the minute bars under the store's coverage,
    the ranges from the candle views (candle_views), the price from the fast book's mid when it is fresh, else the last
    counted trade; then withheld by the build-time reasons (withhold)."""
    out: dict = {"market": market}
    if mid is not None:
        out.update(price=mid, price_from="book_fast", price_time_ms=mid_time_ms,
                   price_label="the fast book's mid (l2Book fast, 5 levels)")
    elif trades_reason is None and bars.last is not None:
        out.update(price=bars.last[2], price_from="last_trade", price_time_ms=bars.last[0],
                   price_label="the last counted public trade (the fast book's mid isn't available)")
    else:
        out.update(price=None, price_from=None, price_time_ms=None,
                   price_reason=(f"no fast book mid ({book_reason or mid_reason or 'not received yet'}); "
                                 f"{trades_reason or 'no counted trade yet'}"))
    price, price_reason = out["price"], out.get("price_reason")
    wm, seg = store.watermark_ms, store.segment_start_ms
    out["as_of_ms"], out["as_of"] = wm, iso_ms(wm)
    if wm is None or seg is None:
        why = "no coverage segment yet"
        for k in TRADES_PARTS:
            out[k] = None
            out[k + "_reason"] = why
        out["coverage"] = {"segment_start_ms": seg, "watermark_ms": wm, "minutes_retained": RETAIN_MINUTES, "reason": why}
    else:
        end = wm // MINUTE_MS * MINUTE_MS
        intervals = coverage_intervals(store)
        mins = {label: window_minutes(bars, end, n, intervals) for label, n in WINDOWS}
        out["flow"] = {label: flow_window(mins[label], store, end - n * MINUTE_MS, end) for label, n in WINDOWS}
        wv, why = wave(mins["4h"], out["flow"]["4h"]["gap_inside"])
        out["wave"] = wv
        if wv is None:
            out["wave_reason"] = why
        tk, why = taker_per_hour(mins["4h"])
        out["taker_per_hour"] = tk
        if tk is None:
            out["taker_per_hour_reason"] = why
        tw, why = top_wallet_1h(mins["1h"])
        out["top_wallet_1h"] = tw
        if tw is None:
            out["top_wallet_1h_reason"] = why
        start4h = end - WINDOWS[-1][1] * MINUTE_MS
        out["coverage"] = {"segment_start_ms": seg, "segment_start": iso_ms(seg), "watermark_ms": wm, "watermark": iso_ms(wm),
                           "windows_end_ms": end, "windows_end": iso_ms(end), "minutes_retained": RETAIN_MINUTES,
                           "gaps_in_4h": [dict(g) for g in gaps_inside(store, start4h, end)][-10:],
                           "rule": "windows are whole UTC minutes ending at the trades watermark's minute; a minute counts "
                                   "only when it lies wholly inside one coverage segment"}
    out["range_24h"] = range_block((views or {}).get(RANGE_24H[0]) if views is not None else None, *RANGE_24H, price, price_reason)
    out["range_7d"] = range_7d(views, price, price_reason)
    out["notes"] = list(NOTES)
    return withhold(out, trades_reason, book_reason)


__all__ = ["MIDDLE_PCT", "MinuteBars", "NOTES", "RETAIN_MINUTES", "START_INTERVALS", "TRADES_PARTS", "WAVE_DELTA",
           "WAVE_MIN_MINUTES_PER_HALF", "WINDOWS", "block", "candle_views", "coverage_intervals", "flow_window",
           "minute_covered", "range_7d", "range_block", "taker_per_hour", "top_wallet_1h", "verdict", "wave",
           "window_minutes", "withhold", "zone_of"]
