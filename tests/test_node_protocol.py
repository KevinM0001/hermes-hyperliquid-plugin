"""Protocol tests on recorded real windows (Handoff 1c Part B step 11, PROTOCOL.md sections 6, 8 and 9).

Every case inspects the intermediate results (the node view the tab and the node_* tools read), not only
the final state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hl_desk.node import panels

from node_helpers import (OPTS, RESTART, XYZ, by_market, check_view, client, feed, hello_from, jsonable,
                          read_recording, read_tap, surfaces)

SP, XY = "xyz:SP500", "xyz:XYZ100"


def _start(c, header):
    c.on_line(hello_from(header), header["producer"]["started_ms"], 0)


def _view(c, market, rx):
    return panels.market_view(c, market, OPTS, rx, rx * 1_000_000)


def _ladder_from_rows(rows, levels=20):
    """An independent ladder from a snapshot's raw rows (oid, side, px, sz, user, time_ms)."""
    lv = {"B": {}, "A": {}}
    for oid, side, px, sz, user, t in rows:
        e = lv[side].setdefault(Decimal(px), [Decimal(0), 0])
        e[0] += Decimal(sz)
        e[1] += 1
    out = {}
    for side, key in (("B", "bids"), ("A", "asks")):
        pxs = sorted(lv[side], reverse=(side == "B"))[:levels]
        out[key] = [(px, lv[side][px][0], lv[side][px][1]) for px in pxs]
    return out


def test_every_intermediate_result_over_two_interleaved_markets():
    """Two markets interleaved, line by line: every intermediate view holds the invariants; families become LIVE
    only after a watermark; a periodic snapshot swaps atomically (reads during its parts see the old state)."""
    h, msgs = read_recording(XYZ)
    c = client()
    _start(c, h)
    became_valid_at, became_live_at = {}, {}
    pending_snapshot = {}
    swaps_checked = 0
    for rx, m in msgs:
        mk = m["market"]
        before = _view(c, mk, rx)
        if m["kind"] == "book_snapshot" and m["body"]["part"] == 1 and before["book"]["valid"]:
            pending_snapshot[mk] = {"ladder": before["ladder"], "rows": []}
        c.on_line(m, rx, rx * 1_000_000)
        for market in (SP, XY):
            v = _view(c, market, rx)
            check_view(v, c, market)
            f = v["families"]["book"]["state"]
            if f != "INVALID":
                became_valid_at.setdefault(market, m["seq"] if m["market"] == market else None)
            if f == "LIVE":
                became_live_at.setdefault(market, (m["kind"], m["market"]))
        ps = pending_snapshot.get(mk)
        if ps is not None and m["kind"] == "book_snapshot":
            ps["rows"].extend(m["body"]["orders"])
            v = _view(c, mk, rx)
            if m["body"]["part"] < m["body"]["parts"]:
                for key in ("bids", "asks"):
                    assert v["ladder"][key] == ps["ladder"][key], "a read during the snapshot's parts sees the whole old state"
            else:
                want = _ladder_from_rows(ps["rows"])
                for key in ("bids", "asks"):
                    got = [(Decimal(r["px"]), Decimal(str(r["total_sz"])), r["orders"]) for r in v["ladder"][key]]
                    assert got == want[key], "after the last part the state is the snapshot's, whole"
                swaps_checked += 1
                del pending_snapshot[mk]
    assert swaps_checked == 2                                   # the 60 s snapshot of each market
    for market in (SP, XY):
        assert became_live_at[market][0] == "watermark"         # only a watermark makes a family LIVE
        mf = c.markets[market]
        assert mf.counters["snapshot_mismatches"] == 0 and mf.counters["snapshots_swapped"] == 4
        assert mf.counters["gaps"] == 0 and mf.counters["delta_errors"] == 0


def test_producer_restart_and_a_retired_sessions_late_lines(surfaces):
    """Real producer restart (Part A follow test): the new epoch invalidates every family at once, nothing of the
    old session is carried forward, and the retired session's late snapshot and watermark are ignored. Read
    through the plugin's tools and tab after every line."""
    lines = read_tap(RESTART)
    hellos = [i for i, o in enumerate(lines) if o.get("control") == "hello"]
    assert len(hellos) == 2
    run1, run2 = lines[hellos[0]:hellos[1]], lines[hellos[1]:]
    plugin = surfaces([XY])
    c = plugin.attach(client())
    t = 0
    for o in run1:
        t = o.get("pub_ms", t) + 50                           # received 50 ms after it was published
        c.on_line(o, t, t * 1_000_000)
        plugin.read(XY, t)
    v1 = plugin.read(XY, t)
    assert v1["book"]["valid"] and v1["families"]["book"]["state"] == "LIVE"
    old_epoch = c.epoch
    t = run2[0]["pub_ms"] + 50
    c.on_line(run2[0], t, t * 1_000_000)                       # run 2's hello
    v = plugin.read(XY, t)
    assert c.epoch != old_epoch and c.epoch > old_epoch
    assert all(f["state"] == "INVALID" for f in v["families"].values())
    assert v["book"]["valid"] is False and c.markets[XY].book is None       # nothing carried forward
    seen_valid = False
    late = [o for o in run1 if o.get("kind") in ("book_snapshot", "watermark")][-3:]
    for i, o in enumerate(run2[1:]):
        t = o.get("pub_ms", t) + 50
        c.on_line(o, t, t * 1_000_000)
        if i == 40:                                           # the retired session's lines arrive late
            before = c.counters["retired_ignored"]
            for old in late:
                c.on_line(old, t, t * 1_000_000)
            assert c.counters["retired_ignored"] == before + len(late)
        v = plugin.read(XY, t)
        if v["book"]["valid"]:
            seen_valid = True
            assert c.markets[XY].book.through_seq is not None
    assert seen_valid and c.epoch == run2[0]["epoch"]


def _xyz_market(market=SP):
    h, msgs = read_recording(XYZ)
    return h, by_market(msgs, market)


