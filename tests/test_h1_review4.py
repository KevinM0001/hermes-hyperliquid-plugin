"""Regression tests for Codex h1 review 4 (commit 3354a41), one per blocking finding, from its reproductions.

B1: a liquidation burst is evaluated at each executions message's and each watermark's own block time t, over
    (t - 60 s, t]: two liquidations 60.1 s apart never make a burst, two within 60 s do, at the second one's block
    time, before its block's watermark; it fires once while the window holds and re-arms when the window empties,
    in a quiet market too. Read through hl_alerts and the tab after every message (as Codex's 1c review_cases.py
    does), and on the unretimed recording against an independent count.
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
from hl_desk.util import iso_ms
from node_helpers import EXECS, client, read_recording, surfaces  # noqa: F401  (surfaces is a fixture)

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
    while t < second_at - 200:                    # continuous watermarks, no gap
        send("watermark", {"through_seq": seq[0]}, t)
        t += 1000
    send("watermark", {"through_seq": seq[0]}, second_at - 200)   # Codex's case: the last one 200 ms before
    send("execs", {"execs": [_liq(2, V2, second_at)]}, second_at)
    send("watermark", {"through_seq": seq[0]}, second_at)
    return list(c.markets["BTC"].panels.liq_events)


def test_b1_liquidations_60_1_s_apart_are_never_a_burst():
    """Codex's case: liquidations at T+1.0 s and T+61.1 s (60.1 s apart), watermarks continuous through T+60.9 s. The
    second's window (T+1.1 s, T+61.1 s] holds one liquidation: below the 2-in-60 s rule, so no event. (The old code's
    window began 60 s before the T+60.9 s watermark and had no end, so it held both.)"""
    assert _run_liquidations(T0 + 61_100) == []


def test_b1_two_within_60_s_fire_once_at_the_second_ones_block_time():
    ev = _run_liquidations(T0 + 59_000)
    assert len(ev) == 1 and ev[0]["liquidations"] == 2 and ev[0]["notional_usd"] == 200.0
    assert ev[0]["at_ms"] == T0 + 59_000                       # its own block time, never an earlier watermark's
    assert ev[0]["longs_liquidated_usd"] == 200.0


class _Drive:
    """One market's feed, message by message; with a plugin, the liquidation alerts read through hl_alerts and the
    tab after every message (they must agree)."""

    def __init__(self, market="BTC", plugin=None, cfg=None):
        self.market, self.plugin, self.seq, self.reads = market, plugin, 0, []
        self.c = client(cfg=cfg)
        if plugin is not None:
            plugin.attach(self.c)
        self.c.on_line({"v": 2, "control": "hello", "epoch": 7, "session": S, "started_ms": T0 - 5000,
                        "pub_ms": T0 - 5000, "mode": "LIVE", "markets": {market: {"start_seq": 1, "snapshot": "pending"}}},
                       T0 - 4990, (T0 - 4990) * 10 ** 6)

    def send(self, kind, body, bt):
        self.seq += 1
        self.c.on_line({"v": 2, "epoch": 7, "session": S, "market": self.market, "seq": self.seq, "kind": kind,
                        "block": self.seq, "block_time_ms": bt, "pub_ms": bt + 10, "body": body},
                       bt + 50, (bt + 50) * 10 ** 6)
        if self.plugin is not None:
            self.plugin.read(self.market, bt + 50)
            liq = [e for e in self.plugin.last["events"] if e.get("type") == "liquidation_burst"]
            assert liq == [e for e in self.plugin.last["tab"]["deck"]["events"] if e.get("type") == "liquidation_burst"]
            self.reads.append((kind, bt, liq))

    def execs(self, fills, bt):
        self.send("execs", {"execs": fills}, bt)

    def watermarks(self, start, stop, step=1000):
        for t in range(start, stop, step):
            self.send("watermark", {"through_seq": self.seq}, t)

    def events(self):
        return list(self.c.markets[self.market].panels.liq_events)


def _alert(ev):
    """The alert itself; its freshness is the node fills' as of each read, so it moves on with the watermarks."""
    return {k: v for k, v in ev.items() if k != "freshness"}


