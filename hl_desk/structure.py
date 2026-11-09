"""Multi-timeframe structure of one market from the venue's candles (the hl_structure tool): numbers only.

Pure functions over the candle store's views (hl_desk.candles CandleStore.view). For each interval the most
recent N closed candles give the range (high and low with their times, where the last close sits between them),
the change over 1, 3 and N-1 bars, the streak of closes, the last bar, the largest bar, the volumes against
their median and the mean bar range; the last closed 1d candle gives the prior day and where the last price
sits against it. Closed candles only: the in-progress candle is reported separately, unverified, and never
enters a statistic. Nothing here is a forecast, a signal or a label: directions are up, down and flat, and a
value that can't be computed is null with a reason beside it. Percentages are rounded to 3 decimals, ratios to
2; prices and volumes stay as the venue gave them.
"""

from __future__ import annotations

import math
import time

from .candles import INTERVAL_MS, SOURCE
from .util import dex_of, iso_ms, median

DEFAULT_INTERVALS = ("1d", "4h", "1h", "30m", "5m", "1m")
CANDLES_USED = {"1d": 30, "4h": 42, "1h": 48, "30m": 48, "15m": 48, "5m": 60, "1m": 60}
MIN_CANDLES = 3
NOTES = ("closed candles only in the statistics; the in-progress candle is reported separately and is unverified",
         "percentages are of the venue's prices; nothing here is a forecast or a signal",
         "change_pct keys are how many bars earlier the compared close is; the largest key spans the candles used",
         "range.position_pct is 0 at the low and 100 at the high of the candles used; "
         "cross.position_in_prior_day_range_pct is not clamped (below 0 or above 100 means outside the prior day's range)",
         "mean_range_pct is the mean of (high-low)/open over the candles used, a plain dispersion number")
PROVENANCE_NOTE = ("computed from the candle store's closed candles at the call (candleSnapshot, refetched at most once per "
                   "30 s per interval); the in-progress candle is the latest update received and is never fresh")


def _pct(part, base, nd: int = 3):
    """part / base * 100 rounded; None when the base isn't a positive number."""
    if part is None or base is None or not base > 0:
        return None
    return round(part / base * 100.0, nd)


def _put(out: dict, key: str, value, why: str) -> None:
    """Set a value, with a reason beside it when it couldn't be computed."""
    out[key] = value
    if value is None:
        out[key + "_reason"] = why


def _cmp(a: float, b: float) -> str:
    return "up" if a > b else ("down" if a < b else "flat")


def _direction(c: dict) -> str:
    return _cmp(c["close"], c["open"])


def _range_pct(c: dict):
    return _pct(c["high"] - c["low"], c["open"])


def _latest_extreme(candles: list[dict], key: str, best) -> dict:
    """The candle holding the extreme of `key` (best = max or min); the most recent one when tied."""
    target = best(c[key] for c in candles)
    return [c for c in candles if c[key] == target][-1]


def _in_progress(cur: dict | None, info: dict) -> dict | None:
    if cur is None:
        return None
    return {"time_ms": cur["time"], "open": cur["open"], "high": cur["high"], "low": cur["low"], "close": cur["close"],
            "volume_base": cur["volume_base"], "status": "unverified",
            "reason": info.get("reason") or "candle updates carry no source timestamp",
            "received_age_s": info.get("received_age_s")}


