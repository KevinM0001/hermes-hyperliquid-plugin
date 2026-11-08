"""Node panels on the recorded window (handoff steps 14, 16, 17 and 18).

Expected numbers are calculated here from the recorded lines' raw JSON with exact decimals, independently of
the panels module.
"""

from __future__ import annotations

import json
from decimal import Decimal

from hl_desk.node import panels
from hl_desk.node.triggers import NodeTriggers

from node_helpers import OPTS, XYZ, by_market, client, feed, hello_from, mutate, read_recording, surfaces

SP, XY = "xyz:SP500", "xyz:XYZ100"


def _setup(plugin=None):
    h, msgs = read_recording(XYZ)
    c = client() if plugin is None else plugin.attach(client())
    c.on_line(hello_from(h), h["producer"]["started_ms"], 0)
    return h, msgs, c


def _fed(c, plugin, lines, market):
    """Feed lines and read the plugin's tools and tab after each line of that market."""
    for rx, m in lines:
        c.on_line(m, rx, rx * 10 ** 6)
        if m.get("market") == market:
            plugin.read(market, rx)


def test_book_ladder_equals_the_engines_depth_view_at_every_depth_message():
    h, msgs, c = _setup()
    checked = 0
    for rx, m in msgs:
        c.on_line(m, rx, rx * 10 ** 6)
        if m["kind"] != "depth" or not c.markets[m["market"]].fam["book"].valid:
            continue
        book = c.markets[m["market"]].book
        for side, key in (("B", "bids"), ("A", "asks")):
            mine = book.top_levels(side, m["body"]["levels"])
            theirs = m["body"][key]
            assert len(mine) == len(theirs)
            for r, (px, tot, n, big, big_t, old_t) in zip(mine, theirs):
                assert (r["px"], r["total_sz"], r["orders"], r["largest_order_sz"], r["largest_order_time_ms"],
                        r["oldest_order_time_ms"]) == (Decimal(px), Decimal(tot), n, Decimal(big), big_t, old_t)
        assert m["body"]["available"] == {"bids": len(book.levels["B"]), "asks": len(book.levels["A"])}
        checked += 1
    assert checked >= 120                                   # every depth view of both markets


def test_stop_bands_match_the_engines_bands_view_and_account_for_every_trigger():
    h, msgs, c = _setup()
    checked = 0
    for rx, m in msgs:
        c.on_line(m, rx, rx * 10 ** 6)
        if m["kind"] != "bands" or m["body"].get("ref_px") is None or not c.markets[m["market"]].fam["triggers"].valid:
            continue
        b = m["body"]
        mine = c.markets[m["market"]].triggers.bands(Decimal(b["ref_px"]), Decimal(b["width"]), b["range_bps"])
        got = sorted((r["lo"], r["kind"], r["side"], r["orders"], r["sz"], r["position_tpsl_orders"]) for r in mine["rows"])
        want = sorted((Decimal(lo), k, s, n, Decimal(sz), pt) for lo, hi, k, s, n, sz, pt in b["rows"])
        assert got == want
        assert mine["accounted"] == mine["waiting_total"] == len(c.markets[m["market"]].triggers.orders)
        checked += 1
    assert checked >= 20


def test_a_band_dominated_by_one_order_is_flagged_and_nothing_is_dropped(surfaces):
    plugin = surfaces([SP, XY])
    h, msgs, c = _setup(plugin)
    _fed(c, plugin, msgs, SP)
    v = plugin.read(SP, msgs[-1][0])
    assert plugin.last["node_stops"]["stops"] == json.loads(json.dumps(v["stops"], default=str))
    rows = v["stops"]["rows"]
    tr = c.markets[SP].triggers
    dominated = [r for r in rows if r["dominated"]]
    assert dominated, "the recorded window has bands where one order is most of the size"
    rlo, rhi = Decimal(v["stops"]["lo"]), Decimal(v["stops"]["hi"])      # the exact ±2% range: edge bands are partial
    for r in dominated:
        members = [g for g in tr.orders.values() if g.kind == r["kind"] and g.side == r["side"]
                   and Decimal(r["lo"]) <= g.trigger_px < Decimal(r["hi"]) and rlo <= g.trigger_px <= rhi
                   and not g.position_tpsl]
        total = sum((g.sz for g in members), Decimal(0))
        big = max(members, key=lambda g: (g.sz, -g.oid))
        assert big.sz / total > Decimal("0.5")
        assert r["largest_order"]["sz"] == float(big.sz) and r["size_check"].startswith("one order is")
        assert r["orders"] >= len(members)                  # count shown beside the size
    acc = v["stops"]["accounted"]
    assert acc["waiting_triggers"] == acc["placed"] == len(tr.orders)
    assert v["stops"]["excluded_by_engine"]["trailing_stops"] is not None   # reported, never silently dropped
    # A band made of one order of 3,200 and a few tiny ones (the October BTC brackets): flagged.
    t = NodeTriggers.from_snapshot([[1, "A", "stop", "75050", "3200", False, "0x" + "1" * 40, 1],
                                    [2, "A", "stop", "75060", "0.0037", False, "0x" + "2" * 40, 1],
                                    [3, "A", "stop", "75070", "0.0037", False, "0x" + "3" * 40, 1],
                                    [4, "A", "stop", "75080", "0", True, "0x" + "4" * 40, 1]], {"trailing": 5, "unclassified": 0})
    b = t.bands(Decimal("75000"), Decimal("100"), 200)
    (row,) = b["rows"]
    assert row["orders"] == 4 and row["sized_orders"] == 3 and row["position_tpsl_orders"] == 1
    assert row["dominated"] and row["largest"]["sz"] == Decimal("3200")
    assert b["accounted"] == 4 and b["excluded"]["trailing"] == 5


