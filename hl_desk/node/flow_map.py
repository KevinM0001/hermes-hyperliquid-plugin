"""The Order Flow Map of one market, from node fills (Kevin's order-flow map report, ported to node mode).

Aggressive (taker) flow over eight 15-minute buckets aligned to the clock, each fill counted once (by tid, the
aggressor side only, at price x size), split by what the taker's own position effect says the fill did:

    Open Long    taker bought, new_long only          -> taker buying,  risk added
    Close Short  taker bought, cover only             -> taker buying,  risk removed
    Short > Long taker bought, cover and new_long > 0 -> taker buying,  a flip (apart: neither added nor removed)
    Close Long   taker sold, exit only                -> taker selling, risk removed
    Open Short   taker sold, new_short only           -> taker selling, risk added
    Long > Short taker sold, exit and new_short > 0   -> taker selling, a flip

Two orthogonal decompositions of the same notional: buy = Open Long + Close Short + Short > Long and
sell = Close Long + Open Short + Long > Short; risk added = Open Long + Open Short and risk removed = Close Long +
Close Short, flips apart; buy + sell = risk added + risk removed + flips. Direction comes only from the effect,
the feed's own statement of the taker's position change, never from price. A fill whose side or effect can't be
told (an incomplete or conflicting record, no single identified taker, no position effect) is counted apart as
unclassified: never guessed, never scaled.

The accumulator is compact: per bucket the six notionals, the fill counts and each taker account's aggressive
notional, plus the spans of block time over which node fills were observed, so a bucket that starts before the
feed's coverage, spans a gap or is still in progress says how much of it is covered; nothing is filled in. It
lives on the market's partition: a new producer or replay session drops it, a gap doesn't (the hole is stated).
"""

from __future__ import annotations

from decimal import Decimal

from .flow import Execution, NodeFlow, _taker

ZERO = Decimal(0)
BUCKET_MS = 15 * 60 * 1000
BUCKETS = 8
WINDOW_MS = BUCKETS * BUCKET_MS

KINDS = ("open_long", "close_short", "flip_to_long", "close_long", "open_short", "flip_to_short")
BUY_KINDS = ("open_long", "close_short", "flip_to_long")
SELL_KINDS = ("close_long", "open_short", "flip_to_short")
FLIP_KINDS = ("flip_to_long", "flip_to_short")
DIR_LABELS = {"open_long": "Open Long", "close_short": "Close Short", "flip_to_long": "Short > Long",
              "close_long": "Close Long", "open_short": "Open Short", "flip_to_short": "Long > Short"}

COUNTING = ("each node fill once (by tid), the aggressor (taker) side only, at price x size; direction from the taker's own "
            "position effect, never from price: Open Long (new long only), Close Short (cover only) and a Short > Long flip "
            "(both) are taker buying; Close Long (exit only), Open Short (new short only) and a Long > Short flip (both) are "
            "taker selling. Risk added = Open Long + Open Short; risk removed = Close Long + Close Short; flips apart, so "
            "buy + sell = risk added + risk removed + flips. A fill whose record is incomplete or conflicting, has no single "
            "identified taker or no position effect is counted apart as unclassified, never guessed")
UNITS_NOTE = ("USD notional (price x size of each fill, the taker side only, each fill once); percentages of the window's "
              "aggressive flow (buy + sell); times are ms of block time; coverage is in seconds")
WINDOW_NOTE = ("eight 15-minute buckets aligned to the clock (UTC quarter hours), the last one in progress through end_ms, the "
               "latest block time whose node fills are known; coverage_s is the block time within the bucket (or window) over "
               "which node fills were observed; a bucket is complete only when all 15 minutes of it are covered and every fill "
               "record in it was read, and one that starts before the node fills were observed, spans a gap or is in progress "
               "says so with its coverage; nothing is scaled or filled in")
SHARES_BASIS = "shares of the window's aggressive flow (buy + sell, the taker side only); ties: the lowest address first"


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _usd(d: Decimal | None) -> float | None:
    return None if d is None else round(float(d), 2)


def _pct(d: Decimal | None) -> float | None:
    return None if d is None else round(float(d), 2)