def test_replay_buffer_overflow_invalidates_until_the_next_snapshot(surfaces):
    """A reconnect the producer can't resume (its ring has moved on): replay_unavailable, every family invalid at
    once, recovery only from the next snapshot, and the flow window restarts after the next watermark."""
    h, ms = _xyz_market(SP)
    plugin = surfaces([SP])
    c = plugin.attach(client())
    _start(c, h)
    cut = next(i for i, (_, m) in enumerate(ms) if m["block_time_ms"] - ms[0][1]["block_time_ms"] > 30_000)
    for rx, m in ms[:cut]:
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(SP, rx)
    assert plugin.read(SP, ms[cut][0])["book"]["valid"]
    c.on_disconnect("connection reset", ms[cut][0])
    plugin.read(SP, ms[cut][0])
    assert plugin.last["node_status"]["node"]["live"]["connection"]["state"] == "disconnected"
    resume_at = next(i for i, (_, m) in enumerate(ms) if m["block_time_ms"] - ms[0][1]["block_time_ms"] > 40_000)
    start_seq = ms[resume_at][1]["seq"]
    c.on_line({"v": 2, "control": "hello", "epoch": c.epoch, "session": c.session, "mode": "LIVE",
               "markets": {SP: {"start_seq": start_seq, "snapshot": "pending"}}}, ms[resume_at][0], ms[resume_at][0] * 10 ** 6)
    c.on_line({"v": 2, "control": "replay_unavailable", "market": SP, "requested": ms[cut][1]["seq"],
               "oldest_seq": start_seq, "next_seq": start_seq, "start_seq": start_seq}, ms[resume_at][0], ms[resume_at][0] * 10 ** 6)
    v = plugin.read(SP, ms[resume_at][0])
    assert all(f["state"] == "INVALID" for f in v["families"].values())
    snap_seq = next(m["seq"] for _, m in ms if m["kind"] == "book_snapshot" and m["seq"] > start_seq)
    for rx, m in ms[resume_at:]:
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(SP, rx)
        if m["seq"] < snap_seq:
            assert v["book"]["valid"] is False and plugin.last["node_book"]["ladder"]["available"] is False
    assert v["book"]["valid"] and c.markets[SP].counters["snapshots_swapped"] >= 4
    assert plugin.last["node_book"]["ladder"]["bids"]
    cov = c.markets[SP].flow.coverage()
    assert cov["since_block"] is not None and cov["since_block"] > ms[cut][1]["block"]   # flow never bridged it
    assert plugin.last["node_book"]["tape"]["coverage"]["since_block"] == cov["since_block"]


def test_duplicate_boundary_event_is_ignored(surfaces):
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms[:200]:
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, rx)
    plugin.read(XY, ms[199][0])
    before = {k: plugin.last[k] for k in ("node_book", "node_stops")}
    for rx, m in [ms[198], ms[199], ms[0], ms[1]]:            # a repeated watermark and repeated snapshot parts
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, ms[199][0])
    assert c.markets[XY].counters["duplicates"] == 4
    for k in ("node_book", "node_stops"):
        for part in ("book", "ladder", "walls", "thinning", "tape", "stops", "families"):
            if part in before[k]:
                assert plugin.last[k][part] == before[k][part], (k, part)


def test_reconnect_during_a_snapshot_discards_it(surfaces):
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    i = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "book_snapshot" and m["body"]["part"] == 1 and m["seq"] > 10)
    for rx, m in ms[:i + 1]:                                  # through part 1 of the 60 s book snapshot
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, rx)
    assert c.markets[XY].assembly is not None
    c.on_disconnect("reset by peer", ms[i][0])
    v = plugin.read(XY, ms[i][0])
    assert v["book"]["valid"] is False and "incomplete snapshot discarded" in v["families"]["book"]["reason"]
    assert plugin.last["node_book"]["ladder"]["available"] is False
    # The resume is honoured and continues with part 2: a part whose part 1 this client didn't keep is ignored.
    c.on_line({"v": 2, "control": "hello", "epoch": c.epoch, "session": c.session, "mode": "LIVE",
               "markets": {XY: {"start_seq": ms[i + 1][1]["seq"], "snapshot": "resumed"}}}, ms[i][0], ms[i][0] * 10 ** 6)
    for rx, m in ms[i + 1:]:
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        assert v["book"]["valid"] is False                    # no later book snapshot in this window: stays invalid
        assert v["walls"].get("available") is False and v["history"].get("available") is not True
    assert c.markets[XY].counters["orphan_parts"] == 1


def test_interrupted_multipart_snapshot_recovers_at_the_next_one(surfaces):
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    dropped = [m for _, m in ms if m["kind"] == "book_snapshot" and m["seq"] == 2]
    assert dropped and dropped[0]["body"]["part"] == 2
    seq_next_snap = next(m["seq"] for _, m in ms if m["kind"] == "book_snapshot" and m["seq"] > 10)
    for rx, m in ms:
        if m["seq"] == 2:
            continue                                         # part 2 of the first snapshot never arrives
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        if m["seq"] < seq_next_snap:
            assert v["book"]["valid"] is False and plugin.last["node_book"]["ladder"]["available"] is False
    assert v["book"]["valid"] is True and plugin.last["node_book"]["ladder"]["bids"]


@pytest.mark.parametrize("bound", ["bytes", "chunks", "time"])
def test_snapshot_assembly_bounds_discard_it(surfaces, bound):
    """Rule 6.3: assembly is bounded by bytes, chunks and time. The real 60 s book snapshot (two parts) is pushed
    over each bound in turn: it is discarded, the book goes invalid with the reason, its history and walls go
    with it, and nothing of it is applied; the other families stay up."""
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    i = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "book_snapshot" and m["body"]["part"] == 1 and m["seq"] > 10)
    assert ms[i][1]["body"]["parts"] == 2
    for rx, m in ms[:i]:
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, rx)
    assert plugin.last["node_status"]["markets"][XY]["families"]["book"]["state"] == "LIVE"
    if bound == "bytes":
        c.cfg["max_snapshot_bytes"] = 10_000                  # part 1 alone is far larger
        why = "byte or chunk bound"
    elif bound == "chunks":
        c.limits["snapshot_max_chunks"] = 1                   # the producer's own limit, as its hello would say
        why = "byte or chunk bound"
    else:
        why = "not complete within 10000 ms"
    rx, m = ms[i]
    c.on_line(m, rx, rx * 10 ** 6)
    if bound == "time":                                       # part 2 is late: only heartbeats arrive meanwhile
        assert c.markets[XY].assembly is not None
        t = rx + 10_001
        c.on_line({"v": 2, "control": "heartbeat", "epoch": c.epoch, "session": c.session, "pub_ms": t}, t, t * 10 ** 6)
        rx = t
    v = plugin.read(XY, rx)
    assert c.markets[XY].assembly is None and c.markets[XY].counters["snapshots_discarded"] == 1
    assert v["families"]["book"]["state"] == "INVALID" and why in v["families"]["book"]["reason"]
    assert v["families"]["flow"]["state"] == "LIVE"
    assert plugin.last["node_book"]["ladder"]["available"] is False and v["history"]["available"] is False
    for rx2, m2 in ms[i + 1:]:                                # part 2 is ignored; the book stays invalid
        c.on_line(m2, max(rx, rx2), max(rx, rx2) * 10 ** 6)
        v = plugin.read(XY, max(rx, rx2))
        assert v["families"]["book"]["state"] == "INVALID" and plugin.last["node_book"]["ladder"]["available"] is False
    assert c.markets[XY].book is None and c.markets[XY].counters["orphan_parts"] >= 1   # nothing of it applied