def test_a_crossed_book_is_invalid_until_a_fresh_snapshot_clears_it(surfaces):
    plugin = surfaces([XY])
    h, msgs, c = _setup(plugin)
    ms = by_market(msgs, XY)
    snap60 = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "book_snapshot" and m["seq"] > 10)
    k = next(i for i, (_, m) in enumerate(ms) if i > 100 and m["kind"] == "book_delta"
             and any(ch["op"] == "add" and ch["side"] == "B" for ch in m["body"]["changes"]))
    _fed(c, plugin, ms[:k], XY)
    assert plugin.last["node_status"]["markets"][XY]["families"]["book"]["state"] == "LIVE"
    best_ask = c.markets[XY].book.best("A")
    rx, m = ms[k]
    changes = [dict(ch) for ch in m["body"]["changes"]]
    for ch in changes:
        if ch["op"] == "add" and ch["side"] == "B":
            ch["px"] = str(best_ask + 1)                     # a real add, moved above the best ask
            break
    crossed = mutate(m, body={"changes": changes})
    c.on_line(crossed, rx, rx * 10 ** 6)
    for rx2, m2 in [(rx, crossed)] + ms[k + 1:snap60 + 2]:
        if m2 is not crossed:
            c.on_line(m2, rx2, rx2 * 10 ** 6)
        v = plugin.read(XY, rx2)
        if m2["seq"] < ms[snap60 + 1][1]["seq"]:
            book_state = plugin.last["node_status"]["markets"][XY]["families"]["book"]
            assert book_state["state"] == "INVALID" and "crossed" in book_state["reason"]     # never "LIVE"
            assert v["book"]["valid"] is False and "crossed" in v["book"]["reason"]
            for p in ("ladder", "walls", "thinning"):
                assert plugin.last["node_book"][p]["available"] is False and "crossed" in plugin.last["node_book"][p]["reason"]
            assert v["stops"]["ref_px"] is None or v["stops"].get("available") is False
    v = plugin.read(XY, ms[snap60 + 1][0])
    assert v["book"]["valid"] is True and v["book"]["crossed"] is None   # cleared by the 60 s snapshot
    assert plugin.last["node_status"]["markets"][XY]["families"]["book"]["state"] == "LIVE"
    assert plugin.last["node_book"]["ladder"]["bids"]
    # Review 6, B2: nothing built on the book spans the crossed interval. The history and walls were dropped
    # when it crossed; after the recovery they start again, with no sample from before the crossing.
    crossed_at = crossed["block_time_ms"]
    for rx2, m2 in ms[snap60 + 2:]:
        c.on_line(m2, rx2, rx2 * 10 ** 6)
        hist = plugin.read(XY, rx2)["history"]
        if hist.get("since_ms") is not None:
            assert hist["since_ms"] > crossed_at and all(t > crossed_at for t, *_ in hist["samples"])
    assert hist["samples"] and plugin.last["tab"]["node"]["live"]["history"]["since_ms"] > crossed_at


