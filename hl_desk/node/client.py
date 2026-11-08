"""The protocol v2 client state machine (PROTOCOL.md section 6 client rules, 8 freshness, 9 replay).

State is partitioned by (source, mode, epoch, session, replay_session, market) and, within a market, by
family. One FeedClient serves one input stream: one source in one mode (LIVE from a socket or relay,
or REPLAY from a recording). A new producer session, or a new replay session, replaces every market's
state at once; nothing is carried forward, and a session the client has left is retired for good.

Transport-agnostic: the transport calls on_line() with each decoded line and its receipt times (wall
clock and monotonic), and on_disconnect()/on_connect(). check_deadlines() enforces the snapshot
assembly time bound when no line arrives at all.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .book import BookError, NodeBook
from .flow import NodeFlow
from .proto import CLIENT_DEFAULTS, FAMILIES, KIND_FAMILY, SNAPSHOT_KINDS, STATE_FAMILIES, VERSION, VIEW_FAMILIES
from .triggers import NodeTriggers, TriggerError


def delay_state(delay_ms, limits: dict) -> tuple[str, str | None]:
    """A family's state from the market's averaged node-feed delay (client.delay), with its reason."""
    win = limits.get("delay_window_ms", 5_000) / 1000
    if not isinstance(delay_ms, (int, float)):
        return "STALE", "the node feed's delay isn't known"
    if delay_ms > limits["stale_after_ms"]:
        return "STALE", (f"the node feed's delay is {delay_ms / 1000:.1f} s ({win:g} s average; stale above "
                         f"{limits['stale_after_ms'] / 1000:g} s); heartbeats don't count")
    if delay_ms > limits["delayed_after_ms"]:
        return "BEHIND", (f"the node feed's delay is {delay_ms / 1000:.1f} s ({win:g} s average; delayed above "
                          f"{limits['delayed_after_ms'] / 1000:g} s)")
    return "LIVE", None

# Clock skew is judged on the worst offset (receipt wall time minus publication time) among the lines accepted in
# the last SKEW_WINDOW_MS: a delayed line, whose offset is large, never clears a skew seen moments before it.
SKEW_WINDOW_MS = 10_000

NS = 1_000_000_000
BOOK_FIELDS = ["oid", "side", "px", "sz", "user", "time_ms"]
TRIG_FIELDS = ["oid", "side", "kind", "trigger_px", "sz", "position_tpsl", "user", "time_ms"]


@dataclass
class Family:
    name: str
    valid: bool = False
    reason: str = "no snapshot yet"
    applied_through: int | None = None     # market seq through which this family's state is current
    last_change_seq: int | None = None     # seq of the last message that changed this family
    since_seq: int | None = None           # seq at which the family became valid
    wm_block: int | None = None
    wm_block_time_ms: int | None = None
    wm_recv_wall_ms: int | None = None
    wm_recv_mono_ns: int | None = None

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.reason = reason
        self.applied_through = None
        self.since_seq = None
        self.wm_block = self.wm_block_time_ms = self.wm_recv_wall_ms = self.wm_recv_mono_ns = None


@dataclass
class Assembly:
    family: str
    kind: str
    snapshot_id: str
    parts: int
    through_seq: int
    next_part: int
    started_mono_ns: int
    coverage: dict | None
    rows: list = field(default_factory=list)
    bytes: int = 0
    excluded: dict | None = None
    total: int | None = None
    block: int | None = None
    block_time_ms: int | None = None


class MarketFeed:
    def __init__(self, market: str, stream: str) -> None:
        self.market = market
        self.fam = {f: Family(f) for f in FAMILIES}
        self.fam["flow"].reason = "no watermark yet"
        self.last_seq: int | None = None        # last market seq processed
        self.run_start: int | None = None       # first seq of the current contiguous run (the gap boundary)
        self.expected_start: int | None = None  # from hello.markets[m].start_seq
        self.book: NodeBook | None = None
        self.wm_hist: deque = deque()                # (receipt mono ns, receipt wall ms - block time ms) per timed watermark
        self.triggers: NodeTriggers | None = None
        self.depth: dict | None = None
        self.bands: dict | None = None
        self.flow = NodeFlow(stream)
        self.assembly: Assembly | None = None
        self.held: list[dict] = []              # complete snapshots offered ahead of the stream
        self.buffer: deque[tuple] = deque()     # book/trigger deltas (seq, kind, body, block, block_time_ms) since run_start
        self.block: int | None = None
        self.block_time_ms: int | None = None
        self.crossed: dict | None = None        # set when the book is seen crossed, cleared by a snapshot
        self.counters = {"duplicates": 0, "gaps": 0, "snapshots_swapped": 0, "snapshots_rejected": 0,
                         "snapshots_discarded": 0, "snapshot_mismatches": 0, "orphan_parts": 0,
                         "deltas_unapplied": 0, "delta_errors": 0, "held_snapshots": 0, "watermarks_without_time": 0}
        self.events: deque[dict] = deque(maxlen=50)   # invalidations, swaps, rejections (for status)

    def note(self, what: str, **kw) -> None:
        self.events.appendleft({"what": what, "seq": self.last_seq, **kw})


class FeedClient:
    def __init__(self, source: str, mode: str = "LIVE", cfg: dict | None = None) -> None:
        self.source = source
        self.mode = mode
        self.cfg = dict(CLIENT_DEFAULTS) | (cfg or {})
        self.epoch: int | None = None
        self.session: str | None = None
        self.retired: set[tuple] = set()
        self.replay_session: str | None = None
        self.retired_replay: set[str] = set()
        self.markets: dict[str, MarketFeed] = {}
        self.hello: dict | None = None
        self.limits: dict = {}
        self.labels: dict = {}
        self.replay: dict | None = None         # the replay status from REPLAY heartbeats
        self.connection = {"state": "never connected", "since_wall_ms": None, "reason": None}
        self.skew = {"bad": False, "last_offset_ms": None, "worst_offset_ms": None, "window_ms": SKEW_WINDOW_MS}
        self.skew_samples: deque[tuple[int, int]] = deque()     # (monotonic ns, offset ms) of accepted lines
        self._accepted = False
        self.last_line_mono_ns: int | None = None
        self.last_heartbeat_mono_ns: int | None = None
        self.counters = {"lines": 0, "retired_ignored": 0, "retired_replay_ignored": 0, "unsupported": 0,
                         "malformed": 0, "sessions": 0, "replay_sessions": 0, "unknown_market": 0,
                         "dropped": 0, "replay_unavailable": 0, "errors": 0}
        self.listeners: list = []               # callables(event: dict) for panels (bursts, alerts)
        self.burst_rule_factory = None          # (market, client) -> bursts.NodeBurstRule, for node-mode bursts

    # --- partition -------------------------------------------------------------
    def stream_label(self) -> str:
        parts = [self.source, self.mode, f"epoch {self.epoch}", f"session {self.session}"]
        if self.mode == "REPLAY":
            parts.append(f"replay {self.replay_session}")
        return " · ".join(str(p) for p in parts)

    def _new_partition(self, why: str) -> None:
        self.skew_samples.clear()                    # a new session or replay learns its clock relationship anew
        self.skew = {"bad": False, "last_offset_ms": None, "worst_offset_ms": None, "window_ms": SKEW_WINDOW_MS}
        old = self.markets
        self.markets = {}
        for m, mf in old.items():
            mf.note("partition replaced", reason=why)
        self.counters["sessions"] += 1

    def _market(self, name: str) -> MarketFeed:
        mf = self.markets.get(name)
        if mf is None:
            mf = self.markets[name] = MarketFeed(name, self.stream_label())
            if self.burst_rule_factory is not None:
                mf.flow.bursts = self.burst_rule_factory(name, self)
        return mf

    def unreadable_line(self, why: str) -> None:
        """A line that can't be read or assigned to a market: counted, and, as in the burst calibration's rule, an
        unreadable record in every market's burst windows (it could have been any market's)."""
        self.counters["malformed"] += 1
        for mf in self.markets.values():
            if mf.flow.bursts is not None:
                mf.flow.bursts.unreadable(why)

    # --- connection --------------------------------------------------------------
    def on_connect(self, wall_ms: int) -> None:
        self.connection = {"state": "connected", "since_wall_ms": wall_ms, "reason": None}

    def on_disconnect(self, reason: str, wall_ms: int) -> None:
        self.connection = {"state": "disconnected", "since_wall_ms": wall_ms, "reason": reason}
        for mf in self.markets.values():
            self._discard_assembly(mf, f"connection closed ({reason})")

    # --- lines ---------------------------------------------------------------------
    def on_line(self, obj: dict, wall_ms: int, mono_ns: int) -> None:
        self.counters["lines"] += 1
        self.last_line_mono_ns = mono_ns
        if not isinstance(obj, dict):
            self.unreadable_line("a feed line that isn't a JSON object")
            return
        if obj.get("v") != VERSION:
            self.counters["unsupported"] += 1
            return
        self.check_deadlines(mono_ns, wall_ms)
        if obj.get("mode") == "REPLAY" and self.mode != "REPLAY":
            # The source turned out to be a replay (a replayer behind a configured feed): from now on it is REPLAY,
            # never live data, in a fresh partition.
            self.mode = "REPLAY"
            self.retired, self.epoch, self.session = set(), None, None
            self._new_partition("the source serves a REPLAY")
        if self.mode == "REPLAY" and not self._replay_session_ok(obj):
            return
        self._accepted = False
        if "control" in obj:
            self._control(obj, wall_ms, mono_ns)
        elif "kind" in obj:
            self._market_line(obj, wall_ms, mono_ns)
        else:
            self.unreadable_line("a feed line that is neither a control nor a market line")
            return
        # The clock relationship is learned only from lines the current session accepted: a market line that
        # advanced its sequence, or the session's own hello or heartbeat. A duplicate, retired, foreign or malformed
        # line never changes it, and a delayed one can't clear a skew seen within the window.
        if self._accepted and isinstance(obj.get("pub_ms"), int):
            self._note_clock(wall_ms - obj["pub_ms"], mono_ns)

    def _note_clock(self, off: int, mono_ns: int) -> None:
        self.skew_samples.append((mono_ns, off))
        horizon = mono_ns - SKEW_WINDOW_MS * 1_000_000
        while self.skew_samples and self.skew_samples[0][0] < horizon:
            self.skew_samples.popleft()
        worst = min(o for _, o in self.skew_samples)
        self.skew = {"bad": worst < -self.cfg["skew_tolerance_ms"], "last_offset_ms": off, "worst_offset_ms": worst,
                     "window_ms": SKEW_WINDOW_MS}

    def _replay_session_ok(self, obj: dict) -> bool:
        rs = obj.get("replay_session")
        if not isinstance(rs, str):
            self.counters["malformed"] += 1
            return False
        if rs in self.retired_replay:
            self.counters["retired_replay_ignored"] += 1
            return False
        if rs != self.replay_session:
            if self.replay_session is not None:
                self.retired_replay.add(self.replay_session)
            self.replay_session = rs
            self.counters["replay_sessions"] += 1
            # A new replay session: every market's state is replaced and the producer session is re-learned;
            # producer-session retirement starts over inside it (the recording's sessions are replayed again).
            self.retired = set()
            self.epoch = self.session = None
            self._new_partition("new replay session")
        return True

    def _session_ok(self, epoch, session) -> bool:
        """Apply rule 6.1; True when the line belongs to the current session (switching to it if newer)."""
        if not isinstance(epoch, int) or not isinstance(session, str):
            self.counters["malformed"] += 1
            return False
        key = (epoch, session)
        if key in self.retired or (self.epoch is not None and epoch < self.epoch):
            self.counters["retired_ignored"] += 1
            return False
        if self.epoch is None or epoch > self.epoch or session != self.session:
            if self.epoch is not None:
                self.retired.add((self.epoch, self.session))
                self._new_partition(f"new producer session (epoch {epoch})")
            self.epoch, self.session = epoch, session
        return True

    def _control(self, obj: dict, wall_ms: int, mono_ns: int) -> None:
        c = obj.get("control")
        if c == "hello":
            if not self._session_ok(obj.get("epoch"), obj.get("session")):
                return
            self._accepted = True
            self.hello = obj
            self.limits = obj.get("limits") or {}
            self.labels = obj.get("labels") or {}
            for m, info in (obj.get("markets") or {}).items():
                mf = self._market(m)
                if not isinstance(info, dict):
                    continue
                start = info.get("start_seq")
                if info.get("snapshot") == "resumed" and mf.last_seq is not None and start == mf.last_seq + 1:
                    continue        # an honoured resume continues with nothing lost
                if mf.last_seq is not None:
                    self._invalidate_market(mf, f"reconnected without a resume ({info.get('snapshot')})")
                mf.expected_start = start
                mf.last_seq = (start - 1) if isinstance(start, int) else None
                mf.run_start = start
        elif c == "replay_unavailable":
            m = obj.get("market")
            mf = self.markets.get(m) if isinstance(m, str) else None
            self.counters["replay_unavailable"] += 1
            if mf is not None:
                self._invalidate_market(mf, "replay unavailable: the resume could not be honoured")
                start = obj.get("start_seq")
                mf.last_seq = (start - 1) if isinstance(start, int) else None
                mf.run_start = start
        elif c == "heartbeat":
            if not self._session_ok(obj.get("epoch"), obj.get("session")):
                return
            self._accepted = True
            self.last_heartbeat_mono_ns = mono_ns
            if isinstance(obj.get("replay"), dict):
                self.replay = dict(obj["replay"])
                if self.replay.get("state") == "ended":
                    for mf in self.markets.values():
                        self._discard_assembly(mf, "the replay ended inside a snapshot")
        elif c == "dropped":
            self.counters["dropped"] += 1
            self.connection = {"state": "dropped", "since_wall_ms": wall_ms,
                               "reason": f"dropped by the producer ({obj.get('reason')})"}
            for mf in self.markets.values():
                self._discard_assembly(mf, "dropped as a slow subscriber")
        elif c == "error":
            self.counters["errors"] += 1
            self.connection = {"state": "refused", "since_wall_ms": wall_ms, "reason": str(obj.get("reason"))}

    # --- market messages -------------------------------------------------------
    def _market_line(self, obj: dict, wall_ms: int, mono_ns: int) -> None:
        if not self._session_ok(obj.get("epoch"), obj.get("session")):
            return
        m, seq, kind = obj.get("market"), obj.get("seq"), obj.get("kind")
        if not isinstance(m, str) or not isinstance(seq, int) or kind not in KIND_FAMILY:
            self.unreadable_line("a feed line that can't be assigned to a market")
            return
        mf = self._market(m)
        if mf.last_seq is not None and seq <= mf.last_seq:
            mf.counters["duplicates"] += 1
            return
        if mf.last_seq is not None and seq > mf.last_seq + 1:
            mf.counters["gaps"] += 1
            self._invalidate_market(mf, f"sequence gap: expected {mf.last_seq + 1}, got {seq}")
            mf.run_start = seq
        elif mf.last_seq is None:
            if mf.expected_start is not None and seq != mf.expected_start:
                self._invalidate_market(mf, f"stream started at {seq}, not {mf.expected_start}")
            mf.run_start = seq
        mf.last_seq = seq
        self._accepted = True                        # this line advanced the current session's sequence
        body = obj.get("body") if isinstance(obj.get("body"), dict) else {}
        block, bt = obj.get("block"), obj.get("block_time_ms")
        if isinstance(block, int):
            mf.block, mf.block_time_ms = block, bt
        self._buffer(mf, seq, kind, body, block, bt)
        # Any other line of this market before a snapshot's last part discards the partial snapshot.
        if mf.assembly is not None and kind not in SNAPSHOT_KINDS:
            self._discard_assembly(mf, f"interrupted by {kind} at seq {seq}")
        if kind in SNAPSHOT_KINDS:
            self._snapshot_part(mf, kind, seq, body, block, bt, mono_ns)
        elif kind == "book_delta":
            self._state_delta(mf, "book", seq, body, bt)
        elif kind == "trig_delta":
            self._state_delta(mf, "triggers", seq, body, bt)
        elif kind in ("depth", "bands"):
            view = dict(body) | {"_block": block, "_block_time_ms": bt, "_seq": seq}
            if kind == "depth":
                mf.depth = view
            else:
                mf.bands = view
            f = mf.fam[kind]
            if not f.valid:
                f.valid, f.reason, f.since_seq = True, None, seq
            f.last_change_seq = seq
        elif kind == "execs":
            new = mf.flow.on_execs(body, block, bt)
            for listener in self.listeners:
                listener({"type": "execs", "market": m, "execs": new, "client": self})
            if mf.flow.restart_pending:
                mf.fam["flow"].invalidate(f"flow window restarting ({mf.flow.restart_reason})")
        # Every valid family's applied-through advances with the contiguous stream.
        for f in mf.fam.values():
            if f.valid:
                f.applied_through = seq
        if kind == "watermark":
            self._watermark(mf, seq, body, block, bt, wall_ms, mono_ns)
        self._release_held(mf)

    def _buffer(self, mf: MarketFeed, seq, kind, body, block, bt) -> None:
        """Keep the book and trigger deltas since the gap boundary, for catching a later snapshot up (6.4).
        Within a contiguous run every other message was seen too; trimming moves the boundary forward."""
        if kind not in ("book_delta", "trig_delta"):
            return
        mf.buffer.append((seq, kind, body, block, bt))
        while len(mf.buffer) > self.cfg["buffer_messages"]:
            dropped = mf.buffer.popleft()
            mf.run_start = dropped[0] + 1

    def _watermark(self, mf: MarketFeed, seq, body, block, bt, wall_ms, mono_ns) -> None:
        through = body.get("through_seq")
        if through != seq - 1:
            mf.note("watermark through_seq mismatch", through=through)
        if not isinstance(bt, int) or isinstance(bt, bool):
            # A watermark without a source time (the node record had no parseable time, PROTOCOL.md 2.1) can't bound
            # the source's age: it advances neither freshness nor the flow window. The next timed one does.
            mf.counters["watermarks_without_time"] += 1
            mf.note("watermark without a block time: freshness not advanced", seq=seq)
            return
        mf.wm_hist.append((mono_ns, wall_ms - bt))
        keep_ns = (self.cfg["delay_window_ms"] + 60_000) * 1_000_000
        while len(mf.wm_hist) > 2 and mf.wm_hist[1][0] < mono_ns - keep_ns:
            mf.wm_hist.popleft()
        if isinstance(block, int):
            mf.flow.on_watermark(block, bt)
            fl = mf.fam["flow"]
            if not mf.flow.restart_pending and not fl.valid:
                fl.valid, fl.reason, fl.since_seq, fl.applied_through = True, None, seq, seq
        for f in mf.fam.values():
            if f.valid and f.applied_through is not None and f.applied_through >= seq - 1:
                f.wm_block, f.wm_block_time_ms = block, bt
                f.wm_recv_wall_ms, f.wm_recv_mono_ns = wall_ms, mono_ns
        for listener in self.listeners:
            listener({"type": "watermark", "market": mf.market, "block": block, "block_time_ms": bt,
                      "client": self})

    # --- state families --------------------------------------------------------
    def _state_delta(self, mf: MarketFeed, fam: str, seq: int, body: dict, bt) -> None:
        f = mf.fam[fam]
        if not f.valid or f.applied_through != seq - 1:
            mf.counters["deltas_unapplied"] += 1
            return
        try:
            if fam == "book":
                mf.book.apply_delta(body.get("changes") or [], bt)
                self._check_crossed(mf, seq, from_snapshot=False)
            else:
                mf.triggers.apply_delta(body.get("changes") or [], body.get("excluded"))
        except (BookError, TriggerError, KeyError, TypeError, ValueError) as exc:
            mf.counters["delta_errors"] += 1
            self._family_lost(mf, fam, f"a {fam} delta didn't fit the state ({exc}); waiting for a snapshot")
            mf.note("delta error", family=fam, error=str(exc))
            return
        f.last_change_seq = seq

    def _check_crossed(self, mf: MarketFeed, seq: int, from_snapshot: bool) -> None:
        if mf.book is None:
            return
        if mf.book.is_crossed():
            if mf.crossed is None:
                mf.crossed = {"since_seq": seq, "since_block_time_ms": mf.block_time_ms,
                              "best_bid": mf.book.best("B"), "best_ask": mf.book.best("A")}
                mf.note("book crossed", best_bid=str(mf.book.best("B")), best_ask=str(mf.book.best("A")))
                # The book is invalid from here until a fresh snapshot: nothing built on it spans that interval.
                self._reset_book_panels(mf)
        elif from_snapshot and mf.crossed is not None:
            mf.note("crossed book cleared by a snapshot")
            mf.crossed = None

    # --- snapshots -------------------------------------------------------------
    def _snapshot_part(self, mf: MarketFeed, kind, seq, body, block, bt, mono_ns) -> None:
        fam = SNAPSHOT_KINDS[kind]
        part, parts, through = body.get("part"), body.get("parts"), body.get("through_seq")
        sid = body.get("snapshot_id")
        if not all(isinstance(x, int) for x in (part, parts, through)) or not isinstance(sid, str):
            self._discard_assembly(mf, "malformed snapshot part")
            self._family_lost(mf, fam, "malformed snapshot part")
            return
        rows_key = "orders" if fam == "book" else "triggers"
        rows = body.get(rows_key) or []
        size = len(str(rows))
        a = mf.assembly
        if part == 1:
            if a is not None:
                self._discard_assembly(mf, f"a new snapshot started at seq {seq}")
            if seq != through + 1:
                self._family_lost(mf, fam, "snapshot part 1 out of place")
                return
            mf.assembly = a = Assembly(fam, kind, sid, parts, through, 1, mono_ns, body.get("coverage"),
                                       block=block, block_time_ms=bt)
        else:
            if a is None:
                mf.counters["orphan_parts"] += 1     # a part whose part 1 this client never saw: ignored
                return
            if a.snapshot_id != sid or a.parts != parts or part != a.next_part or seq != a.through_seq + part:
                self._discard_assembly(mf, "snapshot parts out of order")
                return
        a.rows.extend(rows)
        a.bytes += size
        a.next_part = part + 1
        if body.get("excluded") is not None:
            a.excluded = body.get("excluded")
        total = body.get("total_orders" if fam == "book" else "total_triggers")
        if isinstance(total, int):
            a.total = total
        limit_chunks = self.limits.get("snapshot_max_chunks", 4096)
        if a.bytes > self.cfg["max_snapshot_bytes"] or parts > limit_chunks:
            self._discard_assembly(mf, "snapshot exceeds the client's byte or chunk bound")
            return
        if part == parts:
            mf.assembly = None
            self._complete_snapshot(mf, a)

    def _discard_assembly(self, mf: MarketFeed, reason: str) -> None:
        a = mf.assembly
        if a is None:
            return
        mf.assembly = None
        mf.counters["snapshots_discarded"] += 1
        self._family_lost(mf, a.family, f"incomplete snapshot discarded: {reason}")
        mf.note("snapshot discarded", family=a.family, reason=reason)

    def _family_lost(self, mf: MarketFeed, fam: str, reason: str) -> None:
        """One state family invalidated (not the whole market): its state, any snapshot held aside for it, and the
        panel state built on it (book: heatmap history and walls) are dropped, so nothing bridges the gap."""
        mf.fam[fam].invalidate(reason)
        mf.held = [h for h in mf.held if h["family"] != fam]
        if fam == "book":
            mf.book = None
            self._reset_book_panels(mf)
        else:
            mf.triggers = None

    @staticmethod
    def _reset_book_panels(mf: MarketFeed) -> None:
        """Panel state built on the book (heatmap history, walls) starts again after the book was unusable."""
        ps = getattr(mf, "panels", None)
        if ps is not None:
            ps.history.clear()
            ps.history_since_ms = ps.last_sample_ms = None
            ps.walls.clear()
            ps.walls_gone.clear()

    def check_deadlines(self, mono_ns: int, wall_ms: int | None = None) -> None:
        limit = self.cfg["snapshot_timeout_ms"] * 1_000_000
        for mf in self.markets.values():
            a = mf.assembly
            if a is not None and mono_ns - a.started_mono_ns > limit:
                self._discard_assembly(mf, f"not complete within {self.cfg['snapshot_timeout_ms']} ms")

    def _build(self, a_family: str, rows: list, coverage: dict | None, excluded: dict | None):
        names = (coverage or {}).get("fields")
        want = BOOK_FIELDS if a_family == "book" else TRIG_FIELDS
        if isinstance(names, list) and names != want:
            idx = [names.index(n) for n in want]          # map by the coverage's own field names
            rows = [[r[i] for i in idx] for r in rows]
        if a_family == "book":
            return NodeBook.from_snapshot(rows)
        return NodeTriggers.from_snapshot(rows, excluded)

    def _complete_snapshot(self, mf: MarketFeed, a: Assembly) -> None:
        try:
            state = self._build(a.family, a.rows, a.coverage, a.excluded)
        except (BookError, TriggerError, ValueError, IndexError, TypeError) as exc:
            self._family_lost(mf, a.family, f"snapshot unusable ({exc})")
            mf.counters["snapshots_rejected"] += 1
            return
        if a.total is not None and len(state.orders) != a.total:
            self._family_lost(mf, a.family, f"snapshot holds {len(state.orders)} rows, total says {a.total}")
            mf.counters["snapshots_rejected"] += 1
            return
        self._offer(mf, a.family, a.through_seq, state, in_stream_last_seq=mf.last_seq)

    def offer_snapshot(self, market: str, family: str, through_seq: int, rows: list, coverage: dict | None = None,
                       excluded: dict | None = None) -> str:
        """A complete snapshot that reached the client outside its sequenced stream (a relay cache, a second
        connection of the same session). Rule 6.4 applies; returns what happened."""
        mf = self.markets.get(market)
        if mf is None:
            return "no such market"
        state = self._build(family, rows, coverage, excluded)
        if mf.last_seq is None or through_seq > mf.last_seq:
            # Ahead of the stream: held aside, unapplied, until the stream reaches its boundary.
            mf.held.append({"family": family, "through": through_seq, "state": state})
            mf.counters["held_snapshots"] += 1
            mf.note("snapshot held aside (ahead of the stream)", family=family, through=through_seq)
            return "held"
        return self._offer(mf, family, through_seq, state, in_stream_last_seq=None)

    def _offer(self, mf: MarketFeed, fam: str, through: int, state, in_stream_last_seq) -> str:
        f = mf.fam[fam]
        if f.valid:
            if f.last_change_seq is not None and f.last_change_seq > through:
                mf.counters["snapshots_rejected"] += 1
                mf.note("snapshot rejected (would roll state back)", family=fam, through=through)
                return "rejected"
            current = mf.book if fam == "book" else mf.triggers
            if current is not None and current.snapshot_rows() != state.snapshot_rows():
                mf.counters["snapshot_mismatches"] += 1
                mf.note("snapshot differed from the applied state", family=fam, through=through)
            self._swap(mf, fam, state, through)
            return "swapped"
        # Invalid family: every message of the market from through+1 on must be held.
        if mf.run_start is None or mf.run_start > through + 1:
            mf.counters["snapshots_rejected"] += 1
            mf.note("snapshot rejected (messages after it were lost)", family=fam, through=through,
                    run_start=mf.run_start)
            return "rejected"
        later = [b for b in mf.buffer if b[0] > through]
        # Catch up off to the side through every later message of the family, then swap. The state then stands
        # at the last of those messages, not at the snapshot's boundary: an older snapshot can never replace it.
        applied = through
        try:
            for seq, kind, body, block, bt in later:
                if KIND_FAMILY.get(kind) != fam or kind in SNAPSHOT_KINDS:
                    continue
                if fam == "book":
                    state.apply_delta(body.get("changes") or [], bt)
                else:
                    state.apply_delta(body.get("changes") or [], body.get("excluded"))
                applied = seq
        except (BookError, TriggerError, KeyError, TypeError, ValueError) as exc:
            mf.counters["snapshots_rejected"] += 1
            mf.note("snapshot catch-up failed", family=fam, error=str(exc))
            return "rejected"
        self._swap(mf, fam, state, through, last_change=applied)
        return "swapped"

    def _swap(self, mf: MarketFeed, fam: str, state, through: int, last_change: int | None = None) -> None:
        if fam == "book":
            if mf.book is not None:
                state.changes = mf.book.changes          # keep the labelled change history across a swap
            mf.book = state                              # one reference assignment: readers see old or new
        else:
            mf.triggers = state
        state.through_seq = through
        f = mf.fam[fam]
        was_valid = f.valid
        f.valid, f.reason = True, None
        if not was_valid:
            f.since_seq = mf.last_seq
        f.applied_through = mf.last_seq
        lc = through if last_change is None else max(through, last_change)
        f.last_change_seq = max(lc, f.last_change_seq or 0) if was_valid else lc
        mf.counters["snapshots_swapped"] += 1
        if fam == "book":
            self._check_crossed(mf, mf.last_seq, from_snapshot=True)

    def _release_held(self, mf: MarketFeed) -> None:
        if not mf.held:
            return
        keep = []
        for h in mf.held:
            # Swapped in only once the stream reaches its boundary, so nothing at or before it lands on top.
            if mf.last_seq is not None and mf.last_seq >= h["through"]:
                self._offer(mf, h["family"], h["through"], h["state"], in_stream_last_seq=None)
            else:
                keep.append(h)
        mf.held = keep

    # --- invalidation ----------------------------------------------------------
    def _invalidate_market(self, mf: MarketFeed, reason: str) -> None:
        self._discard_assembly(mf, reason)
        for f in mf.fam.values():
            f.invalidate(reason)
        mf.book = None
        mf.triggers = None
        mf.depth = None
        mf.bands = None
        mf.held = []
        mf.buffer.clear()
        mf.crossed = None
        mf.panels = None          # history, walls and burst state never bridge a gap
        mf.wm_hist.clear()        # nor does the node feed's averaged delay: it restarts at the next timed watermark
        mf.flow.boundary(reason)
        mf.note("invalidated", reason=reason)

    # --- freshness (section 8) -----------------------------------------------------
    def family_status(self, market: str, fam: str, now_wall_ms: int | None = None,
                      now_mono_ns: int | None = None) -> dict:
        now_wall_ms = int(time.time() * 1000) if now_wall_ms is None else now_wall_ms
        now_mono_ns = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        mf = self.markets.get(market)
        if mf is None:
            return {"state": "INVALID", "reason": "no data for this market from this source yet"}
        f = mf.fam[fam]
        out = {"family": fam, "watermark_block": f.wm_block, "watermark_block_time_ms": f.wm_block_time_ms,
               "applied_through": f.applied_through,
               # the limits the state was judged by, so a reader holding this status can keep aging it
               "delay_window_ms": self.cfg["delay_window_ms"], "delayed_after_ms": self.cfg["delayed_after_ms"],
               "stale_after_ms": self.cfg["stale_after_ms"]}
        if not f.valid:
            return out | {"state": "INVALID", "reason": f.reason}
        if fam == "book" and mf.crossed is not None:
            c = mf.crossed
            return out | {"state": "INVALID", "reason": (f"book crossed since seq {c['since_seq']} (best bid {c['best_bid']} at "
                                                         f"or above best ask {c['best_ask']}); invalid until a fresh snapshot "
                                                         "clears it")}
        if self.mode == "REPLAY":
            return out | {"state": "REPLAY", "reason": None, "replay": self.replay}
        if self.skew["bad"]:
            return out | {"state": "UNKNOWN",
                          "reason": f"clock skew: this machine's clock is up to {-self.skew['worst_offset_ms']} ms behind "
                                    f"the producer's (worst over the last {SKEW_WINDOW_MS // 1000} s), so ages can't be "
                                    "bounded"}
        if f.wm_recv_mono_ns is None:
            return out | {"state": "STALE", "reason": "no freshness-advancing watermark since this family became valid"}
        if f.wm_block_time_ms is None:
            return out | {"state": "STALE", "reason": "the last watermark carried no source time, so its age can't be bounded"}
        since_ms = (now_mono_ns - f.wm_recv_mono_ns) / 1e6
        out["watermark_age_ms"] = round(since_ms)
        out["source_age_ms"] = round((f.wm_recv_wall_ms - f.wm_block_time_ms) + since_ms)
        d = self.delay(market, now_mono_ns)
        out["delay_ms"] = d["delay_ms"]
        state, reason = delay_state(d["delay_ms"], self.cfg)
        return out | {"state": state, "reason": reason}

    def delay(self, market: str, now_mono_ns: int | None = None) -> dict:
        """The node feed's delay for a market: the age of its latest watermark (receipt wall time minus block time,
        plus the time since receipt), averaged exactly over the last delay_window_ms (or since the first watermark,
        if that is shorter), and its current value. Heartbeats don't count: a silent feed's delay keeps growing."""
        now_mono_ns = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        mf = self.markets.get(market)
        win = self.cfg["delay_window_ms"]
        base = {"window_ms": win, "delayed_after_ms": self.cfg["delayed_after_ms"],
                "stale_after_ms": self.cfg["stale_after_ms"]}
        allh = list(mf.wm_hist) if mf is not None else []
        if not allh:
            return base | {"delay_ms": None, "current_ms": None,
                           "reason": "no timed watermark from this source yet (since the start or the last gap)"}
        hist = [h for h in allh if h[0] <= now_mono_ns]
        if not hist:
            # Judged at a moment before the first watermark's receipt (another clock): its age is its receipt delay.
            return base | {"delay_ms": round(allh[0][1]), "current_ms": round(allh[0][1]), "covered_ms": 0}
        lo = now_mono_ns - win * 1_000_000
        total, covered = 0.0, 0.0
        for i, (r, off) in enumerate(hist):
            end = hist[i + 1][0] if i + 1 < len(hist) else now_mono_ns
            a, b = max(r, lo), end
            if b <= a:
                continue
            # delay(t) = off + (t - r) on [a, b]; integrate it (ms x ms)
            span = (b - a) / 1e6
            total += span * (off + ((a - r) + (b - r)) / 2e6)
            covered += span
        cur = hist[-1][1] + (now_mono_ns - hist[-1][0]) / 1e6
        avg = total / covered if covered > 0 else cur
        return base | {"delay_ms": round(avg), "current_ms": round(cur), "covered_ms": round(covered)}