def interval_block(view: dict, n: int) -> dict:
    """One interval's block from the store's view: state ready with the statistics over the most recent `n` closed
    candles, or loading / error (the store's reason) / insufficient (fewer than MIN_CANDLES closed candles)."""
    if not view.get("ok"):
        out = {"state": view.get("state") or "error",
               "reason": view.get("reason") or view.get("error") or "the candle store gave no answer"}
        if view.get("next_retry_s") is not None:
            out["next_retry_s"] = view["next_retry_s"]
        return out
    candles = view.get("candles") or []
    closed = [c for c in candles if c.get("state") == "closed"][-n:]
    cur = next((c for c in reversed(candles) if c.get("state") == "in_progress"), None)
    in_progress = _in_progress(cur, view.get("in_progress") or {})
    last = closed[-1] if closed else None
    last_closed = (None if last is None else
                   {"time_ms": last["time"], "open": last["open"], "high": last["high"], "low": last["low"],
                    "close": last["close"], "volume_base": last["volume_base"], "trades": last.get("trades")})
    k = len(closed)
    if k < MIN_CANDLES:
        return {"state": "insufficient",
                "reason": f"only {k} closed candle{'s' if k != 1 else ''} held for this interval; at least {MIN_CANDLES} are needed",
                "candles_used": k, "last_closed": last_closed, "in_progress": in_progress}
    closes = [c["close"] for c in closed]
    vols = [c["volume_base"] for c in closed]
    hi, lo = _latest_extreme(closed, "high", max), _latest_extreme(closed, "low", min)
    high, low = hi["high"], lo["low"]
    width = high - low
    rng = {"high": high, "high_ms": hi["time"], "high_at": iso_ms(hi["time"]),
           "low": low, "low_ms": lo["time"], "low_at": iso_ms(lo["time"])}
    _put(rng, "width_pct", _pct(width, low), "the low isn't a positive price")
    _put(rng, "close_from_high_pct", _pct(high - last["close"], high), "the high isn't a positive price")
    _put(rng, "close_from_low_pct", _pct(last["close"] - low, low), "the low isn't a positive price")
    _put(rng, "position_pct", _pct(last["close"] - low, width), "the high equals the low over the candles used")
    change = {}
    for bars in (1, 3):
        if k > bars:
            _put(change, str(bars), _pct(closes[-1] - closes[-1 - bars], closes[-1 - bars]), "the earlier close isn't a positive price")
        else:
            _put(change, str(bars), None, f"fewer than {bars + 1} closed candles held")
    change.pop(f"{k}_reason", None)                 # with exactly 3 candles the "3" key is the N key
    _put(change, str(k), _pct(closes[-1] - closes[0], closes[0]), "the first close isn't a positive price")
    direction, bars = _cmp(closes[-1], closes[-2]), 1
    i = k - 2
    while i >= 1 and _cmp(closes[i], closes[i - 1]) == direction:
        bars += 1
        i -= 1
    med = median(vols)
    last_bar = {}
    _put(last_bar, "range_pct", _range_pct(last), "the open isn't a positive price")
    _put(last_bar, "body_pct", _pct(abs(last["close"] - last["open"]), last["open"]), "the open isn't a positive price")
    last_bar["direction"] = _direction(last)
    _put(last_bar, "volume_vs_median", round(last["volume_base"] / med, 2) if med > 0 else None,
         "the median base volume of the candles used is 0")
    ranges = [(_range_pct(c), c) for c in closed]
    known = [(r, c) for r, c in ranges if r is not None]
    largest = None
    if known:
        top = max(r for r, _ in known)
        r, c = [(r, c) for r, c in known if r == top][-1]
        largest = {"time_ms": c["time"], "time": iso_ms(c["time"]), "range_pct": r, "direction": _direction(c)}
    out = {"state": "ready", "candles_used": k,
           "span": {"first_ms": closed[0]["time"], "first": iso_ms(closed[0]["time"]),
                    "last_closed_ms": last["time"], "last_closed": iso_ms(last["time"])},
           "last_closed": last_closed, "in_progress": in_progress, "range": rng, "change_pct": change,
           "streak": {"direction": direction, "bars": bars, "reaches_first_candle": bars == k - 1},
           "last_bar": last_bar}
    _put(out, "largest_bar", largest, "no candle has a positive open")
    out["volume"] = {"median_base": round(med, 8), "last_base": last["volume_base"], "sum_base": round(math.fsum(vols), 8)}
    _put(out, "mean_range_pct", round(math.fsum(r for r, _ in known) / len(known), 3) if known else None,
         "no candle has a positive open")
    return out


