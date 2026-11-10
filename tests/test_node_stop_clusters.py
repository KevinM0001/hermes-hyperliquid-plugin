"""The stops panel's stop map (Kevin's stop and liquidity map report): resting trigger orders by 0.5% band beside the
resting depth they would walk, the two largest clusters per side quoted in the report's form, and the counted
exclusions. Checked on the recorded xyz window against numbers computed here from the parsed book and trigger set with
exact decimals, on hand-checked synthetic books and triggers (the ratios, the null forms, the exclusion counts, the size
test's bar: the depth at and beyond the order's band), on the replay view, and through the serving-time gate."""

from __future__ import annotations

import json
import types
from decimal import Decimal

from hl_desk.node import panels, serving
from hl_desk.node.book import NodeBook
from hl_desk.node.client import FeedClient
from hl_desk.node.triggers import NodeTriggers, category, fires_on

from node_helpers import OPTS, XYZ, client, feed, hello_from, jsonable, read_recording, surfaces

SP, XY = "xyz:SP500", "xyz:XYZ100"
STEP = Decimal("0.005")
BANDS = 20
EXCL = ("implausible_price", "already_true_at_price", "beyond_range", "size_outlier")    # the map's order: size last
D = Decimal


def _fed_client(mode="LIVE", cfg=None):
    h, msgs = read_recording(XYZ)
    if mode == "REPLAY":
        c = FeedClient("recording:x", "REPLAY", {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10 ** 15})
        c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
        rs = "f" * 32
        c.on_line(dict(hello_from(h, "REPLAY"), replay_session=rs), h["producer"]["started_ms"], 0)
        for rx, m in msgs:
            c.on_line(dict(m, mode="REPLAY", replay_session=rs), rx, rx * 10 ** 6)
    else:
        c = client(cfg=cfg)
        c.on_line(hello_from(h), h["producer"]["started_ms"], 0)
        feed(c, msgs)
    return c, msgs[-1][0]


def _view(market, mode="LIVE", cfg=None):
    c, rx = _fed_client(mode, cfg)
    return c, panels.market_view(c, market, OPTS, rx, rx * 10 ** 6)


def _expected(c, market):
    """The map from the client's parsed book and trigger set, computed here with exact decimals: depth per band per
    side, the depth at and beyond each band (the bar of the size test), and each trigger's band or exclusion under the
    report's rules in the map's order: implausible price, already true at the price, beyond 10%, and last a size
    outlier, an order whose stated notional at its trigger price is above the depth at and beyond its band on the side
    it fires into (the bids for a fall trigger, the asks for a rise trigger)."""
    mf = c.markets[market]
    book, trig = mf.book, mf.triggers
    mid = (book.best("B") + book.best("A")) / 2
    depth = {"B": [D(0)] * BANDS, "A": [D(0)] * BANDS}
    for o in book.orders.values():
        if o.side == "B":
            if o.px > mid:
                continue
            dist = (mid - o.px) / mid
        else:
            if o.px < mid:
                continue
            dist = (o.px - mid) / mid
        k = int(dist / STEP)
        if k < BANDS:
            depth[o.side][k] += o.px * o.sz
    bars = {s: [sum(depth[s][k:], D(0)) for k in range(BANDS)] for s in ("B", "A")}
    bands = {"fall": [[] for _ in range(BANDS)], "rise": [[] for _ in range(BANDS)]}
    excl = {k: [] for k in EXCL}
    for g in trig.orders.values():
        px = g.trigger_px
        if px <= 0 or px > mid * 10 or px < mid / 10:
            excl["implausible_price"].append(g)
            continue
        falls = (g.kind == "stop") == (g.side == "A")       # a sell stop or a buy take-profit fires on a fall
        if (falls and px >= mid) or (not falls and px <= mid):
            excl["already_true_at_price"].append(g)
            continue
        dist = (mid - px) / mid if falls else (px - mid) / mid
        k = int(dist / STEP)
        if k >= BANDS:
            excl["beyond_range"].append(g)
            continue
        if not g.position_tpsl and g.sz * px > bars["B" if falls else "A"][k]:
            excl["size_outlier"].append(g)
            continue
        bands["fall" if falls else "rise"][k].append(g)
    return mid, depth, bars, bands, excl


def _band_of(g, mid):
    """(move, k) of a trigger inside the range, as the report places it."""
    falls = (g.kind == "stop") == (g.side == "A")
    dist = (mid - g.trigger_px) / mid if falls else (g.trigger_px - mid) / mid
    return ("fall" if falls else "rise"), int(dist / STEP)


def _notional(members):
    return sum((g.sz * g.trigger_px for g in members if not g.position_tpsl), D(0))


def _money(x):
    return f"${x / 1e6:,.2f}M" if x >= 1e6 else f"${x / 1e3:,.0f}K" if x >= 1e3 else f"${x:,.0f}"


def test_the_direction_a_trigger_fires_on_follows_its_kind_and_side():
    assert fires_on("stop", "A") == "fall" and category("stop", "A") == "sell_stop_on_fall"
    assert fires_on("take_profit", "B") == "fall" and category("take_profit", "B") == "buy_take_profit_on_fall"
    assert fires_on("stop", "B") == "rise" and category("stop", "B") == "buy_stop_on_rise"
    assert fires_on("take_profit", "A") == "rise" and category("take_profit", "A") == "sell_take_profit_on_rise"


