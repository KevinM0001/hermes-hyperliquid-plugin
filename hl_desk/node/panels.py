"""The node panels of one market, built from one client's state (handoff steps 14 and 16-18).

Every panel carries its provenance (source, mode, producer session, replay session) and the freshness of
the families it is built on. Panels built on the book (ladder, walls, history, thinning, the stop bands'
reference price) are invalid while the book is invalid, and while it is crossed (step 17: best bid at or
above best ask) until a fresh snapshot clears it.

Panel state that accumulates (the heatmap history, walls, liquidation bursts) lives with the market's
partition and is dropped at every gap or session change: it never bridges one, and it shows its observed
window.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from .book import NodeBook
from .flow import activity_buckets, liquidations, wallet_split, wallet_splits
from .proto import fmt, num

ZERO = Decimal(0)
LADDER_LEVELS = 20
CHANGE_WINDOW_MS = 10_000
HISTORY_INTERVAL_MS = 5_000
HISTORY_SPAN_MS = 15 * 60 * 1000
HISTORY_RANGE = Decimal("0.02")
WALL_RANGE = Decimal("0.02")
WALLS_PER_SIDE = 5
WALL_MIN_MULTIPLE = 5
THIN_STEP = Decimal("0.005")
THIN_BANDS = 20                     # 20 x 0.5% = out to 10% from the current price
THIN_NOTE = "What's resting right now. Orders can be added or pulled at any moment; this isn't a forecast."
DOMINANT_SHARE = Decimal("0.5")
LIQ_WINDOW_MS = 60_000
TAPE_ROWS = 30
STOPS_ROWS_RANGE_BPS = 200          # band rows shown within 2% of the mid; everything beyond is counted below/above
BOOK_PANEL_FAMILIES = ("book",)


class PanelState:
    """Accumulated panel state of one market partition (dropped at every gap or session change)."""

    def __init__(self) -> None:
        self.history: deque = deque()
        self.history_since_ms: int | None = None
        self.last_sample_ms: int | None = None
        self.walls: dict[int, dict] = {}
        self.walls_gone: deque = deque(maxlen=30)
        self.liq_armed = True
        self.liq_events: deque = deque(maxlen=50)
        self.events: deque = deque(maxlen=50)


def state_of(mf) -> PanelState:
    ps = getattr(mf, "panels", None)
    if ps is None:
        ps = mf.panels = PanelState()
    return ps


def usd(d: Decimal | None) -> float | None:
    return None if d is None else round(float(d), 2)


# --- events ---------------------------------------------------------------------

def on_client_event(ev: dict, opts: dict) -> None:
    client, market = ev["client"], ev["market"]
    mf = client.markets.get(market)
    if mf is None:
        return
    ps = state_of(mf)
    if ev["type"] == "watermark":
        t = ev.get("block_time_ms")
        if t is None or not mf.fam["book"].valid or mf.book is None or mf.crossed is not None:
            return
        if ps.last_sample_ms is None or t - ps.last_sample_ms >= HISTORY_INTERVAL_MS:
            _sample_history(mf, ps, t, client)
            _track_walls(mf, ps, t)
    elif ev["type"] == "execs":
        _liquidation_bursts(mf, ps, opts)


def _sample_history(mf, ps: PanelState, t: int, client) -> None:
    book: NodeBook = mf.book
    mid = book.mid()
    if mid is None:
        return
    width = _band_width(client, mf.market)
    lo, hi = mid * (1 - HISTORY_RANGE), mid * (1 + HISTORY_RANGE)
    bins: dict = {}
    for side in ("B", "A"):
        for px, oids in book.levels[side].items():
            if px < lo or px > hi:
                continue
            k = (px / width).to_integral_value(rounding="ROUND_FLOOR") * width
            b = bins.setdefault(k, [ZERO, ZERO])
            tot = sum((book.orders[o].sz for o in oids), ZERO) * px
            b[0 if side == "B" else 1] += tot
    sample = (t, float(mid), tuple((float(k), usd(v[0]), usd(v[1])) for k, v in sorted(bins.items())))
    ps.history.append(sample)
    ps.last_sample_ms = t
    if ps.history_since_ms is None:
        ps.history_since_ms = t
    while ps.history and ps.history[0][0] < t - HISTORY_SPAN_MS:
        ps.history.popleft()


def _band_width(client, market: str) -> Decimal:
    w = ((client.limits or {}).get("bands_width") or {}).get(market)
    try:
        return Decimal(str(w)) if w is not None else Decimal(1)
    except Exception:
        return Decimal(1)


def _track_walls(mf, ps: PanelState, t: int) -> None:
    book: NodeBook = mf.book
    mid = book.mid()
    if mid is None:
        return
    current = {}
    for side in ("B", "A"):
        lo, hi = (mid * (1 - WALL_RANGE), mid) if side == "B" else (mid, mid * (1 + WALL_RANGE))
        cands = []
        for px, oids in book.levels[side].items():
            if lo <= px <= hi:
                cands.extend(book.orders[o] for o in oids)
        if not cands:
            continue
        sizes = sorted(o.sz * o.px for o in cands)
        median = sizes[len(sizes) // 2]
        big = sorted((o for o in cands if o.sz * o.px >= median * WALL_MIN_MULTIPLE),
                     key=lambda o: (-(o.sz * o.px), o.oid))[:WALLS_PER_SIDE]
        for o in big:
            current[o.oid] = o
    for oid, o in current.items():
        w = ps.walls.get(oid)
        if w is None:
            ps.walls[oid] = {"oid": oid, "side": o.side, "px": o.px, "first_seen_ms": t, "placed_ms": o.time_ms,
                             "user": o.user, "peak_sz": o.sz}
        else:
            w["peak_sz"] = max(w["peak_sz"], o.sz)
    for oid in list(ps.walls):
        if oid not in current:
            w = ps.walls.pop(oid)
            still = book.orders.get(oid)
            w["left_ms"] = t
            w["outcome"] = _wall_outcome(book, oid, still)
            ps.walls_gone.appendleft(w)


def _wall_changes(book: NodeBook, oid: int) -> dict:
    out = {"absorbed": ZERO, "pulled": ZERO, "unknown": ZERO, "added": ZERO}
    for (_t, _s, _px, label, size, o) in book.changes:
        if o != oid:
            continue
        if label == "absorbed":
            out["absorbed"] += size
        elif label == "pulled":
            out["pulled"] += size
        elif label == "added":
            out["added"] += size
        else:
            out["unknown"] += size
    return out


def _wall_outcome(book: NodeBook, oid: int, still) -> dict:
    ch = _wall_changes(book, oid)
    if still is not None:
        label = "still resting (no longer among the largest orders near the price)"
    elif ch["pulled"] > 0 and ch["absorbed"] == 0 and ch["unknown"] == 0:
        label = "pulled (canceled with nothing filled)"
    elif ch["absorbed"] > 0 and ch["pulled"] == 0 and ch["unknown"] == 0:
        label = "absorbed (filled as maker)"
    elif ch["absorbed"] > 0 or ch["pulled"] > 0:
        label = "partly absorbed, the rest pulled or unknown"
    else:
        label = "unknown (no lifecycle record explains the change)"
    return {"label": label, "absorbed_sz": num(ch["absorbed"]), "pulled_sz": num(ch["pulled"]),
            "unknown_sz": num(ch["unknown"])}


def _liquidation_bursts(mf, ps: PanelState, opts: dict) -> None:
    if mf.flow.restart_pending or mf.flow.last_block_time_ms is None:
        return
    now = mf.flow.last_block_time_ms
    execs = [e for e in mf.flow.execs.values() if e.time_ms > now - LIQ_WINDOW_MS and e.liquidation is not None]
    liqs = liquidations(execs)
    n = len(liqs)
    total = sum((g["notional"] for g in liqs), ZERO)
    need_n = opts.get("liq_burst_count", 2)
    need_usd = Decimal(str(opts.get("liq_burst_usd", 1_000_000)))
    firing = n >= need_n or total >= need_usd
    if not firing:
        ps.liq_armed = True
        return
    if ps.liq_armed:
        ps.liq_armed = False
        longs = sum((g["notional"] for g in liqs if g["position"] == "long"), ZERO)
        shorts = sum((g["notional"] for g in liqs if g["position"] == "short"), ZERO)
        unknown = total - longs - shorts
        ps.liq_events.appendleft({
            "type": "liquidation_burst", "market": mf.market, "at_ms": now,
            "window_ms": LIQ_WINDOW_MS, "liquidations": n, "notional_usd": usd(total),
            "longs_liquidated_usd": usd(longs), "shorts_liquidated_usd": usd(shorts),
            "unknown_usd": usd(unknown), "threshold": {"count": need_n, "notional_usd": float(need_usd)},
            "traders": [g["liquidated_user"] for g in liqs][:10],
            "text": f"{n} liquidation{'s' if n != 1 else ''} in 60 s: ${float(total):,.0f} "
                    f"(longs ${float(longs):,.0f}, shorts ${float(shorts):,.0f})",
            "counting": "each liquidation once, for the liquidated trader (its fills summed); the trader's own "
                        "position effect sets long or short",
            "stream": "node fills"})


# --- views -------------------------------------------------------------------------

def node_delay(client, market: str, now_mono_ns=None) -> dict:
    """The node feed's delay for the market on screen (the tab's counter): the latest watermark's age averaged over
    the last 5 s, its current value, and the state it gives (LIVE, BEHIND as "delayed", STALE). A replay has no
    delay: it runs on its own clock."""
    from .client import delay_state
    if client.mode == "REPLAY":
        return {"available": False, "reason": "a replay runs on its own clock: no live delay"}
    d = client.delay(market, now_mono_ns)
    state, reason = delay_state(d.get("delay_ms"), d | {"delay_window_ms": d["window_ms"]})
    if client.skew["bad"]:
        # As for the families: this machine's clock is behind the producer's, so the delay can't be bounded.
        state, reason = "UNKNOWN", (f"clock skew: this machine's clock is up to {-client.skew['worst_offset_ms']} ms "
                                    "behind the producer's, so the delay can't be bounded")
    return {"available": d.get("delay_ms") is not None, "state": state, "reason": reason or d.get("reason")} | d


def provenance(client, market: str) -> dict:
    return {"source": client.source, "mode": client.mode, "epoch": client.epoch, "session": client.session,
            "replay_session": client.replay_session if client.mode == "REPLAY" else None,
            "stream": client.stream_label(), "market": market, "protocol": "l4-livefeed v2 (document revision 2.2)"}


def families(client, market: str, now_wall_ms=None, now_mono_ns=None) -> dict:
    return {f: client.family_status(market, f, now_wall_ms, now_mono_ns)
            for f in ("book", "triggers", "depth", "bands", "flow")}


def book_validity(mf, fam_status: dict) -> tuple[bool, str | None]:
    st = fam_status["book"]
    if mf is None or mf.book is None or st["state"] == "INVALID":
        return False, f"book {st['state']}: {st.get('reason')}"
    if mf.crossed is not None:
        c = mf.crossed
        return False, (f"book crossed since seq {c['since_seq']} (best bid {fmt(c['best_bid'])} at or above best ask "
                       f"{fmt(c['best_ask'])}); invalid until a fresh snapshot clears it")
    if st["state"] not in ("LIVE", "REPLAY"):
        return False, f"book {st['state']}: {st.get('reason')}"
    return True, None


def ladder(mf, client, trig_status: dict | None = None, levels: int = LADDER_LEVELS) -> dict:
    book: NodeBook = mf.book
    # The stops column comes from the trigger family: while it isn't usable (invalid, stale, behind or of unknown
    # age), every row says so with the reason, never 0 (which would read as no waiting interest).
    tst = trig_status or {"state": "INVALID", "reason": "trigger state not given"}
    stops_ok = mf.triggers is not None and tst["state"] in ("LIVE", "REPLAY")
    stops_na = None if stops_ok else {"available": False, "reason": f"triggers {tst['state']}: {tst.get('reason')}"}
    now = mf.block_time_ms
    since = (now or 0) - CHANGE_WINDOW_MS
    recent = book.changes_since(since)
    chg: dict = {}
    for (_t, side, px, label, size, _oid) in recent:
        d = chg.setdefault((side, px), {"added": ZERO, "absorbed": ZERO, "pulled": ZERO, "unknown": ZERO})
        key = label if label in ("added", "absorbed", "pulled") else "unknown"
        d[key] += size
    # Waiting triggers sorted by price once, for the per-row "stops to the next level" counts.
    import bisect
    trig = sorted(((g.trigger_px, g.position_tpsl, g.sz) for g in mf.triggers.orders.values()),
                  key=lambda x: x[0]) if stops_ok else []
    tpx = [x[0] for x in trig]
    out = {}
    for side in ("B", "A"):
        pxs = book.sorted_levels(side)[:levels]
        rows = []
        for i, px in enumerate(pxs):
            st = book.level_stats(side, px)
            nxt = pxs[i + 1] if i + 1 < len(pxs) else None
            lo, hi = (nxt, px) if side == "B" else (px, nxt)
            stops_n, stops_sz, stops_unk = 0, ZERO, 0
            if nxt is not None:
                # bids: (next, px]; asks: [px, next)
                i0 = bisect.bisect_right(tpx, lo) if side == "B" else bisect.bisect_left(tpx, lo)
                i1 = bisect.bisect_right(tpx, hi) if side == "B" else bisect.bisect_left(tpx, hi)
                for _px, ptpsl, sz in trig[i0:i1]:
                    stops_n += 1
                    if ptpsl:
                        stops_unk += 1
                    else:
                        stops_sz += sz
            c = chg.get((side, px), {"added": ZERO, "absorbed": ZERO, "pulled": ZERO, "unknown": ZERO})
            rows.append({
                "px": fmt(px), "total_sz": num(st["total_sz"]), "orders": st["orders"],
                "notional_usd": usd(st["total_sz"] * px),
                "largest_order_sz": num(st["largest_order_sz"]), "largest_order_time_ms": st["largest_order_time_ms"],
                "oldest_order_time_ms": st["oldest_order_time_ms"],
                "change_10s": {"added": num(c["added"]), "absorbed": num(c["absorbed"]), "pulled": num(c["pulled"]),
                               "unknown": num(c["unknown"])},
                "stops_to_next_level": (None if nxt is None else dict(stops_na) if stops_na is not None else
                                        {"orders": stops_n, "sz": num(stops_sz), "unknown_size_orders": stops_unk})})
        out["bids" if side == "B" else "asks"] = rows
    return out | {
        "labels": {"largest_order_sz": "largest order size at this level",
                   "oldest_order_time_ms": "oldest resting order at this level",
                   "largest_order_time_ms": "placement time of that same largest order (tie: earliest, then lowest oid)",
                   "change_10s": "size changes at this level in the last 10 s of block time, labelled from lifecycle "
                                 "records only: absorbed = filled as maker; pulled = canceled with nothing filled; "
                                 "unknown = not explained by a record (never called moved)",
                   "stops_to_next_level": "waiting stop/take-profit orders whose trigger price lies between this level "
                                          "and the next one shown (conditional interest)"},
        "stops_available": stops_na is None, "stops_reason": None if stops_na is None else stops_na["reason"],
        "book_time_ms": now, "levels_per_side": {"bids": len(book.levels["B"]), "asks": len(book.levels["A"])},
        "best_bid": fmt(book.best("B")), "best_ask": fmt(book.best("A")), "mid": fmt(book.mid())}


def stop_bands(mf, client) -> dict:
    tr = mf.triggers
    ref = mf.book.mid() if (mf.book is not None and mf.crossed is None) else None
    width = _band_width(client, mf.market)
    rng = STOPS_ROWS_RANGE_BPS
    b = tr.bands(ref, width, rng)

    def row(r):
        lg = r.get("largest")
        return {"lo": fmt(r.get("lo")), "hi": fmt(r.get("hi")), "kind": r.get("kind"), "side": r.get("side"),
                "orders": r["orders"], "sized_orders": r["sized_orders"], "sz": num(r["sz"]),
                "unknown_size_orders": r["position_tpsl_orders"],
                "largest_order": None if lg is None else {"sz": num(lg["sz"]), "user": lg["user"],
                                                           "share_pct": round(float(lg["share"]) * 100, 2) if lg["share"] is not None else None},
                "dominated": r["dominated"],
                "size_check": (f"one order is {float(lg['share']) * 100:.0f}% of this band's stated size"
                               if r["dominated"] else None)}

    return {"label": "conditional interest: waiting stop and take-profit orders, which do nothing unless the price "
                     "reaches them",
            "ref_px": fmt(b["ref_px"]), "ref_px_reason": None if ref is not None else "no valid uncrossed book mid",
            "width": fmt(width), "range_bps": rng, "lo": fmt(b["lo"]), "hi": fmt(b["hi"]),
            "rows": [row(r) for r in b["rows"]],
            "outside": {k: row(v) for k, v in b["outside"].items()},
            "accounted": {"waiting_triggers": b["waiting_total"], "placed": b["accounted"],
                          "note": "every waiting trigger is in a band row, below or above the range, or under no reference price"},
            "excluded_by_engine": {"trailing_stops": b["excluded"].get("trailing"),
                                   "unclassified": b["excluded"].get("unclassified"),
                                   "note": "trailing stops aren't in the feed: the engine doesn't establish their current "
                                           "trigger price (carve-out C-2)"},
            "size_check_rule": f"a band is flagged when one order is more than {int(DOMINANT_SHARE * 100)}% of its stated size; "
                               "whole-position TP/SL orders have no stated size and are counted separately"}


def _changes_by_oid(book: NodeBook, oids) -> dict:
    want = set(oids)
    out = {o: {"absorbed": ZERO, "pulled": ZERO, "unknown": ZERO, "added": ZERO} for o in want}
    for (_t, _s, _px, label, size, o) in book.changes:
        if o in want:
            key = label if label in ("absorbed", "pulled", "added") else "unknown"
            out[o][key] += size
    return out


def walls(mf, ps: PanelState) -> dict:
    book: NodeBook = mf.book
    cur = []
    changes = _changes_by_oid(book, ps.walls)
    for oid, w in ps.walls.items():
        o = book.orders.get(oid)
        if o is None:
            continue
        ch = changes[oid]
        cur.append({"oid": oid, "side": w["side"], "px": fmt(w["px"]), "sz": num(o.sz), "notional_usd": usd(o.sz * o.px),
                    "user": w["user"], "placed_ms": w["placed_ms"], "first_seen_ms": w["first_seen_ms"],
                    "absorbed_sz": num(ch["absorbed"]), "pulled_sz": num(ch["pulled"]), "unknown_sz": num(ch["unknown"])})
    cur.sort(key=lambda w: -(w["notional_usd"] or 0))
    gone = [{"oid": w["oid"], "side": w["side"], "px": fmt(w["px"]), "peak_sz": num(w["peak_sz"]), "user": w["user"],
             "first_seen_ms": w["first_seen_ms"], "left_ms": w.get("left_ms"), "outcome": w.get("outcome")}
            for w in list(ps.walls_gone)]
    return {"definition": f"orders within {int(WALL_RANGE * 100)}% of the mid at least {WALL_MIN_MULTIPLE}x the median "
                          f"order notional there; the {WALLS_PER_SIDE} largest per side",
            "labels": "absorbed = filled as maker; pulled = canceled with nothing filled; unknown otherwise; "
                      "never 'moved' (no lifecycle record links a cancel to a later order)",
            "current": cur, "recently_gone": gone, "observed_since_ms": ps.history_since_ms}


def history(ps: PanelState, client, market: str) -> dict:
    return {"interval_ms": HISTORY_INTERVAL_MS, "span_ms": HISTORY_SPAN_MS,
            "range_pct": float(HISTORY_RANGE * 100), "bin_width": fmt(_band_width(client, market)),
            "since_ms": ps.history_since_ms,
            "partial": True if ps.history_since_ms is None else (
                (ps.history[-1][0] - ps.history_since_ms) < HISTORY_SPAN_MS if ps.history else True),
            "partial_note": "history starts when this source's book last became valid (connect, gap, replay start, or "
                            "recovery from an invalid or crossed book); nothing before it is shown",
            "fields": ["t_ms", "mid", "bins: [lo, bid_usd, ask_usd]"],
            "samples": tuple(ps.history)}


def tape(mf, rows: int = TAPE_ROWS) -> list[dict]:
    out = []
    for e in list(mf.flow.execs.values())[-rows:][::-1]:
        taker = next((p for p in e.participants if p.role == "taker"), None)
        maker = next((p for p in e.participants if p.role == "maker"), None)
        flags = []
        if e.liquidation is not None:
            flags.append("liquidation")
        if any(p.twap_id is not None for p in e.participants):
            flags.append("twap")
        if not e.complete:
            flags.append("incomplete")
        if e.conflict:
            flags.append("conflict")
        out.append({"tid": e.tid, "time_ms": e.time_ms, "px": fmt(e.px), "sz": num(e.sz), "notional_usd": usd(e.notional),
                    "aggressor": e.aggressor, "taker": taker.user if taker else None,
                    "maker": maker.user if maker else None,
                    "taker_effect": ({k: num(v) for k, v in taker.effect.items()} if taker and taker.effect else None),
                    "taker_effect_unavailable": (taker.effect_unavailable if taker and taker.effect is None else None),
                    "flags": flags})
    return out


def thinning(mf) -> dict:
    """Step 18: depth per 0.5% band out to 10% from the current price, from the full book."""
    book: NodeBook = mf.book
    mid = book.mid()
    if mid is None:
        return {"available": False, "reason": "no mid: one side of the book is empty"}
    step_usd = mid * THIN_STEP
    sides = {}
    for side in ("B", "A"):
        bands = []
        for k in range(THIN_BANDS):
            if side == "B":
                lo, hi = mid * (1 - THIN_STEP * (k + 1)), mid * (1 - THIN_STEP * k)
            else:
                lo, hi = mid * (1 + THIN_STEP * k), mid * (1 + THIN_STEP * (k + 1))
            bands.append({"k": k, "lo": lo, "hi": hi, "usd": ZERO, "sz": ZERO, "orders": 0, "largest": None})
        limit = mid * (1 - THIN_STEP * THIN_BANDS) if side == "B" else mid * (1 + THIN_STEP * THIN_BANDS)
        for px, oids in book.levels[side].items():
            if (side == "B" and (px > mid or px < limit)) or (side == "A" and (px < mid or px > limit)):
                continue
            dist = (mid - px) / mid if side == "B" else (px - mid) / mid
            k = int(dist / THIN_STEP)
            if k >= THIN_BANDS:
                continue
            b = bands[k]
            tot = book.level_total.get((side, px), ZERO)
            b["usd"] += tot * px
            b["sz"] += tot
            b["orders"] += len(oids)
            o = book.largest_at(side, px)
            if o is not None and (b["largest"] is None or o.sz > b["largest"].sz or (o.sz == b["largest"].sz and o.oid < b["largest"].oid)):
                b["largest"] = o
        rows = []
        for b in bands:
            lg = b["largest"]
            share = (lg.sz / b["sz"]) if (lg is not None and b["sz"] > 0) else None
            rows.append({"from_pct": float(THIN_STEP * b["k"] * 100), "to_pct": float(THIN_STEP * (b["k"] + 1) * 100),
                         "lo": fmt(b["lo"].quantize(Decimal("0.0001"))), "hi": fmt(b["hi"].quantize(Decimal("0.0001"))),
                         "depth_usd": usd(b["usd"]), "usd_per_1_price": usd(b["usd"] / step_usd),
                         "orders": b["orders"],
                         "largest_order": None if lg is None else {"sz": num(lg.sz), "usd": usd(lg.sz * lg.px),
                                                                     "share_pct": round(float(share) * 100, 2) if share is not None else None,
                                                                     "user": lg.user},
                         "dominated": share is not None and share > DOMINANT_SHARE})
        drop = None
        for i in range(len(rows) - 1):
            a, b = rows[i]["usd_per_1_price"], rows[i + 1]["usd_per_1_price"]
            if a is None or a <= 0:
                continue
            ratio = (a / b) if b and b > 0 else float("inf")
            if ratio > 1 and (drop is None or ratio > drop["times_thinner"]):
                drop = {"times_thinner": ratio, "from_band_pct": [rows[i]["from_pct"], rows[i]["to_pct"]],
                        "to_band_pct": [rows[i + 1]["from_pct"], rows[i + 1]["to_pct"]],
                        "at_price": rows[i + 1]["hi"] if side == "B" else rows[i + 1]["lo"],
                        "distance_pct": rows[i + 1]["from_pct"]}
        if drop is not None:
            drop["times_thinner"] = None if drop["times_thinner"] == float("inf") else round(drop["times_thinner"], 2)
            drop["empty_band"] = drop["times_thinner"] is None
        sides["bids_below" if side == "B" else "asks_above"] = {"bands": rows, "sharpest_drop": drop}
    return {"available": True, "mid": fmt(mid), "band_pct": float(THIN_STEP * 100), "out_to_pct": float(THIN_STEP * THIN_BANDS * 100),
            "band_width_usd": usd(step_usd), "note": THIN_NOTE,
            "units": {"depth_usd": "sum of price x size of resting orders in the band",
                      "usd_per_1_price": "depth_usd divided by the band's width in dollars of price"},
            "size_check": f"a band is flagged when one order is more than {int(DOMINANT_SHARE * 100)}% of its size",
            **sides}


def market_view(client, market: str, opts: dict, now_wall_ms=None, now_mono_ns=None) -> dict:
    mf = client.markets.get(market)
    fam = families(client, market, now_wall_ms, now_mono_ns)
    base = {"provenance": provenance(client, market), "families": fam,
            "delay": node_delay(client, market, now_mono_ns),
            "connection": dict(client.connection),
            "replay": dict(client.replay) if (client.mode == "REPLAY" and client.replay) else None}
    if mf is None:
        return base | {"available": False, "reason": "no data for this market from this source yet"}
    ps = state_of(mf)
    ok, why = book_validity(mf, fam)
    base["book"] = {"valid": ok, "reason": why, "crossed": None if mf.crossed is None else {
        "since_seq": mf.crossed["since_seq"], "best_bid": fmt(mf.crossed["best_bid"]),
        "best_ask": fmt(mf.crossed["best_ask"]), "since_block_time_ms": mf.crossed["since_block_time_ms"]},
        "book_time_ms": mf.block_time_ms}
    base["ladder"] = ladder(mf, client, fam["triggers"]) if ok else {"available": False, "reason": why}
    base["walls"] = walls(mf, ps) if ok else {"available": False, "reason": why}
    base["thinning"] = (thinning(mf) | {"book_time_ms": mf.block_time_ms, "freshness": fam["book"]["state"]}) if ok else {
        "available": False, "reason": why, "note": THIN_NOTE}
    base["history"] = history(ps, client, market) if mf.book is not None else {"available": False, "reason": why}
    tst = fam["triggers"]
    if mf.triggers is not None and tst["state"] in ("LIVE", "REPLAY"):
        base["stops"] = stop_bands(mf, client)
    else:
        base["stops"] = {"available": False, "reason": f"triggers {tst['state']}: {tst.get('reason')}"}
    rule = mf.flow.bursts
    base["bursts"] = ({"available": True, **_jsonable(rule.status())} if rule is not None else
                      {"available": False, "reason": "node-mode bursts aren't attached to this source"})
    fst = fam["flow"]
    if fst["state"] in ("LIVE", "REPLAY"):
        base["tape"] = {"rows": tape(mf), "coverage": mf.flow.coverage()}
    else:
        base["tape"] = {"available": False, "reason": f"node fills {fst['state']}: {fst.get('reason')}",
                        "coverage": mf.flow.coverage()}
    base["liquidation_events"] = [ev | {"provenance": base["provenance"], "freshness": fam["flow"]}
                                  for ev in ps.liq_events]
    base["recent_events"] = list(mf.events)[:10]
    base["counters"] = dict(mf.counters)
    return base


# --- integration with the public panels (one stream per metric) ---------------------------------

def _flow_ok(client, market, now_wall_ms=None, now_mono_ns=None):
    st = client.family_status(market, "flow", now_wall_ms, now_mono_ns)
    return st["state"] in ("LIVE", "REPLAY"), st


def flow_meta(client, market: str, now_wall_ms=None, now_mono_ns=None) -> dict:
    """Provenance and the flow family's freshness (judged at the snapshot's build time when given), for any result
    built from node fills."""
    return {"provenance": provenance(client, market), "freshness": _flow_ok(client, market, now_wall_ms, now_mono_ns)[1]}


def deck_buckets(client, market: str, start_ms: int, end_ms: int, now_wall_ms=None, now_mono_ns=None) -> dict:
    """Flow Deck activity buckets over (start_ms, end_ms], from node fills only."""
    mf = client.markets.get(market) if client else None
    if mf is None:
        return {"available": False, "reason": "no node feed for this market"} | (
            flow_meta(client, market, now_wall_ms, now_mono_ns) if client else {})
    ok, st = _flow_ok(client, market, now_wall_ms, now_mono_ns)
    meta = {"provenance": provenance(client, market), "freshness": st}
    if not ok:
        return meta | {"available": False, "reason": f"node fills {st['state']}: {st.get('reason')}"}
    execs, cov = mf.flow.window(start_ms, end_ms)
    if not cov["covers"]:
        return meta | {"available": False, "reason": cov["reason"], "coverage": cov}
    a = activity_buckets(execs)
    unknown_side = a["uncertain_usd"] + a["no_single_taker_usd"]
    if unknown_side > 0:
        # An execution whose side, size or effects aren't known exactly (incomplete, conflicting, or without a
        # single taker) could belong to either side: no split of this window is exact, so none is shown.
        return meta | {"available": False, "coverage": cov, "unclassified_usd": num(unknown_side),
                       "reason": "unclassified notional in this window: " + a["buying"]["reason"].split(": ", 1)[-1]}
    return meta | {"available": True, "stream": "node fills", "window": {"start_ms": start_ms, "end_ms": end_ms},
                   "coverage": cov, **_jsonable_activity(a)}


def wallet_buckets(client, market: str, users, start_ms: int, end_ms: int, now_wall_ms=None, now_mono_ns=None) -> dict:
    """Each listed wallet's own split over (start_ms, end_ms], from node fills only."""
    mf = client.markets.get(market) if client else None
    if mf is None:
        return {"available": False, "reason": "no node feed for this market"} | (
            flow_meta(client, market, now_wall_ms, now_mono_ns) if client else {})
    ok, st = _flow_ok(client, market, now_wall_ms, now_mono_ns)
    meta = {"provenance": provenance(client, market), "freshness": st}
    if not ok:
        return meta | {"available": False, "reason": f"node fills {st['state']}: {st.get('reason')}"}
    execs, cov = mf.flow.window(start_ms, end_ms)
    if not cov["covers"]:
        return meta | {"available": False, "reason": cov["reason"]}
    return meta | {"available": True, "stream": "node fills", "window": {"start_exclusive_ms": start_ms, "end_ms": end_ms},
                   "coverage": cov, "wallets": _jsonable(wallet_splits(execs, set(users)))}


def _jsonable_activity(a: dict) -> dict:
    return _jsonable(a)


def _jsonable(x):
    if isinstance(x, Decimal):
        return round(float(x), 6)
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, set):
        return sorted(_jsonable(v) for v in x)
    return x