def test_trigger_cancelled_during_a_gap_with_its_snapshot_delayed_or_dropped(surfaces):
    """SP500 seq 456 removes two waiting triggers (one 'siblingFilledCanceled'). Lost in a gap: every family is
    invalid, the stops panel and node_stops never show the stale triggers, and only a trigger snapshot recovers
    them. Read through the plugin after every line."""
    h, ms = _xyz_market(SP)
    gone = [c["oid"] for _, m in ms if m["seq"] == 456 for c in m["body"]["changes"]]
    assert len(gone) == 2
    trig_snap = [(rx, m) for rx, m in ms if m["kind"] == "trig_snapshot" and m["seq"] > 10]
    # (a) the trigger snapshot arrives in the stream
    plugin = surfaces([SP])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms:
        if m["seq"] == 456:
            continue
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(SP, rx)
        if 456 < m["seq"] < trig_snap[-1][1]["seq"]:
            assert v["stops"].get("available") is False and plugin.last["node_stops"]["stops"]["available"] is False
    assert not set(gone) & set(c.markets[SP].triggers.orders)
    assert plugin.last["node_stops"]["stops"]["accounted"]["waiting_triggers"] == len(c.markets[SP].triggers.orders)
    # (b) the trigger snapshot is dropped: the family stays invalid to the end
    plugin = surfaces([SP])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms:
        if m["seq"] == 456 or m["kind"] == "trig_snapshot" and m["seq"] > 10:
            continue
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(SP, rx)
        if m["seq"] > 456:
            assert plugin.last["node_stops"]["stops"]["available"] is False
    assert c.markets[SP].triggers is None and plugin.read(SP, ms[-1][0])["stops"].get("available") is False
    # (c) delayed: the stream stalls just before its trigger snapshot; the triggers stay unavailable however long
    # the wait, and the same snapshot then reaches the client from outside the stream (a relay cache) first.
    snap_seq = trig_snap[0][1]["seq"]
    plugin = surfaces([SP])
    c = plugin.attach(client(cfg={"stale_after_ms": 2_000, "delayed_after_ms": 10 ** 9}))
    _start(c, h)
    for rx, m in ms:
        if m["seq"] != 456 and m["seq"] < snap_seq:
            c.on_line(m, rx, rx * 10 ** 6)
            plugin.read(SP, rx)
    last_rx = [rx for rx, m in ms if m["seq"] < snap_seq][-1]
    for wait in (1_000, 30_000):
        v = plugin.read(SP, last_rx + wait)
        assert v["stops"].get("available") is False and v["families"]["triggers"]["state"] == "INVALID"
        assert plugin.last["node_stops"]["stops"]["available"] is False
    snap_rows = [r for _, m in trig_snap for r in m["body"]["triggers"]]
    through = trig_snap[0][1]["body"]["through_seq"]
    assert through == c.markets[SP].last_seq
    res = c.offer_snapshot(SP, "triggers", through, snap_rows, trig_snap[0][1]["body"]["coverage"],
                           trig_snap[-1][1]["body"].get("excluded"))
    assert res == "swapped" and not set(gone) & set(c.markets[SP].triggers.orders)
    for rx, m in ms:                                           # the in-stream copy then arrives: no change
        if m["seq"] >= snap_seq:
            c.on_line(m, rx, rx * 10 ** 6)
            plugin.read(SP, rx)
    assert c.markets[SP].counters["snapshot_mismatches"] == 0 and not set(gone) & set(c.markets[SP].triggers.orders)


def _rows_of(book):
    rows = []
    for side in ("B", "A"):
        for px in book.sorted_levels(side):
            for oid in book.levels[side][px]:
                o = book.orders[oid]
                rows.append([o.oid, o.side, str(o.px), str(o.sz), o.user, o.time_ms])
    return rows


LEVEL_KEYS = ("px", "total_sz", "orders", "notional_usd", "largest_order_sz", "largest_order_time_ms",
              "oldest_order_time_ms")


def _levels(ladder):
    """A ladder's book state: the level statistics a snapshot determines (not the change log or the stops beside it,
    which depend on what the client saw before the snapshot)."""
    return {"best": (ladder["best_bid"], ladder["best_ask"]), "book_time_ms": ladder["book_time_ms"],
            **{side: [{k: r[k] for k in LEVEL_KEYS} for r in ladder[side]] for side in ("bids", "asks")}}


