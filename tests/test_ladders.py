"""Grouped ladders (book.ladders): the venue's nSigFigs=4, 3 and 2 books as ladders of the same shape as book.ladder,
each gated on its own book's freshness at build time and again at serving time, and the nSigFigs=2 poll end to end
(scheduled by the collector, handled by the state, dispatched by the feed as a replay would)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hl_desk import book as bk, feed, schema, tools
from hl_desk.collector import Collector
from hl_desk.config import DeskConfig
from hl_desk.serving_public import age_snapshot
from hl_desk.state import GROUPED_BOOKS
from hl_desk.store import SnapshotStore

from helpers import NS, T0, SnapRT, book_msg, build, make_desk, recorded_replayer

BASE = 75_000.0
STEP = {4: 10.0, 3: 100.0, 2: 1000.0}     # the venue's steps near 75,000 for 4, 3 and 2 significant figures
FULL = ([(BASE - i * 0.5, 1.0, 1) for i in range(20)], [(BASE + 0.5 + i * 0.5, 1.0, 1) for i in range(20)])


def grouped(nsf, t_ms, coin="BTC"):
    """A 20-level book at t_ms as the venue returns it at that nSigFigs (nothing is re-bucketed by the plugin); sizes
    grow with depth, so each side's largest level is its deepest."""
    step = STEP[nsf]
    bids = [(BASE - i * step, 1.0 + i, 2 + i) for i in range(20)]
    asks = [(BASE + step + i * step, 1.5 + i, 3 + i) for i in range(20)]
    return book_msg(coin, t_ms, bids, asks), bids, asks


def desk_with_books(nsfs=(4, 3, 2), t_ms=T0, mono_ns=0):
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", t_ms, *FULL), mono_ns, t_ms)
    raw = {}
    for nsf in nsfs:
        msg, bids, asks = grouped(nsf, t_ms)
        ms.on_depth_poll(nsf, msg, mono_ns, t_ms)
        raw[nsf] = (bids, asks)
    return desk, ms, raw


