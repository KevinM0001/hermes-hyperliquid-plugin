"""Order book metrics from Hyperliquid's aggregated public levels.

Levels are aggregated price levels, never individual orders. Each carries
n, shown as 'API count' (the docs call it 'number of levels' in one place
and 'number of orders' in another). Depth within a band is reported only
where the returned levels reach the band edge on that side.
"""

from __future__ import annotations

from .util import fnum, na, rnd, val

BANDS_PCT = (0.25, 0.5, 1.0)
API_COUNT_NOTE = ("API count: Hyperliquid's 'n' for an aggregated level. The docs describe it two ways: "
                  "'number of levels' (info l2Book) and 'number of orders' (websocket WsLevel).")


def _api_count(n) -> int | None:
    """Hyperliquid's n as a whole number >= 0, or None (shown with a reason) when missing or not a count;
    a fractional value is never truncated."""
    if isinstance(n, bool):
        return None
    if isinstance(n, int):
        return n if n >= 0 else None
    if isinstance(n, float) and n.is_integer() and n >= 0:
        return int(n)
    return None


def parse_levels(raw_side: list) -> tuple[list[tuple[float, float, int | None]], list[dict]]:
    """(levels, problems). A level whose px or sz is missing, not a number, or not positive isn't summed, and
    is reported in problems so that side's totals are marked unavailable; a missing or invalid n gives an API
    count of None, not 0."""
    out, problems = [], []
    for i, lv in enumerate(raw_side or []):
        if not isinstance(lv, dict):
            problems.append({"index": i, "missing_or_invalid": ["px", "sz"]})
            continue
        px, sz = fnum(lv.get("px")), fnum(lv.get("sz"))
        bad = [k for k, v in (("px", px), ("sz", sz)) if v is None or v <= 0]
        if bad:
            problems.append({"index": i, "missing_or_invalid": bad})
            continue
        out.append((px, sz, _api_count(lv.get("n"))))
    return out, problems


def problem_reason(side: str, problems: list[dict]) -> str | None:
    if not problems:
        return None
    first = problems[0]
    return (f"{len(problems)} returned {side} level(s) had a missing or invalid "
            f"{'/'.join(first['missing_or_invalid'])} (first at level {first['index'] + 1}), so this side's totals "
            "can't be summed")


def top_of_book(bids: list, asks: list, bad_top: dict | None = None) -> dict:
    """bad_top: {"bid": reason, "ask": reason} when that side's first returned level was malformed."""
    bad_top = bad_top or {}
    bid_reason = bad_top.get("bid") or ("empty bid side" if not bids else None)
    ask_reason = bad_top.get("ask") or ("empty ask side" if not asks else None)
    bb = None if bid_reason else bids[0][0]
    ba = None if ask_reason else asks[0][0]
    res = {
        "best_bid": val(bb, "USD") if bb is not None else na(bid_reason, "USD"),
        "best_ask": val(ba, "USD") if ba is not None else na(ask_reason, "USD"),
    }
    if bb is None or ba is None:
        reason = bid_reason or ask_reason
        res.update(mid=na(reason, "USD"), spread=na(reason, "USD"), spread_bps=na(reason, "bps"))
    else:
        mid = (bb + ba) / 2
        res.update(mid=val(rnd(mid, 8), "USD"), spread=val(rnd(ba - bb, 8), "USD"),
                   spread_bps=val(rnd((ba - bb) / mid * 1e4, 4), "bps"))
    return res


def mid_of(bids: list, asks: list) -> float | None:
    if not bids or not asks:
        return None
    return (bids[0][0] + asks[0][0]) / 2


def side_band(levels: list, mid: float, band_pct: float, side: str) -> dict:
    """Depth on one side within band_pct of mid, if the returned levels reach the edge."""
    if not levels:
        return {"covered": False, "reason": f"empty {side} side"}
    eps = abs(mid) * 1e-12          # a level exactly on the edge counts despite float rounding
    if side == "bid":
        edge = mid * (1 - band_pct / 100)
        deepest = min(l[0] for l in levels)
        covered = deepest <= edge + eps
        inside = [l for l in levels if l[0] >= edge - eps]
    else:
        edge = mid * (1 + band_pct / 100)
        deepest = max(l[0] for l in levels)
        covered = deepest >= edge - eps
        inside = [l for l in levels if l[0] <= edge + eps]
    out = {"edge_px": rnd(edge, 8), "deepest_returned_px": deepest, "covered": covered}
    if not covered:
        out["reason"] = "band not covered by returned levels"
        return out
    out["notional_usd"] = rnd(sum(px * sz for px, sz, _ in inside), 2)
    out["size_coin"] = rnd(sum(sz for _, sz, _ in inside), 8)
    out["levels_counted"] = len(inside)
    return out