def classify(e: Execution) -> tuple[str | None, object, str | None]:
    """(kind, taker, reason): the fill's direction from its single taker's own position effect (see the module note),
    or (None, taker or None, why it can't be told)."""
    if not e.complete or e.conflict:
        return None, None, "the execution record is incomplete or conflicting"
    tk = _taker(e)
    if tk is None:
        return None, None, "no single identified taker"
    if tk.effect is None:
        return None, tk, tk.effect_unavailable or "position effect missing"
    if tk.side == "B":
        cover, new_long = tk.effect.get("cover", ZERO), tk.effect.get("new_long", ZERO)
        if cover > 0 and new_long > 0:
            return "flip_to_long", tk, None
        if new_long > 0:
            return "open_long", tk, None
        if cover > 0:
            return "close_short", tk, None
    else:
        exit_, new_short = tk.effect.get("exit", ZERO), tk.effect.get("new_short", ZERO)
        if exit_ > 0 and new_short > 0:
            return "flip_to_short", tk, None
        if new_short > 0:
            return "open_short", tk, None
        if exit_ > 0:
            return "close_long", tk, None
    return None, tk, "the position effect is all zero"


class Bucket:
    __slots__ = ("start_ms", "usd", "fills", "flip_fills", "accounts", "unattributed_usd", "unclassified_usd",
                 "unclassified_fills", "unclassified_why", "unreadable_fills")

    def __init__(self, start_ms: int) -> None:
        self.start_ms = start_ms
        self.usd = {k: ZERO for k in KINDS}
        self.fills = 0
        self.flip_fills = 0
        self.accounts: dict[str, Decimal] = {}
        self.unattributed_usd = ZERO
        self.unclassified_usd = ZERO
        self.unclassified_fills = 0
        self.unclassified_why: dict[str, int] = {}
        self.unreadable_fills = 0

    def merge(self, other: "Bucket") -> None:
        for k in KINDS:
            self.usd[k] += other.usd[k]
        self.fills += other.fills
        self.flip_fills += other.flip_fills
        for u, v in other.accounts.items():
            self.accounts[u] = self.accounts.get(u, ZERO) + v
        self.unattributed_usd += other.unattributed_usd
        self.unclassified_usd += other.unclassified_usd
        self.unclassified_fills += other.unclassified_fills
        for why, n in other.unclassified_why.items():
            self.unclassified_why[why] = self.unclassified_why.get(why, 0) + n
        self.unreadable_fills += other.unreadable_fills


def bucket_start(t_ms: int) -> int:
    return t_ms - t_ms % BUCKET_MS


class FlowMapState:
    """The current window's buckets and the observed spans of block time, for one market partition."""

    def __init__(self) -> None:
        self.buckets: dict[int, Bucket] = {}
        self.segments: list[list] = []          # [start_ms, end_ms, why the hole before it] of observed block time
        self.through_ms: int | None = None      # the latest block time whose node fills are known
        self.seen_malformed = 0
        self.counters = {"fills": 0, "late_dropped": 0}

    def window_start(self) -> int | None:
        return None if self.through_ms is None else bucket_start(self.through_ms) - (BUCKETS - 1) * BUCKET_MS

    def observe(self, flow: NodeFlow, block_time_ms) -> None:
        """Coverage: node fills are known over (flow.since_ms, block_time_ms] of the current observed window. Called
        at every watermark and executions message (watermarks are periodic, not per block)."""
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

    def add(self, flow: NodeFlow, execs: list, block_time_ms) -> None:
        """The executions newly counted by the flow (once by tid, inside its observed window), each into the bucket of
        its own time; executions the flow couldn't read are counted per bucket as unreadable."""
        malformed = flow.counters.get("malformed", 0)
        if malformed > self.seen_malformed:
            ws = self.window_start()
            if _is_int(block_time_ms) and (ws is None or block_time_ms >= ws):
                self._bucket(block_time_ms).unreadable_fills += malformed - self.seen_malformed
            self.seen_malformed = malformed
        ws = self.window_start()
        for e in execs:
            if ws is not None and e.time_ms < ws:
                self.counters["late_dropped"] += 1
                continue
            b = self._bucket(e.time_ms)
            kind, taker, why = classify(e)
            if kind is None:
                b.unclassified_usd += e.notional
                b.unclassified_fills += 1
                b.unclassified_why[why] = b.unclassified_why.get(why, 0) + 1
                continue
            n = e.notional
            b.usd[kind] += n
            b.fills += 1
            self.counters["fills"] += 1
            if kind in FLIP_KINDS:
                b.flip_fills += 1
            if taker.user:
                b.accounts[taker.user] = b.accounts.get(taker.user, ZERO) + n
            else:
                b.unattributed_usd += n

    def _bucket(self, t_ms: int) -> Bucket:
        s = bucket_start(t_ms)
        b = self.buckets.get(s)
        if b is None:
            b = self.buckets[s] = Bucket(s)
        return b

    def _prune(self) -> None:
        ws = self.window_start()
        if ws is None:
            return
        for s in [s for s in self.buckets if s < ws]:
            del self.buckets[s]
        self.segments = [s for s in self.segments if s[1] > ws]