def test_recorded_window_bands_walk_and_exclusions_match_an_independent_calculation():
    outliers = []
    for market in (SP, XY):
        c, v = _view(market)
        st, th = v["stops"], v["thinning"]
        mid, depth, bars, bands, excl = _expected(c, market)
        m = st["map"]
        assert m["available"] and D(m["ref_px"]) == mid == D(th["mid"]) and m["as_of"]["book_time_ms"] == th["book_time_ms"]
        assert m["band_pct"] == 0.5 and m["out_to_pct"] == 10.0 and "current" not in m["as_of"]["note"] or "moment" in m["as_of"]["note"]
        assert "only at restart" in m["venue_check"]
        for d, bside, key in (("fall", "B", "bids_below"), ("rise", "A", "asks_above")):
            side = m["sides"][d]
            assert side["book_side"] == key and side["fires_on"] == d
            assert side["side"] == ("sell" if d == "fall" else "buy")
            assert side["depth_total_usd"] == round(float(sum(depth[bside], D(0))), 2)
            assert len(side["bands"]) == BANDS
            for k, row in enumerate(side["bands"]):
                members = bands[d][k]
                tb = th[key]["bands"][k]
                assert row["k"] == k and row["band"]["lo"] == tb["lo"] and row["band"]["hi"] == tb["hi"]   # the thinning edges
                assert (row["band"]["from_pct"], row["band"]["to_pct"]) == (tb["from_pct"], tb["to_pct"])
                assert row["distance_pct"] == tb["from_pct"]
                assert row["band"]["near_px"] == (tb["hi"] if d == "fall" else tb["lo"])
                assert row["orders"] == len(members)
                assert row["accounts"] == len({g.user for g in members})
                unsized = sum(1 for g in members if g.position_tpsl)
                assert row["unknown_size_orders"] == unsized and row["sized_orders"] == len(members) - unsized
                notional = _notional(members)
                if members and unsized == len(members):
                    assert row["notional_usd"] is None and row["lower_bound"] is False
                    assert row["notional_reason"].startswith("every order is whole-position: size unknown, not 0")
                else:
                    assert row["notional_usd"] == round(float(notional), 2)
                    if unsized:       # a lower bound, said beside the number
                        assert row["lower_bound"] is True
                        assert f"{unsized} of {len(members)} orders report no size" in row["notional_note"]
                    else:
                        assert row["lower_bound"] is False and "notional_reason" not in row and "notional_note" not in row
                for cat, x in row["by_category"].items():
                    cm = [g for g in members if category(g.kind, g.side) == cat]
                    assert cm and x["orders"] == len(cm) and x["accounts"] == len({g.user for g in cm})
                    assert x["unknown_size_orders"] == sum(1 for g in cm if g.position_tpsl)
                    assert x["notional_usd"] == (round(float(_notional(cm)), 2) if x["sized_orders"] else None)
                    assert x["lower_bound"] is bool(x["unknown_size_orders"] and x["sized_orders"])
                assert sum(x["orders"] for x in row["by_category"].values()) == row["orders"]
                # the walk: the band and every band farther out, against the bands nearer than it, same book
                between = sum(depth[bside][:k], D(0))
                beyond = sum(depth[bside][k:], D(0))
                w = row["walk"]
                assert w["depth_between_usd"] == round(float(between), 2)
                assert w["depth_at_and_beyond_usd"] == round(float(beyond), 2)
                assert w["depth_bands_used"] == {"side": key, "k_from": k, "k_to": BANDS - 1, "includes_band": True,
                                                 "from_pct": tb["from_pct"], "to_pct": 10.0,
                                                 "note": w["depth_bands_used"]["note"]}
                if k:
                    assert w["depth_between_usd"] == th[key]["bands"][k - 1]["cumulative_from_price_usd"]
                    assert abs(w["depth_between_usd"] - sum(b["depth_usd"] for b in th[key]["bands"][:k])) < 0.01 * k
                assert abs(w["depth_at_and_beyond_usd"] - sum(b["depth_usd"] for b in th[key]["bands"][k:])) < 0.01 * BANDS
                assert w["unknown_size_orders"] == unsized
                assert beyond == bars[bside][k]
                if row["largest_order"] is not None:      # a sized order left in a band is never worth more than it would walk
                    assert row["largest_order"]["notional_usd"] <= w["depth_at_and_beyond_usd"]
                if not members:
                    assert w["ratio_pct"] is None and w["ratio_reason"] == "no triggers in this band"
                elif unsized == len(members):
                    assert w["ratio_pct"] is None and "every order is whole-position: size unknown, not 0" in w["ratio_reason"]
                elif beyond == 0:
                    assert w["ratio_pct"] is None and "no resting" in w["ratio_reason"]
                else:
                    assert w["ratio_pct"] == float(f"{float(notional / beyond) * 100:.4g}") != 0
                if w["ratio_pct"] is None:
                    assert w["lower_bound"] is False
                else:
                    assert w["lower_bound"] is bool(unsized)
        # exclusions: each category's count and notional, the engine's trailing count, and the total accounted for
        ex = st["exclusions"]
        assert ex["available"] and ex["trailing_stops"]["orders"] == c.markets[market].triggers.excluded["trailing"]
        assert ex["trailing_stops"]["notional_usd"] is None and "not in the feed" in ex["trailing_stops"]["reason"]
        for k in EXCL:
            members = excl[k]
            assert ex[k]["orders"] == len(members), k
            assert ex[k]["accounts"] == len({g.user for g in members})
            assert ex[k]["unknown_size_orders"] == sum(1 for g in members if g.position_tpsl)
            assert ex[k]["notional_usd"] == (round(float(_notional(members)), 2) if ex[k]["sized_orders"] else None)
            assert ex[k]["lower_bound"] is bool(ex[k]["unknown_size_orders"] and ex[k]["sized_orders"])
            assert sum(ex[k]["by_category"].values()) == len(members)
        # the size rule as served: one sentence, the test against the depth the band would walk, no median and no multiple
        rule = ex["size_outlier"]["rule"]
        assert rule.startswith("an order whose stated size, at its trigger price, is worth more than everything resting at and "
                               "beyond its band out to 10% on the side it fires into")
        assert "so it could not be walked through the book" in rule and rule.endswith("(whole-position) never qualify")
        assert "median" not in rule and "100x" not in rule and "cap" not in rule
        assert ex["order"] == ["trailing_stops (engine)", "implausible_price", "already_true_at_price", "beyond_range", "size_outlier"]
        # every size outlier outweighs the exact depth at and beyond its band, the number the band's walk serves
        for g in excl["size_outlier"]:
            d, k = _band_of(g, mid)
            bar = bars["B" if d == "fall" else "A"][k]
            assert not g.position_tpsl and g.sz * g.trigger_px > bar
            assert m["sides"][d]["bands"][k]["walk"]["depth_at_and_beyond_usd"] == round(float(bar), 2)
        acc = ex["accounted"]
        assert acc["complete"] and acc["waiting_triggers"] == len(c.markets[market].triggers.orders)
        assert acc["in_bands"] == sum(len(x) for d in bands for x in bands[d])
        assert acc["excluded_here"] == sum(len(x) for x in excl.values())
        # the window has every case: sized and whole-position orders in bands, an implausible price and a trigger beyond
        # 10% in each market; the size test bites only where an order outweighs the depth at and beyond its band
        assert all(ex[k]["orders"] for k in ("implausible_price", "beyond_range"))
        assert any(r["unknown_size_orders"] for s in m["sides"].values() for r in s["bands"])
        outliers.append(ex["size_outlier"]["orders"])
        # nothing in the stops panel is called a liquidation: these are resting trigger orders
        assert "liquidation" not in json.dumps(st).lower()
        # the rows, the whole-position rule and the venue sentence are as they were
        assert st["rows"] and st["venue_check"] == panels.VENUE_CHECK and st["excluded_by_engine"]["trailing_stops"] is not None
    assert sum(outliers) > 0            # the window exercises the size test: some order outweighs the depth beyond its band


