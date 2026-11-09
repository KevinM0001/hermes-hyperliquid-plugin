"""The Node Project agent's contest review (Nov 9), the plugin's blocking items, on the recorded xyz window.

B2: whole-position TP/SL orders have no stated size (the feed sends sz "0" with position_tpsl true). A stops band or
    ladder cell made only of them shows its size as unknown, never 0; one that holds some says its stated size is a
    lower bound; the commentary skill tells the agent so.
B3: the stops say the waiting-trigger set is checked against the venue only at restart (node_stops, the tab, README).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hl_desk.node import panels

from node_helpers import OPTS, XYZ, client, hello_from, read_recording

ROOT = Path(__file__).resolve().parent.parent
SP = "xyz:SP500"


def _view():
    h, msgs = read_recording(XYZ)
    c = client()
    c.on_line(hello_from(h), h["producer"]["started_ms"], 0)
    for rx, m in msgs:
        c.on_line(m, rx, rx * 10 ** 6)
    rx = msgs[-1][0]
    return c, panels.market_view(c, SP, OPTS, rx, rx * 10 ** 6)


def test_b2_bands_of_whole_position_orders_have_unknown_size_and_mixed_ones_a_lower_bound():
    c, v = _view()
    st = v["stops"]
    rows = st["rows"] + [r for r in st["outside"].values() if r.get("orders")]
    only = [r for r in rows if r["unknown_size_orders"] and r["sized_orders"] == 0]
    mixed = [r for r in rows if r["unknown_size_orders"] and r["sized_orders"]]
    plain = [r for r in rows if not r["unknown_size_orders"]]
    assert only and mixed and plain                          # all three occur on this window
    for r in only:
        assert r["sz"] is None and f"all {r['unknown_size_orders']} orders are whole-position" in r["sz_reason"]
    for r in mixed:
        assert r["sz"] is not None and r["sz"] > 0 and r["sz_lower_bound"] is True
        assert f"{r['unknown_size_orders']} whole-position TP/SL orders" in r["sz_note"]
    for r in plain:
        assert "sz_lower_bound" not in r and "sz_reason" not in r
    # The stated sizes agree with the trigger set counted here: only orders with a size count, as exact decimals.
    trig = c.markets[SP].triggers
    whole = sum(1 for g in trig.orders.values() if g.position_tpsl)
    assert whole == sum(r["unknown_size_orders"] for r in rows)          # every one is counted in a row or outside
    assert all(g.sz == Decimal(0) for g in trig.orders.values() if g.position_tpsl)


def test_b2_ladder_stops_cells_follow_the_same_rule():
    _, v = _view()
    cells = [r["stops_to_next_level"] for side in ("bids", "asks") for r in v["ladder"][side]
             if isinstance(r.get("stops_to_next_level"), dict) and r["stops_to_next_level"].get("orders")]
    assert cells
    for s in cells:
        if s["unknown_size_orders"] == s["orders"]:
            assert s["sz"] is None and s["sz_reason"]
        elif s["unknown_size_orders"]:
            assert s["sz_lower_bound"] is True
        else:
            assert "sz_lower_bound" not in s


def test_b2_b3_the_skill_and_the_stops_say_so():
    _, v = _view()
    assert "only at restart" in v["stops"]["venue_check"]
    skill = (ROOT / "skills" / "hyperliquid-commentary" / "SKILL.md").read_text()
    assert "Always give a band's `unknown_size_orders`" in skill and "lower bound" in skill
    assert "checked against the venue only when the engine restarts" in skill
    readme = (ROOT / "README.md").read_text()
    assert "Checked against the venue only at restart." in readme and "never 0" in readme
    app = (ROOT / "dashboard" / "src" / "app.js").read_text()
    assert "Checked against the venue only at restart" in app and "whole-position)" in app
