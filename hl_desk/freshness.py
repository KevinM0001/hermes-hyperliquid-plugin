"""Freshness tracking, kept separate from provenance.

A value is fresh only when BOTH hold (schema freshness, decision of Nov 8):
- its feed is still advancing: the field's progress signal advanced within
  its receipt-age limit, measured on the local monotonic clock; and
- its source timestamp is recent: local_now - source_time, widened by the
  local clock's known error bound (hl_desk.clock), is within its source-age
  limit, and isn't ahead of local time by more than that bound allows.
A delayed backlog that keeps advancing is "delayed"; with no known clock
bound, or for a field with no source timestamp (activeAssetCtx), it is
"unverified". Status is re-evaluated whenever a snapshot is built, so a
silent socket still turns data stale. A connected socket, a pong or a
repeated identical message never counts as progress.
"""

from __future__ import annotations

from .util import iso_ms

NS = 1_000_000_000


class FieldTracker:
    """basis: 'source_time_progress' (l2Book, trades), 'change_on_receipt' (activeAssetCtx), 'none' (bbo)."""

    FUTURE_TOLERANCE_S = 1.0

    def __init__(self, name: str, basis: str, max_age_s: float | None, when_exceeded: str,
                 progress_signal: str, exceeded_reason: str | None = None,
                 source_max_age_s: float | None = None) -> None:
        self.name = name
        self.basis = basis
        self.max_age_s = max_age_s
        self.source_max_age_s = source_max_age_s
        self.when_exceeded = when_exceeded
        self.progress_signal = progress_signal
        self.exceeded_reason = exceeded_reason
        self.reset()

    def reset(self) -> None:
        self.source_ms: int | None = None
        self.watermark_ms: int | None = None
        self.progress_mono_ns: int | None = None
        self.last_msg_mono_ns: int | None = None
        self.messages = 0
        self.non_progress = 0
        self.restart_reason: str | None = None

    def restart(self, reason: str) -> None:
        """Coverage restarted inside a connection (a gap): earlier progress no longer counts, so the field is
        warming up again until it progresses."""
        self.source_ms = None
        self.watermark_ms = None
        self.progress_mono_ns = None
        self.restart_reason = reason

    def on_progress(self, mono_ns: int, source_ms: int | None = None, watermark_ms: int | None = None) -> None:
        self.restart_reason = None
        self.messages += 1
        self.last_msg_mono_ns = mono_ns
        self.progress_mono_ns = mono_ns
        if source_ms is not None:
            self.source_ms = source_ms
        if watermark_ms is not None:
            self.watermark_ms = watermark_ms

    def on_no_progress(self, mono_ns: int) -> None:
        self.messages += 1
        self.non_progress += 1
        self.last_msg_mono_ns = mono_ns

    def evaluate(self, now_ns: int, link: str, now_wall_ms: int | None = None, clock=None) -> dict:
        """link is the collector's state for this market: 'connected', 'disconnected', 'stopped' or 'replay'.
        now_wall_ms and clock (hl_desk.clock.ClockBound) judge source recency."""
        age = None if self.progress_mono_ns is None else (now_ns - self.progress_mono_ns) / NS
        last_msg_age = None if self.last_msg_mono_ns is None else (now_ns - self.last_msg_mono_ns) / NS
        out = {
            "field": self.name,
            "progress_signal": self.progress_signal,
            "source_time_ms": self.source_ms,
            "source_time": iso_ms(self.source_ms),
            "watermark_ms": self.watermark_ms,
            "progress_age_s": None if age is None else round(age, 3),
            "last_message_age_s": None if last_msg_age is None else round(last_msg_age, 3),
            "max_age_s": self.max_age_s,
            "messages": self.messages,
            "messages_without_progress": self.non_progress,
        }
        if link == "stopped":
            status, reason = "stopped", "collector is not running"
        elif link == "disconnected":
            status, reason = "disconnected", "collector has no connection"
        elif self.messages == 0:
            status, reason = "warming_up", "nothing received on this connection yet"
        elif self.basis == "none":
            status, reason = "unverified", self.exceeded_reason
        elif age is None:
            status, reason = "warming_up", (f"no verified progress since {self.restart_reason}" if self.restart_reason
                                            else "no verified progress on this connection yet")
        elif self.basis == "change_on_receipt" and self.max_age_s is not None and age <= self.max_age_s:
            status, reason = "unverified", (f"{self.name} carries no source timestamp, so its source age is unknown "
                                            f"(a changed value was received {age:.1f} s ago)")
        elif self.max_age_s is not None and age <= self.max_age_s:
            status, reason = self._source_recency(now_wall_ms, clock, out)
        else:
            status = self.when_exceeded
            reason = self.exceeded_reason or (
                f"no progress for {age:.1f} s (max age {self.max_age_s} s)")
            if self.exceeded_reason:
                reason = f"{self.exceeded_reason} (no progress for {age:.1f} s, max age {self.max_age_s} s)"
        out["status"] = status
        out["reason"] = reason
        return out

    def _source_recency(self, now_wall_ms, clock, out: dict) -> tuple[str, str | None]:
        """Receipt progress is recent; now check that the source timestamp itself is recent."""
        if self.source_max_age_s is None:
            return "fresh", None
        bound, basis = (None, "no clock relationship supplied") if clock is None else clock.get()
        out["source_max_age_s"] = self.source_max_age_s
        out["clock_bound_s"] = bound
        out["clock_bound_basis"] = basis
        if now_wall_ms is None or self.source_ms is None:
            return "unverified", "no source timestamp to judge recency"
        est = (now_wall_ms - self.source_ms) / 1000
        out["source_age_s"] = round(est, 3)
        if bound is None:
            return "unverified", f"source recency can't be established: {basis}"
        if est < -(bound + self.FUTURE_TOLERANCE_S):
            return "unverified", (f"source time is {-est:.1f} s ahead of local time, beyond the clock bound "
                                  f"({bound:.3f} s): the clock relationship doesn't hold")
        if est - bound > self.source_max_age_s:
            return "delayed", (f"source time is {est:.1f} s old (limit {self.source_max_age_s} s, clock bound "
                               f"{bound:.3f} s): the feed is advancing but behind")
        if est + bound > self.source_max_age_s:
            return "unverified", (f"source age {est:.1f} s is within the clock bound ({bound:.3f} s) of its "
                                  f"{self.source_max_age_s} s limit")
        return "fresh", None