def test_recorded_window_clusters_are_the_two_largest_bands_per_side_quoted_in_the_reports_form():
    c, v = _view(SP)
    st = v["stops"]
    rows = [r for d in ("fall", "rise") for r in st["map"]["sides"][d]["bands"]]
    want = []
    for d in ("fall", "rise"):
        cands = sorted((r for r in rows if r["fires_on"] == d and r["notional_usd"]),
                       key=lambda r: (-r["notional_usd"], r["k"], -r["orders"]))
        want += cands[:2]
    got = st["clusters"]
    assert [(x["fires_on"], x["k"]) for x in got] == [(x["fires_on"], x["k"]) for x in want]
    assert [x["rank"] for x in got] == [1, 2, 1, 2]
    for cl, row in zip(got, want):
        assert {k: v_ for k, v_ in cl.items() if k not in ("rank", "text")} == row
        label = "Sell-side triggers" if cl["fires_on"] == "fall" else "Buy-side triggers"
        sign = "-" if cl["fires_on"] == "fall" else "+"
        walked = "bids at and below" if cl["fires_on"] == "fall" else "asks at and above"
        b, w, u = cl["band"], cl["walk"], cl["unknown_size_orders"]
        least = "at least " if u else ""                    # a lower bound whenever any order has no size
        assert cl["lower_bound"] is bool(u) and w["lower_bound"] is bool(u) and w["unknown_size_orders"] == u
        head = (f"{label} at {b['near_px']} to {b['far_px']} ({sign}{b['from_pct']:.1f}% to {sign}{b['to_pct']:.1f}%): "
                f"{least}{_money(cl['notional_usd'])} across {cl['orders']} orders from {cl['accounts']} accounts; "
                f"the {walked} it total {_money(w['depth_at_and_beyond_usd'])}, so this cluster is {least}")
        assert cl["text"].startswith(head), cl["text"]
        assert "% of the liquidity it would walk" in cl["text"]
        if u:
            assert cl["text"].endswith(f"; {u} of {cl['orders']} orders report{'s' if u == 1 else ''} no size, so this is understated")
        else:
            assert "understated" not in cl["text"] and "at least" not in cl["text"]
        # the share quoted is the walk's, to two significant figures
        share = cl["text"].split("so this cluster is ")[1].removeprefix("at least ").split("%")[0]
        assert abs(float(share) - w["ratio_pct"]) <= (0.5 if w["ratio_pct"] >= 10 else 0.05 if w["ratio_pct"] >= 1 else 0.005)
    assert "Sell-side triggers at" in got[0]["text"] and "Buy-side triggers at" in got[2]["text"]


def _mf(book_rows, trig_rows, excluded=None, bands=None):
    return types.SimpleNamespace(book=NodeBook.from_snapshot(book_rows),
                                 triggers=NodeTriggers.from_snapshot(trig_rows, excluded or {"trailing": 3, "unclassified": 0}),
                                 block_time_ms=1_700_000_000_000, block=42, market="T", crossed=None, bands=bands,
                                 fam={"bands": types.SimpleNamespace(valid=bands is not None, reason="no bands view")})


def _u(i):
    return "0x" + f"{i:040x}"


BOOK = [[1, "B", "99.9", "10", _u(1), 1],       # bids: k0 999
        [2, "B", "99", "20", _u(2), 1],         # k2 (1.0%) 1980
        [3, "B", "97", "30", _u(3), 1],         # k6 (3.0%) 2910
        [4, "A", "100.1", "5", _u(4), 1],       # asks: k0 500.5
        [5, "A", "102", "50", _u(5), 1]]        # k4 (2.0%) 5100
TRIG = [[11, "A", "stop", "99", "1", False, _u(11), 1],               # fall k2: $99
        [12, "A", "stop", "97.2", "2", False, _u(12), 1],             # fall k5: $194.4
        [13, "B", "take_profit", "97.1", "1", False, _u(13), 1],      # fall k5: $97.1 (a short's take-profit)
        [14, "B", "stop", "102.5", "3", False, _u(11), 1],            # rise k5: $307.5 with nothing resting at and beyond it: a size outlier
        [15, "A", "take_profit", "101", "1", False, _u(15), 1],       # rise k2: $101
        [16, "A", "stop", "98.9", "0", True, _u(16), 1],              # fall k2, whole-position (no size)
        [17, "B", "take_profit", "96.1", "0", True, _u(17), 1],       # fall k7, whole-position, alone
        [18, "A", "stop", "100.5", "1", False, _u(18), 1],            # a fall trigger above the price: already true
        [19, "B", "stop", "99.5", "1", False, _u(18), 1],             # a rise trigger below the price: already true
        [20, "B", "stop", "90000000000", "1", False, _u(20), 1],      # implausible price
        [21, "A", "stop", "0.001", "1", False, _u(20), 1],            # implausible price
        [22, "A", "stop", "85", "1", False, _u(22), 1],               # 15% away: beyond the range
        [23, "A", "stop", "98", "10000", False, _u(23), 1],           # fall k4: $980,000 against the $2,910 of bids at and beyond it: a size outlier
        [24, "B", "stop", "115", "0", True, _u(24), 1],               # beyond the range, whole-position
        [25, "B", "stop", "102", "49", False, _u(25), 1]]             # rise k4: $4,998, just under the $5,100 of asks at and beyond it: stays