def _recorded_liquidations(market="HYPE"):
    """The first fill of each distinct recorded liquidation (Codex's review_cases.py sample), fields intact."""
    import copy
    _, msgs = read_recording(EXECS)
    out, seen = [], set()
    for _, m in msgs:
        if m.get("market") == market and m["kind"] == "execs":
            for e in m["body"]["execs"]:
                p = [q for q in e["participants"] if q.get("liquidated")]
                if e.get("liquidation") and p and p[0]["oid"] not in seen:
                    seen.add(p[0]["oid"])
                    out.append(copy.deepcopy(e))
    assert len(out) >= 2
    return out


def _codex_case(surfaces, gap_ms):
    """Codex's 1c reproducer: two real HYPE liquidation fills, retimed gap_ms apart; a quiet market's watermarks every
    250 ms between; the second fill's execs message arrives before its block's watermark. Read after every message."""
    plugin = surfaces(["HYPE"])
    dr = _Drive("HYPE", plugin, cfg={"stale_after_ms": 2000, "max_source_age_ms": 5000})
    e1, e2 = _recorded_liquidations()[:2]
    e1["time_ms"], e2["time_ms"] = T0 + 100, T0 + 100 + gap_ms
    dr.send("watermark", {"through_seq": 0}, T0 - 1000)
    dr.execs([e1], e1["time_ms"])
    dr.watermarks(T0 + 350, e2["time_ms"] - 200, 250)
    dr.send("watermark", {"through_seq": dr.seq}, e2["time_ms"] - 200)   # the last one 200 ms before e2, as Codex's
    dr.execs([e2], e2["time_ms"])
    before_watermark = dr.reads[-1][2]
    dr.send("watermark", {"through_seq": dr.seq}, e2["time_ms"] + 100)
    return dr, (e1, e2), before_watermark


def test_b1_codex_1c_case_60_1_s_apart_through_hl_alerts_and_the_tab(surfaces):
    dr, _, _ = _codex_case(surfaces, 60_100)
    assert all(liq == [] for _, _, liq in dr.reads)              # after every message: never an alert
    assert dr.events() == []


def test_b1_control_59_9_s_apart_alerts_at_the_second_fills_block_time_before_its_watermark(surfaces):
    dr, (e1, e2), shown = _codex_case(surfaces, 59_900)
    assert all(liq == [] for _, _, liq in dr.reads[:-2])        # nothing before the second fill
    assert len(shown) == 1                                        # shown at the execs message, before its watermark
    assert [_alert(e) for e in dr.reads[-1][2]] == [_alert(shown[0])]   # and the same alert after it
    ev = shown[0]
    total = sum(Decimal(e["px"]) * Decimal(e["sz"]) for e in (e1, e2))
    assert ev["at_ms"] == e2["time_ms"] and ev["at"] == iso_ms(e2["time_ms"])
    assert ev["liquidations"] == 2 and ev["notional_usd"] == float(round(total, 2))
    assert ev["longs_liquidated_usd"] == ev["notional_usd"] and ev["shorts_liquidated_usd"] == 0   # both sold longs
    assert ev["freshness"]["family"] == "flow" and ev["freshness"]["state"] == "LIVE" and ev["source"] == "node fills"


def test_b1_fires_once_while_the_window_holds_and_rearms_when_it_empties():
    """A $200 burst at T+1 s. A third liquidation at T+30 s: same window, no new alert. New pairs at T+60.5 s (the
    first pair is still in (T+0.5 s, T+60.5 s]) give none; at T+61.5 s (it has left) one, stamped T+61.5 s."""
    for second_pair_at, want in ((60_500, [1000]), (61_500, [61_500, 1000])):
        dr = _Drive()
        dr.send("watermark", {"through_seq": 0}, T0)
        dr.execs([_liq(1, V1, T0 + 1000), _liq(2, V2, T0 + 1000)], T0 + 1000)
        dr.watermarks(T0 + 1000, T0 + 30_000)
        dr.execs([_liq(3, M, T0 + 30_000)], T0 + 30_000)
        dr.watermarks(T0 + 30_000, T0 + second_pair_at)
        dr.execs([_liq(4, V1, T0 + second_pair_at), _liq(5, V2, T0 + second_pair_at)], T0 + second_pair_at)
        assert [e["at_ms"] - T0 for e in dr.events()] == want, second_pair_at