def test_ladders_carry_the_three_groupings_in_the_ladders_shape():
    desk, ms, raw = desk_with_books()
    m = build(desk, NS // 2)["markets"]["BTC"]
    ladders, ladder = m["book"]["ladders"], m["book"]["ladder"]
    assert list(ladders) == ["nSigFigs=4", "nSigFigs=3", "nSigFigs=2"]
    for key, label, nsf in GROUPED_BOOKS:
        lad = ladders[label]
        assert lad["nSigFigs"] == nsf and lad["book_time_ms"] == T0 and lad["aggregated"] is True
        assert lad["api_count_note"] == ladder["api_count_note"]
        # the same row code as book.ladder: the same keys per row, the venue's levels in the venue's order
        bids, asks = raw[nsf]
        same = bk.ladder_rows(bids, asks)
        assert lad["bids"] == same["bids"] and lad["asks"] == same["asks"]
        assert len(lad["bids"]) == 20 and [r["px"] for r in lad["bids"]] == [px for px, _, _ in bids]
        assert all(set(r) == set(ladder["bids"][0]) for r in lad["bids"] + lad["asks"])
        assert lad["bids"][0]["px"] - lad["bids"][1]["px"] == pytest.approx(STEP[nsf])       # the display step
        # the largest level by the same rule (the largest aggregate size: here the deepest level), per side
        assert lad["largest"]["bid"] == same["largest"]["bid"] == lad["bids"][-1]
        assert lad["largest"]["ask"] == same["largest"]["ask"] == lad["asks"][-1]
        assert "full-precision book's" in lad["largest"]["by"]
        # its book's own status block, the one under market.freshness
        assert lad["freshness"] == m["freshness"][key] and lad["freshness"]["status"] == "fresh"
        assert lad["freshness"]["field"] == f"l2Book nSigFigs={nsf} (info)"
    assert "book_sf2" in m["freshness"]
    assert not [u for u in m["unavailable"] if u["field"].startswith("l2Book nSigFigs")]
    assert ladder["bids"][0]["px"] == BASE and "ladders" not in ladder          # book.ladder itself is unchanged
    json.dumps(m)


def test_a_malformed_level_withholds_that_sides_largest_in_the_grouped_ladder():
    """Codex h1 review 3's rule carries over: a returned nSigFigs=3 ask level with no sz withholds that side's largest
    with the reason and is left out of the rows; its bids, the other groupings, book.ladder and the chart are
    unaffected."""
    desk, ms, raw = desk_with_books(nsfs=(4, 2))
    lv = lambda px, sz, n: ({"px": str(px), "n": n} | ({"sz": str(sz)} if sz is not None else {}))
    bids = [lv(BASE - i * 100, 1 + i, 2) for i in range(5)]
    asks = [lv(BASE + 100, 1, 4), lv(BASE + 200, None, 9), lv(BASE + 300, 3, 5)]
    ms.on_depth_poll(3, {"coin": "BTC", "time": T0, "levels": [bids, asks]}, 0, T0)
    m = build(desk, NS // 2)["markets"]["BTC"]
    lad = m["book"]["ladders"]["nSigFigs=3"]
    assert lad["malformed_levels"]["ask"] == [{"index": 1, "missing_or_invalid": ["sz"]}]
    assert lad["largest"]["ask"] is None and "can't be established" in lad["largest"]["ask_reason"]
    assert [r["px"] for r in lad["asks"]] == [BASE + 100, BASE + 300]
    assert lad["largest"]["bid"] == {"px": BASE - 400, "sz": 5.0, "api_count": 2, "notional_usd": (BASE - 400) * 5.0}
    assert "bid_reason" not in lad["largest"] and lad["freshness"]["status"] == "fresh"
    for label in ("nSigFigs=4", "nSigFigs=2"):
        other = m["book"]["ladders"][label]
        assert other["largest"]["ask"] is not None and "malformed_levels" not in other
    assert m["book"]["ladder"]["largest"]["ask"] is not None and "price_lines_withheld" not in m["chart"]


def test_a_book_that_is_not_fresh_at_build_time_is_withheld_whole():
    """nSigFigs=2 never polled: warming up, withheld with the reason. nSigFigs=3 polled 15.001 s ago (max 15 s): stale,
    withheld whole, no rows. nSigFigs=4 polled since: shown. A withheld entry is {reason} and nothing else."""
    desk, ms, raw = desk_with_books(nsfs=(3,))
    ms.on_depth_poll(4, grouped(4, T0 + 10_000)[0], 10 * NS, T0 + 10_000)
    m = build(desk, 15 * NS + NS // 1000)["markets"]["BTC"]
    lads = m["book"]["ladders"]
    assert lads["nSigFigs=2"] == {"reason": "l2Book nSigFigs=2 (info) warming_up: nothing received on this connection yet"}
    assert set(lads["nSigFigs=3"]) == {"reason"}
    assert lads["nSigFigs=3"]["reason"].startswith("l2Book nSigFigs=3 (info) stale: no progress for 15.0 s")
    assert m["freshness"]["book_sf3"]["status"] == "stale" and m["freshness"]["book_sf2"]["status"] == "warming_up"
    assert lads["nSigFigs=4"]["bids"] and lads["nSigFigs=4"]["freshness"]["status"] == "fresh"
    assert {u["field"] for u in m["unavailable"]} >= {"l2Book nSigFigs=2 (info)", "l2Book nSigFigs=3 (info)"}


def test_a_book_that_lapses_by_serving_time_is_withheld_whole_with_the_reason():
    """Fresh at build time with a progress age of 14.5 s (max 15 s) and served as the latest snapshot 1 s later: each
    grouped ladder is withheld whole, {reason} saying 'by the time this was served', its status reads stale and it
    is listed as unavailable, while book.ladder (its book polled later) stays. Served 0.4 s later, each is kept with
    its status block aged as market.freshness is. A read by ID is historical and keeps everything."""
    desk, ms, raw = desk_with_books()
    ms.on_book(book_msg("BTC", T0 + 5_000, *FULL), 5 * NS, T0 + 5_000)
    snap = build(desk, int(14.5 * NS), wall_ms=T0 + 14_500)
    m0 = snap["markets"]["BTC"]
    assert all(m0["freshness"][k]["status"] == "fresh" for k, _, _ in GROUPED_BOOKS)
    assert all(m0["book"]["ladders"][label]["bids"] for _, label, _ in GROUPED_BOOKS)
    late = age_snapshot(dict(snap, served={"latest": True, "age_s": 1.0}))["markets"]["BTC"]
    for key, label, nsf in GROUPED_BOOKS:
        entry = late["book"]["ladders"][label]
        assert set(entry) == {"reason"}, entry
        assert entry["reason"].startswith(f"l2Book nSigFigs={nsf} (info) stale: no progress for 15.5 s")
        assert "by the time this was served" in entry["reason"]
        assert late["freshness"][key]["status"] == "stale"
        assert any(u["field"] == late["freshness"][key]["field"] and u["status"] == "stale" for u in late["unavailable"])
    assert late["book"]["ladder"]["bids"] and late["freshness"]["book_full"]["status"] == "fresh"
    soon = age_snapshot(dict(snap, served={"latest": True, "age_s": 0.4}))["markets"]["BTC"]
    for key, label, _ in GROUPED_BOOKS:
        entry = soon["book"]["ladders"][label]
        assert entry["bids"] == m0["book"]["ladders"][label]["bids"] and entry["freshness"] == soon["freshness"][key]
        assert entry["freshness"]["aged_by_s"] == 0.4 and abs(entry["freshness"]["progress_age_s"] - 14.9) < 1e-6
    hist = age_snapshot(dict(snap, served={"latest": False, "age_s": 1.0}))["markets"]["BTC"]
    assert hist["book"]["ladders"] == m0["book"]["ladders"]
    # The chat path ages the latest snapshot the same way before any handler sees it (tools.run_tool).
    st = json.loads(tools.run_tool("hl_status", {}, SnapRT(dict(snap, served={"latest": True, "age_s": 1.0}))))
    assert st["ok"] and st["markets"]["BTC"]["freshness"]["book_sf2"]["status"] == "stale"


def test_the_nsigfigs_2_poll_is_scheduled_with_the_others_and_handled(monkeypatch, tmp_path):
    """The collector's depth loop asks the info endpoint for l2Book at nSigFigs 4, 3 and 2 per followed market; each
    answer reaches its own tracker and grouped ladder with the schema's limits; the feed's dispatch (the replayer's
    path) handles a recorded nSigFigs=2 poll."""
    from hl_desk import collector as col
    seen = []

    def fake_post_info(url, body, timeout=10.0):
        seen.append(dict(body))
        return dict(grouped(body["nSigFigs"], int(time.time() * 1000), coin=body["coin"])[0])

    monkeypatch.setattr(col.info_client, "post_info", fake_post_info)
    c = Collector(DeskConfig(markets=["BTC", "HYPE"], depth_poll_s=0.01), SnapshotStore(tmp_path))
    c.desk = make_desk(markets=("BTC", "HYPE"))
    for ms in c.desk.markets.values():
        ms.on_connect(1)

    async def one_pass():
        c._stop = asyncio.Event()
        task = asyncio.create_task(c._depth_loop())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(seen) >= 6:
                break
        c._stop.set()
        await task

    asyncio.run(one_pass())
    assert [(b["type"], b["coin"], b["nSigFigs"]) for b in seen[:6]] == [
        ("l2Book", coin, nsf) for coin in ("BTC", "HYPE") for nsf in (4, 3, 2)]
    sub = schema.freshness_field("l2Book")["subscriptions"]["sf2"]
    assert sub["request"] == {"type": "l2Book", "nSigFigs": 2} and sub["max_age_s"] == 15.0 and sub["source_max_age_s"] == 20.0
    snap = c.desk.build_snapshot(time.monotonic_ns(), int(time.time() * 1000))
    for coin in ("BTC", "HYPE"):
        ms = c.desk.markets[coin]
        assert {"book_sf4", "book_sf3", "book_sf2"} <= set(ms.books)
        tr = ms.trackers["book_sf2"]
        assert tr.messages >= 1 and tr.max_age_s == 15.0 and tr.source_max_age_s == 20.0
        m = snap["markets"][coin]
        assert m["freshness"]["book_sf2"]["status"] == "fresh"
        lad = m["book"]["ladders"]["nSigFigs=2"]
        assert lad["nSigFigs"] == 2 and len(lad["bids"]) == 20 and lad["bids"][0]["px"] == BASE
    # the feed's dispatch, as the replayer calls it for a recorded poll
    desk2 = make_desk()
    desk2.markets["BTC"].on_connect(1)
    feed.dispatch_depth(desk2, {"type": "l2Book", "coin": "BTC", "nSigFigs": 2}, grouped(2, T0)[0], 0, T0)
    assert desk2.markets["BTC"].books["book_sf2"]["time_ms"] == T0
    assert build(desk2, NS // 2)["markets"]["BTC"]["book"]["ladders"]["nSigFigs=2"]["bids"][1]["px"] == BASE - 1000


def test_recorded_replay_gates_each_grouped_ladder_on_its_own_book():
    """Over the recorded BTC session (nSigFigs=4 and 3 polls; no nSigFigs=2 was recorded): at every snapshot a grouped
    ladder is shown exactly when its own book is fresh, with that book's rows and status block, and withheld as
    {reason} otherwise; the nSigFigs=2 entry is always withheld, never an empty ladder."""
    rp = recorded_replayer(["BTC"])
    shown = set()
    for snap in rp.run_all_with_snapshots(every_s=1.0):
        m = snap["markets"]["BTC"]
        ms = rp.desk.markets["BTC"]
        for key, label, nsf in GROUPED_BOOKS:
            entry = m["book"]["ladders"][label]
            if m["freshness"][key]["status"] == "fresh":
                b = ms.books[key]
                assert entry["bids"] == bk.ladder_rows(b["bids"], b["asks"], b["problems"])["bids"]
                assert entry["book_time_ms"] == b["time_ms"] and entry["freshness"] == m["freshness"][key]
                assert entry["nSigFigs"] == nsf
                shown.add(label)
            else:
                assert set(entry) == {"reason"} and entry["reason"].startswith(m["freshness"][key]["field"])
        assert set(m["book"]["ladders"]["nSigFigs=2"]) == {"reason"}
    assert shown == {"nSigFigs=4", "nSigFigs=3"}