def test_hand_checked_ratios_null_forms_and_exclusions():
    mf = _mf(BOOK, TRIG)
    depth = panels.depth_bands(mf.book, mf.book.mid())
    out = panels.stop_map(mf, depth, None)
    m, cl, ex = out["map"], out["clusters"], out["exclusions"]
    assert m["ref_px"] == "100" and m["as_of"] == {"book_time_ms": 1_700_000_000_000, "block": 42, "note": panels.MAP_NOTE}
    fall, rise = m["sides"]["fall"]["bands"], m["sides"]["rise"]["bands"]
    assert m["sides"]["fall"]["depth_total_usd"] == 5889.0 and m["sides"]["rise"]["depth_total_usd"] == 5600.5
    # fall k2: one sized order ($99) and one whole-position: a lower bound; walks 1980 + 2910 past the 999 nearer
    r = fall[2]
    assert (r["orders"], r["sized_orders"], r["unknown_size_orders"], r["accounts"]) == (2, 1, 1, 2)
    assert r["notional_usd"] == 99.0 and r["lower_bound"] is True and "1 of 2 orders report no size" in r["notional_note"]
    assert r["band"] == {"lo": "98.5", "hi": "99", "near_px": "99", "far_px": "98.5", "from_pct": 1.0, "to_pct": 1.5}
    assert r["walk"]["depth_between_usd"] == 999.0 and r["walk"]["depth_at_and_beyond_usd"] == 4890.0
    assert r["walk"]["ratio_pct"] == 2.025 and r["walk"]["lower_bound"] is True            # 99 / 4890, 4 significant figures
    assert r["walk"]["unknown_size_orders"] == 1
    assert r["by_category"] == {"sell_stop_on_fall": {"orders": 2, "sized_orders": 1, "unknown_size_orders": 1, "accounts": 2,
                                                      "notional_usd": 99.0, "lower_bound": True}}
    # fall k5: a sell stop and a short's take-profit, both sized; walks the 2910 at 3%
    r = fall[5]
    assert r["notional_usd"] == 291.5 and r["orders"] == 2 and r["accounts"] == 2 and r["lower_bound"] is False
    assert r["walk"]["depth_between_usd"] == 2979.0 and r["walk"]["depth_at_and_beyond_usd"] == 2910.0
    assert r["walk"]["ratio_pct"] == 10.02 and r["walk"]["lower_bound"] is False
    assert r["by_category"]["sell_stop_on_fall"]["notional_usd"] == 194.4
    assert r["by_category"]["buy_take_profit_on_fall"]["notional_usd"] == 97.1
    assert r["largest_order"] == {"notional_usd": 194.4, "sz": 2.0, "trigger_px": "97.2", "kind": "stop", "user": _u(12),
                                  "share_pct": 66.69}
    # fall k7: only a whole-position order: notional unknown, never 0, and no share
    r = fall[7]
    assert r["notional_usd"] is None and r["lower_bound"] is False
    assert r["notional_reason"] == "every order is whole-position: size unknown, not 0 (1 order, none with a stated size)"
    assert r["sz"] is None and r["largest_order"] is None
    assert r["walk"]["ratio_pct"] is None and r["walk"]["ratio_reason"] == r["notional_reason"] and r["walk"]["lower_bound"] is False
    assert r["walk"]["depth_between_usd"] == 5889.0 and r["walk"]["depth_at_and_beyond_usd"] == 0.0
    # fall k4: the 10,000 outlier would have landed here; it is excluded, so the band is empty
    r = fall[4]
    assert r["orders"] == 0 and r["notional_usd"] is None and "notional_reason" not in r and r["lower_bound"] is False
    assert r["walk"]["ratio_pct"] is None and r["walk"]["ratio_reason"] == "no triggers in this band"
    assert r["walk"]["depth_between_usd"] == 2979.0 and r["walk"]["depth_at_and_beyond_usd"] == 2910.0
    # rise k2 walks the 5100 at 2%; rise k4 keeps the $4,998 order worth just less than the $5,100 at and beyond it (nearly
    # all of what it would walk); rise k5 is empty: its $307.50 order had nothing resting at and beyond it within 10%, so
    # it could not be walked at all and is a size outlier
    assert rise[2]["notional_usd"] == 101.0 and rise[2]["walk"]["depth_between_usd"] == 500.5
    assert rise[2]["walk"]["depth_at_and_beyond_usd"] == 5100.0 and rise[2]["walk"]["ratio_pct"] == 1.98
    assert rise[4]["notional_usd"] == 4998.0 and rise[4]["orders"] == 1 and rise[4]["walk"]["depth_between_usd"] == 500.5
    assert rise[4]["walk"]["depth_at_and_beyond_usd"] == 5100.0 and rise[4]["walk"]["ratio_pct"] == 98.0
    assert rise[4]["band"] == {"lo": "102", "hi": "102.5", "near_px": "102", "far_px": "102.5", "from_pct": 2.0, "to_pct": 2.5}
    assert rise[5]["orders"] == 0 and rise[5]["notional_usd"] is None and rise[5]["walk"]["ratio_pct"] is None
    assert rise[5]["walk"]["ratio_reason"] == "no triggers in this band"
    assert rise[5]["walk"]["depth_between_usd"] == 5600.5 and rise[5]["walk"]["depth_at_and_beyond_usd"] == 0.0
    assert rise[5]["band"]["near_px"] == "102.5" and rise[5]["band"]["far_px"] == "103"
    # clusters: the two largest stated notionals per side, in rank order, with the report's sentence
    assert [(x["fires_on"], x["k"], x["rank"]) for x in cl] == [("fall", 5, 1), ("fall", 2, 2), ("rise", 4, 1), ("rise", 2, 2)]
    assert cl[0]["text"] == ("Sell-side triggers at 97.5 to 97 (-2.5% to -3.0%): $292 across 2 orders from 2 accounts; "
                             "the bids at and below it total $3K, so this cluster is 10% of the liquidity it would walk")
    assert cl[1]["text"] == ("Sell-side triggers at 99 to 98.5 (-1.0% to -1.5%): at least $99 across 2 orders from 2 accounts; "
                             "the bids at and below it total $5K, so this cluster is at least 2.0% of the liquidity it would "
                             "walk; 1 of 2 orders reports no size, so this is understated")
    assert cl[1]["lower_bound"] is True and cl[1]["unknown_size_orders"] == 1 and cl[0]["lower_bound"] is False
    assert cl[2]["text"] == ("Buy-side triggers at 102 to 102.5 (+2.0% to +2.5%): $5K across 1 order from 1 account; "
                             "the asks at and above it total $5K, so this cluster is 98% of the liquidity it would walk")
    assert cl[3]["text"] == ("Buy-side triggers at 101 to 101.5 (+1.0% to +1.5%): $101 across 1 order from 1 account; "
                             "the asks at and above it total $5K, so this cluster is 2.0% of the liquidity it would walk")
    # exclusions, each counted once in the map's order, with their notional; the engine's trailing count beside them
    assert ex["trailing_stops"] == {"orders": 3, "notional_usd": None, "reason": ex["trailing_stops"]["reason"]}
    assert (ex["implausible_price"]["orders"], ex["implausible_price"]["accounts"]) == (2, 1)
    assert ex["implausible_price"]["notional_usd"] == 90000000000.0 and ex["implausible_price"]["by_category"] == {
        "buy_stop_on_rise": 1, "sell_stop_on_fall": 1}
    # the size outliers: the $980,000 sell stop against $2,910 of bids at and beyond its band, and the $307.50 buy stop
    # against no asks at all; the rule as served, one sentence, beside the counts it produced
    assert ex["size_outlier"] == {"orders": 2, "sized_orders": 2, "unknown_size_orders": 0, "accounts": 2,
                                  "notional_usd": 980307.5, "lower_bound": False,
                                  "by_category": {"buy_stop_on_rise": 1, "sell_stop_on_fall": 1},
                                  "rule": "an order whose stated size, at its trigger price, is worth more than everything "
                                          "resting at and beyond its band out to 10% on the side it fires into (the bids "
                                          "below for a fall trigger, the asks above for a rise trigger: the band's walk "
                                          "depth_at_and_beyond_usd), so it could not be walked through the book; orders "
                                          "with no stated size (whole-position) never qualify"}
    assert ex["already_true_at_price"]["orders"] == 2 and ex["already_true_at_price"]["notional_usd"] == 200.0
    assert ex["already_true_at_price"]["by_category"] == {"buy_stop_on_rise": 1, "sell_stop_on_fall": 1}
    assert ex["beyond_range"]["orders"] == 2 and ex["beyond_range"]["unknown_size_orders"] == 1
    assert ex["beyond_range"]["notional_usd"] == 85.0
    assert ex["accounted"] == {"waiting_triggers": 15, "in_bands": 7, "excluded_here": 8, "complete": True}
    assert ex["order"] == ["trailing_stops (engine)", *EXCL] == ["trailing_stops (engine)", "implausible_price",
                                                                 "already_true_at_price", "beyond_range", "size_outlier"]
    assert "the size test last, inside the bands" in ex["note"]
    # the census: the applied set, the exclusions, the book in total and inside the range; no bands view and no
    # snapshot totals here, so those counts are null with the reason, never 0
    cen = out["census"]
    assert (cen["triggers_total"], cen["unsized_total"], cen["executable_orders_total"]) == (15, 3, 5)
    assert cen["excluded_by_engine"] == {"trailing_stops": 3, "unclassified": 0}
    assert cen["excluded"] == {"implausible_price": 2, "already_true_at_price": 2, "beyond_range": 2, "size_outlier": 2,
                               "excluded_by_engine": {"trailing_stops": 3, "unclassified": 0}}
    assert cen["executable_within_10pct"]["orders"] == 5 and cen["executable_within_10pct"]["ref_px"] == "100"
    assert cen["executable_within_10pct"]["ref_px_source"].startswith("the book mid") and cen["executable_within_10pct"]["range_bps"] == 1000
    assert cen["executable_to_trigger_ratio"] == 0.33                       # 5 / 15
    assert cen["within_10pct"] is None and cen["within_10pct_reason"] == "no bands view from the engine yet"
    assert cen["outside"] is None and cen["unsized_within_10pct"] is None and cen["bands_view"] is None
    assert cen["snapshot_totals"] is None and cen["snapshot_totals_reason"] == "no snapshot totals yet"
    json.dumps(out)                                              # everything is JSON as built


