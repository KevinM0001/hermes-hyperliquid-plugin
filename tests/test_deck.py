"""Flow Deck regimes, latch, events and burst alerts (Handoff 1 step 5a).

Every regime is entered and left at the thresholds hl_deck.py documents
(ported into schema/metrics.v1.json), checked with values calculated here.
"""

from __future__ import annotations

import pytest

from hl_desk import deck, schema
from hl_desk.trades import Trade
from hl_desk.util import iso_ms

from helpers import A, B, C, T0, make_desk, open_segment, trade, build

K = schema.regime_constants()


def M(bps=0.0, net=0.0, vol=1e6, n=50, impact="auto", pressure=None):
    if impact == "auto":
        impact = abs(bps) / (abs(net) / 1e6) if abs(net) >= 100_000 else None
    return {"n": n, "bps": bps, "net": net, "vol": vol, "impact": impact, "pressure": pressure}


# --- classify: each regime at, just inside and just outside its thresholds -------------

def test_quiet_when_no_trades():
    assert deck.classify(None) == ("QUIET", None)
    assert deck.classify(M(n=0)) == ("QUIET", None)


def test_thin_vs_vacuum_below_the_volume_floor():
    assert deck.classify(M(bps=1.999, vol=149_999.99)) == ("THIN", None)
    assert deck.classify(M(bps=2.0, vol=149_999.99)) == ("VACUUM-UP", "thin")
    assert deck.classify(M(bps=-2.0, vol=149_999.99)) == ("VACUUM-DOWN", "thin")
    # At exactly 150,000 the volume floor no longer applies.
    assert deck.classify(M(bps=1.0, vol=150_000.0, net=0)) == ("BALANCED", None)


def test_pulled_offers_and_bids_at_their_thresholds():
    assert deck.classify(M(bps=5.0, net=-40_000)) == ("PULLED-OFFERS", None)
    assert deck.classify(M(bps=-5.0, net=40_000)) == ("PULLED-BIDS", None)
    assert deck.classify(M(bps=4.999, net=-40_000))[0] != "PULLED-OFFERS"
    assert deck.classify(M(bps=5.0, net=-39_999.99))[0] != "PULLED-OFFERS"
    assert deck.classify(M(bps=5.0, net=40_000))[0] != "PULLED-OFFERS"     # same sign: not divergence


def test_vacuum_by_impact_and_absorbing():
    # IMPACT = |bps| / (|net| / 1e6): 20 bps on $1M net = 20.0, the vacuum threshold.
    assert deck.classify(M(bps=20.0, net=1_000_000)) == ("VACUUM-UP", "impact")
    assert deck.classify(M(bps=-20.0, net=-1_000_000)) == ("VACUUM-DOWN", "impact")
    assert deck.classify(M(bps=19.99, net=1_000_000))[0] != "VACUUM-UP"
    # The |bps| >= 1.0 guard: IMPACT 20 with a 0.99 bps move isn't a vacuum.
    assert deck.classify(M(bps=0.99, net=49_500, vol=1e6, impact=20.0))[0] != "VACUUM-UP"
    assert deck.classify(M(bps=2.0, net=1_000_000)) == ("ABSORBING", None)        # IMPACT 2.0
    assert deck.classify(M(bps=2.0001, net=1_000_000))[0] != "ABSORBING"         # IMPACT just above 2.0
    # Below the $100K net floor IMPACT is n/a, so neither rule applies.
    assert deck.classify(M(bps=0.1, net=99_999.99)) == ("BALANCED", None)
    assert deck.classify(M(bps=0.1, net=100_000)) == ("ABSORBING", None)          # IMPACT 1.0


def test_trending_with_public_pressure_at_its_thresholds():
    assert deck.classify(M(bps=3.0, net=50_000, pressure=1.15)) == ("TRENDING-UP", None)
    assert deck.classify(M(bps=-3.0, net=-50_000, pressure=0.87)) == ("TRENDING-DOWN", None)
    assert deck.classify(M(bps=3.0, net=50_000, pressure=1.1499)) == ("BALANCED", None)
    assert deck.classify(M(bps=-3.0, net=-50_000, pressure=0.8701)) == ("BALANCED", None)
    assert deck.classify(M(bps=2.999, net=50_000, pressure=2.0)) == ("BALANCED", None)
    # PRESSURE unavailable (selling under the floor): a >= 3 bps move falls through to BALANCED, as in hl_deck.
    assert deck.classify(M(bps=3.0, net=50_000, pressure=None)) == ("BALANCED", None)
    # Node mode no longer changes anything, and UNRESOLVED no longer exists.
    assert deck.classify(M(bps=3.0, net=50_000), node_mode=True) == deck.classify(M(bps=3.0, net=50_000))


