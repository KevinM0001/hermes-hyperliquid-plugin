"""The Flow Deck (Handoff 1 step 5a): regimes, card numbers, events and burst alerts.

Definitions are ported from the author's terminal Flow Deck (hl_deck.py) into schema/metrics.v1.json
(regimes, deck.*, alerts.*); this module implements those schema entries.
Ticks run on source time, every 15 s, over a 90 s window, and only complete
windows (wholly inside one coverage segment) are classified. No regime,
held time, event or burst spans a gap.
"""

from __future__ import annotations

from collections import deque

from . import schema
from .util import iso_ms, na, rnd, val
from .wallets import ranked_list

C = schema.regime_constants()
WINDOW_MS = 90_000
TICK_MS = 15_000
BURST_WINDOW_MS = 60_000
# Evaluated ticks kept for re-evaluation after a late trade: every tick whose 90 s window can still contain it.
REVISABLE_TICKS = WINDOW_MS // TICK_MS + 2
NODE_REASON = "needs node mode: public trades carry no open/close direction"
IMPACT_UNIT = "bps per $1M: absolute observed trade-price change per $1M of net aggressive flow (descriptive ratio)"
PRESSURE_UNIT = "ratio: aggressive buy notional / aggressive sell notional"
PATH_POINTS = WINDOW_MS // TICK_MS
NO_TRADES_15S = "no trades in this 15 s"
# The node panels' rule for a band (one order over half of it), applied to the card's window (schema deck.dominated_by).
DOMINANT_SHARE_PCT = 50.0
VALIDATION = schema.load()["regimes"]["validation"]["status"]
PATH_NOTE = ("the window as six 15 s points ending at each deck tick: price is the point's last trade price, move_bps the "
             "change from the previous point (the window's first trade for the first point), net_flow_usd and volume_usd its "
             "aggressive flow; the points add up to the window's net flow and volume and chain to its move; a point with no "
             "trades has price and move null with the reason and a real zero flow (plugin addition, schema deck.path)")
DOMINANCE_RULE = ("a piece dominates when it carries more than 50% of the window's net aggressive flow (in the net's "
                  "direction) or of its aggressive volume, the node panels' rule for one order in a band; for each measure the "
                  "largest tick, print and taker address are named with their shares, computed here from the window's "
                  "executions and not to be recomputed; net shares need |net| >= $100K (the IMPACT floor); a share over 100% "
                  "means the other pieces netted the opposite way (plugin addition, schema deck.dominated_by)")


def window_metrics(trades, pressure: float | None = None) -> dict:
    """Card numbers for one window (schema deck.*). trades are ordered by (time, seq).

    PRESSURE is aggressive buy notional over aggressive sell notional, every execution (flips included),
    available when selling is at least $25K, the same in public and node mode (schema deck.pressure).
    The `pressure` argument is ignored; it is kept for callers of the older signature."""
    buy = sum(t.notional for t in trades if t.side == "B")
    sell = sum(t.notional for t in trades if t.side == "A")
    net, vol = buy - sell, buy + sell
    if trades:
        p0, p1 = trades[0].px, trades[-1].px
        bps = (p1 - p0) / p0 * 10000 if p0 else None
    else:
        bps = None
    impact = abs(bps) / (abs(net) / 1e6) if bps is not None and abs(net) >= C["MIN_NET_IMPACT_usd"] else None
    gross: dict[str, float] = {}
    agg_bought: dict[str, float] = {}
    agg_sold: dict[str, float] = {}
    for t in trades:
        tk = t.taker
        if tk is None:
            continue
        gross[tk] = gross.get(tk, 0.0) + t.notional
        if t.side == "B":
            agg_bought[tk] = agg_bought.get(tk, 0.0) + t.notional
        else:
            agg_sold[tk] = agg_sold.get(tk, 0.0) + t.notional
    top = min(gross.items(), key=lambda kv: (-kv[1], kv[0])) if gross else None
    top_ab = agg_bought.get(top[0], 0.0) if top else None
    top_as = agg_sold.get(top[0], 0.0) if top else None
    pressure = buy / sell if sell >= C["MIN_DEN_PRESSURE_usd"] else None
    return {"n": len(trades), "bps": bps, "net": net, "vol": vol, "buy": buy, "sell": sell,
            "impact": impact, "pressure": pressure, "accounts": len(gross),
            "top_wallet": top[0] if top else None,
            "top_share": (top[1] / vol * 100) if top and vol > 0 else None,
            "top_aggressive_bought": top_ab, "top_aggressive_sold": top_as,
            "top_net_aggressive": (top_ab - top_as) if top else None,
            "price": trades[-1].px if trades else None}