def test_census_sums_the_engines_bands_view_and_counts_the_book_inside_its_range():
    bands = {"ref_px": "100", "range_bps": 1000, "width": "1", "lo": "90", "hi": "110", "excluded": {"trailing": 3, "unclassified": 0},
             "rows": [["98", "99", "stop", "A", 5, "1", 2], ["97", "98", "take_profit", "B", 3, "0", 3],
                      ["102", "103", "stop", "B", 4, "2", 0]],
             "outside": {"below": [["stop", "A", 4, "2", 1]], "above": [["take_profit", "A", 2, "0", 2]]},
             "_block": 7, "_block_time_ms": 1_700_000_000_500, "_seq": 9}
    mf = _mf(BOOK, TRIG, bands=bands)
    cen = panels.stop_map(mf, panels.depth_bands(mf.book, mf.book.mid()), None)["census"]
    assert (cen["within_10pct"], cen["unsized_within_10pct"], cen["outside"]) == (12, 5, 6)     # summed, not recounted
    bv = cen["bands_view"]
    assert bv["by_category"] == {"buy_stop_on_rise": {"orders": 4, "unsized": 0}, "buy_take_profit_on_fall": {"orders": 3, "unsized": 3},
                                 "sell_stop_on_fall": {"orders": 5, "unsized": 2}}
    assert bv["outside"] == {"below": {"orders": 4, "unsized": 1}, "above": {"orders": 2, "unsized": 2}}
    assert (bv["ref_px"], bv["range_bps"], bv["block"], bv["seq"], bv["excluded_by_engine"]) == ("100", 1000, 7, 9, bands["excluded"])
    ex = cen["executable_within_10pct"]
    assert ex["orders"] == 5 and ex["ref_px"] == "100" and ex["ref_px_source"].startswith("the bands view's ref_px")
    # a narrower engine range counts fewer executable orders from the book (the 102 ask is 2% away)
    mf = _mf(BOOK, TRIG, bands=dict(bands, range_bps=150))
    cen = panels.stop_map(mf, panels.depth_bands(mf.book, mf.book.mid()), None)["census"]
    assert cen["executable_within_10pct"] == dict(cen["executable_within_10pct"], orders=3, range_bps=150)
    assert "liquidation" not in json.dumps(cen).lower()