def test_thinning_numbers_match_a_calculation_from_the_recorded_snapshot(surfaces):
    """Step 18: depth per 0.5% band out to 10%, from the 60 s snapshot's raw rows, independently, as node_book
    serves it."""
    plugin = surfaces([SP])
    h, msgs, c = _setup(plugin)
    ms = by_market(msgs, SP)
    parts = [m for _, m in ms if m["kind"] == "book_snapshot" and m["seq"] > 10]
    last = max(m["seq"] for m in parts)
    _fed(c, plugin, [(rx, m) for rx, m in ms if m["seq"] <= last], SP)
    rows = [r for m in parts for r in m["body"]["orders"]]
    bids = [(Decimal(px), Decimal(sz), oid) for oid, s, px, sz, u, t in rows if s == "B"]
    asks = [(Decimal(px), Decimal(sz), oid) for oid, s, px, sz, u, t in rows if s == "A"]
    mid = (max(p for p, _, _ in bids) + min(p for p, _, _ in asks)) / 2
    step = Decimal("0.005")
    plugin.read(SP, ms[-1][0])
    th = plugin.last["node_book"]["thinning"]
    assert th["available"] and Decimal(th["mid"]) == mid and th["note"] == panels.THIN_NOTE
    for side, key, orders in (("B", "bids_below", bids), ("A", "asks_above", asks)):
        for k, band in enumerate(th[key]["bands"]):
            lo = mid * (1 - step * (k + 1)) if side == "B" else mid * (1 + step * k)
            hi = mid * (1 - step * k) if side == "B" else mid * (1 + step * (k + 1))
            inside = [(p, s, o) for p, s, o in orders if int(((mid - p) if side == "B" else (p - mid)) / mid / step) == k
                      and ((side == "B" and p <= mid) or (side == "A" and p >= mid))]
            usd = sum((p * s for p, s, _ in inside), Decimal(0))
            assert band["depth_usd"] == round(float(usd), 2)
            assert band["orders"] == len(inside)
            assert band["usd_per_1_price"] == round(float(usd / (mid * step)), 2)
            if inside:
                big = max(inside, key=lambda x: (x[1], -x[2]))
                assert band["largest_order"]["sz"] == float(big[1])
                tot = sum((s for _, s, _ in inside), Decimal(0))
                assert band["dominated"] == (big[1] / tot > Decimal("0.5"))
        # The sharpest drop is the largest ratio between neighbouring bands moving away from the price.
        per = [Decimal(str(b["usd_per_1_price"])) for b in th[key]["bands"]]
        ratios = [(per[i] / per[i + 1] if per[i + 1] > 0 else Decimal("Infinity"), i) for i in range(len(per) - 1)
                  if per[i] > 0 and (per[i + 1] == 0 or per[i] / per[i + 1] > 1)]
        drop = th[key]["sharpest_drop"]
        if ratios:
            r, i = max(ratios)
            assert drop["distance_pct"] == th[key]["bands"][i + 1]["from_pct"]
            assert drop["empty_band"] == (r == Decimal("Infinity"))
            assert drop["times_thinner"] == (None if r == Decimal("Infinity") else round(float(r), 2))
            edge = mid * (1 - step * (i + 1)) if side == "B" else mid * (1 + step * (i + 1))   # where it thins
            assert Decimal(drop["at_price"]) == edge.quantize(Decimal("0.0001"))
        else:
            assert drop is None


def test_lifecycle_labels_never_say_moved():
    h, msgs, c = _setup()
    feed(c, msgs)
    for mk in (SP, XY):
        v = panels.market_view(c, mk, OPTS, msgs[-1][0], msgs[-1][0] * 10 ** 6)
        labels = {ch[3] for ch in c.markets[mk].book.changes}
        assert labels <= {"added", "absorbed", "pulled", "unknown_decrease", "unknown_increase", "unknown_removal"}
        text = json.dumps(v["ladder"]) + json.dumps(v["walls"])
        assert '"moved"' not in text and "moved:" not in text
        # absorbed only from maker_filled, pulled only from canceled with nothing filled: check against the raw deltas
        absorbed_raw = sum((min(Decimal(ch.get("maker_filled") or "0"), Decimal(ch.get("prev_sz") or "0"))
                            for _, m in by_market(msgs, mk) if m["kind"] == "book_delta" and m["seq"] > 10
                            for ch in m["body"]["changes"] if ch["op"] in ("size", "remove")), Decimal(0))
        absorbed_book = sum((ch[4] for ch in c.markets[mk].book.changes if ch[3] == "absorbed"), Decimal(0))
        assert absorbed_book <= absorbed_raw