def test_snapshot_through_100_after_event_101_with_and_without_the_events_between(surfaces):
    """A snapshot through S offered after event S+1 was applied (outside its stream, e.g. from a relay cache). The
    snapshot is the real book state at seq S, taken from a reference client fed the same recorded stream. The
    plugin's ladder is read after every offer and every line, and compared with the reference's. Changed by Kevin,
    Nov 10: a swapped-in book that no watermark covers yet reads STALE and is served under that label, not
    withheld."""
    h, ms = _xyz_market(XY)
    S, X, gap_before, gap_after, ahead = 300, 480, 100, 350, 250
    deltas = {m["seq"] for _, m in ms if m["kind"] == "book_delta"}
    assert {gap_before, gap_after} <= deltas, "the dropped messages must be book events"
    ref = client()
    _start(ref, h)
    feed(ref, [(rx, m) for rx, m in ms if m["seq"] <= S])
    rows = _rows_of(ref.markets[XY].book)
    feed(ref, [(rx, m) for rx, m in ms if S < m["seq"] <= X])
    truth = ref.markets[XY].book.snapshot_rows()
    upto = [(rx, m) for rx, m in ms if m["seq"] <= X]
    rx_x = upto[-1][0]
    truth_ladder = _levels(jsonable(_view(ref, XY, rx_x)["ladder"]))

    def fed(c, plugin, lines):
        for rx, m in lines:
            c.on_line(m, rx, rx * 10 ** 6)
            plugin.read(XY, rx)

    # Valid family that already applied later events: rejected, never rolled back.
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    fed(c, plugin, upto)
    assert c.offer_snapshot(XY, "book", S, rows) == "rejected" and c.markets[XY].book.snapshot_rows() == truth
    plugin.read(XY, rx_x)
    assert _levels(plugin.last["node_book"]["ladder"]) == truth_ladder
    # Invalid family (a gap before S) holding every event after S: caught up off to the side, then swapped in.
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    fed(c, plugin, [(rx, m) for rx, m in upto if m["seq"] != gap_before])
    assert c.markets[XY].book is None and plugin.read(XY, rx_x)["book"]["valid"] is False
    assert c.offer_snapshot(XY, "book", S, rows) == "swapped" and c.markets[XY].book.snapshot_rows() == truth
    v = plugin.read(XY, rx_x)                                  # STALE until a watermark covers it; served labelled
    assert v["book"]["valid"] is True and v["families"]["book"]["state"] == "STALE"
    assert "no freshness-advancing watermark" in v["families"]["book"]["reason"]
    lad = plugin.last["node_book"]["ladder"]
    assert _levels(lad) == truth_ladder and lad["freshness"] == "STALE"
    assert lad["as_of_note"] == "as of the node's book, STALE: " + v["families"]["book"]["reason"]
    wm = next(m["seq"] for _, m in ms if m["seq"] > X and m["kind"] == "watermark")
    fed(c, plugin, [(rx, m) for rx, m in ms if X < m["seq"] <= wm])
    feed(ref, [(rx, m) for rx, m in ms if X < m["seq"] <= wm])
    assert plugin.last["node_status"]["markets"][XY]["families"]["book"]["state"] == "LIVE"
    assert _levels(plugin.last["node_book"]["ladder"]) == _levels(jsonable(_view(ref, XY, plugin.last["snapshot"]["built_at_ms"])["ladder"]))
    # Invalid family missing an event after S: rejected; it stays invalid.
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    fed(c, plugin, [(rx, m) for rx, m in upto if m["seq"] not in (gap_before, gap_after)])
    assert c.offer_snapshot(XY, "book", S, rows) == "rejected" and c.markets[XY].book is None
    assert plugin.read(XY, rx_x)["book"]["valid"] is False and plugin.last["node_book"]["ladder"]["available"] is False
    # Ahead of the stream: held aside, unapplied, swapped in only when the stream reaches S.
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    fed(c, plugin, [(rx, m) for rx, m in upto if m["seq"] <= ahead and m["seq"] != gap_before])
    assert c.offer_snapshot(XY, "book", S, rows) == "held" and c.markets[XY].book is None
    for rx, m in upto:
        if ahead < m["seq"] < S:
            c.on_line(m, rx, rx * 10 ** 6)
            assert plugin.read(XY, rx)["book"]["valid"] is False      # nothing applied before the boundary
    fed(c, plugin, [(rx, m) for rx, m in upto if m["seq"] >= S])
    assert c.markets[XY].book.snapshot_rows() == truth
    assert plugin.read(XY, rx_x)["book"]["valid"] and _levels(plugin.last["node_book"]["ladder"]) == truth_ladder


def _trig_rows_of(tr):
    return [[g.oid, g.side, g.kind, str(g.trigger_px), str(g.sz), g.position_tpsl, g.user, g.time_ms]
            for g in tr.orders.values()]


@pytest.mark.parametrize("family,mk", [("book", XY), ("triggers", SP)])
def test_a_second_older_snapshot_never_undoes_a_catch_up(surfaces, family, mk):
    """Review 4, B1. A gap before 300; the real state at 300 is offered and caught up through the buffered events
    to the stream's position; the family is LIVE at the next watermark. A second snapshot, the real state at 400,
    then arrives (from a cache): the family changed after 400 (for the triggers, SP500's only trigger event, seq
    456), so it is older than the caught-up state: rejected, and nothing rolls back."""
    h, ms = _xyz_market(mk)
    changed = [m["seq"] for _, m in ms if m["kind"] == ("book_delta" if family == "book" else "trig_delta")]
    assert any(400 < q < 480 for q in changed)
    wi = next(i for i, (_, m) in enumerate(ms) if m["seq"] >= 480 and m["kind"] == "watermark")
    ref = client()
    _start(ref, h)
    rows = {}
    for rx, m in ms[:wi + 1]:
        ref.on_line(m, rx, rx * 10 ** 6)
        if m["seq"] in (300, 400):
            rows[m["seq"]] = (_rows_of(ref.markets[mk].book) if family == "book" else _trig_rows_of(ref.markets[mk].triggers))
    state_of = (lambda c: c.markets[mk].book) if family == "book" else (lambda c: c.markets[mk].triggers)
    truth = state_of(ref).snapshot_rows()
    plugin = surfaces([mk])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms[:wi]:
        if m["seq"] != 100:
            c.on_line(m, rx, rx * 10 ** 6)
            plugin.read(mk, rx)
    extra = (next(m for _, m in ms if m["kind"] == "trig_snapshot")["body"].get("excluded"),) if family == "triggers" else ()
    cov = None
    assert c.offer_snapshot(mk, family, 300, rows[300], cov, *extra) == "swapped"
    assert state_of(c).snapshot_rows() == truth                       # caught up to the stream's position
    rx, m = ms[wi]
    c.on_line(m, rx, rx * 10 ** 6)
    v = plugin.read(mk, rx)
    assert v["families"][family]["state"] == "LIVE"
    before = {k: plugin.last[k] for k in ("node_book", "node_stops")}
    assert c.offer_snapshot(mk, family, 400, rows[400], cov, *extra) == "rejected"
    assert state_of(c).snapshot_rows() == truth                       # nothing rolled back
    v = plugin.read(mk, rx)
    assert v["families"][family]["state"] == "LIVE"
    key, part = ("node_book", "ladder") if family == "book" else ("node_stops", "stops")
    assert plugin.last[key][part] == before[key][part]