# --- the block -------------------------------------------------------------------------------

def _merged(segments: list[list]) -> list[list]:
    out: list[list] = []
    for s in sorted((list(s) for s in segments), key=lambda s: s[0]):
        if out and s[0] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], s[1])
        else:
            out.append(s)
    return out


def _covered_ms(segs: list[list], lo: int, hi: int) -> int:
    return sum(max(0, min(s[1], hi) - max(s[0], lo)) for s in segs) if hi > lo else 0


def _holes(segs: list[list], lo: int, hi: int) -> list[dict]:
    """The spans of [lo, hi] with no observed node fills, each with why."""
    out, cur = [], lo
    for s in segs:
        if s[1] <= lo:
            continue
        if s[0] > cur:
            out.append({"start_ms": cur, "end_ms": min(s[0], hi),
                        "why": s[2] or "before the node fills were observed"})
        cur = max(cur, s[1])
        if cur >= hi:
            break
    if cur < hi:
        out.append({"start_ms": cur, "end_ms": hi, "why": "node fills not observed"})
    return [h for h in out if h["end_ms"] > h["start_ms"]]


def _sides(b: Bucket) -> tuple[Decimal, Decimal]:
    return sum((b.usd[k] for k in BUY_KINDS), ZERO), sum((b.usd[k] for k in SELL_KINDS), ZERO)


def _numbers(b: Bucket) -> dict:
    buy, sell = _sides(b)
    return {"buy_usd": _usd(buy), "sell_usd": _usd(sell), "net_usd": _usd(buy - sell),
            "open_long_usd": _usd(b.usd["open_long"]), "close_short_usd": _usd(b.usd["close_short"]),
            "open_short_usd": _usd(b.usd["open_short"]), "close_long_usd": _usd(b.usd["close_long"]),
            "flip_usd": _usd(b.usd["flip_to_long"] + b.usd["flip_to_short"]),
            "flip_to_long_usd": _usd(b.usd["flip_to_long"]), "flip_to_short_usd": _usd(b.usd["flip_to_short"]),
            "risk_added_usd": _usd(b.usd["open_long"] + b.usd["open_short"]),
            "risk_removed_usd": _usd(b.usd["close_long"] + b.usd["close_short"]),
            "fills": b.fills, "flip_fills": b.flip_fills,
            "unclassified_usd": _usd(b.unclassified_usd), "unclassified_fills": b.unclassified_fills,
            "unreadable_fills": b.unreadable_fills}


