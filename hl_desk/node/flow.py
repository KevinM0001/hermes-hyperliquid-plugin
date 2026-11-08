"""Node executions of one market (PROTOCOL.md 4.6) and the metrics built only from them (steps 13-14).

One stream: everything here counts node fills only, never public trades, and never the two added
together. An execution is counted once by its identity (market, tid), whatever order or how many times
its message arrives; a participant is (market, tid, side), so one address on both sides is still two
participants. The observed window restarts at every boundary (a gap, a new producer session, a new
replay session, an `execs` marker with fills_known false, or a change of input stream) after the first
watermark that follows it, counting only blocks after that watermark (PROTOCOL.md 6.7).

Activity buckets (step 14) come from each participant's own position effect as the feed states it:
buy: cover, new_long; sell: exit, new_short; a flip fills both; each converts to notional at the
execution's price. Nothing is inferred from the aggressor side, text or PnL, and a missing effect is
never zero: its side is unavailable, with the unclassified notional shown, and the rest is not scaled.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from decimal import Decimal

from .proto import dec

ZERO = Decimal(0)
RETENTION_MS = 16 * 60 * 1000
LIQ_BURST_WINDOW_MS = 60_000


@dataclass(slots=True)
class Participant:
    user: str | None
    oid: int | None
    side: str | None          # "B" bought, "A" sold
    role: str | None          # "taker" or "maker"
    effect: dict | None       # Decimal sizes: {"cover","new_long"} or {"exit","new_short"}
    effect_unavailable: str | None
    twap_id: object | None
    liquidated: bool


@dataclass(slots=True)
class Execution:
    tid: int
    block: int
    time_ms: int
    px: Decimal
    sz: Decimal
    aggressor: str | None
    complete: bool
    participants: list[Participant]
    liquidation: dict | None
    conflict: bool

    @property
    def notional(self) -> Decimal:
        return self.px * self.sz


def parse_exec(raw: dict, block: int, block_time_ms: int | None) -> Execution | None:
    px, sz = dec(raw.get("px")), dec(raw.get("sz"))
    tid = raw.get("tid")
    t = raw.get("time_ms", block_time_ms)
    if px is None or sz is None or not isinstance(tid, int) or not isinstance(t, int):
        return None
    parts = []
    for p in raw.get("participants") or []:
        eff = p.get("effect")
        effect = None
        if isinstance(eff, dict):
            effect = {k: dec(v) for k, v in eff.items()}
            if any(v is None for v in effect.values()):
                effect = None
        parts.append(Participant(p.get("user"), p.get("oid"), p.get("side"), p.get("role"), effect,
                                 p.get("effect_unavailable") if effect is None else None,
                                 p.get("twap_id"), bool(p.get("liquidated"))))
    return Execution(int(tid), block, t, px, sz, raw.get("aggressor"), bool(raw.get("complete")), parts,
                     raw.get("liquidation") if isinstance(raw.get("liquidation"), dict) else None,
                     bool(raw.get("conflict")))


class NodeFlow:
    """Executions counted once, inside an observed window that never bridges a boundary."""

    def __init__(self, stream: str) -> None:
        self.stream = stream               # the input stream label (source, mode, epoch/session, replay session)
        self.execs: OrderedDict[int, Execution] = OrderedDict()   # tid -> execution, in arrival order
        self.restart_pending = True        # a window starts only after the first watermark
        self.restart_reason = "stream start"
        self.since_block: int | None = None
        self.since_ms: int | None = None
        self.last_block: int | None = None
        self.last_block_time_ms: int | None = None
        self.windows: list[dict] = []      # earlier windows: {since_block, since_ms, until_block, reason}
        self.counters = {"duplicates": 0, "before_window": 0, "malformed": 0, "conflicts": 0}
        self.unreadable: deque[int] = deque(maxlen=1000)    # block times of executions that couldn't be read
        self.liq_events: deque[dict] = deque(maxlen=200)
        self.liq_armed = True
        self.bursts = None                 # node-mode burst rule (bursts.NodeBurstRule), attached by the plugin

    # --- boundaries ------------------------------------------------------------
    def boundary(self, reason: str, burst_gap: bool = True) -> None:
        if not self.restart_pending and self.since_block is not None:
            self.windows.append({"since_block": self.since_block, "since_ms": self.since_ms,
                                 "until_block": self.last_block, "until_ms": self.last_block_time_ms,
                                 "ended_by": reason})
            self.windows = self.windows[-10:]
        self.execs.clear()
        self.unreadable.clear()
        self.restart_pending = True
        self.restart_reason = reason
        self.since_block = self.since_ms = None
        self.liq_armed = True
        if self.bursts is not None and burst_gap:
            self.bursts.boundary(reason)

    def on_watermark(self, block: int, block_time_ms: int | None) -> None:
        if self.restart_pending:
            self.restart_pending = False
            self.since_block, self.since_ms = block, block_time_ms
            if self.bursts is not None and block_time_ms is not None and self.bursts.segment_start_ms is None:
                self.bursts.start(block_time_ms)
        self.last_block, self.last_block_time_ms = block, block_time_ms

    def on_execs(self, body: dict, block: int, block_time_ms: int | None) -> list[Execution]:
        known = body.get("fills_known") is not False
        if self.bursts is not None:
            # Node-mode bursts: one evaluation per block, under the burst calibration's rule. A block whose fills
            # weren't read is an unreadable record for them (its windows incomplete), not a gap.
            self.bursts.on_block(block, block_time_ms, body.get("execs") or [], fills_known=known)
        if not known:
            # For the flow's own window, unknown executions are a boundary (PROTOCOL.md 6.7).
            self.boundary(f"executions unknown at block {block} (fills record not read)", burst_gap=False)
            return []
        if self.restart_pending or (self.since_block is not None and block <= self.since_block):
            self.counters["before_window"] += len(body.get("execs") or [])
            return []
        new = []
        for raw in body.get("execs") or []:
            e = parse_exec(raw, block, block_time_ms)
            if e is None:
                self.counters["malformed"] += 1
                self.unreadable.append(block_time_ms if block_time_ms is not None else 0)
                continue
            if e.tid in self.execs:
                self.counters["duplicates"] += 1
                continue
            if e.conflict:
                self.counters["conflicts"] += 1
            self.execs[e.tid] = e
            new.append(e)
        self._prune(block_time_ms)
        return new

    def _prune(self, now_ms: int | None) -> None:
        if now_ms is None:
            return
        cut = now_ms - RETENTION_MS
        while self.execs:
            tid, e = next(iter(self.execs.items()))
            if e.time_ms >= cut:
                break
            self.execs.popitem(last=False)

    # --- windows -------------------------------------------------------------
    def window(self, start_ms: int, end_ms: int) -> tuple[list[Execution], dict]:
        """Executions with start_ms < time <= end_ms, and whether the observed window covers that span."""
        coverage = self.coverage()
        if self.restart_pending or self.since_ms is None:
            return [], coverage | {"covers": False, "reason": f"node fills not observed yet ({self.restart_reason})"}
        if start_ms < self.since_ms:
            return ([e for e in self.execs.values() if start_ms < e.time_ms <= end_ms],
                    coverage | {"covers": False,
                                "reason": f"node fills observed only since {self.since_ms} (block {self.since_block}); "
                                          "the window starts earlier"})
        if self.last_block_time_ms is None or end_ms > self.last_block_time_ms:
            return ([e for e in self.execs.values() if start_ms < e.time_ms <= end_ms],
                    coverage | {"covers": False, "reason": "node fills not processed through the window's end yet"})
        bad = [t for t in self.unreadable if start_ms < t <= end_ms]
        if bad:
            return ([e for e in self.execs.values() if start_ms < e.time_ms <= end_ms],
                    coverage | {"covers": False, "reason": f"{len(bad)} execution(s) in this window couldn't be read "
                                                           "(missing tid, price, size or time), so its totals are unknown"})
        return [e for e in self.execs.values() if start_ms < e.time_ms <= end_ms], coverage | {"covers": True, "reason": None}

    def coverage(self) -> dict:
        return {"stream": self.stream, "since_block": self.since_block, "since_ms": self.since_ms,
                "through_block": self.last_block, "through_ms": self.last_block_time_ms,
                "restart_pending": self.restart_pending, "restart_reason": self.restart_reason if self.restart_pending else None,
                "earlier_windows": list(self.windows), "counters": dict(self.counters)}


# --- step 14 metrics ----------------------------------------------------------

def _taker(e: Execution) -> Participant | None:
    takers = [p for p in e.participants if p.role == "taker"]
    if len(takers) != 1 or e.aggressor is None or takers[0].side != e.aggressor:
        return None
    return takers[0]


def activity_buckets(execs: list[Execution]) -> dict:
    """Aggressive buying: the takers' new long and cover; aggressive selling: the takers' new short and exit.
    Each percentage divides by the aggressive notional of the same side over the same executions."""
    buy = {"new_long": ZERO, "cover": ZERO}
    sell = {"new_short": ZERO, "exit": ZERO}
    agg_buy = agg_sell = ZERO
    unclassified = {"buy": ZERO, "sell": ZERO, "no_single_taker": ZERO, "uncertain": ZERO}
    reasons = {"buy": set(), "sell": set()}
    for e in execs:
        if not e.complete or e.conflict:
            # Its participants' records are missing or disagree (price, size, time or role), or a repeat of it
            # disagreed: its side, size and effects aren't known exactly, so it is never split.
            unclassified["uncertain"] += e.notional
            continue
        tk = _taker(e)
        if tk is None:
            unclassified["no_single_taker"] += e.notional
            continue
        side = "buy" if tk.side == "B" else "sell"
        if side == "buy":
            agg_buy += e.notional
        else:
            agg_sell += e.notional
        if tk.effect is None:
            unclassified[side] += e.notional
            reasons[side].add(tk.effect_unavailable or "position effect missing")
            continue
        if side == "buy":
            buy["cover"] += tk.effect.get("cover", ZERO) * e.px
            buy["new_long"] += tk.effect.get("new_long", ZERO) * e.px
        else:
            sell["exit"] += tk.effect.get("exit", ZERO) * e.px
            sell["new_short"] += tk.effect.get("new_short", ZERO) * e.px

    def side_out(buckets: dict, denom: Decimal, unc: Decimal, why: set) -> dict:
        if unc > 0:
            reason = "part of this side's aggressive notional can't be classified: " + "; ".join(sorted(why))
            return {"available": False, "reason": reason, "unclassified_usd": unc, "aggressive_usd": denom,
                    "buckets": {k: None for k in buckets}, "classified_usd": {k: v for k, v in buckets.items()}}
        pct = {k: (v / denom * 100 if denom > 0 else None) for k, v in buckets.items()}
        return {"available": True, "reason": None if denom > 0 else "no aggressive notional on this side",
                "unclassified_usd": unc, "aggressive_usd": denom, "buckets": dict(buckets), "pct": pct}

    if unclassified["uncertain"] > 0:
        why = ("an execution in this window is incomplete or conflicting (its participants' records are missing or "
               "disagree), so its side, size and effects aren't known exactly")
        for side in ("buy", "sell"):
            reasons[side].add(why)
            unclassified[side] += unclassified["uncertain"]
    if unclassified["no_single_taker"] > 0:
        # An execution whose aggressive side can't be told (no single taker, or the aggressor missing) could belong to
        # either side: neither side's buckets are complete, so both are unavailable, never scaled.
        why = "an execution in this window has no single identified taker, so its side is unknown"
        reasons["buy"].add(why)
        reasons["sell"].add(why)
        unclassified["buy"] += unclassified["no_single_taker"]
        unclassified["sell"] += unclassified["no_single_taker"]
    return {"buying": side_out(buy, agg_buy, unclassified["buy"], reasons["buy"]),
            "selling": side_out(sell, agg_sell, unclassified["sell"], reasons["sell"]),
            "no_single_taker_usd": unclassified["no_single_taker"], "uncertain_usd": unclassified["uncertain"],
            "executions": len(execs)}


def wallet_split(execs: list[Execution], user: str) -> dict:
    return wallet_splits(execs, {user})[user]


def wallet_splits(execs: list[Execution], users: set) -> dict:
    """Each wallet's own participant effects, in one pass: the full split (maker and taker fills) over its
    total bought / sold notional, and the aggressive split (taker fills only) over its aggressive bought /
    sold notional. A missing effect, side or role makes that split unavailable, never scaled to 100%. So does
    a missing identity: an execution missing a participant's record or address could be any listed wallet's, so
    while one is in the window every split is unavailable."""
    unidentified = sum((e.notional for e in execs if not e.complete or e.conflict or len(e.participants) < 2
                        or any(not p.user for p in e.participants)), ZERO)
    acc = {u: {"full": {"new_long": ZERO, "cover": ZERO, "new_short": ZERO, "exit": ZERO},
               "aggr": {"new_long": ZERO, "cover": ZERO, "new_short": ZERO, "exit": ZERO},
               "tot": {"bought": ZERO, "sold": ZERO, "aggr_bought": ZERO, "aggr_sold": ZERO},
               "missing": {"full": ZERO, "aggressive": ZERO}, "reasons": set()} for u in users}
    for e in execs:
        for p in e.participants:
            a = acc.get(p.user)
            if a is None:
                continue
            n = e.notional
            if p.side not in ("B", "A") or p.role not in ("taker", "maker"):
                a["missing"]["full"] += n
                a["missing"]["aggressive"] += n
                a["reasons"].add("side or role missing")
                continue
            taker = p.role == "taker"
            tot = a["tot"]
            if p.side == "B":
                tot["bought"] += n
                if taker:
                    tot["aggr_bought"] += n
            else:
                tot["sold"] += n
                if taker:
                    tot["aggr_sold"] += n
            if p.effect is None:
                a["missing"]["full"] += n
                if taker:
                    a["missing"]["aggressive"] += n
                a["reasons"].add(p.effect_unavailable or "position effect missing")
                continue
            for k, v in p.effect.items():
                a["full"][k] += v * e.px
                if taker:
                    a["aggr"][k] += v * e.px

    def pct(v: Decimal, d: Decimal):
        return v / d * 100 if d > 0 else None

    def split(b: dict, bought: Decimal, sold: Decimal, unc: Decimal, label: str, reasons: set) -> dict:
        if unidentified > 0:
            return {"label": label, "available": False, "unidentified_usd": unidentified,
                    "reason": "an execution in this window is missing a participant's record or address, or its "
                              "records disagree, so this wallet's fills may be incomplete", "bought_usd": bought,
                    "sold_usd": sold}
        if unc > 0:
            return {"label": label, "available": False, "unclassified_usd": unc,
                    "reason": "position effect missing for part of this wallet's fills: " + "; ".join(sorted(reasons)),
                    "bought_usd": bought, "sold_usd": sold}
        return {"label": label, "available": True, "bought_usd": bought, "sold_usd": sold,
                "usd": dict(b),
                "pct": {"new_long": pct(b["new_long"], bought), "cover": pct(b["cover"], bought),
                        "new_short": pct(b["new_short"], sold), "exit": pct(b["exit"], sold)},
                "pct_reason": {"buying": None if bought > 0 else f"no buying in {label}: percentage unavailable",
                               "selling": None if sold > 0 else f"no selling in {label}: percentage unavailable"}}

    out = {}
    for u, a in acc.items():
        t = a["tot"]
        out[u] = {"user": u,
                  "full": split(a["full"], t["bought"], t["sold"], a["missing"]["full"], "all fills (maker and taker)",
                                a["reasons"]),
                  "aggressive": split(a["aggr"], t["aggr_bought"], t["aggr_sold"], a["missing"]["aggressive"],
                                      "aggressive fills only (taker)", a["reasons"])}
    return out


def liquidations(execs: list[Execution]) -> list[dict]:
    """Each proven liquidation once, for the liquidated trader, with their own position effect saying long or
    short. One liquidation can fill against several makers: it is identified by the liquidated trader and
    their order (the liquidated participant's oid), and its fills are summed (measured on the Nov 7 window:
    21 liquidation executions were 3 liquidations)."""
    groups: dict[tuple, dict] = {}
    for e in execs:
        if e.liquidation is None:
            continue
        victim = [p for p in e.participants if p.liquidated]
        p = victim[0] if len(victim) == 1 else None
        user = (e.liquidation or {}).get("liquidated_user")
        key = (user, p.oid if p is not None else ("tid", e.tid))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"liquidated_user": user, "oid": p.oid if p is not None else None,
                               "first_time_ms": e.time_ms, "last_time_ms": e.time_ms, "fills": 0,
                               "sz": ZERO, "notional": ZERO, "tids": [], "position": None, "reason": None,
                               "method": (e.liquidation or {}).get("method"), "_kinds": set(), "_reasons": set()}
        g["fills"] += 1
        g["sz"] += e.sz
        g["notional"] += e.notional
        g["tids"].append(e.tid)
        g["first_time_ms"] = min(g["first_time_ms"], e.time_ms)
        g["last_time_ms"] = max(g["last_time_ms"], e.time_ms)
        if p is None:
            g["_reasons"].add("the liquidated participant isn't identified in this execution")
        elif p.effect is None:
            g["_reasons"].add(p.effect_unavailable or "position effect missing")
        elif p.side == "A" and p.effect.get("exit", ZERO) > 0:
            g["_kinds"].add("long")
        elif p.side == "B" and p.effect.get("cover", ZERO) > 0:
            g["_kinds"].add("short")
        else:
            g["_reasons"].add("the liquidated participant's effect closes no position")
    out = []
    for g in groups.values():
        kinds, reasons = g.pop("_kinds"), g.pop("_reasons")
        if len(kinds) == 1 and not reasons:
            g["position"] = next(iter(kinds))
        else:
            g["reason"] = "; ".join(sorted(reasons)) or "fills disagree on long or short"
        out.append(g)
    out.sort(key=lambda g: (g["first_time_ms"], str(g["oid"])))
    return out
