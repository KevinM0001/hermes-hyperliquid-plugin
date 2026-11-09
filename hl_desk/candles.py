"""Candle history per interval from Hyperliquid's candleSnapshot, plus the live in-progress candle from the websocket.

One CandleStore per collector, shared by every thread that touches candles: the collector's websocket loop
(candle messages), the fetch workers (candleSnapshot answers) and the readers (the /candles route, the hl_candles
tool, the snapshot's chart.intervals summary). Every public method takes the store's lock; the network fetch runs
on the store's own worker threads, never on the collector's event loop, so a slow or failing fetch can't delay a
snapshot. Numbers are parsed from the venue's strings and nothing is invented: volume is the venue's v (base unit)
only, and the in-progress candle, whose updates carry no source time (only its open and close times), is served
as unverified with its receipt age, never as fresh.

Fetch policy (docs/ENDPOINTS.md): a (market, interval) is fetched on first request (the most recent 5000 candles,
all the venue keeps), refetched only when its newest closed candle from candleSnapshot is older than one interval
(incrementally, from that candle on) and at most once per 30 s. The websocket candle subscription is requested on
first request and never dropped.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import info_client
from .util import dex_of, fnum, iso_ms

INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
               "1d": 86_400_000}
MAX_CANDLES = 5000               # "Only the most recent 5000 candles are available" (info endpoint docs)
DEFAULT_LIMIT = 500
REFETCH_MIN_S = 30.0
FETCH_TIMEOUT_S = 15.0
BASE_WEIGHT, ITEMS_PER_WEIGHT = 20, 60     # rate-limit weight: 20 per request plus 1 per 60 candles returned
VENUE_NOTE = "the venue keeps only the most recent 5000 candles per interval"
SOURCE = ("Hyperliquid info endpoint https://api.hyperliquid.xyz/info (candleSnapshot) for closed candles and public "
          "websocket wss://api.hyperliquid.xyz/ws (candle channel) for the in-progress candle")
THREAD_PREFIX = "hyperliquid-desk-candles"


def parse_candle(raw) -> dict | None:
    """A venue candle {t, T, s, i, o, h, l, c, v, n} with its numbers parsed; None when malformed."""
    if not isinstance(raw, dict):
        return None
    t, close = raw.get("t"), raw.get("T")
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (t, close)) or close < t:
        return None
    o, h, l, c, v = (fnum(raw.get(k)) for k in ("o", "h", "l", "c", "v"))
    if None in (o, h, l, c, v) or v < 0:
        return None
    n = raw.get("n")
    return {"time": t, "close_time": close, "open": o, "high": h, "low": l, "close": c, "volume_base": v,
            "trades": n if isinstance(n, int) and not isinstance(n, bool) else None}


def fetch_weight(items: int) -> int:
    return BASE_WEIGHT + math.ceil(items / ITEMS_PER_WEIGHT)


class _Series:
    """Everything held for one (market, interval)."""

    def __init__(self, market: str, interval: str) -> None:
        self.market, self.interval = market, interval
        self.closed: dict[int, dict] = {}            # open time -> candle
        self.closed_received: dict[int, int] = {}    # open time -> receipt (monotonic ns): the latest receipt wins
        self.current: dict | None = None             # the in-progress candle
        self.current_received_ms: int | None = None
        self.current_received_mono_ns: int | None = None
        self.current_source: str | None = None       # "websocket" or "candleSnapshot"
        self.fetched_at_ms: int | None = None        # receipt of the newest candleSnapshot answer
        self.fetch_started_mono_ns: int | None = None
        self.fetching = False
        self.fetches = 0
        self.last_fetch: dict | None = None
        self.last_error: dict | None = None
        self.snapshot_last_close_ms: int | None = None   # T of the newest closed candle a candleSnapshot gave
        self.ws_messages = 0
        self.ws_last_received_ms: int | None = None
        self.malformed = 0


class CandleStore:
    def __init__(self, info_url: str, fetch=None, executor=None, now=None, mode: str = "LIVE") -> None:
        self.info_url = info_url
        self.mode = mode
        self._fetch = fetch or (lambda market, interval, start_ms, end_ms:
                                info_client.fetch_candles(info_url, market, interval, start_ms, end_ms, FETCH_TIMEOUT_S))
        self._executor = executor          # anything with submit(fn, *args); a thread pool is made on first use
        self._now = now or (lambda: (int(time.time() * 1000), time.monotonic_ns()))
        self._lock = threading.RLock()
        self._series: dict[tuple[str, str], _Series] = {}
        self._wanted: list[tuple[str, str]] = []    # every pair ever requested: its subscription is (re)sent on connect
        self._pending: list[tuple[str, str]] = []   # requested since the current connection subscribed

    # --- requests (any thread; never blocks on the network) -------------------------------------------
    def request(self, market: str, interval: str) -> None:
        """Register the websocket subscription on first request and start a candleSnapshot fetch when the policy
        allows one: never fetched, or the newest closed candle is older than one interval; at most once per 30 s
        and never two at once."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"interval {interval!r} isn't supported; one of {', '.join(INTERVALS)}")
        now_ms, now_mono = self._now()
        with self._lock:
            key = (market, interval)
            s = self._series.get(key)
            if s is None:
                s = self._series[key] = _Series(market, interval)
                self._wanted.append(key)
                self._pending.append(key)
            why = self._fetch_reason(s, now_ms, now_mono)
            if why is None:
                return
            step = INTERVAL_MS[interval]
            if s.snapshot_last_close_ms is None:
                start = now_ms - MAX_CANDLES * step
            else:
                start = s.snapshot_last_close_ms + 1 - step   # from the newest closed candle on (T = t + step - 1); the merge dedupes it
            s.fetching, s.fetch_started_mono_ns = True, now_mono
            self._executor_or_pool().submit(self._run_fetch, market, interval, start, now_ms, why)

    def _fetch_reason(self, s: _Series, now_ms: int, now_mono: int) -> str | None:
        if s.fetching:
            return None
        if s.fetch_started_mono_ns is not None and (now_mono - s.fetch_started_mono_ns) / 1e9 < REFETCH_MIN_S:
            return None
        if s.fetched_at_ms is None:
            return "first fetch" if s.last_error is None else "retry after a failed fetch"
        if s.snapshot_last_close_ms is None:
            return "no closed candle received yet"
        if now_ms > s.snapshot_last_close_ms + INTERVAL_MS[s.interval]:
            return "the newest closed candle is older than one interval"
        return None

    def _executor_or_pool(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=THREAD_PREFIX)
        return self._executor

    def _run_fetch(self, market: str, interval: str, start_ms: int, end_ms: int, why: str) -> None:
        t0 = time.perf_counter()
        try:
            rows = self._fetch(market, interval, start_ms, end_ms)
            if not isinstance(rows, list):
                raise ValueError(f"candleSnapshot answered with {type(rows).__name__}, not a list")
        except Exception as exc:
            now_ms, _ = self._now()
            with self._lock:
                s = self._series[(market, interval)]
                s.fetching = False
                s.last_error = {"at_ms": now_ms, "at": iso_ms(now_ms), "reason": f"candleSnapshot failed: {type(exc).__name__}: {exc}"}
            return
        now_ms, now_mono = self._now()
        with self._lock:
            s = self._series[(market, interval)]
            s.fetching = False
            s.last_fetch = {"why": why, "start_ms": start_ms, "end_ms": end_ms, "items": len(rows),
                            "weight": fetch_weight(len(rows)), "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                            "at_ms": now_ms}
            self.merge_snapshot(market, interval, rows, now_ms, now_mono)

    # --- data in -------------------------------------------------------------------------------------------
    def merge_snapshot(self, market: str, interval: str, rows: list, received_ms: int, received_mono_ns: int) -> None:
        """Merge a candleSnapshot answer: a candle whose close time is in the future at receipt is the in-progress
        candle; the others are closed. Duplicates by open time keep the latest receipt; the store is cut to the
        most recent 5000."""
        with self._lock:
            s = self._series.get((market, interval))
            if s is None:
                s = self._series[(market, interval)] = _Series(market, interval)
            parsed = []
            for raw in rows:
                c = parse_candle(raw)
                if c is None:
                    s.malformed += 1
                else:
                    parsed.append(c)
            parsed.sort(key=lambda c: c["time"])
            newest_closed = None
            for c in parsed:
                if c["close_time"] >= received_ms:
                    self._apply_current(s, c, received_ms, received_mono_ns, "candleSnapshot")
                else:
                    self._close(s, c, received_mono_ns)
                    newest_closed = c["close_time"] if newest_closed is None else max(newest_closed, c["close_time"])
            if newest_closed is not None:
                s.snapshot_last_close_ms = (newest_closed if s.snapshot_last_close_ms is None
                                            else max(s.snapshot_last_close_ms, newest_closed))
            s.fetched_at_ms = received_ms
            s.fetches += 1
            s.last_error = None
            self._prune(s)

    def on_ws_candle(self, data, mono_ns: int, wall_ms: int) -> int:
        """A websocket candle message (one candle, or a list of them): the current candle of its (coin, interval).
        A newer open time closes the candle it replaces; an update of the same open time replaces it; an update of an
        older one goes to the closed candles. Messages for pairs never requested are ignored. Returns the applied count."""
        rows = data if isinstance(data, list) else [data]
        applied = 0
        with self._lock:
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                s = self._series.get((raw.get("s"), raw.get("i")))
                if s is None:
                    continue
                c = parse_candle(raw)
                if c is None:
                    s.malformed += 1
                    continue
                s.ws_messages += 1
                s.ws_last_received_ms = wall_ms
                self._apply_current(s, c, wall_ms, mono_ns, "websocket")
                applied += 1
        return applied

    def _apply_current(self, s: _Series, c: dict, wall_ms: int, mono_ns: int, source: str) -> None:
        cur = s.current
        if cur is not None and c["time"] < cur["time"]:
            self._close(s, c, mono_ns)
            return
        if cur is not None and c["time"] > cur["time"]:
            self._close(s, cur, s.current_received_mono_ns)   # over: a newer candle has started
        s.current, s.current_received_ms, s.current_received_mono_ns, s.current_source = c, wall_ms, mono_ns, source

    def _close(self, s: _Series, c: dict, received_mono_ns: int) -> None:
        prev = s.closed_received.get(c["time"])
        if prev is None or received_mono_ns >= prev:
            s.closed[c["time"]] = c
            s.closed_received[c["time"]] = received_mono_ns

    @staticmethod
    def _prune(s: _Series) -> None:
        if len(s.closed) > MAX_CANDLES:
            for t in sorted(s.closed)[:-MAX_CANDLES]:
                s.closed.pop(t, None)
                s.closed_received.pop(t, None)

    # --- websocket bookkeeping (the collector's loop) -----------------------------------------------------------
    @staticmethod
    def subscription(market: str, interval: str) -> dict:
        return {"type": "candle", "coin": market, "interval": interval}

    def subscriptions_on_connect(self) -> list[dict]:
        """Every subscription requested so far, to (re)send on a new connection; nothing stays pending after it."""
        with self._lock:
            self._pending.clear()
            return [self.subscription(m, i) for m, i in self._wanted]

    def take_pending(self) -> list[dict]:
        """Subscriptions requested since the current connection subscribed (sent once, then cleared)."""
        with self._lock:
            out = [self.subscription(m, i) for m, i in self._pending]
            self._pending.clear()
            return out

    def close(self) -> None:
        ex = self._executor
        if isinstance(ex, ThreadPoolExecutor):
            ex.shutdown(wait=False, cancel_futures=True)

    # --- data out ------------------------------------------------------------------------------------------
    def view(self, market: str, interval: str, limit: int = DEFAULT_LIMIT) -> dict:
        """The /candles answer: the most recent `limit` candles (closed ones, then the in-progress candle) with the
        history's bounds and the in-progress candle's receipt age computed now; or the loading / error forms."""
        limit = max(1, min(MAX_CANDLES, int(limit)))
        now_ms, now_mono = self._now()
        with self._lock:
            s = self._series.get((market, interval))
            base = {"ok": False, "market": market, "interval": interval}
            if s is None or s.fetched_at_ms is None:
                if s is not None and s.last_error is not None and not s.fetching:
                    wait = (REFETCH_MIN_S - (now_mono - s.fetch_started_mono_ns) / 1e9) if s.fetch_started_mono_ns else 0
                    return base | {"state": "error", "reason": s.last_error["reason"], "failed_at": s.last_error["at"],
                                   "next_retry_s": round(max(0.0, wait), 1)}
                return base | {"state": "loading", "reason": "the first candleSnapshot fetch for this market and interval "
                                                              "is running; ask again in a second"}
            closed = [dict(c, state="closed") for t, c in sorted(s.closed.items())]
            cur = s.current
            wanted = limit - 1 if cur is not None else limit        # closed candles that fit beside the in-progress one
            candles = closed[-wanted:] if wanted > 0 else []
            if cur is not None:
                candles = candles + [dict(cur, state="in_progress")]
            if cur is not None:
                age = (now_mono - s.current_received_mono_ns) / 1e9
                reason = f"candle updates carry no source timestamp; received {age:.1f} s ago"
                if cur["close_time"] < now_ms:
                    reason += "; its close time has passed and the venue's final values haven't arrived"
                in_progress = {"status": "unverified", "reason": reason, "received_age_s": round(age, 3),
                               "received_at": iso_ms(s.current_received_ms), "from": s.current_source,
                               "time": cur["time"], "close_time": cur["close_time"]}
            else:
                in_progress = {"status": "unavailable", "received_age_s": None,
                               "reason": "no in-progress candle received yet (no websocket candle update, and the newest "
                                         "candleSnapshot candle had already closed)"}
            n_closed = sum(1 for c in candles if c["state"] == "closed")
            history = {"count": n_closed, "available": len(closed), "first_ms": candles[0]["time"] if candles else None,
                       "last_closed_ms": closed[-1]["time"] if closed else None, "fetched_at_ms": s.fetched_at_ms,
                       "fetched_at": iso_ms(s.fetched_at_ms), "fetches": s.fetches, "refetching": s.fetching,
                       "note": VENUE_NOTE}
            if s.malformed:
                history["malformed_dropped"] = s.malformed
            if s.last_error is not None:
                history["last_error"] = dict(s.last_error)
            prov = {"mode": self.mode, "source": SOURCE, "market": market, "dex": dex_of(market), "interval": interval,
                    "served_at_ms": now_ms, "served_at": iso_ms(now_ms),
                    "websocket": {"subscription": self.subscription(market, interval), "messages": s.ws_messages,
                                  "last_received_at": iso_ms(s.ws_last_received_ms)},
                    "last_fetch": dict(s.last_fetch) if s.last_fetch else None,
                    "note": ("closed candles come from candleSnapshot, refetched only when the newest closed candle is older "
                             "than one interval and at most once per 30 s; the in-progress candle is the latest update "
                             "received and is never fresh")}
            return {"ok": True, "market": market, "interval": interval, "candles": candles, "history": history,
                    "in_progress": in_progress, "source": SOURCE, "provenance": prov}

    def summary(self, market: str) -> dict:
        """chart.intervals for a market's snapshot: the intervals loaded so far, with counts, bounds and the in-progress
        candle's status and receipt time (never an age: the reader computes it when it serves the snapshot)."""
        with self._lock:
            out = {}
            for (m, i), s in self._series.items():
                if m != market or s.fetched_at_ms is None:
                    continue
                ts = sorted(s.closed)
                out[i] = {"count": len(ts), "first_ms": ts[0] if ts else None, "last_ms": ts[-1] if ts else None,
                          "in_progress_status": "unverified" if s.current is not None else "unavailable",
                          "in_progress_received_ms": s.current_received_ms if s.current is not None else None,
                          "fetched_at_ms": s.fetched_at_ms}
            return out


__all__ = ["CandleStore", "DEFAULT_LIMIT", "INTERVALS", "INTERVAL_MS", "MAX_CANDLES", "SOURCE", "VENUE_NOTE",
           "fetch_weight", "parse_candle"]