def observed_depth(levels: list, mid: float | None, side: str) -> dict:
    if not levels:
        return {"label": "observed depth within returned levels", "levels": 0,
                "notional_usd": na(f"empty {side} side", "USD")}
    deepest = min(l[0] for l in levels) if side == "bid" else max(l[0] for l in levels)
    span = None if mid is None else abs(deepest - mid) / mid * 100
    return {
        "label": "observed depth within returned levels",
        "levels": len(levels),
        "notional_usd": val(rnd(sum(px * sz for px, sz, _ in levels), 2), "USD"),
        "size_coin": rnd(sum(sz for _, sz, _ in levels), 8),
        "deepest_px": deepest,
        "span_pct": None if span is None else rnd(span, 4),
    }


def bucket_width(levels: list, other_side: list | None = None) -> float | None:
    """Smallest positive spacing between returned prices within one side (the spread isn't a bucket):
    the edge resolution of an aggregated book. Pass both sides as two lists to take the smaller."""
    def side_min(lv):
        pxs = sorted({l[0] for l in lv})
        gaps = [b - a for a, b in zip(pxs, pxs[1:]) if b - a > 0]
        return min(gaps) if gaps else None
    vals = [v for v in (side_min(levels), side_min(other_side or [])) if v is not None]
    return rnd(min(vals), 10) if vals else None


def _approx_side(levels: list, mid: float, band_pct: float, side: str) -> dict:
    """Approximate depth from aggregated buckets, counted by their returned (representative) price.

    Bucket alignment isn't documented, so the bucket nearest the edge on each side of it may be partly in
    or out of the band: the range runs from leaving out the outermost bucket counted to adding the first
    bucket left out."""
    res = side_band(levels, mid, band_pct, side)
    if not res["covered"]:
        return res
    edge = res["edge_px"]
    if side == "bid":
        inside = sorted((l for l in levels if l[0] >= edge), key=lambda l: l[0])          # outermost first
        outside = sorted((l for l in levels if l[0] < edge), key=lambda l: -l[0])         # nearest first
    else:
        inside = sorted((l for l in levels if l[0] <= edge), key=lambda l: -l[0])
        outside = sorted((l for l in levels if l[0] > edge), key=lambda l: l[0])
    approx = sum(px * sz for px, sz, _ in inside)
    lo = approx - (inside[0][0] * inside[0][1] if inside else 0.0)
    hi = approx + (outside[0][0] * outside[0][1] if outside else 0.0)
    return res | {"approx_usd": rnd(approx, 2), "range_usd": [rnd(lo, 2), rnd(hi, 2)]}


