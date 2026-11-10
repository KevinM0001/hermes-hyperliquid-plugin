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
from .flow_map import FlowMapState, build as build_flow_map
from .triggers import CENSUS_EXCLUSIONS, category, census
from .proto import dec, fmt, num
from ..util import iso_ms, median

# The family states that withhold what is built on the family (Kevin, Nov 10): INVALID and UNKNOWN, besides having
# no data at all. BEHIND and STALE label: their panels are served with the state, the reason and the delay. Until
# Nov 10 the hl-node ran 73 s behind the chain for an hour and every node panel went dark; the stop clusters, resting
# depth and taker flow are worth reading late, said late.
WITHHOLD = ("INVALID", "UNKNOWN")
LATE = ("BEHIND", "STALE")


def usable(st: dict | None) -> bool:
    """Whether what is built on a family is served: any state but INVALID or UNKNOWN (a missing status never is)."""
    return isinstance(st, dict) and st.get("state") not in WITHHOLD


def as_of_note(label: str, st: dict | None) -> str | None:
    """The caveat of a block built on a family that is BEHIND or STALE, so it never reads as the present:
    "as of the node's book at 2026-11-10T15:35:12.345Z, 73.4 s behind"; None while the family is current (LIVE or
    REPLAY). The time is the family's last watermark's block time, the delay the family's delay_ms (at serving time,
    advanced by the served age); a family whose delay isn't known says its state and reason instead."""
    if not isinstance(st, dict) or st.get("state") not in LATE:
        return None
    t, d = st.get("watermark_block_time_ms"), st.get("delay_ms")
    when = f" at {iso_ms(t)}" if isinstance(t, int) and not isinstance(t, bool) else ""
    behind = (f", {d / 1000:.1f} s behind" if isinstance(d, (int, float)) and not isinstance(d, bool)
              else f", {st['state']}: {st.get('reason')}")
    gone = "; the feed is disconnected" if str(st.get("reason") or "").startswith("disconnected:") else ""
    return f"as of the node's {label}{when}{behind}{gone}"


def late_fields(label: str, st: dict) -> dict:
    """The freshness fields a served block carries beside its numbers: the family's state and the 'as of' note."""
    return {"freshness": st.get("state"), "as_of_note": as_of_note(label, st)}


def shift_fields(win: dict, prefix: str, of: str) -> dict:
    """What a result built over a shifted node-fill window says about it (Kevin, Nov 10): an 'as of' note with the
    block time the fills reach and the shift, and a label naming the window it covers; both None when the window
    served is the one asked for (NodeFlow.covered_window), so a current window reads as the present."""
    n = win.get("shifted_by_ms") or 0
    if not n:
        return {"as_of_note": None, "window_label": None}
    length = win["end_ms"] - win["start_ms"]
    span = f"{round(length / 60_000)} min" if length >= 120_000 else f"{round(length / 1000)} s"
    clock = lambda ms: iso_ms(ms)[11:19]
    return {"as_of_note": f"as of the node's fills at {iso_ms(win['end_ms'])}, {n / 1000:.1f} s behind {of}",
            "window_label": f"{prefix}the node's last {span}, {clock(win['start_ms'])} to {clock(win['end_ms'])} UTC, "
                            f"{n / 1000:.0f} s behind {of}"}


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
VENUE_CHECK = ("checked against the venue only at restart: the engine compares its waiting-stop set with the venue's only when it restarts (its rebase check); during a run nothing compares them, so a stop the venue has already ended can stay shown until the next restart")
STOPS_ROWS_RANGE_BPS = 200          # band rows shown within 2% of the mid; everything beyond is counted below/above
BOOK_PANEL_FAMILIES = ("book",)
MAP_PLAUSIBLE_RATIO = Decimal(10)   # stop map: a trigger price outside [ref/10, ref x 10] is implausible
MAP_CLUSTERS_PER_SIDE = 2
MAP_SIDES = {"fall": {"side": "sell", "label": "Sell-side triggers", "book_side": "bids_below", "book": "B",
                      "walked": "bids at and below", "sign": "-",
                      "members": "sell stops and buy (short) take-profits: they fire when the price falls to the band"},
             "rise": {"side": "buy", "label": "Buy-side triggers", "book_side": "asks_above", "book": "A",
                      "walked": "asks at and above", "sign": "+",
                      "members": "buy stops and sell (long) take-profits: they fire when the price rises to the band"}}
MAP_NOTE = ("a census of the engine's waiting-trigger set and resting book at this block time (the node's live memory), "
            "not a scan of past orders: it describes that moment only, and the orders can be added or pulled at any time")
