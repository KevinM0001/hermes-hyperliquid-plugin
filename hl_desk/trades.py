"""Public trades for one market: identity, de-duplication, coverage segments and gaps.

The execution set is every unique trade with time strictly after the current
segment's start. The first trades message on a connection is the
subscription snapshot (recent history whose completeness the docs don't
state): its trades are de-duplicated and shown on the tape, but not counted,
and the segment starts at its latest trade time.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from .util import fnum, iso_ms

RETENTION_MS = 16 * 60 * 1000


@dataclass(frozen=True)
class Trade:
    coin: str
    time: int
    px: float
    sz: float
    side: str          # 'B' buyer aggressed, 'A' seller aggressed (docs notation)
    tid: int
    hash: str
    buyer: str         # users[0]
    seller: str        # users[1]
    seq: int           # local arrival order

    @property
    def notional(self) -> float:
        return self.px * self.sz

    @property
    def taker(self) -> str | None:
        if self.side == "B":
            return self.buyer
        if self.side == "A":
            return self.seller
        return None

    @property
    def identity(self) -> tuple:
        return (self.coin, self.time, self.tid)

    def as_dict(self, counted: bool = True, segment: str = "current") -> dict:
        return {"time_ms": self.time, "time": iso_ms(self.time), "px": self.px, "sz": self.sz,
                "notional_usd": round(self.notional, 2), "side": self.side,
                "aggressor": "buyer" if self.side == "B" else "seller" if self.side == "A" else None,
                "buyer": self.buyer, "seller": self.seller, "tid": self.tid, "hash": self.hash,
                "counted": counted, "segment": segment}


ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _int(x) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.strip().lstrip("-").isdigit():
        return int(x)
    return None


def trade_problems(raw: dict, expect_coin: str | None = None) -> list[str]:
    """Every essential field a public trade needs (docs WsTrade); an empty list means it's valid.
    expect_coin: the subscription's coin; a record naming another coin is invalid for it."""
    if not isinstance(raw, dict):
        return ["the whole record"]
    out = []
    px, sz = fnum(raw.get("px")), fnum(raw.get("sz"))
    if px is None or px <= 0:
        out.append("px")
    if sz is None or sz <= 0:
        out.append("sz")
    if _int(raw.get("time")) is None:
        out.append("time")
    if _int(raw.get("tid")) is None:
        out.append("tid")
    if raw.get("side") not in ("B", "A"):
        out.append("side")
    users = raw.get("users")
    if not (isinstance(users, (list, tuple)) and len(users) == 2
            and all(isinstance(u, str) and ADDRESS_RE.match(u) for u in users)):
        out.append("users")
    if not isinstance(raw.get("coin"), str) or not raw.get("coin") or (expect_coin and raw.get("coin") != expect_coin):
        out.append("coin")
    return out


def parse_trade(raw: dict, seq: int) -> Trade | None:
    """A Trade, or None when any essential field is missing or invalid (nothing is filled in)."""
    if not isinstance(raw, dict) or trade_problems(raw):
        return None
    users = raw["users"]
    return Trade(coin=str(raw["coin"]), time=_int(raw["time"]), px=fnum(raw["px"]), sz=fnum(raw["sz"]),
                 side=raw["side"], tid=_int(raw["tid"]), hash=str(raw.get("hash", "")),
                 buyer=users[0].lower(), seller=users[1].lower(), seq=seq)