def test_pressure_is_aggressive_buy_over_sell_with_the_25k_floor():
    def window(buy, sell):
        return [_t(1, T0, 100.0, buy / 100.0, "B", A, B), _t(2, T0 + 1, 100.0, sell / 100.0, "A", A, C)]
    m = deck.window_metrics(window(57_500.0, 25_000.0))
    assert m["pressure"] == pytest.approx(57_500.0 / 25_000.0)          # at the floor: available
    assert deck.window_metrics(window(57_500.0, 24_999.0))["pressure"] is None   # just under: unavailable
    # Flips are ordinary aggressive executions here: every execution counts, whatever its position effect.
    m2 = deck.window_metrics(window(300_000.0, 200_000.0))
    assert m2["pressure"] == pytest.approx(1.5)


def test_regime_order_first_match_wins():
    # Divergence beats IMPACT: 5 bps up on -$1M net would be ABSORBING by IMPACT (5.0 > 2, < 20) but is PULLED-OFFERS.
    assert deck.classify(M(bps=5.0, net=-1_000_000)) == ("PULLED-OFFERS", None)
    # The volume floor beats everything.
    assert deck.classify(M(bps=50.0, net=-100_000, vol=140_000)) == ("VACUUM-UP", "thin")


# --- window metrics against an independent calculation --------------------------------

def _t(i, t, px, sz, side, buyer, seller):
    return Trade("BTC", t, px, sz, side, i, f"h{i}", buyer, seller, i)


def test_window_metrics_match_independent_values():
    trades = [_t(1, T0, 100.0, 2000.0, "B", A, B),     # buy  $200,000, taker A
              _t(2, T0 + 1, 100.5, 1000.0, "A", A, C),  # sell $100,500, taker C
              _t(3, T0 + 2, 101.0, 500.0, "B", B, C)]   # buy  $50,500,  taker B
    m = deck.window_metrics(trades)
    buy, sell = 200_000 + 50_500, 100_500
    assert m["net"] == pytest.approx(buy - sell)
    assert m["vol"] == pytest.approx(buy + sell)
    assert m["bps"] == pytest.approx((101.0 - 100.0) / 100.0 * 10000)
    assert m["impact"] == pytest.approx(100.0 / ((buy - sell) / 1e6))
    assert m["top_wallet"] == A and m["top_share"] == pytest.approx(200_000 / (buy + sell) * 100)
    assert m["accounts"] == 3 and m["n"] == 3


# --- ticks, latch and events through the market state ----------------------------------

def _feed_window(ms, start, end, px0, px1, buy_usd, sell_usd, tid0, mono):
    """Spread trades over (start, end]: one buy and one sell, then a price-setting trade."""
    ms.on_trades([trade("BTC", start + 1000, px0, buy_usd / px0, "B", A, B, tid0)], mono, start)
    ms.on_trades([trade("BTC", start + 2000, px0, sell_usd / px0, "A", B, C, tid0 + 1)], mono, start)
    ms.on_trades([trade("BTC", end - 500, px1, 0.0001, "B", A, B, tid0 + 2)], mono, end)


def _advance_to(ms, t_ms, tid, mono=0):
    ms.on_trades([trade("BTC", t_ms, 100.0, 0.0001, "B", A, B, tid)], mono, t_ms)


