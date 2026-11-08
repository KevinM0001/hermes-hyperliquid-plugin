"""Book metrics: depth bands only where returned levels reach them, empty sides, API count labels."""

from __future__ import annotations

import pytest

from hl_desk import book as bk

from helpers import T0, book_msg, make_desk, build


def levels(start, step, count, sz=1.0, n=3):
    return [(round(start + i * step, 6), sz, n) for i in range(count)]


def _book(kind, bids, asks, mid=100.0):
    return {"kind": kind, "usable": True, "bids": bids, "asks": asks, "mid": mid, "time_ms": T0,
            "bucket_width": bk.bucket_width(bids, asks), "bad": {}}


def test_band_not_reached_falls_back_to_observed_depth():
    bids = levels(99.99, -0.01, 20)          # deepest bid 99.80: -0.2% from mid 100.0
    asks = levels(100.01, 0.01, 20)          # deepest ask 100.20: +0.2%
    mid = bk.mid_of(bids, asks)
    assert mid == pytest.approx(100.0)
    bands = bk.depth_bands(_book("full precision", bids, asks, mid), [])
    b025 = bands[0]
    assert b025["band_pct"] == 0.25
    ex = b025["exact"]
    assert ex["bid"]["status"] == "unavailable" and ex["bid"]["notional_usd"]["value"] is None
    assert ex["bid"]["notional_usd"]["reason"] == "band not covered by the full-precision returned levels"
    assert ex["ask"]["status"] == "unavailable"
    assert ex["imbalance"]["value"] is None
    assert b025["approximate"]["bid"]["status"] == "unavailable"     # no aggregated book supplied
    obs = bk.observed_depth(bids, mid, "bid")
    assert obs["label"] == "observed depth within returned levels"
    assert obs["notional_usd"]["value"] == pytest.approx(sum(px * sz for px, sz, _ in bids))
    assert obs["span_pct"] == pytest.approx(0.2)


def test_exact_band_counts_only_full_precision_levels_inside_the_edge():
    bids = levels(99.9, -0.1, 20, sz=2.0)    # 99.9 .. 98.0
    asks = levels(100.1, 0.1, 20, sz=3.0)    # 100.1 .. 102.0
    bands = {b["band_pct"]: b for b in bk.depth_bands(_book("full precision", bids, asks), [])}
    # ±0.5%: bids >= 99.5 are 99.9 .. 99.5 (5 levels), asks <= 100.5 are 100.1 .. 100.5 (5 levels)
    exp_bid = sum(px * 2.0 for px in (99.9, 99.8, 99.7, 99.6, 99.5))
    exp_ask = sum(px * 3.0 for px in (100.1, 100.2, 100.3, 100.4, 100.5))
    ex = bands[0.5]["exact"]
    assert ex["bid"]["notional_usd"]["value"] == pytest.approx(exp_bid)
    assert ex["ask"]["notional_usd"]["value"] == pytest.approx(exp_ask)
    assert ex["bid"]["levels_counted"] == 5 and ex["bid"]["book"] == "full precision"
    assert ex["imbalance"]["value"] == pytest.approx((exp_bid - exp_ask) / (exp_bid + exp_ask), abs=1e-4)
    assert bands[1.0]["exact"]["bid"]["status"] == "covered"     # 98.0 <= 99.0


def test_coarse_buckets_are_a_labeled_approximation_with_a_range():
    coarse_bids = levels(99.9, -0.3, 20, sz=10.0)      # 99.9, 99.6, 99.3, ... buckets of width 0.3
    coarse_asks = levels(100.1, 0.3, 20, sz=10.0)
    fine = _book("full precision", levels(99.99, -0.01, 20), levels(100.01, 0.01, 20))   # reaches only 0.2%
    bands = {b["band_pct"]: b for b in bk.depth_bands(fine, [_book("nSigFigs=4", coarse_bids, coarse_asks)])}
    b = bands[0.5]
    assert b["exact"]["bid"]["status"] == "unavailable"            # exact needs full-precision coverage
    ap = b["approximate"]
    assert ap["label"] == "approximate: aggregated buckets" and "representative" in ap["convention"]
    # bid edge 99.5: buckets counted by representative price >= 99.5 are 99.9 and 99.6
    inside = [99.9, 99.6]
    approx = sum(px * 10.0 for px in inside)
    assert ap["bid"]["status"] == "approximate" and ap["bid"]["approx_usd"]["value"] == pytest.approx(approx)
    assert ap["bid"]["range_usd"] == [pytest.approx(approx - 99.6 * 10.0), pytest.approx(approx + 99.3 * 10.0)]
    assert ap["bid"]["aggregation"] == "nSigFigs=4" and ap["bid"]["bucket_width"] == pytest.approx(0.3)
    assert ap["imbalance"]["value"] is not None                    # both sides from one aggregated book
    stale = {"kind": "nSigFigs=3", "usable": False, "reason": "l2Book nSigFigs=3 (info) stale: ..."}
    only_stale = bk.depth_bands(fine, [stale])
    assert only_stale[0]["approximate"]["bid"]["status"] == "unavailable"
    assert only_stale[0]["approximate"]["bid"]["tried"][0]["reason"].startswith("l2Book nSigFigs=3 (info) stale")


def test_empty_side_is_null_with_reason_not_zero():
    top = bk.top_of_book([], [(100.0, 1.0, 1)])
    assert top["best_bid"]["value"] is None and top["best_bid"]["reason"] == "empty bid side"
    assert top["mid"]["value"] is None and top["spread"]["value"] is None
    band = bk.side_band([], 100.0, 0.25, "bid")
    assert band["covered"] is False and band["reason"] == "empty bid side"
    obs = bk.observed_depth([], 100.0, "bid")
    assert obs["notional_usd"]["value"] is None


def test_empty_side_through_the_snapshot():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, [], [(100.0, 1.0, 2), (100.1, 1.0, 3)]), 0, T0)
    snap = build(desk, 10**8)
    top = snap["markets"]["BTC"]["book"]["top"]
    assert top["best_bid"]["value"] is None and top["best_bid"]["reason"] == "empty bid side"
    assert top["best_ask"]["value"] == 100.0


def test_levels_are_labeled_api_count_and_aggregated():
    rows = bk.ladder_rows([(100.0, 2.0, 7)], [(100.5, 1.0, 1)])
    assert rows["bids"][0]["api_count"] == 7
    assert "n" not in rows["bids"][0]
    assert rows["aggregated"] is True
    assert "number of levels" in rows["api_count_note"] and "number of orders" in rows["api_count_note"]


def test_fast_and_full_books_are_told_apart_by_level_count():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    five = [(100.0 - i, 1.0, 1) for i in range(5)]
    twenty = [(100.0 - i, 1.0, 1) for i in range(20)]
    ms.on_book(book_msg("BTC", T0, five, [(101.0 + i, 1.0, 1) for i in range(5)]), 0, T0)
    assert "book_fast" in ms.books and "book_full" not in ms.books
    ms.on_book(book_msg("BTC", T0, twenty, [(101.0 + i, 1.0, 1) for i in range(20)]), 0, T0)
    assert len(ms.books["book_full"]["bids"]) == 20
    thin = [(100.0, 1.0, 1)]
    ms.on_book(book_msg("BTC", T0 + 500, thin, [(101.0, 1.0, 1)]), 0, T0 + 500)
    # Fewer than 5 levels on both sides: both subscriptions carry the same levels.
    assert ms.books["book_fast"]["time_ms"] == T0 + 500 and ms.books["book_full"]["time_ms"] == T0 + 500
