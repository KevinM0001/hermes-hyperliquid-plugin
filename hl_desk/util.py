"""Small helpers shared by the engine: null-with-reason values, parsing, time formatting."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def val(value: Any, unit: str | None = None, reason: str | None = None) -> dict:
    """A metric value. When value is None, reason says why (never a silent zero)."""
    out: dict = {"value": value}
    if unit is not None:
        out["unit"] = unit
    if value is None:
        out["reason"] = reason or "unavailable"
    return out


def median(values):
    """The median of a non-empty collection: the middle value, or the mean of the two middle values when there is an
    even number (Decimals stay exact)."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("median of no values")
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def na(reason: str, unit: str | None = None) -> dict:
    return val(None, unit, reason)


def unverified_receipt(field: str, received_age_s: float) -> str:
    """The reason a value with no source timestamp is served with: only its receipt age is known."""
    return f"{field} carries no source timestamp; a changed value was received {received_age_s:.1f} s ago"


def mark_unverified(m: dict, field: str, received_age_s: float) -> dict:
    """A metric served marked unverified (schema 1.3.0, context values): the value stays, with status, the reason
    and the receipt age of the last changed value in seconds. A null value keeps its own reason."""
    if not isinstance(m, dict) or m.get("value") is None:
        return m
    return m | {"status": "unverified", "reason": unverified_receipt(field, received_age_s),
                "received_age_s": round(received_age_s, 3)}


def fnum(x: Any) -> float | None:
    """Parse a Hyperliquid numeric string or number; None when absent or not finite."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def iso_ms(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rnd(x: float | None, nd: int = 6) -> float | None:
    if x is None:
        return None
    return round(x, nd)


def dex_of(coin: str) -> str:
    """HIP-3 markets are named '{dex}:{coin}'; the default perp dex is ''."""
    return coin.split(":", 1)[0] if ":" in coin else ""