def test_ladder_stops_are_unavailable_while_the_triggers_are(surfaces):
    """Review 4, B2. After a gap the book recovers from the real state at 300 and goes LIVE while the triggers stay
    INVALID: every node_book ladder row then says the stops to the next level are unavailable, with the reason,
    never 0, until the in-stream 60 s trigger snapshot makes the triggers usable again. Changed by Kevin, Nov 10:
    once swapped in, the trigger family reads STALE until the next watermark and the column is filled under that
    label (a late family labels, it doesn't withhold); only INVALID withholds it."""
    h, ms = _xyz_market(XY)
    wi = next(i for i, (_, m) in enumerate(ms) if m["seq"] >= 480 and m["kind"] == "watermark")
    ref = client()
    _start(ref, h)
    for rx, m in ms:
        ref.on_line(m, rx, rx * 10 ** 6)
        if m["seq"] == 300:
            rows = _rows_of(ref.markets[XY].book)
            break
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms[:wi]:
        if m["seq"] != 100:
            c.on_line(m, rx, rx * 10 ** 6)
    assert c.offer_snapshot(XY, "book", 300, rows) == "swapped"
    first = next(m for _, m in ms if m["kind"] == "trig_snapshot" and m["seq"] > ms[wi][1]["seq"])
    trig_snap_end = first["seq"] + first["body"]["parts"] - 1
    usable_again = seen_stale = False
    for rx, m in ms[wi:]:
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        lad = plugin.last["node_book"]["ladder"]
        if v["families"]["book"]["state"] != "LIVE":
            continue
        rows_shown = [r for side in ("bids", "asks") for r in lad[side]]
        stops = [r["stops_to_next_level"] for r in rows_shown if r["stops_to_next_level"] is not None]
        assert len(stops) == len(rows_shown) - 2                       # only each side's last row has no range
        tst = v["families"]["triggers"]["state"]
        ns = plugin.last["node_stops"]["stops"]
        if tst == "INVALID":                                 # until the snapshot's last part
            assert m["seq"] <= trig_snap_end
            assert lad["stops_available"] is False and "triggers INVALID" in lad["stops_reason"]
            assert all(st == {"available": False, "reason": lad["stops_reason"]} for st in stops)
            assert ns["available"] is False
        else:                                                # STALE until the next watermark, then LIVE: filled
            assert tst in ("STALE", "LIVE")
            assert lad["stops_available"] is True and all(isinstance(st.get("orders"), int) for st in stops)
            assert ns.get("available") is not False and ns["freshness"] == tst and isinstance(ns["rows"], list)
            if tst == "STALE":
                seen_stale = True
                assert ns["as_of_note"] == "as of the node's waiting stops, STALE: " + v["families"]["triggers"]["reason"]
            else:
                usable_again = True
                assert ns["as_of_note"] is None
    assert usable_again and seen_stale


def test_old_backlog_delivered_at_normal_speed_reads_behind(surfaces):
    """Lines received 10 minutes after their blocks: every family reads BEHIND once a watermark has set the delay.
    Changed by Kevin, Nov 10: the ladder is served under that label with the delay stated, not withheld."""
    from hl_desk.util import iso_ms
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"delayed_after_ms": 5_000, "stale_after_ms": 10 ** 12}))
    _start(c, h)
    lag = 10 * 60 * 1000                                       # received 10 minutes after the blocks
    for rx, m in ms[:300]:
        c.on_line(m, rx + lag, rx * 10 ** 6)
        v = plugin.read(XY, rx + lag, rx * 10 ** 6)
        assert all(f["state"] != "LIVE" for f in v["families"].values()), v["families"]
        if any(m2["kind"] == "watermark" for _, m2 in ms[:ms.index((rx, m)) + 1]):
            assert all(f["state"] in ("BEHIND", "INVALID") for f in v["families"].values()), v["families"]
    st = v["families"]["book"]
    assert st["state"] == "BEHIND" and st["source_age_ms"] > 5_000 and st["delay_ms"] > 5_000
    lad = plugin.last["node_book"]["ladder"]
    assert lad.get("available") is not False and lad["bids"] and lad["freshness"] == "BEHIND"
    assert lad["as_of_note"] == f"as of the node's book at {iso_ms(st['watermark_block_time_ms'])}, {st['delay_ms'] / 1000:.1f} s behind"
    assert plugin.last["node_book"]["thinning"]["as_of_note"] == lad["as_of_note"]
    assert plugin.last["node_stops"]["stops"]["as_of_note"].startswith("as of the node's waiting stops at ")


def _avg_delay(watermarks, t, window=5_000):
    """The node feed's delay at t, counted here in exact fractions: over (t - window, t], each instant's latest
    watermark received at or before it, aged by (the instant minus its block time), averaged over the covered time."""
    from fractions import Fraction
    total, covered = Fraction(0), 0
    got = [(rx, bt) for rx, bt in watermarks if rx <= t]
    for i, (rx, bt) in enumerate(got):
        a, b = max(rx, t - window), (got[i + 1][0] if i + 1 < len(got) else t)
        if b > a:
            total += (b - a) * (Fraction(a + b, 2) - bt)
            covered += b - a
    return float(total / covered)