def test_whole_position_orders_never_count_as_size_outliers_and_never_make_a_zero():
    rows = [[1, "A", "stop", "99", "0", True, _u(1), 1], [2, "A", "stop", "98.9", "0", True, _u(2), 1],
            [3, "B", "stop", "101", "0.001", False, _u(3), 1],      # the only sized order: $0.10 against $5,100
            [4, "B", "take_profit", "96.1", "0", True, _u(4), 1]]   # fall k7, whole-position, nothing resting at and beyond it
    mf = _mf(BOOK, rows)
    out = panels.stop_map(mf, panels.depth_bands(mf.book, mf.book.mid()), None)
    ex, fall = out["exclusions"], out["map"]["sides"]["fall"]["bands"]
    assert ex["size_outlier"]["orders"] == 0 and ex["size_outlier"]["notional_usd"] is None
    assert ex["size_outlier"]["rule"].endswith("orders with no stated size (whole-position) never qualify")
    assert fall[2]["orders"] == 2 and fall[2]["notional_usd"] is None and fall[2]["sz"] is None
    assert fall[2]["notional_reason"] == "every order is whole-position: size unknown, not 0 (2 orders, none with a stated size)"
    assert fall[2]["lower_bound"] is False and fall[2]["walk"]["lower_bound"] is False
    assert fall[2]["walk"]["ratio_pct"] is None and fall[2]["walk"]["ratio_reason"] == fall[2]["notional_reason"]
    # with no size there is nothing to weigh against the depth: the order at the empty band stays in it, size unknown
    assert fall[7]["orders"] == 1 and fall[7]["unknown_size_orders"] == 1 and fall[7]["notional_usd"] is None
    assert fall[7]["walk"]["depth_at_and_beyond_usd"] == 0.0 and fall[7]["walk"]["ratio_pct"] is None
    assert fall[7]["walk"]["ratio_reason"] == fall[7]["notional_reason"]
    assert [(x["fires_on"], x["k"]) for x in out["clusters"]] == [("rise", 2)]    # a band without a stated notional never ranks
    rows_all = [r for s in out["map"]["sides"].values() for r in s["bands"]]
    assert all(r["notional_usd"] != 0 and r["walk"]["ratio_pct"] != 0 for r in rows_all)     # unknown, never 0
    assert all("; 0 of them" not in x["text"] for x in out["clusters"])
    assert out["exclusions"]["accounted"] == {"waiting_triggers": 4, "in_bands": 4, "excluded_here": 0, "complete": True}
    # no sized trigger at all: nothing is excluded, the rule reads the same, and nothing ranks
    mf = _mf(BOOK, rows[:2])
    out = panels.stop_map(mf, panels.depth_bands(mf.book, mf.book.mid()), None)
    assert out["exclusions"]["size_outlier"]["orders"] == 0 and out["exclusions"]["size_outlier"]["rule"] == ex["size_outlier"]["rule"]
    assert out["clusters"] == []


def test_a_size_outlier_is_an_order_worth_more_than_the_depth_its_band_would_walk():
    """The size test is absolute and per order (Kevin, Nov 9): the bar is the resting depth at and beyond the order's
    band on the side it fires into, the band's walk depth_at_and_beyond_usd. Equal stays and a cent over goes; the bar is
    the band's, so depth nearer the price than the band doesn't count; fall triggers are weighed against the bids and
    rise triggers against the asks; nothing resting at and beyond a band makes every sized trigger there an outlier
    while a whole-position order there stays; and the test comes last, so an order that is implausible, already true
    or beyond 10% is counted there whatever its size."""
    book = [[1, "B", "99", "10", _u(1), 1],         # bids: k2 $990
            [2, "B", "95", "10", _u(2), 1],         # k10 $950: the bar for fall bands k3..k10
            [3, "A", "101", "10", _u(3), 1],        # asks: k2 $1,010
            [4, "A", "105", "10", _u(4), 1]]        # k10 $1,050: the bar for rise bands k3..k10
    trig = [[11, "A", "stop", "95", "10", False, _u(11), 1],          # fall k10: $950, the bar exactly: stays
            [12, "A", "stop", "95", "10.01", False, _u(12), 1],       # fall k10: $950.95, 95 cents over: an outlier
            [13, "A", "stop", "97", "9", False, _u(13), 1],           # fall k6: $873 against the $950 beyond it (its own band empty): stays
            [14, "B", "take_profit", "97", "10", False, _u(14), 1],   # fall k6 (a short's take-profit fires on a fall): $970 > $950: an outlier
            [15, "A", "stop", "99", "19", False, _u(15), 1],          # fall k2: $1,881 against $1,940 (the 990 and the 950): stays
            [16, "A", "stop", "96", "19", False, _u(16), 1],          # fall k8: $1,824 against $950 (the 990 nearer the price doesn't count): an outlier
            [17, "B", "stop", "103", "10", False, _u(17), 1],         # rise k6: $1,030 against the $1,050 of asks beyond it: stays
            [18, "B", "stop", "103", "10.2", False, _u(18), 1],       # rise k6: $1,050.60 > $1,050: an outlier
            [19, "A", "stop", "94", "0.001", False, _u(19), 1],       # fall k12: $0.09 against nothing resting at and beyond it: an outlier
            [20, "A", "stop", "94", "0", True, _u(20), 1],            # fall k12, whole-position: no stated size, never an outlier
            [21, "A", "stop", "85", "10000", False, _u(21), 1],       # 15% away and huge: beyond_range, judged before size
            [22, "A", "stop", "100.5", "10000", False, _u(22), 1],    # a fall trigger above the price and huge: already true, judged before size
            [23, "A", "stop", "0.5", "10000", False, _u(23), 1]]      # an implausible price and huge: implausible_price
    mf = _mf(book, trig)
    out = panels.stop_map(mf, panels.depth_bands(mf.book, mf.book.mid()), None)
    fall, rise, ex = out["map"]["sides"]["fall"]["bands"], out["map"]["sides"]["rise"]["bands"], out["exclusions"]
    assert [b["walk"]["depth_at_and_beyond_usd"] for b in fall] == [1940.0] * 3 + [950.0] * 8 + [0.0] * 9
    assert [b["walk"]["depth_at_and_beyond_usd"] for b in rise] == [2060.0] * 3 + [1050.0] * 8 + [0.0] * 9
    assert (fall[10]["orders"], fall[10]["notional_usd"], fall[10]["walk"]["ratio_pct"]) == (1, 950.0, 100.0)
    assert (fall[6]["orders"], fall[6]["notional_usd"], fall[6]["walk"]["ratio_pct"]) == (1, 873.0, 91.89)
    assert (fall[2]["orders"], fall[2]["notional_usd"], fall[2]["walk"]["ratio_pct"]) == (1, 1881.0, 96.96)
    assert fall[8]["orders"] == 0 and fall[12]["orders"] == 1 and fall[12]["unknown_size_orders"] == 1
    assert fall[12]["notional_usd"] is None and fall[12]["walk"]["ratio_pct"] is None
    assert (rise[6]["orders"], rise[6]["notional_usd"], rise[6]["walk"]["ratio_pct"]) == (1, 1030.0, 98.1)
    assert sum(b["orders"] for b in fall + rise) == 5
    so = ex["size_outlier"]
    assert (so["orders"], so["sized_orders"], so["unknown_size_orders"], so["accounts"]) == (5, 5, 0, 5)
    assert so["notional_usd"] == 4795.64                      # 950.95 + 970 + 1,824 + 1,050.60 + 0.094
    assert so["by_category"] == {"buy_stop_on_rise": 1, "buy_take_profit_on_fall": 1, "sell_stop_on_fall": 3}
    assert (ex["beyond_range"]["orders"], ex["beyond_range"]["notional_usd"]) == (1, 850000.0)
    assert (ex["already_true_at_price"]["orders"], ex["already_true_at_price"]["notional_usd"]) == (1, 1005000.0)
    assert (ex["implausible_price"]["orders"], ex["implausible_price"]["notional_usd"]) == (1, 5000.0)
    assert ex["accounted"] == {"waiting_triggers": 13, "in_bands": 5, "excluded_here": 8, "complete": True}
    assert [(x["fires_on"], x["k"]) for x in out["clusters"]] == [("fall", 2), ("fall", 10), ("rise", 6)]
    assert out["clusters"][1]["text"] == ("Sell-side triggers at 95 to 94.5 (-5.0% to -5.5%): $950 across 1 order from 1 account; "
                                          "the bids at and below it total $950, so this cluster is 100% of the liquidity it would walk")
    assert not hasattr(panels, "MAP_OUTLIER_MULTIPLE")        # the median multiple is gone


