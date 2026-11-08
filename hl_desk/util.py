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


def na(reason: str, unit: str | None = None) -> dict:
    return val(None, unit, reason)


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