def window_path(trades, start_ms: int) -> list[dict]:
    """The window as six 15 s points, one per deck tick (schema deck.path). Point k holds the trades with time in
    (start + (k-1)*15 s, start + k*15 s], the first one also the trade at the window start (the window is start-inclusive),
    so the points partition the window and their nets add up to its net. price is the point's last trade price and move_bps
    the change from the previous priced point (the window's first trade for the first point): the points chain from the
    window's first trade to its last. trades are ordered by (time, seq)."""
    points, ref, i, n = [], (trades[0].px if trades else None), 0, len(trades)
    for k in range(1, PATH_POINTS + 1):
        e = start_ms + k * TICK_MS
        j = n if k == PATH_POINTS else i
        while j < n and trades[j].time <= e:
            j += 1
        chunk, i = trades[i:j], j
        buy = sum(t.notional for t in chunk if t.side == "B")
        sell = sum(t.notional for t in chunk if t.side == "A")
        p = {"tick_ms": e, "n": len(chunk), "net": buy - sell, "vol": buy + sell, "price": None, "move_bps": None}
        if chunk:
            px = chunk[-1].px
            p["price"] = px
            p["move_bps"] = (px - ref) / ref * 10000 if ref else None
            ref = px
        points.append(p)
    return points


def path_block(trades, points: list[dict]) -> dict:
    first = ({"time": iso_ms(trades[0].time), "time_ms": trades[0].time, "price": trades[0].px} if trades
             else {"time": None, "time_ms": None, "price": None, "reason": "no trades in window"})
    pts = []
    for p in points:
        q = {"tick": iso_ms(p["tick_ms"]), "tick_ms": p["tick_ms"], "price": p["price"], "move_bps": rnd(p["move_bps"], 4),
             "net_flow_usd": rnd(p["net"], 2), "volume_usd": rnd(p["vol"], 2), "trades": p["n"]}
        if p["n"] == 0:
            q["reason"] = NO_TRADES_15S
        pts.append(q)
    return {"points": pts, "point_s": TICK_MS // 1000, "first_trade": first, "note": PATH_NOTE}


def _largest(items, amount):
    """The item with the largest amount: on a tie the first in order (addresses are passed sorted, so the lowest wins)."""
    best, best_a = None, None
    for it in items:
        a = amount(it)
        if best is None or a > best_a:
            best, best_a = it, a
    return best, best_a


def dominance(trades, points: list[dict], m: dict) -> dict:
    """schema deck.dominated_by: for the window's net aggressive flow and for its volume, the largest single tick (path
    point), print and taker address, each with its share and whether it dominates (more than half), and a summary."""
    net, vol = m["net"], m["vol"]
    gross: dict[str, float] = {}
    wnet: dict[str, float] = {}
    for t in trades:
        if t.taker is None:
            continue
        gross[t.taker] = gross.get(t.taker, 0.0) + t.notional
        wnet[t.taker] = wnet.get(t.taker, 0.0) + (t.notional if t.side == "B" else -t.notional)
    named: list[str] = []

    def piece(kind, it, amount, basis, label, key, value):
        share = amount / basis * 100
        dom = share > DOMINANT_SHARE_PCT
        if kind == "tick":
            out = {"tick": iso_ms(it["tick_ms"]), "tick_ms": it["tick_ms"]}
            what = f"one tick (the 15 s to {out['tick']})"
        elif kind == "print":
            out = {"time": iso_ms(it.time), "time_ms": it.time, "side": it.side, "px": it.px, "sz": it.sz,
                   "notional_usd": rnd(it.notional, 2), "taker": it.taker, "tid": it.tid}
            what = f"one print ({out['time']}, {'buy' if it.side == 'B' else 'sell'} ${it.notional:,.0f})"
        else:
            out = {"address": it[0]}
            what = f"one address ({it[0]})"
        out |= {key: rnd(value, 2), "share_pct": rnd(share, 4), "dominates": dom}
        if kind == "wallet":
            out["note"] = "an address, not an identified owner"
        if dom:
            named.append(f"{what} carries {share:.0f}% of the window's {label}")
        return out

    if abs(net) >= C["MIN_NET_IMPACT_usd"]:
        sign, side, basis = (1, "B", net) if net > 0 else (-1, "A", -net)
        tk, a = _largest(points, lambda p: sign * p["net"])
        pr, b = _largest([t for t in trades if t.side == side], lambda t: t.notional)
        wl, c = _largest(sorted(wnet.items()), lambda kv: sign * kv[1])
        net_part = {"basis_usd": rnd(net, 2),
                    "tick": piece("tick", tk, a, basis, "net aggressive flow", "net_flow_usd", tk["net"]),
                    "print": piece("print", pr, b, basis, "net aggressive flow", "notional_usd", pr.notional),
                    "wallet": (piece("wallet", wl, c, basis, "net aggressive flow", "net_aggressive_usd", wl[1])
                               if wl else None)}
        net_note = ""
    else:
        net_part = {"basis_usd": None, "reason": "net aggressive flow below the $100K floor: shares of it aren't computed",
                    "tick": None, "print": None, "wallet": None}
        net_note = " (net aggressive flow below the $100K floor: its shares aren't computed)"
    if vol > 0:
        tk, a = _largest(points, lambda p: p["vol"])
        pr, b = _largest(trades, lambda t: t.notional)
        wl, c = _largest(sorted(gross.items()), lambda kv: kv[1])
        vol_part = {"basis_usd": rnd(vol, 2),
                    "tick": piece("tick", tk, a, vol, "volume", "volume_usd", tk["vol"]),
                    "print": piece("print", pr, b, vol, "volume", "notional_usd", pr.notional),
                    "wallet": piece("wallet", wl, c, vol, "volume", "volume_usd", wl[1]) if wl else None}
    else:
        vol_part = {"basis_usd": 0.0, "reason": "no aggressive volume in window", "tick": None, "print": None, "wallet": None}
    summary = ("; ".join(named) if named else
               "no single tick, print or address carries more than 50% of the window's net aggressive flow or of its volume")
    return {"any": bool(named), "threshold_pct": DOMINANT_SHARE_PCT, "rule": DOMINANCE_RULE,
            "net_flow": net_part, "volume": vol_part, "summary": summary + net_note}


def classify(m: dict | None, node_mode: bool = False) -> tuple[str, str | None]:
    """First match wins (hl_deck classify 351-379, with PRESSURE as defined in schema deck.pressure).
    Returns (state, tag). node_mode no longer changes the result."""
    if m is None or m["n"] == 0:
        return "QUIET", None
    bps, net, vol, impact, pressure = m["bps"], m["net"], m["vol"], m["impact"], m["pressure"]
    if vol < C["MIN_VOL_CLASSIFY_usd"]:
        if abs(bps) < C["THIN_MAX_BPS"]:
            return "THIN", None
        return ("VACUUM-UP" if bps > 0 else "VACUUM-DOWN"), "thin"
    if abs(bps) >= C["DIV_MIN_BPS"] and abs(net) >= C["DIV_MIN_NET_usd"] and (bps > 0) != (net > 0):
        return ("PULLED-OFFERS" if bps > 0 else "PULLED-BIDS"), None
    if impact is not None:
        if impact >= C["IMPACT_VACUUM"] and abs(bps) >= C["VACUUM_MIN_BPS"]:
            return ("VACUUM-UP" if bps > 0 else "VACUUM-DOWN"), "impact"
        if impact <= C["IMPACT_ABSORB"]:
            return "ABSORBING", None
    if abs(bps) >= C["TREND_MIN_BPS"] and pressure is not None:
        if pressure >= C["PRESS_BUY"] and bps > 0:
            return "TRENDING-UP", None
        if pressure <= C["PRESS_SELL"] and bps < 0:
            return "TRENDING-DOWN", None
    return "BALANCED", None


def meaning_block(state: str | None) -> dict | None:
    if state is None:
        return None
    m = schema.meaning(state)
    if m is None:
        return None
    return {"state": state, "key_number": m["key"], "text": m["text"],
            "wording": "heuristic classification of the window's measurements (schema regimes.meanings)"}


class DeckEngine:
    def __init__(self, market: str, burst_threshold: float, target_pct: float = 50.0, cap: int = 50,
                 node_mode: bool = False) -> None:
        self.market = market
        self.burst_threshold = burst_threshold
        self.target_pct = target_pct
        self.cap = cap
        self.node_mode = node_mode
        self.events: deque[dict] = deque(maxlen=50)
        self.reset_segment(None)

    # --- segments --------------------------------------------------------
    def reset_segment(self, segment_start_ms: int | None) -> None:
        self.segment_start_ms = segment_start_ms
        self.confirmed: str | None = None
        self.confirmed_tag: str | None = None
        self.since_ms: int | None = None
        self.pending: tuple[str | None, int] = (None, 0)
        self.next_tick_ms = None if segment_start_ms is None else (segment_start_ms // TICK_MS + 1) * TICK_MS
        self.last_tick: dict | None = None
        self.last_complete_tick_ms: int | None = None
        self.burst_armed = {"buying": True, "selling": True}
        # One entry per evaluated tick: its latch state before the tick and the events it added (see _revise).
        self.history: deque[dict] = deque(maxlen=REVISABLE_TICKS)
        self._tick_events: list | None = None

    def on_gap(self, last_watermark_ms: int | None, reason: str) -> None:
        if self.confirmed is not None and self.since_ms is not None and self.last_complete_tick_ms is not None:
            self.events.appendleft({
                "type": "regime_ended_by_gap", "market": self.market, "at_ms": self.last_complete_tick_ms,
                "at": iso_ms(self.last_complete_tick_ms), "from": self.confirmed, "to": None,
                "held_s": round((self.last_complete_tick_ms - self.since_ms) / 1000, 3),
                "reason": reason,
                "text": f"{self.confirmed} -> (gap: {reason}); held {(self.last_complete_tick_ms - self.since_ms) / 1000:.0f}s up to the last complete tick"})
        self.reset_segment(None)

    # --- ticks -----------------------------------------------------------
    def advance(self, store, pressure_hook=None, new_trades=()) -> None:
        """Evaluate every tick the trades watermark has passed. A late trade (one whose time falls in a tick that
        was already evaluated) first re-evaluates the affected ticks, so the card, the latch and the events always
        agree with the execution set (schema deck.late_trades)."""
        if self.segment_start_ms is None:
            self.reset_segment(store.segment_start_ms)
        wm = store.watermark_ms
        if self.next_tick_ms is None or wm is None:
            return
        last_evaluated = self.next_tick_ms - TICK_MS
        late = [t for t in new_trades if t.time <= last_evaluated]
        if late:
            self._revise(store, late, pressure_hook)
        while self.next_tick_ms < wm:
            self._tick(store, self.next_tick_ms, pressure_hook)
            self.next_tick_ms += TICK_MS

    def _revise(self, store, late: list, pressure_hook) -> None:
        """Re-run the ticks whose window contains a late trade, from the earliest one, starting from the latch
        state saved before it. Events those ticks added are taken back and re-derived; a regime change that the
        re-evaluation no longer supports is reported as withdrawn."""
        earliest = min(t.time for t in late)
        idx = next((i for i, h in enumerate(self.history) if h["tick_ms"] >= earliest), None)
        if idx is None:
            return          # the late trades are older than every retained window: no evaluated card contains them
        # Ticks older than the retained ones (more than about 2 minutes back) keep their latch steps as evaluated;
        # no displayed card's window contains trades that late.
        entries = list(self.history)[idx:]
        first = entries[0]
        old_events = [ev for h in entries for ev in h["events"]]
        for ev in old_events:
            try:
                self.events.remove(ev)
            except ValueError:
                pass
        (self.confirmed, self.confirmed_tag, self.since_ms, self.pending,
         self.last_tick, self.last_complete_tick_ms) = first["before"]
        for _ in entries:
            self.history.pop()
        for h in entries:
            self._tick(store, h["tick_ms"], pressure_hook)
        new_events = [ev for h in list(self.history)[-len(entries):] for ev in h["events"]]
        kept = {(e["type"], e["at_ms"], e.get("from"), e.get("to")) for e in new_events}
        at = max(t.time for t in late)
        for ev in old_events:
            if ev.get("type") == "regime_change" and (ev["type"], ev["at_ms"], ev.get("from"), ev.get("to")) not in kept:
                self.events.appendleft({
                    "type": "regime_change_withdrawn", "market": self.market, "at_ms": ev["at_ms"], "at": ev["at"],
                    "from": ev.get("from"), "to": ev.get("to"),
                    "text": (f"{ev.get('from')} -> {ev.get('to')} at {ev['at']} withdrawn: "
                             f"{len(late)} late trade(s) changed the ticks it was confirmed on")})
        if self.last_tick is not None:
            self.last_tick["revised"] = {
                "late_trades": len(late), "latest_late_trade_ms": at, "latest_late_trade": iso_ms(at),
                "ticks_reevaluated": [iso_ms(h["tick_ms"]) for h in entries],
                "note": ("trades that arrived after these ticks were first evaluated are included; the card, latch "
                         "and events were re-derived from the execution set")}

    def _tick(self, store, t: int, pressure_hook) -> None:
        entry = {"tick_ms": t, "before": (self.confirmed, self.confirmed_tag, self.since_ms, self.pending,
                                          self.last_tick, self.last_complete_tick_ms), "events": []}
        self._tick_events = entry["events"]
        try:
            self._evaluate(store, t, pressure_hook)
        finally:
            self._tick_events = None
        self.history.append(entry)

    def _emit(self, ev: dict) -> None:
        """Add an event produced by a tick's latch, remembering it on that tick so a revision can take it back."""
        self.events.appendleft(ev)
        if self._tick_events is not None:
            self._tick_events.append(ev)

    def _evaluate(self, store, t: int, pressure_hook) -> None:
        start = t - WINDOW_MS
        complete = start > self.segment_start_ms
        if not complete:
            self.last_tick = {"tick_ms": t, "complete": False,
                              "reason": f"window incomplete: needs 90 s after the segment start at {iso_ms(self.segment_start_ms)}"}
            return
        trades = store.window(start, t)
        pressure = pressure_hook(self.market, start, t) if pressure_hook else None
        m = window_metrics(trades, pressure)
        live, tag = classify(m, self.node_mode)
        if live != self.confirmed:
            ps, pc = self.pending
            pc = pc + 1 if ps == live else 1
            self.pending = (live, pc)
            if pc >= C["CONFIRM_TICKS"]:
                if self.confirmed is not None:
                    held = (t - self.since_ms) / 1000
                    self._emit({
                        "type": "regime_change", "market": self.market, "at_ms": t, "at": iso_ms(t),
                        "from": self.confirmed, "to": live, "held_s": round(held, 3),
                        "text": f"{self.confirmed} -> {live} (held {held:.0f}s)"})
                self.confirmed, self.confirmed_tag, self.since_ms = live, tag, t
                self.pending = (None, 0)
        else:
            self.pending = (None, 0)
            self.confirmed_tag = tag
        # SPARSE (hl_deck 450-453) is a flag on the live classification: it never enters the latch or the events.
        # hl_deck applies it only when the window has trades (an empty window is QUIET) and isn't a vacuum.
        sparse = 0 < m["n"] < C["SPARSE_MAX_TRADES"] and live not in ("VACUUM-UP", "VACUUM-DOWN")
        points = window_path(trades, start)
        self.last_tick = {"tick_ms": t, "complete": True, "metrics": m, "live": live, "live_tag": tag, "sparse": sparse,
                          "path": path_block(trades, points), "dominated_by": dominance(trades, points, m)}
        self.last_complete_tick_ms = t

    # --- bursts ----------------------------------------------------------
    def check_burst(self, store, new_trades) -> None:
        """Evaluate burst alerts at the new trades watermark (schema alerts.burst)."""
        wm = store.watermark_ms
        seg = self.segment_start_ms if self.segment_start_ms is not None else store.segment_start_ms
        if wm is None or seg is None or not new_trades:
            return
        if wm - BURST_WINDOW_MS < seg:
            return  # window would reach before the segment: incomplete, never spans a gap
        window = store.window(wm - BURST_WINDOW_MS, wm, start_inclusive=False)
        added = {t.identity for t in new_trades}
        for side_name, code in (("buying", "B"), ("selling", "A")):
            # The rolling sum just before this message's executions were added, at the new watermark: the
            # smallest the sum has been since the last evaluation (it only falls between messages).
            before = sum(t.notional for t in window if t.side == code and t.identity not in added)
            total = sum(t.notional for t in window if t.side == code)
            if before < self.burst_threshold:
                self.burst_armed[side_name] = True
            if total >= self.burst_threshold and self.burst_armed[side_name]:
                self.burst_armed[side_name] = False
                self.events.appendleft(self._burst_event(side_name, code, window, total, wm))
            elif total < self.burst_threshold:
                self.burst_armed[side_name] = True

    def _burst_event(self, side_name: str, code: str, window, total: float, wm: int) -> dict:
        execs = [t for t in window if t.side == code]
        takers: dict[str, float] = {}
        for t in execs:
            takers[t.taker] = takers.get(t.taker, 0.0) + t.notional
        wallets = ranked_list(takers, total, self.target_pct, self.cap, f"the burst's aggressive {side_name}")
        return {"type": "burst", "market": self.market, "side": side_name, "at_ms": wm, "at": iso_ms(wm),
                "window_start_ms_exclusive": wm - BURST_WINDOW_MS, "window_end_ms": wm,
                "aggressive_notional_usd": rnd(total, 2), "threshold_usd": self.burst_threshold,
                "executions": len(execs), "wallets": wallets,
                "text": f"Aggressive {side_name} of ${total:,.0f} within 60 s (threshold ${self.burst_threshold:,.0f})"}

    # --- card --------------------------------------------------------------
    def card(self, unavailable_reason: str | None) -> dict:
        lt = self.last_tick
        base = {"market": self.market, "window_s": WINDOW_MS // 1000, "tick_s": TICK_MS // 1000}
        if unavailable_reason or lt is None or not lt.get("complete"):
            reason = unavailable_reason or (lt or {}).get("reason") or "warming up: no tick evaluated yet"
            base.update({
                "regime": na(reason), "confirmed_regime": None, "live_classification": None, "pending": None,
                "last_complete_tick_ms": self.last_complete_tick_ms,
                "numbers": {k: na(reason) for k in ("move_bps", "net_flow_usd", "volume_usd", "impact", "pressure",
                                                    "trades", "accounts")}
                | {"top_wallet": na(reason)},
                "activity_buckets": self._buckets()})
            if self.confirmed is not None:
                base["last_confirmed"] = {"regime": self.confirmed, "since_ms": self.since_ms,
                                          "since": iso_ms(self.since_ms),
                                          "as_of_tick_ms": self.last_complete_tick_ms,
                                          "note": "historical: the last complete tick's confirmed regime"}
            return base
        m = lt["metrics"]
        t = lt["tick_ms"]
        held = None if self.since_ms is None else round((t - self.since_ms) / 1000, 3)
        impact = (val(rnd(m["impact"], 4), IMPACT_UNIT) if m["impact"] is not None
                  else na("net aggressive flow below the $100K floor (hl_deck shows n/a)", IMPACT_UNIT))
        if m["n"] == 0:
            move = na("no trades in window", "bps")
        else:
            move = val(rnd(m["bps"], 4), "bps")
        top = (na("no aggressive volume in window") if m["top_wallet"] is None else
               {"value": m["top_wallet"], "share_pct": rnd(m["top_share"], 4),
                "share_label": "share of the window's aggressive (taker) notional, both sides",
                "net_aggressive_usd": rnd(m["top_net_aggressive"], 2),
                "aggressive_bought_usd": rnd(m["top_aggressive_bought"], 2),
                "aggressive_sold_usd": rnd(m["top_aggressive_sold"], 2),
                "net_label": "net aggressive notional: its aggressive bought minus its aggressive sold in this window "
                             "(plugin addition, not in hl_deck), so a wallet that both bought and sold doesn't read as one-sided",
                "note": "an address, not an identified owner"})
        base.update({
            "tick_ms": t, "tick": iso_ms(t),
            "window_start_ms": t - WINDOW_MS, "window_end_ms": t, "complete": True,
            # The confirmed (latched) regime, the live classification of these numbers, and any pending
            # change are reported separately, so a confirmed label never reads as describing other numbers.
            "regime": (val(self.confirmed) if self.confirmed is not None else
                       na(f"no regime confirmed yet: a classification must repeat on {C['CONFIRM_TICKS']} consecutive complete ticks")),
            "confirmed_regime": ({"state": self.confirmed, "tag": self.confirmed_tag, "since_ms": self.since_ms,
                                  "since": iso_ms(self.since_ms), "held_s": held, "as_of_tick": iso_ms(t),
                                  "meaning": meaning_block(self.confirmed), "validation": VALIDATION}
                                 if self.confirmed is not None else None),
            "live_classification": {"state": lt["live"], "tag": lt["live_tag"], "tick": iso_ms(t),
                                    "describes": "this tick's window numbers below",
                                    "sparse": lt["sparse"],
                                    "sparse_note": meaning_block("SPARSE")["text"] if lt["sparse"] else None,
                                    "meaning": meaning_block(lt["live"]), "validation": VALIDATION},
            "pending": ({"state": self.pending[0], "ticks": self.pending[1], "of": C["CONFIRM_TICKS"],
                         "validation": VALIDATION} if self.pending[0] else None),
            "numbers": {
                "price": val(m["price"], "USD") if m["price"] is not None else na("no trades in window", "USD"),
                "move_bps": move,
                "net_flow_usd": val(rnd(m["net"], 2), "USD"),
                "volume_usd": val(rnd(m["vol"], 2), "USD"),
                "impact": impact,
                "pressure": (val(rnd(m["pressure"], 4), PRESSURE_UNIT) if m["pressure"] is not None
                             else na("aggressive selling below the $25K floor", PRESSURE_UNIT)),
                "top_wallet": top,
                "accounts": val(m["accounts"], "taker addresses"),
                "trades": val(m["n"], "trades"),
            },
            # The window's path and whether one piece dominates it (Commentary TOOL_PROPOSALS 1 and 4): complete-card
            # fields, absent from a withheld card like tick and window_start_ms.
            "path": lt["path"],
            "dominated_by": lt["dominated_by"],
            "activity_buckets": self._buckets(),
        })
        if lt.get("revised"):
            base["revised"] = lt["revised"]
        return base

    def _buckets(self) -> dict:
        return {k: na(NODE_REASON, "USD") for k in ("new_long", "cover", "new_short", "exit")} | {
            "hook": "node_mode.activity_buckets(market, window_start_ms, window_end_ms)"}

    def events_list(self, limit: int = 20) -> list[dict]:
        return list(self.events)[:limit]