class TradeStore:
    def __init__(self, market: str) -> None:
        self.market = market
        self.trades: deque[Trade] = deque()      # counted trades of the current segment
        self.tape: deque[tuple[Trade, bool, int]] = deque(maxlen=60)   # (trade, counted, segment number)
        self.segment_no = 0
        self.seen: dict[tuple, int] = {}
        self.seq = 0
        self.segment_start_ms: int | None = None
        self.segment_conn: int | None = None
        self.watermark_ms: int | None = None
        self.snapshot_pending = False
        self.segments: deque[dict] = deque(maxlen=20)
        self.gaps: deque[dict] = deque(maxlen=50)
        self.counters = {"duplicates": 0, "pre_segment": 0, "late": 0, "malformed": 0, "snapshot_trades": 0,
                         "unattributed_messages": 0}

    # --- lifecycle -------------------------------------------------------
    def begin(self, conn: int) -> None:
        """Called when trades are (re)subscribed on a connection."""
        self.snapshot_pending = True
        self.segment_conn = conn

    def end(self, reason: str, local_wall_ms: int | None = None) -> None:
        """Close the current segment; the time until the next segment starts is a gap."""
        if self.segment_start_ms is not None:
            self.segments.append({"conn": self.segment_conn, "start_ms": self.segment_start_ms,
                                  "end_ms": self.watermark_ms, "end_reason": reason})
            self.gaps.append({"start_ms": self.watermark_ms, "start": iso_ms(self.watermark_ms),
                              "end_ms": None, "end": None, "reason": reason,
                              "local_wall_ms": local_wall_ms})
        self.trades.clear()
        self.segment_start_ms = None
        self.watermark_ms = None
        self.snapshot_pending = False

    # --- ingest ----------------------------------------------------------
    def on_message(self, data: list, fallback_start_ms: int | None = None) -> list[Trade]:
        parsed, bad = [], []
        for raw in data or []:
            self.seq += 1
            t = None if trade_problems(raw, self.market) else parse_trade(raw, self.seq)
            if t is None:
                self.counters["malformed"] += 1
                bad.append(raw)
                continue
            parsed.append(t)
        if self.snapshot_pending:
            return self._on_snapshot(parsed, fallback_start_ms, bad)
        if bad and self.segment_start_ms is not None:
            return self._malformed_boundary(parsed, bad, fallback_start_ms)
        if self.segment_start_ms is None:
            # Trades arrived with no segment open (not subscribed on a live connection).
            self.counters["pre_segment"] += len(parsed)
            return []
        new = []
        for t in parsed:
            if t.identity in self.seen:
                self.counters["duplicates"] += 1
                continue
            self.seen[t.identity] = t.time
            if t.time <= self.segment_start_ms:
                self.counters["pre_segment"] += 1
                self.tape.append((t, False, self.segment_no))
                continue
            if self.watermark_ms is not None and t.time < self.watermark_ms:
                self.counters["late"] += 1
            self.trades.append(t)
            self.tape.append((t, True, self.segment_no))
            new.append(t)
            if self.watermark_ms is None or t.time > self.watermark_ms:
                self.watermark_ms = t.time
        self._prune()
        return new

    def force_boundary(self, raw: list, reason: str, now_ms: int | None) -> bool:
        """End the open segment because a message that may belong to it couldn't be attributed (see
        feed.dispatch). Returns whether a segment was open; a pending subscription snapshot is left alone."""
        if self.segment_start_ms is None or self.snapshot_pending:
            return False
        self.counters["unattributed_messages"] += 1
        self._malformed_boundary([], list(raw or []), now_ms, reason=reason)
        return True

    def _malformed_boundary(self, parsed: list[Trade], bad: list, now_ms: int | None,
                            reason: str | None = None) -> list[Trade]:
        """A live message with an invalid trade: its notional and wallets are unknown, so no window may claim
        to be complete across it. The segment ends here (a gap) and a new one starts after this message,
        at the latest time it or the book shows; its valid trades go to the tape, uncounted."""
        if reason is None:
            missing = trade_problems(bad[0], self.market)
            reason = f"malformed trade (missing or invalid {', '.join(missing)})"
        times = [t.time for t in parsed] + [x for x in (_int(b.get("time")) for b in bad if isinstance(b, dict))
                                             if x is not None]
        start = max([x for x in times + [self.watermark_ms, now_ms] if x is not None])
        self.end(reason)
        self.segment_no += 1
        for t in parsed:
            if t.identity not in self.seen:
                self.seen[t.identity] = t.time
                self.tape.append((t, False, self.segment_no))
        self.segment_start_ms = start
        self.watermark_ms = start
        if self.gaps and self.gaps[-1]["end_ms"] is None:
            self.gaps[-1]["end_ms"] = start
            self.gaps[-1]["end"] = iso_ms(start)
        self._prune()
        return []

    def _on_snapshot(self, parsed: list[Trade], fallback_start_ms: int | None, bad: list = ()) -> list[Trade]:
        """The subscription's first message (recent trades, never counted). The segment starts after all of it,
        including any invalid trade whose time is known."""
        self.snapshot_pending = False
        self.segment_no += 1
        self.counters["snapshot_trades"] += len(parsed)
        latest = None
        for t in sorted(parsed, key=lambda x: (x.time, x.seq)):
            if t.identity not in self.seen:
                self.seen[t.identity] = t.time
                self.tape.append((t, False, self.segment_no))
            latest = t.time if latest is None else max(latest, t.time)
        prev_end = self.gaps[-1]["start_ms"] if self.gaps else None
        bad_times = [x for x in (_int(b.get("time")) for b in bad if isinstance(b, dict)) if x is not None]
        candidates = [x for x in (latest, prev_end, fallback_start_ms, *bad_times) if x is not None]
        start = max(candidates) if candidates else 0
        self.segment_start_ms = start
        self.watermark_ms = start
        if self.gaps and self.gaps[-1]["end_ms"] is None:
            self.gaps[-1]["end_ms"] = start
            self.gaps[-1]["end"] = iso_ms(start)
        self._prune()
        return []

    def _prune(self) -> None:
        if self.watermark_ms is None:
            return
        cut = self.watermark_ms - RETENTION_MS
        while self.trades and self.trades[0].time < cut:
            self.trades.popleft()
        if len(self.seen) > 50000 or (self.seen and min(self.seen.values()) < cut - RETENTION_MS):
            self.seen = {k: v for k, v in self.seen.items() if v >= cut - RETENTION_MS}

    # --- queries ---------------------------------------------------------
    def window(self, start_ms: int, end_ms: int, start_inclusive: bool = True) -> list[Trade]:
        """Counted trades with start <= time <= end (or start < time when start_inclusive is False)."""
        if start_inclusive:
            sel = [t for t in self.trades if start_ms <= t.time <= end_ms]
        else:
            sel = [t for t in self.trades if start_ms < t.time <= end_ms]
        sel.sort(key=lambda x: (x.time, x.seq))
        return sel

    def coverage(self) -> dict:
        start, wm = self.segment_start_ms, self.watermark_ms
        return {
            "segment_start_ms": start, "segment_start": iso_ms(start),
            "segment_start_rule": "latest trade time in the subscription snapshot; trades at or before it are not counted",
            "watermark_ms": wm, "watermark": iso_ms(wm),
            "observed_s": None if start is None or wm is None else round((wm - start) / 1000, 3),
            "retained_s": RETENTION_MS / 1000,
            "gaps": list(self.gaps)[-10:],
            "counters": dict(self.counters),
        }
