"""Node-mode burst alerts: the burst calibration's rule, evaluated once per node block (Kevin's choice A, Nov 8).

The rule is the one the thresholds were measured with (analysis PLAN.md, "The burst rule" and "Validation"):
- For each side, sum the taker notional (price x size) of the market's executions in (W - 60 s, W], where W is the
  block time of each node executions message: one message per block with executions in the market, so one
  evaluation per block.
- Re-arm a disarmed side when its sum before this block's executions are added is below the threshold.
- Add the executions, then fire the side if it is armed and its sum has reached the threshold, once, and disarm it.
- A gap (a boundary of the node fills' window: a sequence gap, a new producer or replay session) re-arms both sides
  and starts a new segment. No burst spans a gap.
- Incomplete windows: a window that starts before the segment's start, or that contains a rejected or unreadable
  record, keeps its executions but never fires, re-arms or disarms.

A record is rejected or unreadable when the execution can't be read (tid, price, size or time); when its
participants' records are missing or disagree (`complete` false) or its repeated records disagree (`conflict`); when it
isn't exactly one taker and one maker, one buyer and one seller, each with an address; when its fill time isn't the
block time; when its identity was already seen in an earlier block; or when the block's fills weren't read at all
(`fills_known` false). A block time running backwards is an unreadable record at the last block time; a block with
executions but no block time is one at both the last block time and the next.

Thresholds are per market: automatic (the dated calibration, `calibration.py`) or manual (`node_burst_min_usd`).
Public mode doesn't use this module; its bursts keep the public-trades rule (`deck.py`).
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from decimal import Decimal

from ..util import iso_ms
from ..wallets import ranked_list
from .flow import Execution, activity_buckets, parse_exec

WINDOW_MS = 60_000
SEEN_RETENTION_MS = 16 * 60 * 1000
SIDE_NAME = {"B": "buying", "A": "selling"}
ZERO = Decimal(0)
RULE = ("node fills, one evaluation per node block: per side, the taker notional in (W - 60 s, W]; re-arm when the "
        "sum before the block's executions is below the threshold; fire once when it reaches it; a gap re-arms and "
        "starts a new segment; a window starting before the segment start or containing a rejected or unreadable "
        "record never fires, re-arms or disarms")


class NodeBurstRule:
    """The node burst rule for one market of one input stream (a feed client's flow)."""

    def __init__(self, market: str, threshold: dict, ranker=None, provenance=None,
                 target_pct: float = 50.0, cap: int = 50) -> None:
        self.market = market
        self.threshold = threshold                  # {"usd", "source", "label", ...} from calibration.threshold()
        self.ranker = ranker                        # (market, side, total_usd) -> rank dict
        self.provenance = provenance or (lambda: None)
        self.target_pct, self.cap = target_pct, cap
        self.events: deque[dict] = deque(maxlen=50)
        self.counters = {"evaluations": 0, "incomplete_windows": 0, "fired": 0, "gaps": 0, "rejected": {}}
        # Identities already seen, tid -> (block, block time, digest of the record), kept 16 min in arrival order
        # (block times never run backwards here) to reject an identity repeated in a later block.
        self.seen: dict[int, tuple] = {}
        self.seen_order: deque = deque()
        self._reset()

    def _reset(self) -> None:
        self.segment_start_ms: int | None = None
        self.samples: deque = deque()               # (block time, side name, notional, Execution), valid only
        self.bad: deque[int] = deque()              # block times of rejected or unreadable records
        self.armed = {"buying": True, "selling": True}
        self.last_bt: int | None = None
        self.untimed = False                        # an executions message without a block time since last_bt

    # --- segments --------------------------------------------------------------------
    def boundary(self, reason: str) -> None:
        """A gap: both sides re-arm and a new segment starts at the node fills' next window start."""
        self.counters["gaps"] += 1
        self.gap_reason = reason
        self._reset()

    def start(self, start_ms: int) -> None:
        """The segment starts (the node fills' first watermark after a gap)."""
        if self.segment_start_ms is None:
            self.segment_start_ms = start_ms

    # --- evaluation --------------------------------------------------------------------
    def on_block(self, block: int, bt, raws, fills_known: bool = True) -> None:
        """One evaluation per node block carrying the market's executions."""
        if self.segment_start_ms is None:
            return
        if not isinstance(bt, int) or isinstance(bt, bool):
            # A block without a time (the node record had none): its executions can't be placed, so they're an
            # unreadable record at some time between the last block time and the next one. Every window that could
            # contain it is incomplete: those containing the last block time now, and the next block's.
            if raws or fills_known is False:
                self._reject("a block without a block time", self.last_bt if self.last_bt is not None
                             else self.segment_start_ms)
                self.untimed = True
            return
        if self.last_bt is not None and bt < self.last_bt:
            self._reject("a block time ran backwards", self.last_bt)
            return
        self.last_bt = bt
        if self.untimed:
            self._reject("a block without a block time", bt)
            self.untimed = False
        valid, problems = self._validate(block, bt, raws, fills_known)
        for why in problems:
            self._reject(why, bt)
        lo = bt - WINDOW_MS
        while self.samples and self.samples[0][0] <= lo:
            self.samples.popleft()
        while self.bad and self.bad[0] <= lo:
            self.bad.popleft()
        self._prune_seen(bt)
        self.counters["evaluations"] += 1
        before = {"buying": ZERO, "selling": ZERO}
        for _t, side, n, _e in self.samples:
            before[side] += n
        added = {"buying": ZERO, "selling": ZERO}
        for side, n, _e in valid:
            added[side] += n
        if lo < self.segment_start_ms or self.bad:
            self.counters["incomplete_windows"] += 1     # never fires, re-arms or disarms
        elif self.threshold.get("usd"):
            thr = Decimal(str(self.threshold["usd"]))
            for side in ("buying", "selling"):
                total = before[side] + added[side]
                if before[side] < thr:
                    self.armed[side] = True
                if total >= thr and self.armed[side]:
                    self.armed[side] = False
                    self._fire(side, block, bt, total, valid)
        for side, n, e in valid:                         # the window keeps its executions either way
            self.samples.append((bt, side, n, e))

    def _validate(self, block: int, bt: int, raws, fills_known: bool):
        if fills_known is False:
            return [], ["the block's fills weren't read (fills_known false)"]
        valid, problems = [], []
        for raw in raws if isinstance(raws, list) else []:
            e = parse_exec(raw, block, bt) if isinstance(raw, dict) else None
            if e is None:
                problems.append("an unreadable execution (tid, price, size or time)")
                continue
            record = hashlib.blake2b(json.dumps(raw, sort_keys=True).encode(), digest_size=16).digest()
            prev = self.seen.get(e.tid)
            if prev is not None:
                if prev[0] != block:
                    problems.append("an execution identity repeated in a later block")
                elif prev[2] != record:
                    problems.append("conflicting copies of an execution in one block")
                continue                                 # an identical copy within its own block counts once
            self.seen[e.tid] = (block, bt, record)
            self.seen_order.append((bt, e.tid))
            why = _problem(e, bt)
            if why:
                problems.append(why)
                continue
            taker = next(p for p in e.participants if p.role == "taker")
            valid.append((SIDE_NAME[taker.side], e.notional, e))
        return valid, problems

    def _reject(self, why: str, at_ms: int) -> None:
        self.bad.append(at_ms)
        self.counters["rejected"][why] = self.counters["rejected"].get(why, 0) + 1

    def _prune_seen(self, now_ms: int) -> None:
        cut = now_ms - SEEN_RETENTION_MS
        while self.seen_order and self.seen_order[0][0] < cut:
            _bt, tid = self.seen_order.popleft()
            if self.seen.get(tid, (None, None))[1] == _bt:
                del self.seen[tid]

    # --- events ------------------------------------------------------------------------
    def _fire(self, side: str, block: int, bt: int, total: Decimal, block_valid: list) -> None:
        execs = [e for _t, s, _n, e in self.samples if s == side] + [e for s, _n, e in block_valid if s == side]
        takers: dict[str, float] = {}
        for e in execs:
            t = next(p for p in e.participants if p.role == "taker")
            takers[t.user] = takers.get(t.user, 0.0) + float(e.notional)
        thr = self.threshold
        rank = (self.ranker(self.market, side, float(total)) if self.ranker else
                {"available": False, "reason": "no burst reference"})
        self.counters["fired"] += 1
        self.events.appendleft({
            "type": "burst", "stream": "node fills", "market": self.market, "side": side,
            "at_ms": bt, "at": iso_ms(bt), "block": block,
            "window_start_ms_exclusive": bt - WINDOW_MS, "window_end_ms": bt,
            "aggressive_notional_usd": round(float(total), 2), "threshold_usd": thr["usd"],
            "threshold": {k: v for k, v in thr.items() if k != "usd"} | {"usd": thr["usd"]},
            "rank": rank, "executions": len(execs),
            "wallets": ranked_list(takers, float(total), self.target_pct, self.cap, f"the burst's aggressive {side}"),
            "activity": activity_buckets(execs), "provenance": self.provenance(),
            "rule": RULE,
            "text": (f"Aggressive {side} of ${float(total):,.0f} within 60 s of node fills (threshold "
                     f"${thr['usd']:,.0f}, {thr['label']})" + (f"; {rank['label']}" if rank.get("available") else "")),
        })

    def status(self) -> dict:
        return {"rule": RULE, "threshold": dict(self.threshold), "segment_start_ms": self.segment_start_ms,
                "armed": dict(self.armed), "counters": {k: (dict(v) if isinstance(v, dict) else v)
                                                        for k, v in self.counters.items()},
                "events": len(self.events)}


def _problem(e: Execution, bt: int) -> str | None:
    if not e.complete:
        return "an execution whose participants' records are missing or disagree (complete false)"
    if e.conflict:
        return "an execution whose repeated records disagree (conflict)"
    parts = e.participants
    takers = [p for p in parts if p.role == "taker"]
    makers = [p for p in parts if p.role == "maker"]
    if len(parts) != 2 or len(takers) != 1 or len(makers) != 1:
        return "not exactly one taker and one maker"
    if {takers[0].side, makers[0].side} != {"B", "A"}:
        return "not one buyer and one seller"
    if e.aggressor is not None and e.aggressor != takers[0].side:
        return "the aggressor isn't the taker's side"
    if not all(isinstance(p.user, str) and p.user for p in parts):
        return "a participant's address is missing"
    if e.time_ms != bt:
        return "a fill time other than the block time"
    if e.px <= 0 or e.sz <= 0:
        return "a price or size that isn't positive"
    return None