def test_latch_needs_two_ticks_and_events_carry_held_time():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    eng = ms.deck
    # Absorbing flow (big net buying, tiny move) for long enough to complete windows.
    tid = 1
    for k in range(0, 12):
        t = T0 + 1000 + k * 15_000
        ms.on_trades([trade("BTC", t, 100.0, 5000.0, "B", A, B, tid)], 0, t)
        tid += 1
    # ticks at T0+105s and T0+120s are the first complete ones (window start must be after T0)
    assert eng.last_complete_tick_ms is not None
    assert eng.confirmed == "ABSORBING"
    first_since = eng.since_ms
    assert first_since == T0 + 120_000            # confirmed on the 2nd consecutive complete tick
    assert not [e for e in eng.events if e["type"] == "regime_change"]   # first confirmation: no event
    # Now a vacuum: price runs up on small net flow.
    px = 100.0
    for k in range(12, 24):
        t = T0 + 1000 + k * 15_000
        px *= 1.003
        ms.on_trades([trade("BTC", t, px, 200.0, "B", A, B, tid)], 0, t)
        ms.on_trades([trade("BTC", t + 1, px, 1000.0, "A", B, C, tid + 1)], 0, t)
        tid += 2
    changes = [e for e in eng.events if e["type"] == "regime_change"]
    assert changes, "expected a confirmed regime change"
    ev = changes[-1]
    assert ev["from"] == "ABSORBING"
    assert ev["held_s"] == (ev["at_ms"] - first_since) / 1000


def test_flip_flop_does_not_confirm():
    eng = deck.DeckEngine("BTC", 10e6)
    eng.reset_segment(T0)

    class S:   # a store stub returning prepared windows per tick
        segment_start_ms = T0
        watermark_ms = None

        def __init__(self, seq):
            self.seq = seq

        def window(self, s, e, start_inclusive=True):
            return self.seq.pop(0)

    absorbing = [_t(1, T0, 100.0, 5000.0, "B", A, B), _t(2, T0 + 1, 100.0, 5000.0, "B", A, B)]
    thin = [_t(3, T0, 100.0, 1.0, "B", A, B)]
    seq = [absorbing, absorbing, thin, absorbing, thin, absorbing]
    st = S(seq)
    for i in range(6):
        eng._tick(st, T0 + 105_000 + i * 15_000, None)
    # ABSORBING confirmed on ticks 1-2; then THIN and ABSORBING alternate: THIN never confirms.
    assert eng.confirmed == "ABSORBING"
    assert not [e for e in eng.events if e["type"] == "regime_change"]


def test_incomplete_window_is_not_classified_and_numbers_are_null():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    _advance_to(ms, T0 + 50_000, 1)
    snap = build(desk, 1)
    card = snap["markets"]["BTC"]["deck"]["card"]
    assert card["regime"]["value"] is None
    assert "incomplete" in card["regime"]["reason"]
    assert all(v["value"] is None for k, v in card["numbers"].items())
    assert "incomplete" in card["numbers"]["pressure"]["reason"]
    assert card["confirmed_regime"] is None and card["live_classification"] is None


def test_gap_ends_the_regime_and_nothing_spans_it():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    tid = 1
    for k in range(0, 12):
        t = T0 + 1000 + k * 15_000
        ms.on_trades([trade("BTC", t, 100.0, 5000.0, "B", A, B, tid)], 0, t)
        tid += 1
    assert ms.deck.confirmed == "ABSORBING"
    ms.on_disconnect("test reconnect", T0 + 200_000)
    gap_ev = ms.deck.events[0]
    assert gap_ev["type"] == "regime_ended_by_gap" and gap_ev["from"] == "ABSORBING"
    assert ms.deck.confirmed is None
    # After reconnecting, the first 90 s are incomplete; the same flow must re-confirm from scratch.
    open_segment(ms, T0 + 300_000, conn=2)
    ms.on_trades([trade("BTC", T0 + 330_000, 100.0, 5000.0, "B", A, B, tid)], 0, T0 + 330_000)
    assert ms.deck.confirmed is None
    assert ms.deck.last_tick["complete"] is False


def test_sparse_is_a_flag_on_the_live_classification_only():
    eng = deck.DeckEngine("BTC", 10e6)
    eng.reset_segment(T0)

    class S:
        def window(self, s, e, start_inclusive=True):
            return [_t(1, T0, 100.0, 5000.0, "B", A, B), _t(2, T0 + 1, 100.0, 5000.0, "B", A, B)]

    eng._tick(S(), T0 + 105_000, None)
    eng._tick(S(), T0 + 120_000, None)
    assert eng.confirmed == "ABSORBING"            # the latch holds the real classification
    card = eng.card(None)
    assert card["regime"]["value"] == "ABSORBING"
    live = card["live_classification"]
    assert live["state"] == "ABSORBING" and live["sparse"] is True       # 2 trades < 5
    assert live["sparse_note"].startswith("Fewer than five executions")
    assert all(e.get("to") != "SPARSE" for e in eng.events)