def test_without_a_valid_book_the_map_and_clusters_are_unavailable_with_the_reason():
    mf = _mf(BOOK, TRIG)
    out = panels.stop_map(mf, None, "book INVALID: not recovered since a gap")
    assert out["map"] == {"available": False, "reason": "needs a valid book for the reference price and the resting depth: "
                                                        "book INVALID: not recovered since a gap"}
    assert out["clusters"] is None and out["clusters_reason"] == out["map"]["reason"]
    assert out["exclusions"] == {"available": False, "reason": out["map"]["reason"]}
    cen = out["census"]                       # the feed-only counts stay; the book-built ones are null with the reason
    assert cen["triggers_total"] == 15 and cen["unsized_total"] == 3 and cen["excluded_by_engine"]["trailing_stops"] == 3
    for k in ("excluded", "executable_orders_total", "executable_within_10pct", "executable_to_trigger_ratio"):
        assert cen[k] is None and cen[f"{k}_reason"] == out["map"]["reason"], k
    assert cen["within_10pct"] is None and cen["within_10pct_reason"] == "no bands view from the engine yet"


def test_thinning_bands_carry_the_cumulative_depth_from_the_price():
    _, v = _view(XY)
    th, m = v["thinning"], v["stops"]["map"]
    for key, d in (("bids_below", "fall"), ("asks_above", "rise")):
        cum = 0.0
        for b in th[key]["bands"]:
            cum += b["depth_usd"]
            assert abs(b["cumulative_from_price_usd"] - cum) < 0.01 * BANDS
        assert th[key]["bands"][-1]["cumulative_from_price_usd"] == m["sides"][d]["depth_total_usd"]
    assert "cumulative_from_price_usd" in th["units"]


def test_the_replay_view_carries_the_same_map_and_the_tools_serve_it(surfaces):
    _, live = _view(SP)
    _, rep = _view(SP, "REPLAY")
    assert rep["provenance"]["mode"] == "REPLAY" and rep["families"]["triggers"]["state"] == "REPLAY"
    assert rep["stops"]["map"]["available"] and rep["stops"]["clusters"] == live["stops"]["clusters"]
    assert rep["stops"]["exclusions"] == live["stops"]["exclusions"]
    assert rep["stops"]["map"]["sides"] == live["stops"]["map"]["sides"]
    # through the plugin: node_stops (chat and tab) and the tab's snapshot show the same map, as JSON
    plugin = surfaces([SP])
    h, msgs = read_recording(XYZ)
    c = plugin.attach(client())
    c.on_line(hello_from(h), h["producer"]["started_ms"], 0)
    feed(c, msgs)
    v = plugin.read(SP, msgs[-1][0])
    ns = plugin.last["node_stops"]["stops"]
    assert ns["clusters"] == jsonable(v["stops"]["clusters"]) and ns["map"]["available"] and ns["exclusions"]["available"]
    assert plugin.last["tab"]["node"]["live"]["stops"]["clusters"] == ns["clusters"]
    assert all(x["text"].startswith(("Sell-side triggers at", "Buy-side triggers at")) for x in ns["clusters"])