# The stop map's size outlier (Kevin, Nov 9): an absolute test against the depth the order's band would walk, in
# place of a multiple of the market's median stated size (on BTC that median was 0.00186, a cap near $14K, and the
# multiple excluded 1,005 orders / $182M, more than every cluster together). It is judged last, inside the bands.
MAP_SIZE_OUTLIER_RULE = ("an order whose stated size, at its trigger price, is worth more than everything resting at and "
                         f"beyond its band out to {float(THIN_STEP * THIN_BANDS * 100):g}% on the side it fires into (the "
                         "bids below for a fall trigger, the asks above for a rise trigger: the band's walk "
                         "depth_at_and_beyond_usd), so it could not be walked through the book; orders with no stated "
                         "size (whole-position) never qualify")


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


def flow_map_state_of(mf) -> FlowMapState:
    """The Order Flow Map's accumulator of this market partition. Unlike PanelState it survives a gap (the hole is
    stated as uncovered time, nothing is filled in); a new producer or replay session replaces the partition and
    drops it with everything else."""
    st = getattr(mf, "flow_map", None)
    if st is None:
        st = mf.flow_map = FlowMapState()
    return st


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
        flow_map_state_of(mf).observe(mf.flow, t)
        # The liquidation window moves with every block, executions or not: a quiet market's window empties and the
        # rule re-arms at its watermarks (a watermark adds no fills, so it never fires one).
        _liquidation_bursts(mf, ps, opts, t)
        if t is None or not mf.fam["book"].valid or mf.book is None or mf.crossed is not None:
            return
        if ps.last_sample_ms is None or t - ps.last_sample_ms >= HISTORY_INTERVAL_MS:
            _sample_history(mf, ps, t, client)
            _track_walls(mf, ps, t)
    elif ev["type"] == "execs":
        fm = flow_map_state_of(mf)
        fm.observe(mf.flow, ev.get("block_time_ms"))
        fm.add(mf.flow, ev.get("execs") or [], ev.get("block_time_ms"))
        _liquidation_bursts(mf, ps, opts, ev.get("block_time_ms"))


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
        med = median(o.sz * o.px for o in cands)       # the mean of the two middle notionals when their count is even
        big = sorted((o for o in cands if o.sz * o.px >= med * WALL_MIN_MULTIPLE),
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


def _liquidation_bursts(mf, ps: PanelState, opts: dict, block_time_ms=None) -> None:
    """Evaluated at each node executions message and each watermark, at its own block time t, over (t - 60 s, t]
    (Codex h1 review 4, B1: never at an earlier watermark's time, which let a window include fills after its own end).
    It fires once when the window reaches a threshold, and re-arms when the window falls below both."""
    if mf.flow.restart_pending or not isinstance(block_time_ms, int) or isinstance(block_time_ms, bool):
        return
    now = block_time_ms
    execs = [e for e in mf.flow.execs.values()
             if now - LIQ_WINDOW_MS < e.time_ms <= now and e.liquidation is not None]
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
    if not usable(st):                 # UNKNOWN (clock skew) withholds; BEHIND and STALE serve the book with their state
        return False, f"book {st['state']}: {st.get('reason')}"
    return True, None


def ladder(mf, client, trig_status: dict | None = None, levels: int = LADDER_LEVELS) -> dict:
    book: NodeBook = mf.book
    # The stops column comes from the trigger family: while it isn't usable (invalid, stale, behind or of unknown
    # age), every row says so with the reason, never 0 (which would read as no waiting interest).
    tst = trig_status or {"state": "INVALID", "reason": "trigger state not given"}
    stops_ok = mf.triggers is not None and usable(tst)     # a late trigger family fills the column, labelled (Kevin, Nov 10)
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
                                        {"orders": stops_n, **stated_size(stops_sz, stops_n - stops_unk, stops_unk),
                                         "unknown_size_orders": stops_unk})})
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


def stated_size(sz, sized: int, unknown: int) -> dict:
    """A trigger group's stated size: whole-position TP/SL orders have none (the venue gives 0), so the sum of the
    sized orders is a lower bound when any are present, and unknown, never 0, when every order is one."""
    if unknown and not sized:
        return {"sz": None, "sz_reason": f"unknown: all {unknown} orders are whole-position TP/SL with no stated size"}
    out = {"sz": num(sz)}
    if unknown:
        out |= {"sz_lower_bound": True,
                "sz_note": f"a lower bound: {unknown} whole-position TP/SL orders here have no stated size"}
    return out