def test_card_reports_confirmed_live_and_pending_separately():
    eng = deck.DeckEngine("BTC", 10e6)
    eng.reset_segment(T0)
    absorbing = [_t(i, T0 + i, 100.0, 5000.0, "B", A, B) for i in range(6)]
    thin = [_t(i, T0 + i, 100.0, 1.0, "B", A, B) for i in range(6)]

    class S:
        def __init__(self, w):
            self.w = w

        def window(self, s, e, start_inclusive=True):
            return self.w

    eng._tick(S(absorbing), T0 + 105_000, None)
    eng._tick(S(absorbing), T0 + 120_000, None)
    eng._tick(S(thin), T0 + 135_000, None)            # one THIN tick: pending, not confirmed
    card = eng.card(None)
    assert card["confirmed_regime"]["state"] == "ABSORBING" and card["confirmed_regime"]["since"]
    assert card["live_classification"]["state"] == "THIN"
    assert card["live_classification"]["meaning"]["state"] == "THIN"
    assert card["confirmed_regime"]["meaning"]["state"] == "ABSORBING"
    assert card["pending"] == {"state": "THIN", "ticks": 1, "of": 2, "validation": deck.VALIDATION}


# --- burst alerts ---------------------------------------------------------------------

def _burst_desk(threshold=10_000_000.0):
    desk = make_desk(burst_min_usd={"BTC": threshold})
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    # A small aggressive sell moves the watermark so the 60 s window lies inside the segment;
    # it is a sell, so it adds nothing to the buying side under test.
    ms.on_trades([trade("BTC", T0 + 61_000, 100.0, 0.0001, "A", B, A, 1)], 0, T0 + 61_000)
    return desk, ms


@pytest.mark.parametrize("notional,fires", [(9_999_999.99, False), (10_000_000.0, True), (10_000_000.01, True)])
def test_burst_threshold_below_at_above(notional, fires):
    desk, ms = _burst_desk()
    t = T0 + 70_000
    half = notional / 2
    ms.on_trades([trade("BTC", t, 100.0, half / 100.0, "B", A, B, 2)], 0, t)
    ms.on_trades([trade("BTC", t + 5000, 100.0, (notional - half) / 100.0, "B", C, B, 3)], 0, t + 5000)
    bursts = [e for e in ms.deck.events if e["type"] == "burst"]
    window_total = sum(x.notional for x in ms.store.window(t + 5000 - 60_000, t + 5000, start_inclusive=False)
                       if x.side == "B")
    assert window_total == pytest.approx(notional, abs=1e-6)
    assert bool(bursts) is fires
    if fires:
        b = bursts[0]
        assert b["side"] == "buying"
        assert b["aggressive_notional_usd"] == pytest.approx(notional, abs=0.01)
        listed = b["wallets"]["listed"]
        assert {r["address"] for r in listed} <= {A, C}       # only the burst's aggressive buyers
        assert B not in {r["address"] for r in listed}         # the passive seller never appears
        assert sum(r["share_pct"] for r in b["wallets"]["listed"]) == pytest.approx(
            b["wallets"]["share_reached_pct"]["value"])


def test_burst_fires_once_then_rearms_after_falling_below():
    desk, ms = _burst_desk(1_000_000.0)
    t = T0 + 70_000
    ms.on_trades([trade("BTC", t, 100.0, 12_000.0, "B", A, B, 2)], 0, t)          # $1.2M: fires
    ms.on_trades([trade("BTC", t + 1000, 100.0, 1000.0, "B", A, B, 3)], 0, t + 1000)  # still above: no repeat
    assert len([e for e in ms.deck.events if e["type"] == "burst"]) == 1
    # 70 s later the first trades have left the window; a new $1.2M burst fires again.
    ms.on_trades([trade("BTC", t + 75_000, 100.0, 12_000.0, "B", A, B, 4)], 0, t + 75_000)
    assert len([e for e in ms.deck.events if e["type"] == "burst"]) == 2