def test_frozen_producer_with_live_heartbeats_reads_stale(surfaces):
    """Heartbeats keep arriving, blocks don't. Lines are received 400 ms after their blocks (published at 300 ms),
    a steady feed. After the freeze, the latest watermark's age keeps growing by exactly the time passed, and its
    5 s average (the node feed's delay) grows with it, both as counted here independently; the families read STALE
    exactly once the average passes the stale limit, set 2 s above the delay the feed had while it ran, and the
    ladder is then served under that label with the delay stated (changed by Kevin, Nov 10), not withheld."""
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"delayed_after_ms": 10 ** 12, "stale_after_ms": 10 ** 12}))
    start = ms[0][1]["block_time_ms"]
    c.on_line(dict(hello_from(h), pub_ms=start + 300), start + 400, (start + 400) * 10 ** 6)
    wms = []
    for _, m in ms[:300]:
        now = m["block_time_ms"] + 400
        c.on_line(dict(m, pub_ms=m["block_time_ms"] + 300), now, now * 10 ** 6)
        if m["kind"] == "watermark" and m.get("block_time_ms"):
            wms.append((now, m["block_time_ms"]))
    v = plugin.read(XY, now)
    d0, cur0 = v["families"]["book"]["delay_ms"], v["delay"]["current_ms"]
    assert v["families"]["book"]["state"] == "LIVE" and abs(d0 - _avg_delay(wms, now)) <= 1 and d0 < 1_000
    c.cfg["stale_after_ms"] = d0 + 2_000
    prev, states = d0, []
    for k in range(1, 9):                                      # heartbeats keep arriving, blocks don't
        t = now + k * 1000
        c.on_line({"v": 2, "control": "heartbeat", "epoch": c.epoch, "session": c.session, "pub_ms": t}, t, t * 10 ** 6)
        v = plugin.read(XY, t)
        st = v["families"]["book"]
        assert v["delay"]["current_ms"] == cur0 + k * 1000          # heartbeats don't count
        assert st["delay_ms"] > prev and abs(st["delay_ms"] - _avg_delay(wms, t)) <= 1
        assert st["state"] == ("STALE" if st["delay_ms"] > d0 + 2_000 else "LIVE")
        prev = st["delay_ms"]
        states.append(st["state"])
    assert states[0] == "LIVE" and states[-1] == "STALE"
    assert "heartbeats don't count" in st["reason"]
    lad = plugin.last["node_book"]["ladder"]
    assert lad.get("available") is not False and lad["bids"] and lad["freshness"] == "STALE"
    assert lad["as_of_note"].endswith(f", {st['delay_ms'] / 1000:.1f} s behind") and " at " in lad["as_of_note"]


def test_lost_final_event(surfaces):
    h, ms = _xyz_market(XY)
    assert ms[-1][1]["kind"] == "watermark"
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"stale_after_ms": 2_000, "delayed_after_ms": 10 ** 9}))
    _start(c, h)
    for rx, m in ms[:-1]:                                     # the final watermark never arrives
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, rx)
    rx = ms[-1][0] + 3000
    assert plugin.read(XY, rx)["families"]["book"]["state"] == "STALE"
    plugin = surfaces([XY])
    c = plugin.attach(client())
    _start(c, h)
    for rx, m in ms[:-2] + ms[-1:]:                          # the final event lost; the next watermark exposes it
        c.on_line(m, rx, rx * 10 ** 6)
        plugin.read(XY, rx)
    v = plugin.read(XY, ms[-1][0])
    assert all(v["families"][f]["state"] == "INVALID" for f in ("book", "triggers", "depth", "bands"))
    assert plugin.last["node_book"]["ladder"]["available"] is False
    # flow restarts at the watermark that exposed the gap (rule 6.7): nothing before it is counted
    assert c.markets[XY].flow.coverage()["since_block"] == ms[-1][1]["block"]


def test_quiet_market_stays_live_on_watermarks(surfaces):
    """xyz:XYZ100 has 25 s with no execution (92 watermarks): flow stays LIVE on its watermarks alone (its stale limit
    is 2 s above the recording's largest receipt delay, which a 25 s silence without watermarks would pass), the
    tape doesn't change, and the activity buckets over the quiet span are available and zero, never unavailable."""
    h, ms = _xyz_market(XY)
    ex = [m["block_time_ms"] for _, m in ms if m["kind"] == "execs" and m["body"].get("execs")]
    quiet_len, q0, q1 = max((b - a, a, b) for a, b in zip(ex, ex[1:]))
    assert quiet_len > 20_000
    worst = max(rx - m["block_time_ms"] for rx, m in ms if m["kind"] == "watermark" and m.get("block_time_ms"))
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"delayed_after_ms": 10 ** 12, "stale_after_ms": worst + 2_000}))
    _start(c, h)
    tape = None
    watermarks = 0
    for rx, m in ms:
        c.on_line(m, rx, rx * 10 ** 6)
        if not q0 < m["block_time_ms"] < q1:
            continue
        v = plugin.read(XY, rx)
        assert m["kind"] != "execs" or not m["body"].get("execs")
        if m["kind"] == "watermark" and m["block_time_ms"] - q0 > 1_000:
            watermarks += 1
            assert v["families"]["flow"]["state"] == "LIVE" and v["families"]["book"]["state"] == "LIVE"
            rows = plugin.last["node_book"]["tape"]["rows"]
            assert tape is None or rows == tape
            tape = rows
    assert watermarks > 80
    b = panels.deck_buckets(c, XY, q0, q1 - 1)
    assert b["available"] and b["executions"] == 0
    assert all(b["buying"]["buckets"][k] == 0 for k in ("new_long", "cover"))


def test_a_retired_sessions_line_never_changes_freshness(surfaces):
    """Review 4, B3. A new producer session whose lines arrive on a clock 2.5 s behind reads UNKNOWN (clock skew).
    A delayed watermark of the retired session (an older publication time) is ignored and must not touch the
    current session's clock relationship: the families stay UNKNOWN, through chat and tab."""
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"stale_after_ms": 10 ** 9, "delayed_after_ms": 10 ** 9}))
    _start(c, h)
    for rx, m in ms:
        c.on_line(m, rx, rx * 10 ** 6)
    old_epoch, old_session = c.epoch, c.session
    new = {"epoch": old_epoch + 1, "session": "f" * 32}
    t = ms[-1][0] + 1
    c.on_line(dict(hello_from(h), **new, pub_ms=t + 2_500), t, t * 10 ** 6)
    for rx, m in ms:
        c.on_line(dict(m, **new, pub_ms=rx + 2_500), rx, rx * 10 ** 6)   # received 2.5 s "before" publication
        v = plugin.read(XY, rx)
    assert v["families"]["book"]["state"] == "UNKNOWN" and "clock skew" in v["families"]["book"]["reason"]
    before = plugin.last["node_status"]["markets"][XY]["families"]
    late = next(m for _, m in ms if m["kind"] == "watermark")              # the old session's, published long ago
    assert (late["epoch"], late["session"]) == (old_epoch, old_session)
    ignored = c.counters["retired_ignored"]
    c.on_line(late, rx, rx * 10 ** 6)
    v = plugin.read(XY, rx)
    assert c.counters["retired_ignored"] == ignored + 1 and c.epoch == old_epoch + 1
    assert plugin.last["node_status"]["markets"][XY]["families"] == before
    assert all(f["state"] == "UNKNOWN" for f in v["families"].values())