def stop_bands(mf, client) -> dict:
    tr = mf.triggers
    ref = mf.book.mid() if (mf.book is not None and mf.crossed is None) else None
    width = _band_width(client, mf.market)
    rng = STOPS_ROWS_RANGE_BPS
    b = tr.bands(ref, width, rng)

    def row(r):
        lg = r.get("largest")
        return {"lo": fmt(r.get("lo")), "hi": fmt(r.get("hi")), "kind": r.get("kind"), "side": r.get("side"),
                "orders": r["orders"], "sized_orders": r["sized_orders"],
                **stated_size(r["sz"], r["sized_orders"], r["position_tpsl_orders"]),
                "unknown_size_orders": r["position_tpsl_orders"],
                "largest_order": None if lg is None else {"sz": num(lg["sz"]), "user": lg["user"],
                                                           "share_pct": round(float(lg["share"]) * 100, 2) if lg["share"] is not None else None},
                "dominated": r["dominated"],
                "size_check": (f"one order is {float(lg['share']) * 100:.0f}% of this band's stated size"
                               if r["dominated"] else None)}

    return {"label": "conditional interest: waiting stop and take-profit orders, which do nothing unless the price "
                     "reaches them",
            "venue_check": VENUE_CHECK,
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


# --- the stop map (Kevin's stop and liquidity map report): trigger bands beside the depth they would walk ----------

def stop_map(mf, depth: dict | None, unavailable: str | None) -> dict:
    """The stops panel's map, clusters and exclusions: every waiting trigger in a 0.5% band of the move that fires it
    (the thinning panel's edges, out to 10%) or under one counted exclusion; per band the resting depth of the same
    book, same moment, that its triggers would walk (the band and every band farther out, the report's "bids at and
    below it"), the depth between the price and the band, and the trigger notional as a share of the walked depth;
    the two largest bands per side as quotable clusters. Stated notional is a lower bound where whole-position orders
    sit (never 0), unknown when every order is one. The exclusions are judged in the census's order (implausible
    price, already true at the price, beyond the range, size outlier last): the size test needs the band's walked
    depth, the same exact sum the band's walk ratio divides by."""
    tr, book = mf.triggers, mf.book
    if unavailable is not None:
        why = f"needs a valid book for the reference price and the resting depth: {unavailable}"
        return {"map": {"available": False, "reason": why}, "clusters": None, "clusters_reason": why,
                "exclusions": {"available": False, "reason": why}, "census": _census(mf, None, why)}
    mid = book.mid()
    walked = {d: _walked_depth(depth[meta["book"]]) for d, meta in MAP_SIDES.items()}
    c = census(tr, mid, THIN_STEP, THIN_BANDS, MAP_PLAUSIBLE_RATIO, {d: w["beyond"] for d, w in walked.items()})
    sides, candidates = {}, []
    for d, meta in MAP_SIDES.items():
        dbands, w = depth[meta["book"]], walked[d]
        rows = []
        for k, b in enumerate(c["bands"][d]):
            row = _map_row(d, meta, k, b, dbands[k], w["between"][k], w["beyond"][k])
            rows.append(row)
            if row["notional_usd"]:
                candidates.append((d, k, row))
        sides[d] = {**{x: meta[x] for x in ("side", "label", "book_side", "members")}, "fires_on": d,
                    "depth_total_usd": usd(w["total"]), "bands": rows}
    clusters = []
    for d in MAP_SIDES:
        ranked = sorted((x for x in candidates if x[0] == d), key=lambda x: (-x[2]["notional_usd"], x[1], -x[2]["orders"]))
        for rank, (_d, k, row) in enumerate(ranked[:MAP_CLUSTERS_PER_SIDE], 1):
            clusters.append({"rank": rank, "text": _cluster_text(row), **row})
    return {"map": {"available": True, "label": "resting trigger orders (stops and take-profits) by 0.5% band out to 10% "
                                              "from the current price, beside the resting limit depth they would walk",
                    "as_of": {"book_time_ms": mf.block_time_ms, "block": mf.block, "note": MAP_NOTE},
                    "ref_px": fmt(mid), "ref_px_rule": "the book mid (the thinning panel's and the stop bands' reference)",
                    "band_pct": float(THIN_STEP * 100), "out_to_pct": float(THIN_STEP * THIN_BANDS * 100),
                    "sides": sides,
                    "rules": {"band": "a trigger lands in band k = floor(distance / 0.5%) of the move that fires it: a sell stop or "
                                      "buy take-profit on a fall (below the price), a buy stop or sell take-profit on a rise (above it); "
                                      "the same edges as the thinning panel's bands",
                              "notional": "sum of stated size x trigger price over the band's sized orders; whole-position TP/SL "
                                          "orders have no stated size (unknown_size_orders), so it is a lower bound (lower_bound "
                                          "true, understated) where they sit and unknown, never 0, when every order is one; the "
                                          "ratio follows the notional",
                              "walk": "depth_at_and_beyond_usd = the thinning panel's depth_usd summed over the band and every "
                                      "band farther from the price on the walked side, to 10% (the report's 'bids at and below "
                                      "it'); depth_between_usd = the bands nearer than it (the thinning panel's "
                                      "cumulative_from_price_usd of the band before); ratio_pct = notional_usd / "
                                      "depth_at_and_beyond_usd x 100",
                              "clusters": f"the {MAP_CLUSTERS_PER_SIDE} bands per side with the largest stated notional (ties: the "
                                          "nearer, then the more orders); fewer when fewer bands have one",
                              "accounts": "distinct owners of the band's orders (the feed's user field)"},
                    "venue_check": VENUE_CHECK},
            "clusters": clusters,
            "exclusions": _exclusions(c),
            "census": _census(mf, c, None)}


def _walked_depth(dbands: list) -> dict:
    """Per band of one book side, exact decimals: the depth between the price and the band (`between`, the bands
    nearer than it) and the depth at and beyond it (`beyond`: the band and every band farther from the price, the
    report's "bids at and below it"). `beyond` is the walk ratio's denominator and the size outlier's bar."""
    usds = [b["usd"] for b in dbands]
    total = sum(usds, ZERO)
    between, beyond, cum = [], [], ZERO
    for u in usds:
        between.append(cum)
        beyond.append(total - cum)
        cum += u
    return {"total": total, "between": between, "beyond": beyond}


def _map_row(d: str, meta: dict, k: int, b: dict, dband: dict, between: Decimal, beyond: Decimal) -> dict:
    near, far = (dband["hi"], dband["lo"]) if d == "fall" else (dband["lo"], dband["hi"])
    q = Decimal("0.0001")
    n, unsized = b["orders"], b["unsized_orders"]
    notional = b["notional"] if b["sized_orders"] else None
    lg = b.pop("_largest", None)
    row = {"side": meta["side"], "fires_on": d, "book_side": meta["book_side"], "k": k,
           "band": {"lo": fmt(dband["lo"].quantize(q)), "hi": fmt(dband["hi"].quantize(q)),
                    "near_px": fmt(near.quantize(q)), "far_px": fmt(far.quantize(q)),
                    "from_pct": float(THIN_STEP * k * 100), "to_pct": float(THIN_STEP * (k + 1) * 100)},
           "distance_pct": float(THIN_STEP * k * 100),
           # the notional and, right beside it, whether it is only a lower bound and how many orders have no size
           "notional_usd": usd(notional), "lower_bound": bool(unsized and notional is not None),
           "unknown_size_orders": unsized, "orders": n, "sized_orders": b["sized_orders"], "accounts": len(b["users"]),
           "sz": num(b["sz"]) if b["sized_orders"] else None}
    if n and notional is None:
        row["notional_reason"] = _unsized_reason(unsized)
    elif unsized:
        row["notional_note"] = (f"a lower bound: {unsized} of {n} orders report no size (whole-position TP/SL), "
                                "so this is understated")
    row["by_category"] = {cat: {"orders": x["orders"], "sized_orders": x["sized_orders"],
                                "unknown_size_orders": x["unsized_orders"], "accounts": len(x["users"]),
                                "notional_usd": usd(x["notional"]) if x["sized_orders"] else None,
                                "lower_bound": bool(x["unsized_orders"] and x["sized_orders"])}
                          for cat, x in sorted(b["by_category"].items())}
    row["largest_order"] = None if lg is None else {
        "notional_usd": usd(lg.sz * lg.trigger_px), "sz": num(lg.sz), "trigger_px": fmt(lg.trigger_px),
        "kind": lg.kind, "user": lg.user,
        "share_pct": round(float(lg.sz * lg.trigger_px / notional) * 100, 2) if notional else None}
    walk = {"ratio_pct": None, "lower_bound": False, "unknown_size_orders": unsized,
            "depth_at_and_beyond_usd": usd(beyond), "depth_between_usd": usd(between),
            "depth_bands_used": {"side": meta["book_side"], "k_from": k, "k_to": THIN_BANDS - 1, "includes_band": True,
                                 "from_pct": float(THIN_STEP * k * 100), "to_pct": float(THIN_STEP * THIN_BANDS * 100),
                                 "note": "thinning bands k_from..k_to summed for depth_at_and_beyond_usd (the band itself "
                                         "and every band farther from the price); the bands before k_from make "
                                         "depth_between_usd"},
            "depth_source": "the node book of the same snapshot (the thinning panel's bands, same book_time_ms)"}
    if n == 0:
        walk["ratio_reason"] = "no triggers in this band"
    elif notional is None:
        walk["ratio_reason"] = row["notional_reason"]
    elif beyond <= 0:
        walk["ratio_reason"] = f"no resting {meta['walked']} it within 10%, so the share can't be given"
    else:
        walk["ratio_pct"] = _sig(float(notional / beyond) * 100)      # 4 significant figures: a tiny share is never a 0
        walk["lower_bound"] = bool(unsized)
    row["walk"] = walk
    return row


def _unsized_reason(n: int) -> str:
    return f"every order is whole-position: size unknown, not 0 ({n} order{'s' if n != 1 else ''}, none with a stated size)"


def _money(x: float) -> str:
    if x >= 1e6:
        return f"${x / 1e6:,.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:,.0f}K"
    return f"${x:,.0f}"


def _sig(x: float, n: int = 4) -> float:
    return float(f"{x:.{n}g}")


def _pct2(x: float) -> str:
    """Two significant figures, as the report quotes shares: 5.2%, 0.92%, 12%, 0.019%."""
    if x >= 10:
        return f"{x:,.0f}%"
    if x >= 1:
        return f"{x:.1f}%"
    if x >= 0.1:
        return f"{x:.2f}%"
    return f"{x:.2g}%"


def _cluster_text(row: dict) -> str:
    meta = MAP_SIDES[row["fires_on"]]
    band, walk, sign = row["band"], row["walk"], meta["sign"]
    n, unsized, acc = row["orders"], row["unknown_size_orders"], row["accounts"]
    where = (f"{meta['label']} at {band['near_px']} to {band['far_px']} "
             f"({sign}{band['from_pct']:.1f}% to {sign}{band['to_pct']:.1f}%)")
    who = f"{n} order{'s' if n != 1 else ''} from {acc} account{'s' if acc != 1 else ''}"
    depth = _money(walk["depth_at_and_beyond_usd"]) if walk["depth_at_and_beyond_usd"] else None
    if row["notional_usd"] is None:
        head = f"{where}: {who}, every one whole-position: size unknown, not 0"
        return "; ".join([head] + ([f"the {meta['walked']} it total {depth}"] if depth else [walk["ratio_reason"]]))
    size = ("at least " if row["lower_bound"] else "") + _money(row["notional_usd"])
    head = f"{where}: {size} across {who}"
    if walk["ratio_pct"] is not None:
        tail = (f"the {meta['walked']} it total {depth}, so this cluster is {'at least ' if walk['lower_bound'] else ''}"
                f"{_pct2(walk['ratio_pct'])} of the liquidity it would walk")
    elif depth:
        tail = f"the {meta['walked']} it total {depth}; {walk['ratio_reason']}"
    else:
        tail = walk["ratio_reason"]
    parts = [head, tail]
    if unsized:
        parts.append(f"{unsized} of {n} orders report{'s' if unsized == 1 else ''} no size, so this is understated")
    return "; ".join(parts)


def _exclusions(c: dict) -> dict:
    eng = c["engine_excluded"]
    rules = {"implausible_price": f"a trigger price outside [reference / {MAP_PLAUSIBLE_RATIO}, reference x {MAP_PLAUSIBLE_RATIO}] "
                                  "(a 90% fall or a 900% rise) or not positive: a data artifact, not a level",
             "already_true_at_price": "a fall trigger at or above the reference price, or a rise trigger at or below it: its "
                                      "condition is already true, so it won't fire on the move the map implies (a stale bracket)",
             "beyond_range": f"more than {float(THIN_STEP * THIN_BANDS * 100):g}% from the reference price",
             "size_outlier": MAP_SIZE_OUTLIER_RULE}
    out = {"available": True,
           "note": "each waiting trigger is counted once, in a band or under the first exclusion that applies, in this order "
                   "(the size test last, inside the bands, against the depth the order's band would walk); nothing listed "
                   "here is in the map's bands; trailing stops never reach the feed, so only the engine's count of them is "
                   "known",
           "order": ["trailing_stops (engine)", *CENSUS_EXCLUSIONS],
           "trailing_stops": {"orders": eng.get("trailing"), "notional_usd": None,
                              "reason": "not in the feed: the engine leaves trailing stops out (carve-out C-2: an activation "
                                        "condition, no fixed trigger price) and reports only their count; no price or size"},
           "unclassified_by_engine": {"orders": eng.get("unclassified"), "notional_usd": None,
                                      "reason": "the engine's count of waiting trigger orders it couldn't classify; not in the feed"}}
    for k in CENSUS_EXCLUSIONS:
        b = c["excluded"][k]
        b.pop("_largest", None)
        out[k] = {"orders": b["orders"], "sized_orders": b["sized_orders"], "unknown_size_orders": b["unsized_orders"],
                  "accounts": len(b["users"]), "notional_usd": usd(b["notional"]) if b["sized_orders"] else None,
                  "lower_bound": bool(b["unsized_orders"] and b["sized_orders"]),
                  "by_category": {cat: x["orders"] for cat, x in sorted(b["by_category"].items())}, "rule": rules[k]}
    excluded = sum(c["excluded"][k]["orders"] for k in CENSUS_EXCLUSIONS)
    out["accounted"] = {"waiting_triggers": c["waiting_total"], "in_bands": c["in_bands"], "excluded_here": excluded,
                        "complete": c["waiting_total"] == c["in_bands"] + excluded}
    return out


CENSUS_BOOK_KEYS = ("excluded", "executable_orders_total", "executable_within_10pct", "executable_to_trigger_ratio")
CENSUS_BANDS_KEYS = ("within_10pct", "outside", "unsized_within_10pct", "bands_view")


def _census(mf, c: dict | None, why: str | None) -> dict:
    """The sanity-check counts of the feed's resting trigger orders for this market (Kevin's reviewer, Nov 9), each
    from the client state that already holds it: the applied waiting set now; the engine's own bands view for the
    count within 10% of the price (its rows summed, by kind and side, its outside counts; never a recount); the
    map's exclusions; the resting executable book, in total and inside the same range; and the snapshots' stated
    totals with their block. A count that isn't available is null with the reason, never 0."""
    tr, book = mf.triggers, mf.book
    total = len(tr.orders)
    out = {"label": "resting trigger orders (stops and take-profits) the feed holds for this market, for sanity checks",
           "triggers_total": total, "triggers_through_seq": tr.through_seq,
           "unsized_total": sum(1 for g in tr.orders.values() if g.position_tpsl),
           "excluded_by_engine": {"trailing_stops": tr.excluded.get("trailing"), "unclassified": tr.excluded.get("unclassified")},
           "note": "a count can be complete while the notional is not: whole-position orders rest with size 0 (unsized), and "
                   "a warm start's snapshot can hold many of them; within_10pct is the engine's bands view by distance alone, "
                   "before the map's exclusions"}
    bv = mf.bands if (mf.fam["bands"].valid and isinstance(mf.bands, dict)) else None
    if bv is None:
        out = census_withheld(out, "no bands view from the engine yet" if mf.bands is None else
                              f"bands view INVALID: {mf.fam['bands'].reason}", CENSUS_BANDS_KEYS)
    else:
        try:
            by_cat: dict = {}
            n = uns = 0
            for _lo, _hi, kind, side, orders, _sz, ptpsl in bv.get("rows") or []:
                x = by_cat.setdefault(category(kind, side), {"orders": 0, "unsized": 0})
                x["orders"] += orders
                x["unsized"] += ptpsl
                n += orders
                uns += ptpsl
            outside = {}
            for where in ("below", "above"):
                o = u = 0
                for _kind, _side, orders, _sz, ptpsl in (bv.get("outside") or {}).get(where) or []:
                    o += orders
                    u += ptpsl
                outside[where] = {"orders": o, "unsized": u}
            out |= {"within_10pct": n, "outside": outside["below"]["orders"] + outside["above"]["orders"],
                    "unsized_within_10pct": uns,
                    "bands_view": {"ref_px": bv.get("ref_px"), "range_bps": bv.get("range_bps"), "width": bv.get("width"),
                                   "lo": bv.get("lo"), "hi": bv.get("hi"), "block": bv.get("_block"),
                                   "block_time_ms": bv.get("_block_time_ms"), "seq": bv.get("_seq"),
                                   "by_category": dict(sorted(by_cat.items())), "outside": outside,
                                   "excluded_by_engine": bv.get("excluded"),
                                   "source": "the engine's bands view (protocol 4.5): the waiting triggers within range_bps "
                                             "of its ref_px (the mid at its block), as it counted them, not a recount"}}
        except (TypeError, ValueError):
            out = census_withheld(out, "the bands view couldn't be read", CENSUS_BANDS_KEYS)
    bt, tt = (book.snapshot_total if book is not None else None), tr.snapshot_total
    if bt is None or tt is None:
        out |= {"snapshot_totals": None, "snapshot_totals_reason": "no snapshot totals yet"}
    else:
        out["snapshot_totals"] = {"book": {"total_orders": bt, "block": book.snapshot_block, "block_time_ms": book.snapshot_block_time_ms},
                                  "triggers": {"total_triggers": tt, "block": tr.snapshot_block, "block_time_ms": tr.snapshot_block_time_ms},
                                  "executable_to_trigger_ratio": round(bt / tt, 2) if tt else None,
                                  "note": "as the snapshots stated them, each at its own block; the other counts are the "
                                          "applied state now"}
        if not tt:
            out["snapshot_totals"]["executable_to_trigger_ratio_reason"] = "the trigger snapshot stated no orders"
    if c is None:
        return census_withheld(out, why, CENSUS_BOOK_KEYS)
    ref = dec(bv.get("ref_px")) if bv is not None else None
    src = "the bands view's ref_px, the same range as within_10pct" if ref is not None else "the book mid (no bands view)"
    ref = book.mid() if ref is None else ref
    bps = bv.get("range_bps") if (bv is not None and isinstance(bv.get("range_bps"), int)) else 1000
    lo, hi = ref * (1 - Decimal(bps) / 10_000), ref * (1 + Decimal(bps) / 10_000)
    n_exec = sum(len(oids) for side in ("B", "A") for px, oids in book.levels[side].items() if lo <= px <= hi)
    out |= {"excluded": {k: c["excluded"][k]["orders"] for k in CENSUS_EXCLUSIONS} | {
                "excluded_by_engine": dict(out["excluded_by_engine"])},
            "executable_orders_total": len(book.orders),
            "executable_within_10pct": {"orders": n_exec, "ref_px": fmt(ref), "ref_px_source": src, "range_bps": bps,
                                        "book_time_ms": mf.block_time_ms,
                                        "source": "the client's book state (snapshot and deltas): resting executable orders "
                                                  "priced inside the range"},
            "executable_to_trigger_ratio": round(len(book.orders) / total, 2) if total else None}
    if not total:
        out["executable_to_trigger_ratio_reason"] = "no waiting trigger orders"
    return out


def census_withheld(cen: dict, why: str, keys=CENSUS_BOOK_KEYS) -> dict:
    """A census with the given counts withheld (their source isn't valid at build time, or lapsed by serving time):
    each is null with the reason; the others stay."""
    return dict(cen, **{k: None for k in keys}, **{f"{k}_reason": why for k in keys})


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


def flow_map(mf, prov: dict, flow_status: dict) -> dict:
    """The Order Flow Map (hl_desk/node/flow_map.py): taker flow over eight 15-minute buckets split by risk opened,
    closed or flipped, with its readings, from this partition's node fills; with provenance and the flow family's
    freshness as judged for this view."""
    return build_flow_map(getattr(mf, "flow_map", None)) | {"stream": "node fills", "provenance": prov,
                                                            "freshness": flow_status}


def depth_bands(book: NodeBook, mid: Decimal) -> dict:
    """The thinning panel's bands as exact decimals: per side ("B" below, "A" above) THIN_BANDS bands of THIN_STEP
    out from the mid, each with its resting notional, size, order count and largest order. The stop map sums the
    same bands, so the two panels never disagree on an edge."""
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
        sides[side] = bands
    return sides


def thinning(mf, depth: dict | None = None) -> dict:
    """Step 18: depth per 0.5% band out to 10% from the current price, from the full book."""
    book: NodeBook = mf.book
    mid = book.mid()
    if mid is None:
        return {"available": False, "reason": "no mid: one side of the book is empty"}
    step_usd = mid * THIN_STEP
    depth = depth_bands(book, mid) if depth is None else depth
    sides = {}
    for side in ("B", "A"):
        bands = depth[side]
        rows = []
        cum = ZERO
        for b in bands:
            lg = b["largest"]
            share = (lg.sz / b["sz"]) if (lg is not None and b["sz"] > 0) else None
            cum += b["usd"]
            rows.append({"from_pct": float(THIN_STEP * b["k"] * 100), "to_pct": float(THIN_STEP * (b["k"] + 1) * 100),
                         "lo": fmt(b["lo"].quantize(Decimal("0.0001"))), "hi": fmt(b["hi"].quantize(Decimal("0.0001"))),
                         "depth_usd": usd(b["usd"]), "usd_per_1_price": usd(b["usd"] / step_usd),
                         "cumulative_from_price_usd": usd(cum),
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
                      "usd_per_1_price": "depth_usd divided by the band's width in dollars of price",
                      "cumulative_from_price_usd": "resting notional from the current price out to and including this band, same side"},
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
    # Each block built on a family carries the family's state and, when it is BEHIND or STALE, the "as of" note:
    # served late with its delay stated, never as the present, and never withheld for lateness (Kevin, Nov 10).
    late = late_fields("book", fam["book"])
    base["ladder"] = (ladder(mf, client, fam["triggers"]) | late) if ok else {"available": False, "reason": why}
    base["walls"] = (walls(mf, ps) | late) if ok else {"available": False, "reason": why}
    depth = depth_bands(mf.book, mf.book.mid()) if ok and mf.book.mid() is not None else None
    base["thinning"] = (thinning(mf, depth) | {"book_time_ms": mf.block_time_ms, "freshness": fam["book"]["state"]} | late) if ok else {
        "available": False, "reason": why, "note": THIN_NOTE}
    base["history"] = history(ps, client, market) if mf.book is not None else {"available": False, "reason": why}
    tst = fam["triggers"]
    if mf.triggers is not None and usable(tst):
        base["stops"] = late_fields("waiting stops", tst) | stop_bands(mf, client) | stop_map(mf, depth, None if depth is not None else (
            why or "no mid: one side of the book is empty"))
    else:
        base["stops"] = {"available": False, "reason": f"triggers {tst['state']}: {tst.get('reason')}"}
    rule = mf.flow.bursts
    base["bursts"] = ({"available": True, **_jsonable(rule.status())} if rule is not None else
                      {"available": False, "reason": "node-mode bursts aren't attached to this source"})
    fst = fam["flow"]
    if usable(fst):
        base["tape"] = {"rows": tape(mf), "coverage": mf.flow.coverage()} | late_fields("fills", fst)
        base["flow_map"] = flow_map(mf, base["provenance"], fst) | {"as_of_note": as_of_note("fills", fst)}
    else:
        base["tape"] = {"available": False, "reason": f"node fills {fst['state']}: {fst.get('reason')}",
                        "coverage": mf.flow.coverage()}
        base["flow_map"] = {"available": False, "reason": f"node fills {fst['state']}: {fst.get('reason')}",
                            "stream": "node fills", "provenance": base["provenance"], "freshness": fst}
    base["liquidation_events"] = [ev | {"provenance": base["provenance"], "freshness": fam["flow"]}
                                  for ev in ps.liq_events]
    base["recent_events"] = list(mf.events)[:10]
    base["counters"] = dict(mf.counters)
    return base


# --- integration with the public panels (one stream per metric) ---------------------------------

def _flow_ok(client, market, now_wall_ms=None, now_mono_ns=None):
    st = client.family_status(market, "flow", now_wall_ms, now_mono_ns)
    return usable(st), st


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
    execs, cov, win = mf.flow.covered_window(start_ms, end_ms)
    if not cov["covers"]:
        return meta | {"available": False, "reason": cov["reason"], "coverage": cov}
    a = activity_buckets(execs)
    unknown_side = a["uncertain_usd"] + a["no_single_taker_usd"]
    if unknown_side > 0:
        # An execution whose side, size or effects aren't known exactly (incomplete, conflicting, or without a
        # single taker) could belong to either side: no split of this window is exact, so none is shown.
        return meta | {"available": False, "coverage": cov, "unclassified_usd": num(unknown_side),
                       "reason": "unclassified notional in this window: " + a["buying"]["reason"].split(": ", 1)[-1]}
    return meta | {"available": True, "stream": "node fills", "window": win, "coverage": cov,
                   **shift_fields(win, "", "the card's window"), **_jsonable_activity(a)}


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
    execs, cov, win = mf.flow.covered_window(start_ms, end_ms)
    if not cov["covers"]:
        return meta | {"available": False, "reason": cov["reason"]}
    req = win["requested"]
    window = {"start_exclusive_ms": win["start_ms"], "end_ms": win["end_ms"], "shifted_by_ms": win["shifted_by_ms"],
              "requested": {"start_exclusive_ms": req["start_ms"], "end_ms": req["end_ms"]} if req else None}
    return meta | {"available": True, "stream": "node fills", "window": window, "coverage": cov,
                   **shift_fields(win, "these wallets' node split over ", "the panel's window"),
                   "wallets": _jsonable(wallet_splits(execs, set(users)))}


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