def test_burst_never_spans_a_gap():
    desk = make_desk(burst_min_usd={"BTC": 1_000_000.0})
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 70_000, 100.0, 6000.0, "B", A, B, 1)], 0, T0 + 70_000)   # $600K
    ms.on_disconnect("gap", T0 + 71_000)
    open_segment(ms, T0 + 75_000, conn=2)
    # $600K more 10 s after the first: within 60 s in time, but across the gap.
    ms.on_trades([trade("BTC", T0 + 80_000, 100.0, 6000.0, "B", A, B, 2)], 0, T0 + 80_000)
    assert not [e for e in ms.deck.events if e["type"] == "burst"]
    # Even $1.2M right after reconnecting doesn't fire until a full 60 s fits in the new segment.
    ms.on_trades([trade("BTC", T0 + 90_000, 100.0, 12_000.0, "B", A, B, 3)], 0, T0 + 90_000)
    assert not [e for e in ms.deck.events if e["type"] == "burst"]


def test_meanings_are_the_adopted_wording_and_claim_no_cause():
    m = schema.load()["regimes"]["meanings"]
    assert m["ABSORBING"]["text"] == ("Small observed trade-price change relative to net aggressive notional. Absorption is a "
                                      "hypothesis; this calculation does not identify an absorber or its remaining size.")
    assert m["PULLED-OFFERS"]["text"] == ("Observed trade prices rose while net aggressive flow was selling. The cause is "
                                          "undetermined; this classification does not establish offer cancellations.")
    assert m["QUIET"]["text"] == ("No qualifying executions occurred in the complete observed window. This is distinct "
                                  "from an unavailable or unverified feed.")
    assert set(m) == {"ABSORBING", "VACUUM-UP", "VACUUM-DOWN", "PULLED-OFFERS", "PULLED-BIDS", "TRENDING-UP",
                      "TRENDING-DOWN", "BALANCED", "THIN", "SPARSE", "QUIET"}
    banned = ("Someone large", "eating everything", "being withdrawn", "trend is done", "No edge", "real buying",
              "Do not read", "wait in")
    for state, v in m.items():
        assert not any(b in v["text"] for b in banned), state

# --- plugin additions agreed with Kevin (Nov 7) ------------------------------------------

def test_top_wallet_that_buys_and_sells_equally_shows_a_near_zero_net():
    # C takes $500K of buying and $498K of selling; A takes a smaller $300K of buying.
    trades = [_t(1, T0, 100.0, 5000.0, "B", C, B),       # C aggressive buy  $500,000
              _t(2, T0 + 1, 100.0, 4980.0, "A", B, C),   # C aggressive sell $498,000
              _t(3, T0 + 2, 100.0, 3000.0, "B", A, B)]   # A aggressive buy  $300,000
    m = deck.window_metrics(trades)
    vol = 500_000 + 498_000 + 300_000
    assert m["top_wallet"] == C
    assert m["top_share"] == pytest.approx(998_000 / vol * 100)        # ~77%: looks dominant on its own
    assert m["top_aggressive_bought"] == pytest.approx(500_000)
    assert m["top_aggressive_sold"] == pytest.approx(498_000)
    assert m["top_net_aggressive"] == pytest.approx(2_000)              # but it is nearly flat


def test_top_wallet_net_aggressive_on_the_card_and_in_hl_alerts():
    import json
    from hl_desk import tools
    from helpers import SnapRT
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    tid = 1
    for k in range(0, 9):                     # C buys and sells about equally every 15 s
        t = T0 + 1000 + k * 15_000
        ms.on_trades([trade("BTC", t, 100.0, 1000.0, "B", C, B, tid),
                      trade("BTC", t + 1, 100.0, 990.0, "A", B, C, tid + 1)], 0, t)
        tid += 2
    snap = build(desk, 1)
    tw = snap["markets"]["BTC"]["deck"]["card"]["numbers"]["top_wallet"]
    assert tw["value"] == C and tw["share_pct"] == pytest.approx(100.0)
    assert tw["net_aggressive_usd"] == pytest.approx(tw["aggressive_bought_usd"] - tw["aggressive_sold_usd"])
    assert abs(tw["net_aggressive_usd"]) < 0.02 * tw["aggressive_bought_usd"]
    assert "plugin addition" in tw["net_label"]
    alerts = json.loads(tools.run_tool("hl_alerts", {"market": "BTC"}, SnapRT(snap)))
    assert alerts["cards"]["BTC"]["card"]["numbers"]["top_wallet"]["net_aggressive_usd"] == tw["net_aggressive_usd"]
    s = schema.load()["metrics"]["deck.top_wallet_net_aggressive"]
    assert s["source"] == "plugin" and s["plugin_addition"] is True


