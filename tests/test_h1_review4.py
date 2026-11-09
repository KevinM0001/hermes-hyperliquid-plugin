"""Regression tests for Codex h1 review 4 (commit 3354a41), one per blocking finding, from its reproductions.

B1: a liquidation burst is evaluated at each executions message's own block time t, over (t - 60 s, t]: two
    liquidations 60.1 s apart never make a burst, two within 60 s do, at the second one's block time.
B2: a position effect counts only when complete for its side (both components, none negative, adding up to the fill
    size); otherwise it is unavailable with the reason and its notional is unclassified, never a zero bucket.
B3: each tool's numbers carry their own window (hl_flow's and hl_wallets' window labels) and each approximate depth
    value its own resolution; the commentary skill keeps them apart.
"""

from __future__ import annotations

from decimal import Decimal

from hl_desk import book as bk
from hl_desk.node import panels
from hl_desk.node.flow import activity_buckets, parse_effect, parse_exec

from helpers import NS, T0, build, make_desk, open_segment, trade
from node_helpers import client

V1, V2, M = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
S = "0" * 31 + "1"


def _liq(tid, victim, t):
    """A $100 long liquidation: the liquidated trader sells 1 at 100 as taker, from a long (exit 1)."""
    return {"tid": tid, "px": "100", "sz": "1", "time_ms": t, "aggressor": "A", "complete": True,
            "liquidation": {"liquidated_user": victim, "mark_px": "100", "method": "market"},
            "participants": [{"user": victim, "side": "A", "role": "taker", "oid": tid, "liquidated": True,
                              "effect": {"exit": "1", "new_short": "0"}},
                             {"user": M, "side": "B", "role": "maker", "oid": 10_000 + tid,
                              "effect": {"cover": "0", "new_long": "1"}}]}


def _run_liquidations(second_at):
    c = client()
    c.on_line({"v": 2, "control": "hello", "epoch": 7, "session": S, "started_ms": T0 - 5000, "pub_ms": T0 - 5000,
               "mode": "LIVE", "markets": {"BTC": {"start_seq": 1, "snapshot": "pending"}}}, T0 - 4990, 0)
    seq, block = [0], [0]

    def send(kind, body, bt):
        seq[0] += 1
        block[0] += 1
        c.on_line({"v": 2, "epoch": 7, "session": S, "market": "BTC", "seq": seq[0], "kind": kind,
                   "block": block[0], "block_time_ms": bt, "pub_ms": bt + 10, "body": body}, bt + 50, (bt + 50) * 10 ** 6)
    send("watermark", {"through_seq": 0}, T0)
    send("execs", {"execs": [_liq(1, V1, T0 + 1000)]}, T0 + 1000)
    send("watermark", {"through_seq": seq[0]}, T0 + 1000)
    t = T0 + 2000
    while t < second_at:                          # continuous watermarks, no gap
        send("watermark", {"through_seq": seq[0]}, t)
        t += 1000
    send("execs", {"execs": [_liq(2, V2, second_at)]}, second_at)
    send("watermark", {"through_seq": seq[0]}, second_at)
    return list(c.markets["BTC"].panels.liq_events)


def test_b1_liquidations_60_1_s_apart_are_never_a_burst():
    """Codex's case: liquidations at T+1.0 s and T+61.1 s (60.1 s apart), watermarks continuous between. The second's
    window (T+1.1 s, T+61.1 s] holds one liquidation: below the 2-in-60 s rule, so no event."""
    assert _run_liquidations(T0 + 61_100) == []


def test_b1_two_within_60_s_fire_once_at_the_second_ones_block_time():
    ev = _run_liquidations(T0 + 59_000)
    assert len(ev) == 1 and ev[0]["liquidations"] == 2 and ev[0]["notional_usd"] == 200.0
    assert ev[0]["at_ms"] == T0 + 59_000                       # its own block time, never an earlier watermark's
    assert ev[0]["longs_liquidated_usd"] == 200.0


