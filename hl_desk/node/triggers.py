"""Waiting stop and take-profit orders of one market (PROTOCOL.md 4.4), and the stop bands (step 16).

This is conditional interest: orders that do nothing unless the price reaches them. Trailing stops are
not here (the engine leaves them out, carve-out C-2) and are reported as excluded with the engine's
count. A whole-position TP/SL (position_tpsl) has no stated size ("0" until it fires): it is counted,
its size unknown, never treated as zero size or dropped.

Bands (step 16, Kevin Nov 8): every waiting trigger lands in exactly one place: a band row inside the
range, "below" or "above" the range, or "no reference price" when the book has no mid. Each band shows
its order count beside its size, and when one order makes up most of a band's stated size
(DOMINANT_SHARE), the band says so with that order's size and share.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .proto import dec

ZERO = Decimal(0)
DOMINANT_SHARE = Decimal("0.5")


@dataclass(slots=True)
class Trigger:
    oid: int
    side: str           # side the order trades when it fires
    kind: str           # "stop" or "take_profit"
    trigger_px: Decimal
    sz: Decimal         # "0" for a whole-position TP/SL (size unknown)
    position_tpsl: bool
    user: str
    time_ms: int | None


class TriggerError(Exception):
    pass


class NodeTriggers:
    def __init__(self) -> None:
        self.orders: dict[int, Trigger] = {}
        self._floors: dict[tuple, Decimal] = {}     # (oid, width) -> band floor, computed once per order
        self.excluded = {"trailing": None, "unclassified": None}
        self.through_seq: int | None = None

    @classmethod
    def from_snapshot(cls, rows: list, excluded: dict | None) -> "NodeTriggers":
        t = cls()
        for r in rows:
            oid, side, kind, tpx, sz, ptpsl, user, tm = r
            g = Trigger(int(oid), side, kind, dec(tpx), dec(sz), bool(ptpsl), user, tm)
            if g.trigger_px is None or g.sz is None or kind not in ("stop", "take_profit"):
                raise TriggerError(f"malformed trigger {oid}")
            if g.oid in t.orders:
                raise TriggerError(f"duplicate trigger oid {oid}")
            t.orders[g.oid] = g
        if isinstance(excluded, dict):
            t.excluded = {"trailing": excluded.get("trailing"), "unclassified": excluded.get("unclassified")}
        return t

    def snapshot_rows(self) -> list[tuple]:
        return sorted((g.oid, g.side, g.kind, g.trigger_px, g.sz, g.position_tpsl, g.user, g.time_ms)
                      for g in self.orders.values())

    def apply_delta(self, changes: list, excluded: dict | None) -> None:
        for c in changes:
            op = c.get("op")
            oid = int(c["oid"])
            if op == "add":
                if oid in self.orders:
                    raise TriggerError(f"add of waiting oid {oid}")
                g = Trigger(oid, c["side"], c["kind"], dec(c["trigger_px"]), dec(c["sz"]),
                            bool(c.get("position_tpsl")), c.get("user"), c.get("time_ms"))
                if g.trigger_px is None or g.sz is None:
                    raise TriggerError(f"malformed trigger add {oid}")
                self.orders[oid] = g
            elif op == "remove":
                if self.orders.pop(oid, None) is None:
                    raise TriggerError(f"remove of unknown trigger {oid}")
                for k in [k for k in self._floors if k[0] == oid]:
                    del self._floors[k]
            else:
                raise TriggerError(f"unknown op {op!r}")
        if isinstance(excluded, dict):
            self.excluded = {"trailing": excluded.get("trailing"), "unclassified": excluded.get("unclassified")}

    # --- bands (step 16) -----------------------------------------------------
    def bands(self, ref_px: Decimal | None, width: Decimal, range_bps: int) -> dict:
        """Every waiting trigger in exactly one place; each band's count beside its size and the size check."""
        rows: dict[tuple, dict] = {}
        outside = {"below": _bucket(), "above": _bucket(), "no_reference_price": _bucket()}
        lo = hi = None
        if ref_px is not None:
            # The range is trigger prices within range_bps of the reference price (PROTOCOL.md 4.5);
            # bands are fixed [k*width, (k+1)*width) buckets of the triggers inside it.
            lo = ref_px * (1 - Decimal(range_bps) / 10_000)
            hi = ref_px * (1 + Decimal(range_bps) / 10_000)
        for g in self.orders.values():
            if ref_px is None:
                _add(outside["no_reference_price"], g)
                continue
            if g.trigger_px < lo:
                _add(outside["below"], g)
                continue
            if g.trigger_px > hi:
                _add(outside["above"], g)
                continue
            fk = (g.oid, width)
            b_lo = self._floors.get(fk)
            if b_lo is None:
                b_lo = self._floors[fk] = _floor_to(g.trigger_px, width)
            key = (b_lo, g.kind, g.side)
            r = rows.get(key)
            if r is None:
                r = rows[key] = _bucket() | {"lo": b_lo, "hi": b_lo + width, "kind": g.kind, "side": g.side}
            _add(r, g)
        out_rows = []
        for key in sorted(rows, key=lambda k: (-k[0], k[1], k[2])):
            out_rows.append(_finish(rows[key]))
        total = len(self.orders)
        placed = sum(r["orders"] for r in out_rows) + sum(b["orders"] for b in outside.values())
        return {"ref_px": ref_px, "width": width, "range_bps": range_bps, "lo": lo, "hi": hi,
                "rows": out_rows, "outside": {k: _finish(v) for k, v in outside.items()},
                "waiting_total": total, "accounted": placed, "excluded": dict(self.excluded)}


def _floor_to(px: Decimal, width: Decimal) -> Decimal:
    return (px / width).to_integral_value(rounding=ROUND_FLOOR) * width


def _bucket() -> dict:
    return {"orders": 0, "sized_orders": 0, "sz": ZERO, "position_tpsl_orders": 0, "_largest": None}


def _add(b: dict, g: Trigger) -> None:
    b["orders"] += 1
    if g.position_tpsl:
        b["position_tpsl_orders"] += 1
        return
    b["sized_orders"] += 1
    b["sz"] += g.sz
    lg = b["_largest"]
    if lg is None or g.sz > lg.sz or (g.sz == lg.sz and g.oid < lg.oid):
        b["_largest"] = g


def _finish(b: dict) -> dict:
    lg = b.pop("_largest")
    share = (lg.sz / b["sz"]) if (lg is not None and b["sz"] > 0) else None
    b["largest"] = None if lg is None else {"sz": lg.sz, "user": lg.user, "oid": lg.oid, "share": share}
    b["dominated"] = share is not None and share > DOMINANT_SHARE
    return b
