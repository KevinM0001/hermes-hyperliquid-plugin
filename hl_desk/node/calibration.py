"""Node-mode burst thresholds and the reference that live node bursts are ranked against.

The calibration is a dated, versioned file measured on Kevin's node fills (Nov 3 to 7) with the node burst rule
(`bursts.py`): for each market, the grid threshold whose achieved rate is nearest the target of 10 bursts a covered
day, with that achieved rate. Its reference holds every eligible 60 s sum, per market and side, sampled every 15 s
over the same days. Both ship in this package; neither changes at runtime. A manual threshold per market
(setting `node_burst_min_usd`) overrides the automatic one; removing it, or setting it to "auto", restores it.

Public mode doesn't use any of this: its bursts keep the public-trades rule and `burst_min_usd`.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / "calibration"
CALIBRATION_FILE = HERE / "burst-calibration-2026-11-08.json"


class Calibration:
    """The loaded calibration (thresholds) and, separately, its reference (ranks). Either can be missing: then the
    automatic threshold, or the rank, is unavailable with the reason, never estimated."""

    def __init__(self, path: Path = CALIBRATION_FILE) -> None:
        self.path = path
        self.data: dict | None = None
        self.error: str | None = None
        self.reference: dict | None = None
        self.reference_error: str | None = None
        try:
            self.data = json.loads(path.read_text())
            if not isinstance(self.data.get("markets"), dict):
                raise ValueError("no markets in the calibration file")
        except (OSError, ValueError) as exc:
            self.data, self.error = None, f"the burst calibration file isn't loaded ({path.name}: {exc})"
            self.reference_error = self.error
            return
        ref = path.parent / self.data.get("reference_file", "")
        try:
            raw = gzip.decompress(ref.read_bytes())
            want = ((self.data.get("source") or {}).get("reference") or {}).get("sha256")
            if want and hashlib.sha256(raw).hexdigest() != want:
                raise ValueError("its sha256 doesn't match the calibration's")
            r = json.loads(raw)
            self.reference = {m: {"buying": x["buying_sums_usd_sorted"], "selling": x["selling_sums_usd_sorted"]}
                              for m, x in r["markets"].items()}
        except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
            self.reference, self.reference_error = None, f"the burst reference file isn't loaded ({ref.name}: {exc})"

    # --- labels -------------------------------------------------------------------
    @property
    def measured(self) -> str:
        return (self.data or {}).get("measured_label") or "the calibration period"

    def automatic(self, market: str) -> dict | None:
        """The automatic threshold for a market, with its label: 'about N a day' when the achieved rate is within the
        tolerance of the target, otherwise the achieved rate itself."""
        if self.data is None:
            return None
        m = self.data["markets"].get(market)
        if m is None:
            return None
        target, achieved = self.data["target_per_day"], m["achieved_per_day"]
        if m["near_target"]:
            label = f"automatic: about {target:g} a day, measured {self.measured}"
        else:
            label = (f"automatic: {achieved:.1f} a day, measured {self.measured} (the target of {target:g} a day "
                     f"has no threshold within {self.data['near_tolerance'] * 100:.0f}%)")
        return {"usd": float(m["threshold_usd"]), "source": "automatic", "label": label,
                "version": self.data["version"], "dated": self.data["dated"], "measured_utc": self.data["measured_utc"],
                "target_per_day": target, "achieved_per_day": achieved, "near_target": m["near_target"]}

    def threshold(self, market: str, manual: dict) -> dict:
        """The threshold node-mode bursts use for a market: the manual one when set, else the automatic one."""
        v = (manual or {}).get(market)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return {"usd": float(v), "source": "manual", "label": f"manual: ${v:,.0f} (setting node_burst_min_usd)"}
        auto = self.automatic(market)
        if auto is not None:
            return auto
        why = self.error or f"the calibration has no threshold for {market}"
        return {"usd": None, "source": "unavailable", "label": f"no threshold: {why}; set one in node_burst_min_usd",
                "reason": why}

    # --- rank -----------------------------------------------------------------------
    def rank(self, market: str, side: str, total_usd: float) -> dict:
        """Upper-tail frequency against the reference: count(reference sums >= the burst's sum) / reference count,
        ties counted as at or above. Unavailable, never estimated, when the reference isn't loaded."""
        if self.reference is None:
            return {"available": False, "reason": self.reference_error or "the burst reference isn't loaded"}
        sums = (self.reference.get(market) or {}).get(side)
        if not sums:
            return {"available": False, "reason": f"the burst reference has no {side} sums for {market}"}
        at_or_above = len(sums) - bisect.bisect_left(sums, total_usd)
        frac = at_or_above / len(sums)
        pct = frac * 100
        shown = f"{pct:.2g}" if pct < 1 else f"{pct:.1f}".rstrip("0").rstrip(".")
        label = (f"a top {shown}% minute, {self.measured}" if at_or_above else
                 f"larger than every minute in the reference, {self.measured}")
        return {"available": True, "upper_tail": frac, "top_pct": pct, "at_or_above": at_or_above,
                "reference_count": len(sums), "label": label,
                "reference": f"{self.data['source']['reference']['file']} ({self.measured}, 60 s sums every 15 s)"}


_LOADED: Calibration | None = None


def load() -> Calibration:
    """The shipped calibration, loaded once."""
    global _LOADED
    if _LOADED is None:
        _LOADED = Calibration()
    return _LOADED