def test_b1_a_quiet_market_rearms_at_its_watermarks(surfaces):
    """Two real bursts 129 s apart with only watermarks between (no executions): both alert. The rule re-arms at the
    watermark where the first burst's window empties; it used to wait for the next executions message."""
    plugin = surfaces(["BTC"])
    dr = _Drive("BTC", plugin)
    dr.send("watermark", {"through_seq": 0}, T0)
    dr.execs([_liq(1, V1, T0 + 1000), _liq(2, V2, T0 + 1000)], T0 + 1000)
    dr.watermarks(T0 + 1000, T0 + 130_000)
    dr.execs([_liq(3, V1, T0 + 130_000), _liq(4, V2, T0 + 130_000)], T0 + 130_000)
    assert [e["at_ms"] - T0 for e in dr.reads[-1][2]] == [130_000, 1000]


def test_b1_the_unretimed_recording_matches_an_independent_count_through_the_tools(surfaces):
    """The recorded 120 s window as it arrived, read through hl_alerts and the tab after every message. Every alert's
    count, notional, long/short split and time come from exactly its window (at - 60 s, at] of the liquidation fills
    received so far, counted independently here (each liquidation once: liquidated trader and order). The recorded
    HYPE alert (2 liquidations, all in block time 1794084524208) is stamped that block time, not the previous
    watermark's 1794084524073 (Codex 1c's recorded case); BTC's single liquidation never alerts."""
    h, msgs = read_recording(EXECS)
    plugin = surfaces(["BTC", "HYPE"])
    c = plugin.attach(client("sock-a"))
    p = h["producer"]
    c.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
               "markets": {}}, p["started_ms"], 0)
    fills: dict[str, list] = {"BTC": [], "HYPE": []}
    checked: dict[tuple, dict] = {}
    for rx, m in msgs:
        mk = m["market"]
        if mk not in fills:
            continue
        c._market(mk).last_seq = m["seq"] - 1               # this fixture keeps only execs and watermarks
        c.on_line(m, rx, rx * 10 ** 6)
        if m["kind"] == "execs":
            fills[mk] += [e for e in m["body"]["execs"] if e.get("liquidation")]
        plugin.read(mk, rx)
        for ev in plugin.last["events"]:
            if ev.get("type") != "liquidation_burst":
                continue
            key = (mk, ev["at_ms"])
            if key in checked:
                assert _alert(checked[key]) == _alert(ev)
                continue
            assert ev["at_ms"] == m["block_time_ms"]         # stamped by the message that fired it
            inside = [e for e in fills[mk] if ev["at_ms"] - 60_000 < e["time_ms"] <= ev["at_ms"]]
            groups: dict[tuple, list] = {}
            for e in inside:
                q = next(x for x in e["participants"] if x.get("liquidated"))
                groups.setdefault((e["liquidation"]["liquidated_user"], q["oid"]), []).append((e, q["side"]))
            total = sum(Decimal(e["px"]) * Decimal(e["sz"]) for e in inside)
            longs = sum(Decimal(e["px"]) * Decimal(e["sz"]) for g in groups.values() for e, side in g if side == "A")
            assert ev["liquidations"] == len(groups) and ev["notional_usd"] == float(round(total, 2))
            assert ev["longs_liquidated_usd"] == float(round(longs, 2))
            assert ev["shorts_liquidated_usd"] == float(round(total - longs, 2))
            checked[key] = ev
    assert sorted(checked) == [("HYPE", 1794084524208)]
    assert checked[("HYPE", 1794084524208)]["liquidations"] == 2


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
