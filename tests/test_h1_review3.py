"""Regression test for Codex h1 review 3 (commit f92239f), from its reproduction.

B1: when a returned level on one side has a missing or invalid px or sz, that side's largest returned level can't be
    established (the unreadable level could be the largest): levels.largest withholds it with the reason, and the
    chart draws no largest-level line for that side, saying why. The other side is unaffected.
"""

from __future__ import annotations

import json

from helpers import NS, T0, build, make_desk, open_segment, trade

from hl_desk import tools


def _desk_with_codexs_book():
    """Codex's fixture: bids 99.9 x 1, 99.8 x 2; asks 100.1 x 1, 100.2 x (missing sz), 100.3 x 3, as a fresh full book."""
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 1000, 100.0, 0.01, "B", "0x" + "1" * 40, "0x" + "2" * 40, 7)], NS, T0 + 1000)
    lv = lambda px, sz, n: ({"px": str(px), "n": n} | ({"sz": str(sz)} if sz is not None else {}))
    bids = [lv(99.9, 1, 2), lv(99.8, 2, 3)] + [lv(99.7 - i * 0.1, 0.5, 1) for i in range(4)]
    asks = [lv(100.1, 1, 4), lv(100.2, None, 9), lv(100.3, 3, 5)] + [lv(100.4 + i * 0.1, 0.5, 1) for i in range(3)]
    ms.on_book({"coin": "BTC", "time": T0 + 1500, "levels": [bids, asks]}, NS + 500 * 10 ** 6, T0 + 1500)
    return desk


def test_b1_a_malformed_level_withholds_that_sides_largest_and_its_chart_line():
    desk = _desk_with_codexs_book()
    snap = build(desk, NS + 600 * 10 ** 6, wall_ms=T0 + 1700)
    lad = snap["markets"]["BTC"]["book"]["ladder"]
    assert lad["malformed_levels"]["ask"] == [{"index": 1, "missing_or_invalid": ["sz"]}]
    assert lad["largest"]["ask"] is None and "can't be established" in lad["largest"]["ask_reason"]
    assert lad["largest"]["bid"] == {"px": 99.8, "sz": 2.0, "api_count": 3, "notional_usd": 199.6}   # bids are fine
    chart = snap["markets"]["BTC"]["chart"]
    kinds = [ln["kind"] for ln in chart["price_lines"]]
    assert "largest_ask_level" not in kinds and "largest_bid_level" in kinds
    assert chart["price_lines_withheld"] == [{"kind": "largest_ask_level", "reason": lad["largest"]["ask_reason"]}]
    # Through the actual hl_market handler, as the tool and the commentary skill see it.
    m = tools.hl_market(dict(snap, served={"latest": False, "age_s": 0.1}), {"market": "BTC"})
    assert m["levels"]["largest"]["ask"] is None and m["levels"]["largest"]["ask_reason"]
    assert json.loads(json.dumps(m))["levels"]["largest"]["bid"]["px"] == 99.8
