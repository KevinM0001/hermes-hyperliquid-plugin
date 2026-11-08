"""Trade identity, duplicates, the subscription snapshot and reconnect gaps (recorded and synthetic)."""

from __future__ import annotations

from helpers import A, B, C, T0, make_desk, open_segment, recorded_records, recorded_replayer, trade, build


def test_subscription_snapshot_is_not_counted_and_starts_the_segment():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    snapshot = [trade("BTC", T0 - 5000 + i, 100.0, 1.0, "B", A, B, i) for i in range(30)]
    ms.on_trades(snapshot, 0, T0)
    assert ms.store.segment_start_ms == T0 - 5000 + 29
    assert len(ms.store.trades) == 0
    assert ms.store.counters["snapshot_trades"] == 30
    ms.on_trades([trade("BTC", T0, 100.0, 1.0, "A", A, B, 100)], 0, T0)
    assert len(ms.store.trades) == 1


def test_duplicate_trade_counted_once_even_across_a_reconnect():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    t1 = trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)
    t2 = trade("BTC", T0 + 2000, 100.0, 1.0, "A", A, B, 2)
    ms.on_trades([t1], 0, 0)
    ms.on_trades([t1, t2], 0, 0)
    assert ms.store.counters["duplicates"] == 1
    ms.on_disconnect("reconnect", T0 + 3000)
    ms.on_connect(2)
    ms.on_trades([t1, t2, trade("BTC", T0 + 9000, 100.0, 1.0, "B", A, C, 3)], 0, 0)   # reconnect snapshot
    t4 = trade("BTC", T0 + 10_000, 100.0, 1.0, "B", A, C, 4)
    ms.on_trades([t2, t4], 0, 0)
    assert [t.tid for t in ms.store.trades] == [4]        # t2 duplicate, t1/t2/t3 belong before the new segment
    assert ms.store.counters["duplicates"] == 2


def test_reconnect_records_a_gap_and_windows_restart():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, 0)
    ms.on_disconnect("ConnectionClosed: test", T0 + 2000)
    gap = ms.store.gaps[-1]
    assert gap["start_ms"] == T0 + 1000 and gap["end_ms"] is None and gap["reason"].startswith("ConnectionClosed")
    open_segment(ms, T0 + 20_000, conn=2)
    assert ms.store.gaps[-1]["end_ms"] == T0 + 20_000
    ms.on_trades([trade("BTC", T0 + 21_000, 100.0, 2.0, "A", B, C, 2)], 0, 0)
    snap = build(desk, 1)
    flow = snap["markets"]["BTC"]["flow"]
    assert flow["window"]["start_exclusive_ms"] == T0 + 20_000      # since the last gap
    assert flow["values"]["volume"]["value"] == 200.0               # only the post-gap trade
    assert snap["markets"]["BTC"]["provenance"]["coverage"]["trades"]["gaps"][-1]["end_ms"] == T0 + 20_000


def test_late_trade_is_flagged():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 5000, 100.0, 1.0, "B", A, B, 1)], 0, 0)
    ms.on_trades([trade("BTC", T0 + 4000, 100.0, 1.0, "B", A, B, 2)], 0, 0)
    assert ms.store.counters["late"] == 1


def test_recorded_session_has_a_real_reconnect_gap_and_no_double_counting():
    rp = recorded_replayer(["BTC"])
    last_disconnect = max(i for i, r in enumerate(rp.records) if r.get("event") == "disconnect")
    rp.run_until(rp.records[last_disconnect - 1]["recv_mono_ns"])   # stop before the recording's final close
    ms = rp.desk.markets["BTC"]
    assert len(ms.store.gaps) == 1
    gap = ms.store.gaps[0]
    assert gap["reason"] == "recorded disconnect" and gap["end_ms"] > gap["start_ms"]
    ids = [t.identity for t in ms.store.trades]
    assert len(ids) == len(set(ids))
    assert all(t.time > ms.store.segment_start_ms for t in ms.store.trades)
    # Independent count: every unique BTC trade on connection 2 after its snapshot message.
    first = True
    expect = set()
    for r in recorded_records():
        m = r.get("msg") or {}
        if r.get("conn") != 2 or m.get("channel") != "trades" or m["data"][0]["coin"] != "BTC":
            continue
        if first:
            first = False
            start = max(t["time"] for t in m["data"])
            continue
        expect |= {("BTC", t["time"], t["tid"]) for t in m["data"] if t["time"] > start}
    assert set(ids) == expect


def test_tape_marks_trades_from_before_a_gap():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 1.0, "B", A, B, 1)], 0, 0)
    ms.on_disconnect("gap", T0 + 2000)
    open_segment(ms, T0 + 10_000, conn=2)
    ms.on_trades([trade("BTC", T0 + 11_000, 100.0, 1.0, "A", A, B, 2)], 0, 0)
    tape = build(desk, 1)["markets"]["BTC"]["flow"]["tape"]
    by_tid = {r["tid"]: r for r in tape}
    assert by_tid[2]["segment"] == "current" and by_tid[2]["counted"] is True
    assert by_tid[1]["segment"].startswith("before a gap") and by_tid[1]["counted"] is True
    assert by_tid[10_000_002]["counted"] is False and by_tid[10_000_002]["segment"] == "current"