def cross_block(blocks: dict[str, dict]) -> dict:
    """The prior day (the last closed 1d candle) and the last price against it. The last price is the close of the
    most recently received in-progress candle when any interval has one (unverified), else the last closed close of
    the finest interval loaded; cross.last_price_from says which."""
    day = blocks.get("1d")
    prior, prior_why = None, None
    if day is None:
        prior_why = "the 1d interval wasn't requested"
    elif not day.get("last_closed"):
        prior_why = f"the 1d interval is {day.get('state')}: {day.get('reason')}"
    else:
        lc = day["last_closed"]
        prior = {"time_ms": lc["time_ms"], "time": iso_ms(lc["time_ms"]), "open": lc["open"], "high": lc["high"],
                 "low": lc["low"], "close": lc["close"]}
    live = []
    for iv, b in blocks.items():
        ip = b.get("in_progress")
        if ip and ip.get("close") is not None:
            age = ip.get("received_age_s")
            live.append((math.inf if age is None else age, INTERVAL_MS[iv], iv, ip))
    last_price, origin, status, price_why = None, None, None, None
    if live:
        age, _, iv, ip = min(live, key=lambda x: (x[0], x[1]))
        last_price, status = ip["close"], "unverified"
        origin = (f"the in-progress {iv} candle's close (unverified; received {age:.1f} s ago)" if age != math.inf
                  else f"the in-progress {iv} candle's close (unverified)")
    else:
        closed = sorted((INTERVAL_MS[iv], iv, b["last_closed"]) for iv, b in blocks.items() if b.get("last_closed"))
        if closed:
            _, iv, lc = closed[0]
            last_price, status = lc["close"], "closed"
            origin = f"the last closed {iv} candle's close (opened {iso_ms(lc['time_ms'])})"
        else:
            price_why = "no interval has a candle yet"
    out = {"prior_day": prior}
    if prior is None:
        out["prior_day_reason"] = prior_why
    out["last_price"], out["last_price_from"], out["last_price_status"] = last_price, origin, status
    if last_price is None:
        out["last_price_reason"] = price_why
    out["above_prior_day_high"] = out["below_prior_day_low"] = out["position_in_prior_day_range_pct"] = None
    if prior is None or last_price is None:
        out["comparison_reason"] = prior_why if prior is None else price_why
        return out
    out["above_prior_day_high"] = last_price > prior["high"]
    out["below_prior_day_low"] = last_price < prior["low"]
    _put(out, "position_in_prior_day_range_pct", _pct(last_price - prior["low"], prior["high"] - prior["low"]),
         "the prior day's high equals its low")
    return out


def structure(market: str, intervals: list[str], views: dict[str, dict], as_of_ms: int | None = None) -> dict:
    """The hl_structure answer from one view per interval (CandleStore.view with limit CANDLES_USED + 1)."""
    blocks = {iv: interval_block(views[iv], CANDLES_USED[iv]) for iv in intervals}
    ready = [views[iv] for iv in intervals if views[iv].get("ok")]
    if as_of_ms is None:
        served = [v["provenance"]["served_at_ms"] for v in ready if (v.get("provenance") or {}).get("served_at_ms")]
        as_of_ms = max(served) if served else int(time.time() * 1000)
    per_interval = {}
    for iv in intervals:
        v, b = views[iv], blocks[iv]
        if v.get("ok"):
            h, p = v.get("history") or {}, v.get("provenance") or {}
            per_interval[iv] = {"state": b["state"], "candles_held": h.get("available"), "fetched_at_ms": h.get("fetched_at_ms"),
                                "fetched_at": h.get("fetched_at"), "fetches": h.get("fetches"), "refetching": h.get("refetching"),
                                "websocket_messages": (p.get("websocket") or {}).get("messages"),
                                "in_progress_from": (v.get("in_progress") or {}).get("from")}
        else:
            per_interval[iv] = {"state": b["state"], "reason": b.get("reason")}
    notes = list(NOTES)
    loading = [iv for iv in intervals if blocks[iv]["state"] == "loading"]
    if loading:
        notes.append(f"interval{'s' if len(loading) > 1 else ''} {', '.join(loading)} still loading: the first candleSnapshot "
                     "fetch is running; call again in a second for them")
    prov = {"mode": ready[0]["provenance"].get("mode") if ready else None, "source": SOURCE, "market": market,
            "dex": dex_of(market), "as_of_ms": as_of_ms, "as_of": iso_ms(as_of_ms), "intervals": per_interval,
            "note": PROVENANCE_NOTE}
    return {"ok": True, "market": market, "as_of_ms": as_of_ms, "as_of": iso_ms(as_of_ms), "intervals": blocks,
            "cross": cross_block(blocks), "source": SOURCE, "provenance": prov, "notes": notes}


__all__ = ["CANDLES_USED", "DEFAULT_INTERVALS", "MIN_CANDLES", "NOTES", "cross_block", "interval_block", "structure"]