def test_serving_time_aging_keeps_the_map_and_the_panel_labelled_when_the_families_lapse():
    """Changed by Kevin, Nov 10: a late family labels, it doesn't withhold. LIVE at build time on the recorded delay;
    the limits are passed so aging past them lapses the families to BEHIND. The stops panel (rows, map, clusters,
    exclusions, census) and the thinning panel are then served as built, each carrying its family's state and an
    "as of" note that states the block time and the delay, whichever family lapses: both, the book alone (the
    trigger family judged by a wider limit) or the bands alone; within the limit nothing is labelled."""
    from hl_desk.util import iso_ms
    words = ("freshness", "as_of_note")
    same = lambda a, b: {k: a[k] for k in a if k not in words} == {k: b[k] for k in b if k not in words}
    note = lambda label, st: f"as of the node's {label} at {iso_ms(st['watermark_block_time_ms'])}, {st['delay_ms'] / 1000:.1f} s behind"
    c, v = _view(SP, cfg={"delayed_after_ms": 10 ** 9, "stale_after_ms": 2 * 10 ** 9})
    assert v["families"]["book"]["state"] == "LIVE" == v["families"]["triggers"]["state"]
    assert v["stops"]["map"]["available"] and v["stops"]["clusters"]
    assert v["stops"]["freshness"] == "LIVE" == v["thinning"]["freshness"]
    assert v["stops"]["as_of_note"] is None and v["thinning"]["as_of_note"] is None
    # within the limit: everything stays, unlabelled
    ok = serving.aged_view(v, 1_000)
    assert ok["stops"] == v["stops"] and ok["thinning"] == v["thinning"]
    # both families lapse: the whole panel is served as built, labelled BEHIND, the delay stated
    aged = serving.aged_view(v, 1.5 * 10 ** 9)
    bst, tst = aged["families"]["book"], aged["families"]["triggers"]
    assert bst["state"] == "BEHIND" == tst["state"] and bst["delay_ms"] > 10 ** 9
    st = aged["stops"]
    assert same(st, v["stops"]) and st["freshness"] == "BEHIND" and st["as_of_note"] == note("waiting stops", tst)
    assert st["map"]["available"] and st["clusters"] == v["stops"]["clusters"] and st["census"] == v["stops"]["census"]
    assert same(aged["thinning"], v["thinning"]) and aged["thinning"]["freshness"] == "BEHIND"
    assert aged["thinning"]["as_of_note"] == note("book", bst)
    # the book alone lapses (the trigger family judged by a wider limit): the panel reads current and the map built
    # on the late book stays; the thinning panel, built on the book, carries the label
    v2 = dict(v, families=dict(v["families"], triggers=dict(v["families"]["triggers"], delayed_after_ms=10 ** 12,
                                                             stale_after_ms=10 ** 13)))
    aged2 = serving.aged_view(v2, 1.5 * 10 ** 9)
    assert aged2["families"]["book"]["state"] == "BEHIND" and aged2["families"]["triggers"]["state"] == "LIVE"
    assert aged2["stops"] == v["stops"] and aged2["thinning"]["freshness"] == "BEHIND"
    assert aged2["thinning"]["as_of_note"] == note("book", aged2["families"]["book"])
    # the bands family alone lapses: the census counts read from the bands view stay, nothing is labelled
    v3 = dict(v, families=dict(v["families"], **{f: dict(v["families"][f], delayed_after_ms=10 ** 12, stale_after_ms=10 ** 13)
                                                 for f in ("book", "triggers")}))
    aged3 = serving.aged_view(v3, 1.5 * 10 ** 9)
    assert aged3["families"]["bands"]["state"] == "BEHIND" and aged3["families"]["book"]["state"] == "LIVE"
    assert aged3["stops"] == v["stops"] and aged3["thinning"] == v["thinning"]


def test_recorded_window_census_reads_the_engines_bands_view_the_book_and_the_snapshot_totals():
    c, v = _view(SP)
    mf = c.markets[SP]
    cen = v["stops"]["census"]
    assert cen["triggers_total"] == len(mf.triggers.orders) and cen["triggers_through_seq"] == mf.triggers.through_seq
    assert cen["unsized_total"] == sum(1 for g in mf.triggers.orders.values() if g.position_tpsl) > 0
    # within 10%: the engine's bands view, summed as it stands in the client, with its own moment
    bv = mf.bands
    assert cen["within_10pct"] == sum(r[4] for r in bv["rows"]) and cen["unsized_within_10pct"] == sum(r[6] for r in bv["rows"])
    assert cen["outside"] == sum(r[2] for w in ("below", "above") for r in bv["outside"][w])
    assert cen["bands_view"]["ref_px"] == bv["ref_px"] and cen["bands_view"]["seq"] == bv["_seq"] and cen["bands_view"]["block"] == bv["_block"]
    assert cen["bands_view"]["excluded_by_engine"] == bv["excluded"] == {"trailing": cen["excluded_by_engine"]["trailing_stops"],
                                                                         "unclassified": 0}
    assert cen["within_10pct"] + cen["outside"] == cen["triggers_total"]        # the engine's view counts the same set
    assert sum(x["orders"] for x in cen["bands_view"]["by_category"].values()) == cen["within_10pct"]
    # the exclusions are the map's; the executable book is the client's, in total and inside the bands view's range
    ex = v["stops"]["exclusions"]
    assert cen["excluded"] == {k: ex[k]["orders"] for k in EXCL} | {"excluded_by_engine": cen["excluded_by_engine"]}
    assert cen["executable_orders_total"] == len(mf.book.orders)
    ref = D(bv["ref_px"])
    lo, hi = ref * D("0.9"), ref * D("1.1")
    assert cen["executable_within_10pct"]["orders"] == sum(1 for o in mf.book.orders.values() if lo <= o.px <= hi)
    assert cen["executable_within_10pct"]["ref_px"] == bv["ref_px"] and cen["executable_within_10pct"]["book_time_ms"] == mf.block_time_ms
    assert cen["executable_to_trigger_ratio"] == round(len(mf.book.orders) / len(mf.triggers.orders), 2)
    # the snapshots' stated totals, with their block: the last complete snapshot of each family in the recording
    h, msgs = read_recording(XYZ)
    bs = [m for _, m in msgs if m.get("market") == SP and m["kind"] == "book_snapshot" and m["body"]["part"] == 1][-1]
    ts = [m for _, m in msgs if m.get("market") == SP and m["kind"] == "trig_snapshot" and m["body"]["part"] == 1][-1]
    snap = cen["snapshot_totals"]
    assert snap["book"] == {"total_orders": bs["body"]["total_orders"], "block": bs["block"], "block_time_ms": bs["block_time_ms"]}
    assert snap["triggers"] == {"total_triggers": ts["body"]["total_triggers"], "block": ts["block"], "block_time_ms": ts["block_time_ms"]}
    assert snap["executable_to_trigger_ratio"] == round(bs["body"]["total_orders"] / ts["body"]["total_triggers"], 2)
    assert bs["body"]["total_orders"] > 1000 and ts["body"]["total_triggers"] > 1000
    assert "liquidation" not in json.dumps(cen).lower()