def test_a_duplicate_or_delayed_line_never_clears_clock_skew(surfaces):
    """Review 5, B1. Lines received on a clock 2.5 s behind the producer's: every family is UNKNOWN. A verbatim
    duplicate of an earlier watermark (ignored as a duplicate), and then an accepted heartbeat that was simply
    delayed (an old publication time), must not make the ages look bounded: still UNKNOWN through chat and tab.
    Only once no skewed line has been seen for the whole window does the clock relationship recover."""
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"stale_after_ms": 10 ** 9, "delayed_after_ms": 10 ** 9}))
    _start(c, h)
    for rx, m in ms:
        c.on_line(dict(m, pub_ms=rx + 2_500), rx, rx * 10 ** 6)     # received 2.5 s "before" publication
    rx = ms[-1][0]
    v = plugin.read(XY, rx)
    assert all(f["state"] == "UNKNOWN" for f in v["families"].values())
    before = plugin.last["node_status"]["markets"][XY]["families"]
    dup = next(m for _, m in ms if m["kind"] == "watermark")              # verbatim: its old publication time
    dups = c.markets[XY].counters["duplicates"]
    c.on_line(dup, rx, rx * 10 ** 6)
    v = plugin.read(XY, rx)
    assert c.markets[XY].counters["duplicates"] == dups + 1
    assert plugin.last["node_status"]["markets"][XY]["families"] == before
    t = rx + 500                                                           # a heartbeat delayed by 60 s: accepted
    c.on_line({"v": 2, "control": "heartbeat", "epoch": c.epoch, "session": c.session, "pub_ms": t - 60_000}, t, t * 10 ** 6)
    v = plugin.read(XY, t)
    assert all(f["state"] == "UNKNOWN" for f in v["families"].values()) and "worst over the last" in v["families"]["book"]["reason"]
    t2 = t + 10_001                                                        # a whole window of well-clocked lines
    c.on_line({"v": 2, "control": "heartbeat", "epoch": c.epoch, "session": c.session, "pub_ms": t2 - 30}, t2, t2 * 10 ** 6)
    assert c.family_status(XY, "book", t2, t2 * 10 ** 6)["state"] != "UNKNOWN"


def test_a_watermark_without_a_source_time_never_makes_a_family_live(surfaces):
    """Review 6, B1. PROTOCOL.md 2.1 lets block_time_ms be null when the node record had no parseable time. Such a
    watermark can't bound the source's age: (a) after the first snapshots it leaves the families STALE, never
    LIVE, through chat and tab, with the ladder served under that label (changed by Kevin, Nov 10: a late family
    labels, it doesn't withhold); the first timed watermark then makes them LIVE. (b) Once LIVE, null-time
    watermarks don't refresh the delay: it keeps growing, and past the stale limit (2 s above the delay at the
    last timed watermark) the families read STALE, the ladder served with that label and the delay."""
    h, ms = _xyz_market(XY)
    first_wm = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "watermark")
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"delayed_after_ms": 10 ** 12, "stale_after_ms": 10 ** 12}))
    _start(c, h)
    for rx, m in ms[:first_wm + 1]:
        m = dict(m, block_time_ms=None) if m["kind"] == "watermark" else m
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        assert all(f["state"] != "LIVE" for f in v["families"].values()), v["families"]
    st = plugin.last["node_status"]["markets"][XY]["families"]["book"]
    lad = plugin.last["node_book"]["ladder"]
    assert st["state"] == "STALE" and lad.get("available") is not False and lad["bids"] and lad["freshness"] == "STALE"
    assert lad["as_of_note"] == "as of the node's book, STALE: " + st["reason"] and "delay_ms" not in st
    assert c.markets[XY].counters["watermarks_without_time"] == 1
    nxt = next(i for i, (_, m) in enumerate(ms) if i > first_wm and m["kind"] == "watermark")
    for rx, m in ms[first_wm + 1:nxt + 1]:
        c.on_line(m, rx, rx * 10 ** 6)
    v = plugin.read(XY, ms[nxt][0])
    assert v["families"]["book"]["state"] == "LIVE" and plugin.last["node_book"]["ladder"]["bids"]
    # (b) later null-time watermarks, received promptly for 8 s: the delay isn't refreshed by them
    limit = v["families"]["book"]["delay_ms"] + 2_000
    c.cfg["stale_after_ms"] = limit
    later = [(rx, m) for rx, m in ms[nxt + 1:] if ms[nxt][0] < rx <= ms[nxt][0] + 8_000]
    prev, stale = v["families"]["book"]["delay_ms"], False
    for rx, m in later:
        c.on_line(dict(m, block_time_ms=None) if m["kind"] == "watermark" else m, rx, rx * 10 ** 6)
        st = plugin.read(XY, rx)["families"]["book"]
        assert st["delay_ms"] >= prev
        prev = st["delay_ms"]
        assert st["state"] == ("STALE" if st["delay_ms"] > limit else "LIVE")
        lad = plugin.last["node_book"]["ladder"]
        assert lad.get("available") is not False and lad["bids"] and lad["freshness"] == st["state"]
        if st["state"] == "STALE":
            stale = True
            assert lad["as_of_note"].endswith(f", {st['delay_ms'] / 1000:.1f} s behind")
        else:
            assert lad["as_of_note"] is None
    assert stale and any(m["kind"] == "watermark" for _, m in later)


def test_clock_skew_reads_unknown(surfaces):
    h, ms = _xyz_market(XY)
    plugin = surfaces([XY])
    c = plugin.attach(client(cfg={"stale_after_ms": 10 ** 9, "delayed_after_ms": 10 ** 9}))
    _start(c, h)
    for rx, m in ms[:200]:
        t = m["pub_ms"] - 2_500                               # this machine's clock is 2.5 s behind the producer's
        c.on_line(m, t, rx * 10 ** 6)
        v = plugin.read(XY, t, rx * 10 ** 6)
    st = v["families"]["book"]
    assert st["state"] == "UNKNOWN" and "clock skew" in st["reason"]
    assert plugin.last["node_book"]["ladder"]["available"] is False