def test_unresolved_is_gone_everywhere():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    reg = schema.load()["regimes"]
    assert all(r["state"] != "UNRESOLVED" for r in reg["rules"]) and "UNRESOLVED" not in reg["meanings"]
    assert "UNRESOLVED" not in (root / "skills" / "hyperliquid-commentary" / "SKILL.md").read_text()
    assert "UNRESOLVED" not in (root / "hl_desk" / "deck.py").read_text()


def json_text(p):
    return p.read_text()


# --- the window's path, dominance and the labels' validation status (Commentary TOOL_PROPOSALS 1, 4 and 2; Nov 9) ----

class _Fixed:
    """A store serving one fixed trade list, filtered to the window asked for."""

    def __init__(self, rows):
        self.rows = rows

    def window(self, s, e, start_inclusive=True):
        return [t for t in self.rows if (s <= t.time if start_inclusive else s < t.time) and t.time <= e]


def _card_at(rows, tick_ms):
    eng = deck.DeckEngine("BTC", 10e6)
    eng.reset_segment(T0)
    eng._tick(_Fixed(rows), tick_ms, None)
    return eng, eng.card(None)


def test_path_is_six_points_that_add_up_to_the_window_and_chain_to_its_move():
    """Tick T0+105 s, window [T0+15 s, T0+105 s]: the points end at T0+30, 45, 60, 75, 90 and 105 s. The trade at the
    window start belongs to the first point and one on a boundary to the point it ends; the fourth 15 s has no trades."""
    t = T0 + 105_000
    rows = [_t(1, T0 + 15_000, 100.0, 1000.0, "B", A, B),      # the window start: point 1, buy $100,000
            _t(2, T0 + 30_000, 101.0, 500.0, "A", B, C),       # on the boundary: point 1, sell $50,500
            _t(3, T0 + 31_000, 102.0, 2000.0, "A", B, C),      # point 2, sell $204,000
            _t(4, T0 + 50_000, 99.0, 100.0, "B", A, B),        # point 3, buy $9,900
            _t(5, T0 + 80_000, 103.0, 300.0, "B", C, B),       # point 5, buy $30,900 (point 4 has no trades)
            _t(6, T0 + 105_000, 104.0, 10.0, "A", B, A)]       # the tick itself: point 6, sell $1,040
    eng, card = _card_at(rows, t)
    path, n = card["path"], card["numbers"]
    pts = path["points"]
    assert path["point_s"] == 15 and len(pts) == 6
    assert [p["tick_ms"] for p in pts] == [T0 + 30_000 + k * 15_000 for k in range(6)]
    assert pts[-1]["tick"] == card["tick"] and pts[0]["tick_ms"] - 15_000 == card["window_start_ms"]
    assert [p["trades"] for p in pts] == [2, 1, 1, 0, 1, 1]
    assert path["first_trade"] == {"time": iso_ms(T0 + 15_000), "time_ms": T0 + 15_000, "price": 100.0}
    assert pts[0]["net_flow_usd"] == pytest.approx(100_000 - 50_500) and pts[0]["volume_usd"] == pytest.approx(150_500)
    assert pts[0]["price"] == 101.0 and pts[0]["move_bps"] == pytest.approx(100.0)              # 100 -> 101
    assert pts[1]["price"] == 102.0 and pts[1]["move_bps"] == pytest.approx((102 - 101) / 101 * 1e4)
    empty = pts[3]
    assert empty["price"] is None and empty["move_bps"] is None and empty["reason"] == "no trades in this 15 s"
    assert empty["net_flow_usd"] == 0 and empty["volume_usd"] == 0 and empty["trades"] == 0      # real zeros
    assert "reason" not in pts[0]
    assert pts[4]["move_bps"] == pytest.approx((103 - 99) / 99 * 1e4)       # measured from the last priced point
    # The points partition the window: they add up to the card's net flow and volume and compound to its move.
    assert sum(p["net_flow_usd"] for p in pts) == pytest.approx(n["net_flow_usd"]["value"])
    assert sum(p["volume_usd"] for p in pts) == pytest.approx(n["volume_usd"]["value"])
    chained = 1.0
    for p in pts:
        if p["move_bps"] is not None:
            chained *= 1 + p["move_bps"] / 1e4
    assert (chained - 1) * 1e4 == pytest.approx(n["move_bps"]["value"], abs=1e-2)
    assert pts[-1]["price"] == n["price"]["value"] == 104.0
    assert "plugin addition" in path["note"]


