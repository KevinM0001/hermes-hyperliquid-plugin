"""Recordings and their replay (PROTOCOL.md section 9; handoff step 12).

A recording (written by the engine's recorder) is one header line {"recording": {...}}, then
{"rx_ms": ..., "msg": <feed line as received>} or {"gap": {...}} lines, then {"recording_end": {...}}.
It is partial history: each market starts at its first full book + trigger snapshot pair.

The player serves a recording into a FeedClient in mode REPLAY on its own replay clock: it paces lines by
block_time_ms at the chosen speed, starts a NEW replay_session at every start, seek and restart (the
client retires the old one for good), adds "mode":"REPLAY" and "replay_session" to every line and leaves
every original field unchanged. Its status (playing, paused or ended; position; speed; the recording's
span and date) rides on heartbeats. At the end it stays REPLAY: an ended replay never becomes live.
Replay is only ever started explicitly; it is never a fallback for a live source.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass


@dataclass
class Recording:
    path: str
    header: dict
    lines: list            # (block_time_ms or None, market or None, kind or None, msg dict) in file order
    gaps: list
    trailer: dict | None
    start_ms: int | None
    end_ms: int | None
    pair_starts: dict      # market -> [(block_time_ms, line index of the book snapshot's part 1)]

    @classmethod
    def load(cls, path: str) -> "Recording":
        opener = gzip.open if path.endswith(".gz") else open
        header, trailer, lines, gaps = None, None, [], []
        pair_starts: dict[str, list] = {}
        with opener(path, "rt", encoding="utf-8") as fh:
            for raw in fh:
                o = json.loads(raw)
                if "recording" in o:
                    header = o["recording"]
                elif "recording_end" in o:
                    trailer = o["recording_end"]
                elif "gap" in o:
                    gaps.append(o["gap"])
                    lines.append((None, None, "gap", o["gap"]))
                elif "msg" in o:
                    m = o["msg"]
                    bt = m.get("block_time_ms")
                    lines.append((bt, m.get("market"), m.get("kind") or m.get("control"), m))
                    body = m.get("body") or {}
                    if m.get("kind") == "book_snapshot" and body.get("part") == 1:
                        pair_starts.setdefault(m["market"], []).append((bt, len(lines) - 1))
        if header is None:
            raise ValueError(f"{path}: no recording header")
        times = [t for t, *_ in lines if isinstance(t, int)]
        return cls(path, header, lines, gaps, trailer, min(times) if times else None, max(times) if times else None,
                   pair_starts)


def _session_id() -> str:
    return os.urandom(16).hex()


class ReplayPlayer:
    """Serves a Recording into a REPLAY FeedClient on a replay clock. Call tick() regularly (wall time)."""

    def __init__(self, recording: Recording, client, speed: float = 1.0, clock=time.monotonic) -> None:
        self.rec = recording
        self.client = client
        self.clock = clock
        self.speed = float(speed)
        self.state = "stopped"
        self.session: str | None = None
        self.index = 0
        self.position_ms: int | None = None
        self._anchor_wall = None       # (wall seconds, position ms) the clock runs from while playing
        self.skip_before: dict[str, int] = {}
        self.last_heartbeat = 0.0
        self.sessions_started = 0

    # --- control --------------------------------------------------------------
    def start(self) -> None:
        """Start (or restart) from the beginning: a new replay session."""
        starts = {m: pairs[0][1] for m, pairs in self.rec.pair_starts.items() if pairs}
        self._open_session(self.rec.start_ms, starts, min(starts.values()) if starts else 0)

    def seek(self, target_ms: int) -> None:
        """A new replay session from each market's latest snapshot pair at or before target_ms."""
        starts = {}
        for m, pairs in self.rec.pair_starts.items():
            before = [i for t, i in pairs if t is not None and t <= target_ms]
            starts[m] = before[-1] if before else pairs[0][1]
        first = min(starts.values()) if starts else 0
        self._open_session(target_ms, starts, first)

    def pause(self) -> None:
        if self.state == "playing":
            self.position_ms = self._now_position()
            self.state = "paused"
            self._heartbeat(force=True)

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "playing"
            self._anchor_wall = (self.clock(), self.position_ms)

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        if self.state == "playing":
            self.position_ms = self._now_position()
            self._anchor_wall = (self.clock(), self.position_ms)
        self.speed = float(speed)
        self._heartbeat(force=True)

    def _open_session(self, position_ms, market_starts: dict, index: int) -> None:
        self.session = _session_id()
        self.sessions_started += 1
        self.skip_before = dict(market_starts)
        self.index = index
        self.position_ms = position_ms
        self.state = "playing"
        self._anchor_wall = (self.clock(), position_ms)
        p = self.rec.header.get("producer") or {}
        markets = {}
        for m in self.rec.header.get("markets") or []:
            i = market_starts.get(m)
            seq = None
            if i is not None and i < len(self.rec.lines):
                seq = (self.rec.lines[i][3] or {}).get("seq")
            markets[m] = {"start_seq": seq, "snapshot": "in_stream"}
        hello = {"v": 2, "control": "hello", "epoch": p.get("epoch"), "session": p.get("session"),
                 "started_ms": p.get("started_ms"), "pub_ms": p.get("started_ms"), "mode": "REPLAY",
                 "markets": markets, "limits": self.rec.header.get("limits") or {},
                 "labels": self.rec.header.get("labels") or {},
                 "allowlist_version": self.rec.header.get("allowlist_version")}
        self._send(hello)
        self._heartbeat(force=True)

    # --- clock ----------------------------------------------------------------
    def _now_position(self) -> int | None:
        if self.state != "playing" or self._anchor_wall is None:
            return self.position_ms
        w0, p0 = self._anchor_wall
        return int(p0 + (self.clock() - w0) * 1000 * self.speed)

    def tick(self) -> int:
        """Deliver every line due at the current replay position; returns how many were delivered."""
        if self.state != "playing":
            self._heartbeat()
            return 0
        pos = self._now_position()
        sent = 0
        lines = self.rec.lines
        while self.index < len(lines):
            bt, market, kind, msg = lines[self.index]
            if bt is not None and pos is not None and bt > pos:
                break
            if kind != "gap" and market is not None and self.index < self.skip_before.get(market, 0):
                self.index += 1
                continue
            if kind != "gap":
                self._send(msg)
                sent += 1
            self.index += 1
        self.position_ms = pos
        if self.index >= len(lines):
            self.state = "ended"
            self.position_ms = self.rec.end_ms
            self._heartbeat(force=True)
        else:
            self._heartbeat()
        return sent

    # --- output ---------------------------------------------------------------
    def status(self) -> dict:
        h = self.rec.header
        return {"state": self.state, "speed": self.speed, "position_ms": self._now_position(),
                "recording_start_ms": self.rec.start_ms, "recording_end_ms": self.rec.end_ms,
                "partial_history": bool(h.get("partial_history", True)), "recording_id": h.get("recording_id"),
                "replay_session": self.session, "gaps": len(self.rec.gaps), "file": os.path.basename(self.rec.path)}

    def _heartbeat(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last_heartbeat < 1.0:
            return
        self.last_heartbeat = now
        if self.session is None:
            return
        p = self.rec.header.get("producer") or {}
        st = self.status()
        hb = {"v": 2, "control": "heartbeat", "epoch": p.get("epoch"), "session": p.get("session"),
              "pub_ms": st["position_ms"],
              "replay": {k: st[k] for k in ("state", "speed", "position_ms", "recording_start_ms",
                                            "recording_end_ms", "partial_history", "recording_id")}}
        self._send(hb)

    def _send(self, line: dict) -> None:
        out = dict(line)
        out["mode"] = "REPLAY"
        out["replay_session"] = self.session
        wall = int(time.time() * 1000)
        self.client.on_line(out, wall, time.monotonic_ns())