def test_b2_only_complete_effects_count():
    q = Decimal("4")
    assert parse_effect({"cover": "3", "new_long": "1"}, "B", q) == ({"cover": Decimal(3), "new_long": Decimal(1)}, None)
    assert parse_effect({"exit": "4", "new_short": "0"}, "A", q)[0] == {"exit": Decimal(4), "new_short": Decimal(0)}
    bad = {"missing": ({"cover": "3"}, "B", "incomplete position effect: new_long missing"),
           "other side": ({"cover": "3", "exit": "1"}, "B", "don't belong to a buy"),
           "negative": ({"cover": "5", "new_long": "-1"}, "B", "negative component"),
           "not conserved": ({"cover": "3", "new_long": "2"}, "B", "doesn't add up to the fill size 4")}
    for name, (eff, side, why) in bad.items():
        got, reason = parse_effect(eff, side, q)
        assert got is None and why in reason, name


def test_b2_an_incomplete_effect_is_unclassified_never_a_zero_bucket():
    """Codex's case: an aggressive buy of 4 at 100 whose taker effect gives only cover 3. The buying side's buckets
    are unavailable with the reason and $400 unclassified; not cover $300 / new long $0 / 75%. The valid flip control
    (cover 3, new long 1) gives $300 / $100 and 75% / 25%."""
    def ex(effect):
        return parse_exec({"tid": 1, "px": "100", "sz": "4", "time_ms": T0, "aggressor": "B", "complete": True,
                           "participants": [{"user": V1, "side": "B", "role": "taker", "oid": 1, "effect": effect},
                                            {"user": M, "side": "A", "role": "maker", "oid": 2,
                                             "effect": {"exit": "4", "new_short": "0"}}]}, 2, T0)
    bad = activity_buckets([ex({"cover": "3"})])["buying"]
    assert bad["available"] is False and bad["unclassified_usd"] == Decimal(400)
    assert all(v is None for v in bad["buckets"].values()) and "new_long missing" in bad["reason"]
    good = activity_buckets([ex({"cover": "3", "new_long": "1"})])["buying"]
    assert good["available"] and good["buckets"] == {"new_long": Decimal(100), "cover": Decimal(300)}
    assert good["pct"] == {"new_long": Decimal(25), "cover": Decimal(75)}


def test_b3_each_tool_window_and_depth_band_carries_its_own_label():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for k in range(1, 30):
        ms.on_trades([trade("BTC", T0 + k * 1000, 100.0, 1.0, "B", V1, V2, k)], k * NS, T0 + k * 1000)
    snap = build(desk, 30 * NS, wall_ms=T0 + 30_000)
    m = snap["markets"]["BTC"]
    fw = m["flow"]["window"]
    assert fw["label"].startswith(f"the flow window: {fw['length_s']:.1f} s") and "not the wallets'" in fw["label"]
    ww = m["wallets"]["5m"]["window"]
    assert ww["label"].startswith("the wallets' 5-minute window") and "not the flow window's" in ww["label"]
    full = {"kind": "full precision", "usable": False, "reason": "not received yet"}
    coarse = [{"kind": "nSigFigs=4", "usable": True, "mid": 100.0, "time_ms": T0, "bucket_width": 0.01, "bad": {},
               "bids": [(99.99, 5.0, 1), (99.70, 5.0, 1)], "asks": [(100.01, 5.0, 1), (100.30, 5.0, 1)]},   # past ±0.25%
              {"kind": "nSigFigs=3", "usable": True, "mid": 100.0, "time_ms": T0, "bucket_width": 0.1, "bad": {},
               "bids": [(99.9, 5.0, 1), (99.0, 9.0, 1), (98.0, 9.0, 1)], "asks": [(100.1, 5.0, 1), (101.0, 9.0, 1), (102.0, 9.0, 1)]}]
    bands = bk.depth_bands(full, coarse)
    res = {b["band_pct"]: b["approximate"]["bid"].get("resolution") for b in bands}
    assert res[0.25].startswith("nSigFigs=4 book, 0.01 price buckets") and res[1.0].startswith("nSigFigs=3 book, 0.1")
    from pathlib import Path
    skill = (Path(__file__).resolve().parent.parent / "skills" / "hyperliquid-commentary" / "SKILL.md").read_text()
    assert "Every number belongs to its own tool's window" in skill and "its own `resolution`" in skill