def test_dominated_by_names_the_tick_print_and_address_over_half_with_their_shares():
    """Point 2 holds C's four $150,000 sells; points 3 to 6 each hold a $200,000 buy by A and a $150,000 sell by B. Net
    -$400,000 on $2M volume: the one tick carries 150% of the net (the rest netted the other way) but only 30% of the
    volume; no print dominates; by net B and C tie at -$600,000 and the lowest address is named."""
    t = T0 + 105_000
    rows = [_t(10 + i, T0 + 31_000 + i, 100.0, 1500.0, "A", B, C) for i in range(4)]
    for k, base in enumerate((46_000, 61_000, 76_000, 91_000)):
        rows += [_t(20 + 2 * k, T0 + base, 100.0, 2000.0, "B", A, C),
                 _t(21 + 2 * k, T0 + base + 1000, 100.0, 1500.0, "A", A, B)]
    eng, card = _card_at(rows, t)
    d = card["dominated_by"]
    assert d["any"] is True and d["threshold_pct"] == 50.0
    nf, vol = d["net_flow"], d["volume"]
    assert nf["basis_usd"] == pytest.approx(-400_000) and vol["basis_usd"] == pytest.approx(2_000_000)
    assert nf["tick"]["tick_ms"] == T0 + 45_000 and nf["tick"]["net_flow_usd"] == pytest.approx(-600_000)
    assert nf["tick"]["share_pct"] == pytest.approx(150.0) and nf["tick"]["dominates"] is True
    assert nf["print"]["tid"] == 10 and nf["print"]["side"] == "A" and nf["print"]["taker"] == C
    assert nf["print"]["notional_usd"] == pytest.approx(150_000) and nf["print"]["share_pct"] == pytest.approx(37.5)
    assert nf["print"]["dominates"] is False
    assert nf["wallet"]["address"] == B and nf["wallet"]["net_aggressive_usd"] == pytest.approx(-600_000)
    assert nf["wallet"]["share_pct"] == pytest.approx(150.0) and nf["wallet"]["dominates"] is True
    assert vol["tick"]["tick_ms"] == T0 + 45_000 and vol["tick"]["share_pct"] == pytest.approx(30.0)
    assert vol["tick"]["dominates"] is False
    assert vol["print"]["tid"] == 20 and vol["print"]["share_pct"] == pytest.approx(10.0) and not vol["print"]["dominates"]
    assert vol["wallet"]["address"] == A and vol["wallet"]["share_pct"] == pytest.approx(40.0) and not vol["wallet"]["dominates"]
    assert vol["wallet"]["share_pct"] == card["numbers"]["top_wallet"]["share_pct"]
    assert d["summary"] == (f"one tick (the 15 s to {iso_ms(T0 + 45_000)}) carries 150% of the window's net aggressive flow; "
                            f"one address ({B}) carries 150% of the window's net aggressive flow")
    assert "not to be recomputed" in d["rule"]