def build(st: FlowMapState | None) -> dict:
    """The map's window, buckets, totals, account shares and readings (provenance and freshness are added by the panel)."""
    if st is None or st.through_ms is None:
        return {"available": False, "reason": "no node fills observed for this market yet"}
    end, start = st.through_ms, st.window_start()
    segs = _merged(st.segments)
    total = Bucket(start)
    rows = []
    for k in range(BUCKETS):
        bs = start + k * BUCKET_MS
        be = bs + BUCKET_MS
        b = st.buckets.get(bs) or Bucket(bs)
        total.merge(b)
        hi = min(be, end)
        cov = _covered_ms(segs, bs, hi)
        in_progress = be > end
        reasons = []
        if in_progress:
            reasons.append(f"in progress: {(hi - bs) / 1000:.0f} s of its 900 s have passed")
        if cov < hi - bs:
            holes = _holes(segs, bs, hi)
            reasons.append(f"{(hi - bs - cov) / 1000:.0f} s of it not covered by node fills: "
                           + "; ".join(h["why"] for h in holes))
        if b.unreadable_fills:
            reasons.append(f"{b.unreadable_fills} fill record(s) in it couldn't be read, so its totals are unknown")
        agg = sum(_sides(b), ZERO)
        top_share = (max(b.accounts.values()) / agg * 100) if (b.accounts and agg > 0) else None
        rows.append({"start_ms": bs, "end_ms": be, **_numbers(b),
                     "accounts": len(b.accounts), "top_account_share_pct": _pct(top_share),
                     "coverage_s": round(cov / 1000, 1),
                     "complete": (not in_progress) and cov == BUCKET_MS and b.unreadable_fills == 0,
                     "in_progress": in_progress, "reason": "; ".join(reasons) or None})
    cov_all = _covered_ms(segs, start, end)
    holes = _holes(segs, start, end)
    window = {"start_ms": start, "end_ms": end, "bucket_s": BUCKET_MS // 1000, "buckets": BUCKETS,
              "coverage_s": round(cov_all / 1000, 1), "complete": cov_all == end - start and total.unreadable_fills == 0,
              "uncovered": holes, "note": WINDOW_NOTE}
    buy, sell = _sides(total)
    basis = buy + sell
    ranked = sorted(total.accounts.items(), key=lambda kv: (-kv[1], kv[0]))
    top5 = ranked[:5]

    def share(v: Decimal):
        return _pct(v / basis * 100) if basis > 0 else None
    accounts = {"top": None if not ranked else {"address": ranked[0][0], "notional_usd": _usd(ranked[0][1]),
                                                 "share_pct": share(ranked[0][1])},
                "top5_share_pct": share(sum((v for _, v in top5), ZERO)) if top5 else None,
                "distinct": len(ranked), "unattributed_usd": _usd(total.unattributed_usd), "basis_usd": _usd(basis),
                "basis": SHARES_BASIS}
    return {"available": True, "window": window, "buckets": rows,
            "totals": _numbers(total) | {"aggressive_usd": _usd(basis)}, "accounts": accounts,
            "readings": readings(total, ranked, basis),
            "counting": COUNTING, "units_note": UNITS_NOTE,
            "counters": dict(st.counters)}


# --- the readings (the report's "What this shows") ------------------------------------------------

def money(x) -> str:
    """The report's money format: $5.76M, $711K, $850 (two decimals in millions, none below)."""
    v = abs(float(x))
    if v >= 999_500_000:
        return f"${v / 1e9:.2f}B"
    if v >= 999_500:
        return f"${v / 1e6:.2f}M"
    if v >= 999.5:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def pct_text(x) -> str:
    return f"{float(x):.1f}%"