def test_heatmap_history_and_walls_never_bridge_a_book_only_invalidation(surfaces):
    """The book alone goes invalid (its 60 s snapshot is cut off by a reconnect) while the stream continues: the
    heatmap history and the walls are dropped then, and start again only once the book is valid again (here from
    the real state at seq S, offered from outside the stream), never spanning the invalid period."""
    plugin = surfaces([XY])
    h, msgs, c = _setup(plugin)
    ms = by_market(msgs, XY)
    i = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "book_snapshot" and m["body"]["part"] == 1 and m["seq"] > 10)
    _fed(c, plugin, ms[:i + 1], XY)
    before = plugin.last["tab"]["node"]["live"]["history"]
    assert before["samples"] and before["since_ms"] is not None
    cut_rx = ms[i][0]
    c.on_disconnect("reset by peer", cut_rx)
    v = plugin.read(XY, cut_rx)
    assert v["families"]["book"]["state"] == "INVALID" and v["families"]["flow"]["state"] == "LIVE"   # book only
    assert v["history"]["available"] is False                       # withheld while the book is invalid
    ps = c.markets[XY].panels
    assert not ps.history and ps.history_since_ms is None and not ps.walls   # and dropped, not kept for later
    assert v["walls"].get("available") is False
    c.on_line({"v": 2, "control": "hello", "epoch": c.epoch, "session": c.session, "mode": "LIVE",
               "markets": {XY: {"start_seq": ms[i + 1][1]["seq"], "snapshot": "resumed"}}}, cut_rx, cut_rx * 10 ** 6)
    j = i + 40
    _fed(c, plugin, ms[i + 1:j + 1], XY)
    assert plugin.last["tab"]["node"]["live"]["history"]["available"] is False and not ps.history
    ref = client()
    ref.on_line(hello_from(h), h["producer"]["started_ms"], 0)
    feed(ref, [(rx, m) for rx, m in ms[:j + 1]])
    S = ms[j][1]["seq"]
    rb = ref.markets[XY].book
    rows = [[o.oid, o.side, str(o.px), str(o.sz), o.user, o.time_ms]           # the snapshot's wire rows
            for side in ("B", "A") for px in rb.sorted_levels(side) for o in (rb.orders[x] for x in rb.levels[side][px])]
    assert c.offer_snapshot(XY, "book", S, rows) == "swapped"
    revalid_at = None
    for rx, m in ms[j + 1:]:
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        if revalid_at is None and v["families"]["book"]["state"] == "LIVE":
            revalid_at = m["block_time_ms"]
        hist = v["history"]
        if hist["since_ms"] is not None:
            assert revalid_at is not None and hist["since_ms"] >= revalid_at
            assert all(t >= revalid_at for t, *_ in hist["samples"])
    assert revalid_at is not None and v["history"]["samples"]


def test_heatmap_history_and_walls_restart_after_a_delta_that_doesnt_fit(surfaces):
    """The other book-only invalidation: a book event that doesn't fit the book (a real remove pointed at an order
    that isn't resting). The book family goes invalid, its history and walls are dropped, the other families stay
    up, and both start again only after the in-stream 60 s snapshot makes the book valid again."""
    plugin = surfaces([XY])
    h, msgs, c = _setup(plugin)
    ms = by_market(msgs, XY)
    snap60 = next(i for i, (_, m) in enumerate(ms) if m["kind"] == "book_snapshot" and m["seq"] > 10)
    k = next(i for i, (_, m) in enumerate(ms) if 100 < i < snap60 and m["kind"] == "book_delta"
             and any(ch["op"] == "remove" for ch in m["body"]["changes"]))
    _fed(c, plugin, ms[:k], XY)
    assert plugin.last["tab"]["node"]["live"]["history"]["samples"]
    rx, m = ms[k]
    changes = [dict(ch) for ch in m["body"]["changes"]]
    bad = next(ch for ch in changes if ch["op"] == "remove")
    bad["oid"] = 1                                           # not a resting order
    assert 1 not in c.markets[XY].book.orders
    c.on_line(mutate(m, body={"changes": changes}), rx, rx * 10 ** 6)
    v = plugin.read(XY, rx)
    assert v["families"]["book"]["state"] == "INVALID" and "didn't fit" in v["families"]["book"]["reason"]
    assert v["families"]["flow"]["state"] == "LIVE" and c.markets[XY].counters["delta_errors"] == 1
    ps = c.markets[XY].panels
    assert v["history"]["available"] is False and not ps.history and ps.history_since_ms is None and not ps.walls
    revalid_at = None
    for rx, m in ms[k + 1:]:
        c.on_line(m, rx, rx * 10 ** 6)
        v = plugin.read(XY, rx)
        if revalid_at is None and v["families"]["book"]["state"] == "LIVE":
            revalid_at = m["block_time_ms"]
        hist = v["history"]
        if hist.get("since_ms") is not None:
            assert revalid_at is not None and hist["since_ms"] >= revalid_at
            assert all(t >= revalid_at for t, *_ in hist["samples"])
    assert revalid_at is not None and v["history"]["samples"] and ms[snap60][1]["block_time_ms"] <= revalid_at
