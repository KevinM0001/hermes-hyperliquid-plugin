"""The resting executable book of one market, from book snapshots and book deltas (PROTOCOL.md 4.1, 4.2).

Orders keep queue order within a level (a snapshot lists each level in queue order; an add joins the
back; a size change keeps the place). Level statistics follow 4.3 exactly:
- total size and order count;
- "largest order size at this level": the largest single resting order, with the time of that SAME
  order (tie: earliest time, then lowest oid);
- "oldest resting order at this level": the earliest time of any resting order, a separate statistic.

Changes are labelled only from the lifecycle evidence the feed carries (4.2): "absorbed" is the part
of a decrease covered by maker_filled; "pulled" is a removal whose status is "canceled" with
maker_filled "0"; "moved" is never used; anything else is "unknown".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from .proto import dec

ZERO = Decimal(0)
CHANGE_RETENTION_MS = 15 * 60 * 1000


@dataclass(slots=True)
class Order:
    oid: int
    side: str        # "B" bid or "A" ask
    px: Decimal
    sz: Decimal
    user: str
    time_ms: int | None


class BookError(Exception):
    """A delta that doesn't fit the book (an add of a resting oid, a change to an unknown oid)."""


class NodeBook:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.levels: dict[str, dict[Decimal, dict[int, None]]] = {"B": {}, "A": {}}
        self.through_seq: int | None = None
        # The snapshot this state was built from: its stated total_orders and block (the stops census's whole-market
        # ratio reports them as the feed stated them); None for a state offered from outside the stream.
        self.snapshot_total: int | None = None
        self.snapshot_block: int | None = None
        self.snapshot_block_time_ms: int | None = None
        self.block: int | None = None
        self.block_time_ms: int | None = None
        # Lifecycle-labelled changes (block_time_ms, side, px, label, size, oid), kept 15 minutes.
        self.changes: deque[tuple] = deque()
        self.crossed_since: int | None = None    # block time the book was first seen crossed
        # Per-level aggregates kept incrementally: total size, and the largest order (cleared when the level changes).
        self.level_total: dict[tuple, Decimal] = {}
        self._largest: dict[tuple, "Order | None"] = {}

    # --- building -------------------------------------------------------------
    @classmethod
    def from_snapshot(cls, rows: list) -> "NodeBook":
        """rows: [oid, side, px, sz, user, time_ms] per order, bids best-first then asks best-first,
        each level in queue order."""
        b = cls()
        for r in rows:
            oid, side, px, sz, user, t = r
            o = Order(int(oid), side, dec(px), dec(sz), user, t)
            if o.px is None or o.sz is None or side not in ("B", "A"):
                raise BookError(f"malformed snapshot order {oid}")
            if o.oid in b.orders:
                raise BookError(f"duplicate oid {oid} in snapshot")
            b.orders[o.oid] = o
            b.levels[side].setdefault(o.px, {})[o.oid] = None
            k = (side, o.px)
            b.level_total[k] = b.level_total.get(k, ZERO) + o.sz
        return b

    def snapshot_rows(self) -> list[tuple]:
        """The book as comparable rows (order identity and state), for snapshot comparison."""
        return sorted((o.oid, o.side, o.px, o.sz, o.user, o.time_ms) for o in self.orders.values())

    # --- deltas ---------------------------------------------------------------
    def apply_delta(self, changes: list, block_time_ms: int | None) -> None:
        t = block_time_ms
        for c in changes:
            op = c.get("op")
            oid = int(c["oid"])
            if op == "add":
                if oid in self.orders:
                    raise BookError(f"add of resting oid {oid}")
                o = Order(oid, c["side"], dec(c["px"]), dec(c["sz"]), c.get("user"), c.get("time_ms"))
                if o.px is None or o.sz is None:
                    raise BookError(f"malformed add {oid}")
                self.orders[oid] = o
                self.levels[o.side].setdefault(o.px, {})[oid] = None
                self._level_changed(o.side, o.px, o.sz)
                self._record(t, o.side, o.px, "added", o.sz, oid)
            elif op == "size":
                o = self.orders.get(oid)
                if o is None:
                    raise BookError(f"size change of unknown oid {oid}")
                new, prev = dec(c["sz"]), dec(c.get("prev_sz"))
                filled = dec(c.get("maker_filled")) or ZERO
                if new is None:
                    raise BookError(f"malformed size change {oid}")
                prev = o.sz if prev is None else prev
                if new < prev:
                    dec_by = prev - new
                    absorbed = min(filled, dec_by)
                    if absorbed > 0:
                        self._record(t, o.side, o.px, "absorbed", absorbed, oid)
                    if dec_by - absorbed > 0:
                        self._record(t, o.side, o.px, "unknown_decrease", dec_by - absorbed, oid)
                elif new > prev:
                    self._record(t, o.side, o.px, "unknown_increase", new - prev, oid)
                self._level_changed(o.side, o.px, new - o.sz)
                o.sz = new
            elif op == "remove":
                o = self.orders.pop(oid, None)
                if o is None:
                    raise BookError(f"remove of unknown oid {oid}")
                lvl = self.levels[o.side].get(o.px)
                if lvl is not None:
                    lvl.pop(oid, None)
                    if not lvl:
                        del self.levels[o.side][o.px]
                self._level_changed(o.side, o.px, -o.sz)
                prev = dec(c.get("prev_sz"))
                prev = o.sz if prev is None else prev
                filled = dec(c.get("maker_filled")) or ZERO
                absorbed = min(filled, prev)
                if absorbed > 0:
                    self._record(t, o.side, o.px, "absorbed", absorbed, oid)
                rest = prev - absorbed
                if rest > 0:
                    pulled = c.get("status") == "canceled" and filled == 0
                    self._record(t, o.side, o.px, "pulled" if pulled else "unknown_removal", rest, oid)
            else:
                raise BookError(f"unknown op {op!r}")
        if t is not None:
            self._prune(t)

    def _level_changed(self, side: str, px: Decimal, size_delta: Decimal) -> None:
        k = (side, px)
        if px in self.levels[side]:
            self.level_total[k] = self.level_total.get(k, ZERO) + size_delta
        else:
            self.level_total.pop(k, None)
        self._largest.pop(k, None)

    def largest_at(self, side: str, px: Decimal) -> "Order | None":
        """The largest resting order at a level (tie: earliest time, then lowest oid), cached until the level changes."""
        k = (side, px)
        if k in self._largest:
            return self._largest[k]
        best = None
        for oid in self.levels[side].get(px) or {}:
            o = self.orders[oid]
            if best is None or o.sz > best.sz or (o.sz == best.sz and (_t(o.time_ms), o.oid) < (_t(best.time_ms), best.oid)):
                best = o
        self._largest[k] = best
        return best

    def _record(self, t, side, px, label, size, oid) -> None:
        if t is not None:
            self.changes.append((t, side, px, label, size, oid))

    def _prune(self, now_ms: int) -> None:
        cut = now_ms - CHANGE_RETENTION_MS
        while self.changes and self.changes[0][0] < cut:
            self.changes.popleft()

    # --- reads ----------------------------------------------------------------
    def sorted_levels(self, side: str) -> list[Decimal]:
        return sorted(self.levels[side], reverse=(side == "B"))

    def best(self, side: str) -> Decimal | None:
        lv = self.levels[side]
        if not lv:
            return None
        return max(lv) if side == "B" else min(lv)

    def is_crossed(self) -> bool:
        bb, ba = self.best("B"), self.best("A")
        return bb is not None and ba is not None and bb >= ba

    def mid(self) -> Decimal | None:
        bb, ba = self.best("B"), self.best("A")
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def level_stats(self, side: str, px: Decimal) -> dict:
        oids = self.levels[side].get(px) or {}
        total, count = ZERO, 0
        largest: Order | None = None
        oldest: int | None = None
        for oid in oids:
            o = self.orders[oid]
            total += o.sz
            count += 1
            if largest is None or o.sz > largest.sz or (
                    o.sz == largest.sz and (_t(o.time_ms), o.oid) < (_t(largest.time_ms), largest.oid)):
                largest = o
            if o.time_ms is not None and (oldest is None or o.time_ms < oldest):
                oldest = o.time_ms
        return {"px": px, "total_sz": total, "orders": count,
                "largest_order_sz": largest.sz if largest else None,
                "largest_order_time_ms": largest.time_ms if largest else None,
                "largest_order_oid": largest.oid if largest else None,
                "largest_order_user": largest.user if largest else None,
                "oldest_order_time_ms": oldest}

    def top_levels(self, side: str, n: int) -> list[dict]:
        return [self.level_stats(side, px) for px in self.sorted_levels(side)[:n]]

    def changes_since(self, since_ms: int) -> list[tuple]:
        return [c for c in self.changes if c[0] > since_ms]


def _t(t):
    return t if t is not None else 1 << 62