def readings(t: Bucket, ranked: list, basis: Decimal) -> list[dict]:
    """The four sentences, chosen by the report's rules: (1) the net side; (2) which component of that side is
    larger, closing or opening; (3) risk opened against risk closed, with the flips; (4) the top account's share and
    the top five's. An empty window has one sentence; unclassified or unreadable fills add one stating them."""
    buy, sell = _sides(t)
    net = buy - sell
    ol, cs, os_, cl = t.usd["open_long"], t.usd["close_short"], t.usd["open_short"], t.usd["close_long"]
    flips = t.usd["flip_to_long"] + t.usd["flip_to_short"]
    added, removed = ol + os_, cl + cs
    unc = t.unclassified_usd
    out = []
    if t.fills == 0:
        out.append({"kind": "empty", "text": "No taker fills in the window.", "basis": {"fills": 0}})
    else:
        side = "buy" if net > 0 else "sell" if net < 0 else "flat"
        in_doubt = unc > 0 and unc >= abs(net)
        bought_sold = f"{money(buy)} bought against {money(sell)} sold"
        if side == "flat":
            text = f"Taker flow ran flat on net — {bought_sold}."
        else:
            text = f"Taker flow ran {money(net)} to the {side} side on net — {bought_sold}."
        if in_doubt:
            text = (f"Among the fills that could be classified, taker flow ran {money(net)} to the {side} side on net "
                    f"({bought_sold}), but {money(unc)} couldn't be classified: enough to turn it."
                    if side != "flat" else
                    f"Among the fills that could be classified, taker flow ran flat on net ({bought_sold}), but "
                    f"{money(unc)} couldn't be classified.")
        out.append({"kind": "net", "text": text,
                    "basis": {"side": side, "net_usd": _usd(net), "buy_usd": _usd(buy), "sell_usd": _usd(sell),
                              "unclassified_usd": _usd(unc), "net_in_doubt": in_doubt}})
        if side == "sell":
            if cl > os_:
                text, dom, label = (f"That selling is longs leaving, not shorts arriving: {money(cl)} of it closed existing "
                                    f"longs against only {money(os_)} opening new shorts. An unwind, not a bet."), "close_long", "unwind"
            elif os_ > cl:
                text, dom, label = (f"That selling is shorts arriving, not longs leaving: {money(os_)} of it opened new "
                                    f"shorts against only {money(cl)} closing existing longs. A bet, not an unwind."), "open_short", "bet"
            else:
                text, dom, label = (f"That selling split evenly: {money(cl)} of it closed existing longs and {money(os_)} "
                                    f"opened new shorts."), None, "even"
        elif side == "buy":
            if ol > cs:
                text, dom, label = (f"That buying is new longs, not shorts covering: {money(ol)} opened fresh long risk "
                                    f"against {money(cs)} closing shorts. Position building."), "open_long", "position building"
            elif cs > ol:
                text, dom, label = (f"That buying is shorts covering, not new longs: {money(cs)} closed existing shorts "
                                    f"against {money(ol)} opening fresh long risk. Short covering, not position building."), "close_short", "short covering"
            else:
                text, dom, label = (f"That buying split evenly: {money(ol)} opened fresh long risk and {money(cs)} "
                                    f"closed shorts."), None, "even"
        else:
            text, dom, label = (f"Neither side led: {money(ol)} opened fresh long risk and {money(cs)} closed shorts on the "
                                f"buy side; {money(cl)} closed existing longs and {money(os_)} opened new shorts on the "
                                f"sell side."), None, "flat"
        out.append({"kind": "composition", "text": text,
                    "basis": {"side": side, "dominant": dom, "label": label, "close_long_usd": _usd(cl),
                              "open_short_usd": _usd(os_), "open_long_usd": _usd(ol), "close_short_usd": _usd(cs)}})
        if flips > 0:
            text = (f"Across the window, {money(added)} opened risk and {money(removed)} closed it, with {money(flips)} in "
                    f"single fills that flipped a position from one side to the other.")
        else:
            text = (f"Across the window, {money(added)} opened risk and {money(removed)} closed it; no single fill flipped "
                    f"a position from one side to the other.")
        out.append({"kind": "risk", "text": text,
                    "basis": {"risk_added_usd": _usd(added), "risk_removed_usd": _usd(removed), "flip_usd": _usd(flips),
                              "flip_fills": t.flip_fills}})
        n = len(ranked)
        top5 = sum((v for _, v in ranked[:5]), ZERO)
        if n == 0:
            text, top = "No taker account could be identified behind the aggressive flow.", None
        else:
            top = {"address": ranked[0][0], "notional_usd": _usd(ranked[0][1]),
                   "share_pct": _pct(ranked[0][1] / basis * 100)}
            p = pct_text(ranked[0][1] / basis * 100)
            q = pct_text(top5 / basis * 100)
            if n >= 5:
                text = (f"The most active taker account took {p} of the aggressive flow ({money(ranked[0][1])}); the top "
                        f"five took {q}.")
            elif n > 1:
                text = (f"The most active taker account took {p} of the aggressive flow ({money(ranked[0][1])}); all "
                        f"{n} accounts together took {q}.")
            else:
                text = f"The only identified taker account took {p} of the aggressive flow ({money(ranked[0][1])})."
        out.append({"kind": "concentration", "text": text,
                    "basis": {"top": top, "top5_share_pct": _pct(top5 / basis * 100) if (n and basis > 0) else None,
                              "distinct": n, "basis_usd": _usd(basis)}})
    if t.unclassified_fills or t.unreadable_fills:
        parts = []
        if t.unclassified_fills:
            why = "; ".join(f"{k} ({v})" for k, v in sorted(t.unclassified_why.items()))
            parts.append(f"Another {money(unc)} in {t.unclassified_fills} fill(s) couldn't be classified ({why}) and is in "
                         f"none of these numbers.")
        if t.unreadable_fills:
            parts.append(f"{t.unreadable_fills} fill record(s) in the window couldn't be read at all.")
        out.append({"kind": "unclassified", "text": " ".join(parts),
                    "basis": {"unclassified_usd": _usd(unc), "unclassified_fills": t.unclassified_fills,
                              "unreadable_fills": t.unreadable_fills, "reasons": dict(sorted(t.unclassified_why.items()))}})
    return out