def depth_bands(full_book: dict | None, coarse_books: list[dict], bands=BANDS_PCT) -> list[dict]:
    """Exact band depth from full-precision levels only; coarse aggregated books give a labeled approximation.

    full_book / coarse_books: {kind, usable, bids, asks, mid, time_ms, bucket_width, bad, reason}."""
    out = []
    for band in bands:
        row = {"band_pct": band}
        # --- exact: full-precision returned levels only
        exact = {}
        for side, key in (("bid", "bids"), ("ask", "asks")):
            if not full_book or not full_book.get("usable"):
                exact[side] = {"notional_usd": na((full_book or {}).get("reason") or "no full-precision book", "USD"),
                               "status": "unavailable"}
                continue
            bad = (full_book.get("bad") or {}).get(side)
            if bad:
                exact[side] = {"notional_usd": na(bad, "USD"), "status": "unavailable"}
                continue
            res = side_band(full_book[key], full_book["mid"], band, side)
            if not res["covered"]:
                exact[side] = {"notional_usd": na("band not covered by the full-precision returned levels", "USD"),
                               "status": "unavailable", "deepest_returned_px": res.get("deepest_returned_px"),
                               "see": "observed depth within returned levels"}
            else:
                exact[side] = {"notional_usd": val(res["notional_usd"], "USD"), "size_coin": res["size_coin"],
                               "levels_counted": res["levels_counted"], "edge_px": res["edge_px"],
                               "book": full_book["kind"], "book_time_ms": full_book["time_ms"], "status": "covered"}
        if exact["bid"]["status"] == "covered" and exact["ask"]["status"] == "covered":
            tot = exact["bid"]["notional_usd"]["value"] + exact["ask"]["notional_usd"]["value"]
            imb = (val(rnd((exact["bid"]["notional_usd"]["value"] - exact["ask"]["notional_usd"]["value"]) / tot, 4), "ratio")
                   if tot > 0 else na("no depth on either side", "ratio"))
        else:
            imb = na("needs both sides covered by the full-precision book", "ratio")
        row["exact"] = exact | {"imbalance": imb, "label": "exact: full-precision returned levels"}
        # --- approximate: the finest coarse book whose buckets reach the edge
        approx, chosen = {}, {}
        for side, key in (("bid", "bids"), ("ask", "asks")):
            pick, tried = None, []
            for b in coarse_books:
                if not b.get("usable"):
                    tried.append({"book": b["kind"], "reason": b.get("reason")})
                    continue
                if (b.get("bad") or {}).get(side):
                    tried.append({"book": b["kind"], "reason": b["bad"][side]})
                    continue
                res = _approx_side(b[key], b["mid"], band, side)
                if res["covered"]:
                    pick = (b, res)
                    break
                tried.append({"book": b["kind"], "reason": res.get("reason")})
            if pick is None:
                approx[side] = {"approx_usd": na("no aggregated book reaches this band", "USD"), "status": "unavailable",
                                "tried": tried}
            else:
                b, res = pick
                chosen[side] = b
                approx[side] = {"approx_usd": val(res["approx_usd"], "USD"), "range_usd": res["range_usd"],
                                "book": b["kind"], "aggregation": b["kind"], "bucket_width": b.get("bucket_width"),
                                "book_time_ms": b["time_ms"], "status": "approximate"}
        if (approx["bid"]["status"] == "approximate" and approx["ask"]["status"] == "approximate"
                and chosen["bid"] is chosen["ask"]):
            tot = approx["bid"]["approx_usd"]["value"] + approx["ask"]["approx_usd"]["value"]
            aimb = (val(rnd((approx["bid"]["approx_usd"]["value"] - approx["ask"]["approx_usd"]["value"]) / tot, 4), "ratio")
                    if tot > 0 else na("no depth on either side", "ratio"))
        else:
            aimb = na("an approximate imbalance needs both sides from one aggregated book snapshot", "ratio")
        row["approximate"] = approx | {
            "imbalance": aimb,
            "label": "approximate: aggregated buckets",
            "convention": ("each bucket is counted entirely at its returned (representative) price times its aggregate size; "
                           "Hyperliquid doesn't document bucket alignment, so a bucket at the edge may be partly in or out of the band. "
                           "range_usd runs from leaving out the outermost bucket counted to adding the first bucket left out."),
        }
        out.append(row)
    return out


def _row(px, sz, n) -> dict:
    row = {"px": px, "sz": sz, "api_count": n, "notional_usd": rnd(px * sz, 2)}
    if n is None:
        row["api_count_reason"] = "the returned level had no n field"
    return row


def largest_level(rows: list[dict], side: str) -> dict | None:
    """The returned level with the largest aggregate size on one side (ties to the level nearer the top of book):
    the same level as the chart's 'largest returned … level' price line."""
    if not rows:
        return None
    return dict(max(rows, key=lambda r: (r["sz"], -r["px"] if side == "asks" else r["px"])))


def ladder_rows(bids: list, asks: list, problems: dict | None = None) -> dict:
    out = {
        "bids": [_row(*lv) for lv in bids],
        "asks": [_row(*lv) for lv in asks],
        "api_count_note": API_COUNT_NOTE,
        "aggregated": True,
    }
    out["largest"] = {
        "by": ("the largest aggregate size (coin) among the returned levels on that side, ties to the level nearer the "
               "top; an aggregated level, never one order; the same level as the chart's largest-level price line"),
    }
    for side, key in (("bid", "bids"), ("ask", "asks")):
        bad = (problems or {}).get(side)
        if bad:
            # A returned level with a missing or invalid px or sz could be the largest: the ranking isn't established
            # (Codex h1 review 3, B1).
            out["largest"][side] = None
            out["largest"][f"{side}_reason"] = (f"{len(bad)} returned {side} level(s) have a missing or invalid px or sz, "
                                                "so the largest returned level can't be established")
        else:
            out["largest"][side] = largest_level(out[key], key)
    if problems and (problems.get("bid") or problems.get("ask")):
        out["malformed_levels"] = problems
        out["malformed_note"] = "levels with a missing or invalid px or sz are left out of the rows and make that side's totals unavailable"
    return out
