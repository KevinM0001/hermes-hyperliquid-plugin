"""Aggressive flow over the observed window (schema metrics flow.*)."""

from __future__ import annotations

from .util import iso_ms, na, rnd, val

CVD_BUCKET_MS = 30_000
CVD_SPAN_MS = 15 * 60 * 1000


def totals(trades) -> dict:
    buy = sum(t.notional for t in trades if t.side == "B")
    sell = sum(t.notional for t in trades if t.side == "A")
    return {"buy": buy, "sell": sell, "net": buy - sell, "volume": buy + sell, "count": len(trades)}


def flow_values(trades, unavailable_reason: str | None) -> dict:
    if unavailable_reason:
        return {k: na(unavailable_reason, "USD") for k in
                ("buy_aggressive_notional", "sell_aggressive_notional", "net", "volume")} | {
            "trade_count": na(unavailable_reason, "trades")}
    s = totals(trades)
    return {"buy_aggressive_notional": val(rnd(s["buy"], 2), "USD"),
            "sell_aggressive_notional": val(rnd(s["sell"], 2), "USD"),
            "net": val(rnd(s["net"], 2), "USD"),
            "volume": val(rnd(s["volume"], 2), "USD"),
            "trade_count": val(s["count"], "trades")}


def cvd_buckets(store, unavailable_reason: str | None) -> dict:
    seg, wm = store.segment_start_ms, store.watermark_ms
    if seg is None or wm is None:
        return {"bucket_s": CVD_BUCKET_MS // 1000, "buckets": [], "reason": unavailable_reason or "no coverage segment"}
    first = max(seg, wm - CVD_SPAN_MS)
    b0 = (first // CVD_BUCKET_MS) * CVD_BUCKET_MS
    buckets = []
    cum = 0.0
    origin = None
    trades = store.window(b0, wm)
    idx = 0
    t0 = b0
    while t0 <= wm:
        t1 = t0 + CVD_BUCKET_MS
        buy = sell = 0.0
        while idx < len(trades) and trades[idx].time < t1:
            tr = trades[idx]
            if tr.side == "B":
                buy += tr.notional
            elif tr.side == "A":
                sell += tr.notional
            idx += 1
        complete = t0 > seg and t1 - 1 <= wm
        if unavailable_reason or not complete:
            reason = unavailable_reason or ("bucket incomplete: it starts before the segment start" if t0 <= seg
                                            else "bucket incomplete: it ends after the trades watermark")
            buckets.append({"start_ms": t0, "start": iso_ms(t0), "buy_usd": None, "sell_usd": None,
                            "delta_usd": None, "cumulative_usd": None, "complete": complete, "reason": reason})
        else:
            delta = buy - sell
            cum += delta
            if origin is None:
                origin = t0
            buckets.append({"start_ms": t0, "start": iso_ms(t0), "buy_usd": rnd(buy, 2), "sell_usd": rnd(sell, 2),
                            "delta_usd": rnd(delta, 2), "cumulative_usd": rnd(cum, 2), "complete": True})
        t0 = t1
    return {"bucket_s": CVD_BUCKET_MS // 1000, "buckets": buckets,
            "cumulative_from_ms": origin, "cumulative_from": iso_ms(origin),
            "cumulative_rule": "cumulative_usd sums delta_usd from the first complete bucket (cumulative_from) onward; it resets at every gap",
            "resets_at_gap": True,
            "reason": unavailable_reason}


def large_prints(trades, threshold: float, unavailable_reason: str | None = None, limit: int = 20) -> dict:
    big = [t for t in trades if t.notional >= threshold]
    big.sort(key=lambda t: (t.time, t.seq), reverse=True)
    count = {"count": None, "reason": unavailable_reason} if unavailable_reason else {"count": len(big)}
    return {"threshold_usd": threshold, **count,
            "note": "a public trade is one fill between two wallets; one taker order can produce several",
            "prints": [t.as_dict() for t in big[:limit]]}