def test_dominated_by_when_nothing_dominates_below_the_net_floor_and_with_no_trades():
    t = T0 + 105_000
    # Three equal $40,000 buys by three addresses in three points: every share is a third; ties name the earliest
    # tick and print and the lowest address.
    rows = [_t(1, T0 + 20_000, 100.0, 400.0, "B", C, B), _t(2, T0 + 40_000, 100.0, 400.0, "B", B, A),
            _t(3, T0 + 55_000, 100.0, 400.0, "B", A, C)]
    eng, card = _card_at(rows, t)
    d = card["dominated_by"]
    assert d["any"] is False
    assert d["summary"] == ("no single tick, print or address carries more than 50% of the window's net aggressive flow "
                            "or of its volume")
    for part in (d["net_flow"], d["volume"]):
        assert part["tick"]["tick_ms"] == T0 + 30_000 and part["print"]["tid"] == 1 and part["wallet"]["address"] == A
        assert all(part[k]["share_pct"] == pytest.approx(100 / 3, abs=1e-3) and part[k]["dominates"] is False
                   for k in ("tick", "print", "wallet"))
    # Net +$10,000 is under the $100K floor: no net shares, with the reason; the volume shares still say one tick is 55%.
    rows = [_t(1, T0 + 20_000, 100.0, 600.0, "B", A, B), _t(2, T0 + 40_000, 100.0, 500.0, "A", B, C)]
    eng, card = _card_at(rows, t)
    d = card["dominated_by"]
    assert d["net_flow"] == {"basis_usd": None, "reason": "net aggressive flow below the $100K floor: shares of it aren't computed",
                             "tick": None, "print": None, "wallet": None}
    assert d["volume"]["tick"]["share_pct"] == pytest.approx(60_000 / 110_000 * 100) and d["volume"]["tick"]["dominates"]
    assert d["any"] is True and d["summary"].endswith("(net aggressive flow below the $100K floor: its shares aren't computed)")
    # No trades at all: six empty points, no first trade, no basis for any share.
    eng, card = _card_at([], t)
    assert card["live_classification"]["state"] == "QUIET"
    assert all(p["trades"] == 0 and p["price"] is None and p["reason"] == "no trades in this 15 s" for p in card["path"]["points"])
    assert card["path"]["first_trade"]["price"] is None and card["path"]["first_trade"]["reason"] == "no trades in window"
    d = card["dominated_by"]
    assert d["any"] is False and d["volume"]["basis_usd"] == 0.0 and d["volume"]["reason"] == "no aggressive volume in window"
    assert d["volume"]["tick"] is None and d["net_flow"]["basis_usd"] is None


def test_path_dominance_and_validation_served_by_hl_alerts_and_absent_from_a_withheld_card():
    import json
    from hl_desk import tools
    from hl_desk.serving_public import withheld_card
    from helpers import SnapRT
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    tid = 1
    for k in range(0, 9):                     # A buys $500,000 every 15 s
        t = T0 + 1000 + k * 15_000
        ms.on_trades([trade("BTC", t, 100.0, 5000.0, "B", A, B, tid)], 0, t)
        tid += 1
    snap = build(desk, 1)
    card = snap["markets"]["BTC"]["deck"]["card"]
    assert card["complete"] and len(card["path"]["points"]) == 6
    assert all(p["trades"] == 1 and p["net_flow_usd"] == pytest.approx(500_000) for p in card["path"]["points"])
    d = card["dominated_by"]
    assert d["any"] is True and d["net_flow"]["tick"]["dominates"] is False      # each tick is a sixth
    assert d["volume"]["wallet"]["address"] == A and d["volume"]["wallet"]["share_pct"] == pytest.approx(100.0)
    assert d["volume"]["wallet"]["share_pct"] == card["numbers"]["top_wallet"]["share_pct"]
    assert d["summary"] == (f"one address ({A}) carries 100% of the window's net aggressive flow; "
                            f"one address ({A}) carries 100% of the window's volume")
    v = schema.load()["regimes"]["validation"]["status"]
    assert v == "descriptive; no predictive test on record"
    assert card["confirmed_regime"]["validation"] == card["live_classification"]["validation"] == deck.VALIDATION == v
    alerts = json.loads(tools.run_tool("hl_alerts", {"market": "BTC"}, SnapRT(snap)))
    served = alerts["cards"]["BTC"]["card"]
    assert served["path"] == card["path"] and served["dominated_by"] == card["dominated_by"]
    assert served["confirmed_regime"]["validation"] == v
    # Withheld at serving time (trades lapsed) or incomplete at build time: neither field is carried, like tick and
    # window_start_ms; the serving-time card still equals the build's own withheld card key for key.
    w = withheld_card(card, "trades stale: test")
    assert "path" not in w and "dominated_by" not in w and "tick" not in w
    built = ms.deck.card("trades stale: test")           # the snapshot's activity_buckets are node-enriched: keys compared
    assert set(w) == set(built)
    assert {k: v for k, v in w.items() if k != "activity_buckets"} == {k: v for k, v in built.items() if k != "activity_buckets"}
    for key in ("deck.path", "deck.dominated_by"):
        s = schema.load()["metrics"][key]
        assert s["plugin_addition"] is True and "complete card" in s["completeness_rule"]