# The recorder's own lines (header, trailer) as Part A's recorder writes them. PROTOCOL.md section 11 lists their
# names only in the engine's tooling, so these are pinned from the recordings: an added field fails the scan.
RECORDING_HEADER = {"protocol", "recording_id", "partial_history", "partial_history_note", "markets", "recorded_wall_ms",
                    "span_block_ms", "producer", "limits", "labels", "allowlist_version"}
PRODUCER = {"epoch", "session", "started_ms", "mode"}
RECORDING_TRAILER = {"reason", "messages", "gaps", "cut", "first_block_time_ms", "last_block_time_ms",
                     "markets_recording", "allowlist_violations"}


def _recorder_line_violations(o: dict) -> list[str]:
    from hl_desk.node.proto import LABEL_FIELDS, _map, _uint_or_str, limits_violations
    out = []
    if "recording" in o:
        h = o["recording"]
        out += [f"recording.{k}: not listed" for k in h if k not in RECORDING_HEADER]
        for k, v in h.items():
            if k == "producer":
                out += _map("recording.producer", v, PRODUCER)
            elif k == "limits":
                out += limits_violations("recording.limits", v)
            elif k == "labels":
                out += _map("recording.labels", v, LABEL_FIELDS, lambda x: isinstance(x, str))
            elif k == "markets":
                out += [] if isinstance(v, list) and all(isinstance(x, str) for x in v) else ["recording.markets"]
            elif k == "partial_history":
                out += [] if isinstance(v, bool) else ["recording.partial_history"]
            elif not _uint_or_str(v):
                out.append(f"recording.{k}: wrong shape")
    elif "recording_end" in o:
        t = o["recording_end"]
        out += [f"recording_end.{k}: not listed" for k in t if k not in RECORDING_TRAILER]
        for k, v in t.items():
            if k in ("first_block_time_ms", "last_block_time_ms"):
                out += _map(f"recording_end.{k}", v, None)
            elif k == "markets_recording":
                out += [] if isinstance(v, list) and all(isinstance(x, str) for x in v) else [f"recording_end.{k}"]
            elif k == "gaps":
                out += [] if v == [] else ["recording_end.gaps: gap markers aren't in these fixtures"]
            elif k == "cut":
                out += [] if isinstance(v, dict) and all(_uint_or_str(x) for x in v.values()) else ["recording_end.cut"]
            elif not _uint_or_str(v):
                out.append(f"recording_end.{k}: wrong shape")
    else:
        out.append(f"unexpected recorder line {sorted(o)}")
    return out


def test_every_fixture_line_is_on_the_v2_allowlist_and_matches_its_manifest():
    """The shipped fixtures carry only what protocol v2 allows: every data line's fields at every depth (never a
    dir, fee, hash or other node field), every control line's fields by name and by value (section 11, revision
    2.2), and the recorder's own header and trailer. Each file is byte for byte the one its manifest describes."""
    import gzip
    import hashlib
    import json
    from pathlib import Path

    from hl_desk.node.proto import fixture_violations

    root = Path(__file__).parent / "fixtures" / "node"
    manifest = json.loads((root / "MANIFEST.json").read_text())
    checked = {"data": 0, "control": 0, "recorder": 0}
    for fx in manifest["fixtures"]:
        raw = gzip.decompress((root / fx["file"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == fx["sha256_uncompressed"], fx["file"]
        lines = raw.splitlines()
        assert len(lines) == fx["lines_kept"], fx["file"]
        for n, text in enumerate(lines, 1):
            o = json.loads(text)
            if "msg" not in o and "control" not in o and "kind" not in o:
                bad = _recorder_line_violations(o)
                checked["recorder"] += 1
            else:
                if "msg" in o:
                    assert set(o) == {"msg", "rx_ms"} and isinstance(o["rx_ms"], int), f"{fx['file']} line {n}"
                msg = o.get("msg", o)
                bad = fixture_violations(msg)
                checked["control" if "control" in msg else "data"] += 1
            assert not bad, f"{fx['file']} line {n}: {bad}"
    assert checked == {"data": 3714, "control": 4, "recorder": 3}       # 2 hellos and 2 heartbeats; 2 headers, 1 trailer
    # The check isn't vacuous: an extra field, a forbidden node field, an unknown kind, a nested object under a
    # scalar control field, an unlisted key in a nested map, or an unlisted recorder field is reported.
    line = next(m for m in (json.loads(t).get("msg") for t in
                            gzip.decompress((root / "execs-4markets-120s.jsonl.gz").read_bytes()).splitlines())
                if m and m.get("kind") == "execs" and m["body"].get("execs"))
    assert fixture_violations(line) == []
    tampered = json.loads(json.dumps(line))
    tampered["body"]["execs"][0]["participants"][0]["dir"] = "Open Long"
    tampered["note"] = "x"
    assert set(fixture_violations(tampered)) >= {"never:dir", "execs.participant.dir", "envelope.note"}
    assert fixture_violations({"v": 2, "kind": "orders"}) == ["kind:orders"]
    hello = next(o for o in (json.loads(t) for t in gzip.decompress((root / "restart-xyz-XYZ100.ndjson.gz").read_bytes()).splitlines())
                 if o.get("control") == "hello")
    assert fixture_violations(hello) == []
    for path, value in ((("session",), {"id": "x"}), (("markets", "BTC", "host"), "h"), (("limits", "path"), "/x"),
                        (("labels", "time_ms"), {"a": 1}), (("unknown_markets",), [{"m": 1}]), (("epoch",), -1)):
        bad = json.loads(json.dumps(hello))
        node = bad
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        assert fixture_violations(bad), path
    hb = {"v": 2, "control": "heartbeat", "epoch": 1, "session": "s", "pub_ms": 1,
          "replay": {"state": "playing", "speed": 2.0, "position_ms": 5, "partial_history": True}}
    assert fixture_violations(hb) == []
    assert fixture_violations(hb | {"replay": hb["replay"] | {"file": "x.jsonl"}})
    assert _recorder_line_violations({"recording": {"protocol": 2, "host": "h"}}) == ["recording.host: not listed"]