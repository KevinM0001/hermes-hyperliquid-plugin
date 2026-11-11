"""The Order Flow Map from node fills (Kevin's order-flow map report, ported to node mode): market.node.live.flow_map
and market.node.replay.flow_map.

The recorded window is checked against totals counted here, independently, from the raw feed JSON (exact decimals,
each tid once, the taker side only, the dir read from the taker's own effect); the eight-bucket shape, the dir
mapping, flips, partial coverage and gaps, the account shares, every sentence branch, the withheld form, the replay
view and the serving-time withholding are checked on synthetic fills at chosen block times, in the feed's own format.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from hl_desk.node import flow_map, panels
from hl_desk.node.flow_map import BUCKET_MS, Bucket, money, pct_text, readings
from hl_desk.node.serving import age_snapshot_for_serving, aged_view

from node_helpers import EXECS, OPTS, client, read_recording, surfaces

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40
SP = "xyz:SP500"
SESSION = "0" * 31 + "7"
T0 = 1_800_000_000_000                       # a quarter-hour boundary (a multiple of 900 000 ms)
assert T0 % BUCKET_MS == 0
KINDS = ("open_long", "close_short", "flip_to_long", "close_long", "open_short", "flip_to_short")
USD_KEYS = ("buy_usd", "sell_usd", "net_usd", "open_long_usd", "close_short_usd", "open_short_usd", "close_long_usd",
            "flip_usd", "flip_to_long_usd", "flip_to_short_usd", "risk_added_usd", "risk_removed_usd")


# --- fills and lines in the feed's own format (PROTOCOL.md 4.6) ----------------------------------------------

def _p(user, side, role, effect=None, unavailable=None, oid=1):
    p = {"user": user, "side": side, "role": role, "oid": oid}
    if effect is not None:
        p["effect"] = effect
    if unavailable is not None:
        p["effect_unavailable"] = unavailable
    return p


def _fill(tid, kind, t, px="100", sz="1", user=A, maker=B, split=None, aggressor=None, complete=True):
    """One execution whose taker's position effect says `kind` (Open Long, Close Short, Open Short, Close Long, or a
    flip with `split` = (closing part, opening part)); the maker's record mirrors it."""
    q = Decimal(sz)
    if kind in ("open_long", "close_short", "flip_to_long"):
        side, mside = "B", "A"
        if kind == "flip_to_long":
            eff = {"cover": split[0], "new_long": split[1]}
        else:
            eff = {"cover": sz, "new_long": "0"} if kind == "close_short" else {"cover": "0", "new_long": sz}
        meff = {"exit": str(q), "new_short": "0"}
    else:
        side, mside = "A", "B"
        if kind == "flip_to_short":
            eff = {"exit": split[0], "new_short": split[1]}
        else:
            eff = {"exit": sz, "new_short": "0"} if kind == "close_long" else {"exit": "0", "new_short": sz}
        meff = {"cover": "0", "new_long": str(q)}
    return {"tid": tid, "px": px, "sz": sz, "time_ms": t, "aggressor": aggressor or side, "complete": complete,
            "participants": [_p(user, side, "taker", eff), _p(maker, mside, "maker", meff)]}


class Feed:
    """Protocol v2 lines of one market into a client, numbered contiguously; skip() loses one (a sequence gap)."""

    def __init__(self, c, market, epoch=7, session=SESSION, replay_session=None):
        self.c, self.market, self.epoch, self.session = c, market, epoch, session
        self.seq, self.block = 0, 1000
        self.extra = {"mode": "REPLAY", "replay_session": replay_session} if replay_session else {}

    def hello(self, started_ms=T0 - 60_000):
        line = {"v": 2, "control": "hello", "epoch": self.epoch, "session": self.session, "started_ms": started_ms,
                "pub_ms": started_ms, "mode": "LIVE", "markets": {self.market: {"start_seq": 1, "snapshot": "pending"}}}
        self.c.on_line(line | self.extra, started_ms + 50, (started_ms + 50) * 10 ** 6)

    def line(self, kind, body, bt):
        self.seq += 1
        self.block += 1
        m = {"v": 2, "epoch": self.epoch, "session": self.session, "market": self.market, "seq": self.seq, "kind": kind,
             "block": self.block, "block_time_ms": bt, "pub_ms": bt + 10, "body": body} | self.extra
        rx = bt + 50
        self.c.on_line(m, rx, rx * 10 ** 6)
        return m

    def wm(self, bt):
        return self.line("watermark", {"through_seq": self.seq}, bt)

    def execs(self, bt, fills, fills_known=True):
        body = {"execs": fills} if fills_known else {"execs": fills, "fills_known": False}
        return self.line("execs", body, bt)

    def skip(self):
        self.seq += 1


def _map(c, market, now_ms):
    return panels.market_view(c, market, OPTS, now_ms, now_ms * 10 ** 6)["flow_map"]


def _check_identities(block):
    """What every bucket and the totals must satisfy."""
    rows = block["buckets"] + [block["totals"]]
    for r in rows:
        assert r["buy_usd"] == pytest.approx(r["open_long_usd"] + r["close_short_usd"] + r["flip_to_long_usd"])
        assert r["sell_usd"] == pytest.approx(r["close_long_usd"] + r["open_short_usd"] + r["flip_to_short_usd"])
        assert r["net_usd"] == pytest.approx(r["buy_usd"] - r["sell_usd"])
        assert r["risk_added_usd"] == pytest.approx(r["open_long_usd"] + r["open_short_usd"])
        assert r["risk_removed_usd"] == pytest.approx(r["close_long_usd"] + r["close_short_usd"])
        assert r["flip_usd"] == pytest.approx(r["flip_to_long_usd"] + r["flip_to_short_usd"])
        assert r["buy_usd"] + r["sell_usd"] == pytest.approx(r["risk_added_usd"] + r["risk_removed_usd"] + r["flip_usd"])
    for k in USD_KEYS + ("fills", "flip_fills", "unclassified_usd", "unclassified_fills", "unreadable_fills"):
        assert block["totals"][k] == pytest.approx(sum(b[k] for b in block["buckets"])), k
    w = block["window"]
    assert len(block["buckets"]) == w["buckets"] == 8 and w["bucket_s"] == 900
    for i, b in enumerate(block["buckets"]):
        assert b["start_ms"] == w["start_ms"] + i * BUCKET_MS and b["end_ms"] == b["start_ms"] + BUCKET_MS
        assert b["start_ms"] % BUCKET_MS == 0
        assert b["in_progress"] == (b["end_ms"] > w["end_ms"])
        assert 0 <= b["coverage_s"] <= 900
        assert b["complete"] == (b["coverage_s"] == 900 and not b["in_progress"] and b["unreadable_fills"] == 0)
        assert (b["reason"] is None) == b["complete"]
    assert block["buckets"][-1]["start_ms"] <= w["end_ms"] < block["buckets"][-1]["end_ms"]
    assert block["stream"] == "node fills" and block["freshness"]["family"] == "flow"
    text = json.dumps(block)
    assert "liquidation" not in text and '"dir"' not in text            # an exit is Close Long, never a liquidation


# --- the recorded window ---------------------------------------------------------------------------------

def _client_from(msgs, source="sock-a", cfg=None, plugin=None):
    h, _ = read_recording(EXECS)
    c = client(source, cfg=cfg)
    if plugin is not None:
        plugin.attach(c)
    p = h["producer"]
    c.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
               "markets": {}}, p["started_ms"], 0)
    for rx, m in msgs:
        mf = c._market(m["market"])
        mf.last_seq = m["seq"] - 1                      # this fixture keeps only execs and watermarks: treat as contiguous
        c.on_line(m, rx, rx * 10 ** 6)
    return c


def _independent(msgs, market):
    """Straight from the raw JSON: each tid once, blocks after the market's first watermark, the taker side only, the
    dir from the taker's own effect. Returns (notional per kind, per account, fills, flips, volume)."""
    first_wm = next(m["block"] for _, m in msgs if m["market"] == market and m["kind"] == "watermark")
    seen, usd, acc = set(), {k: Decimal(0) for k in KINDS}, {}
    fills = flips = 0
    vol = Decimal(0)
    for _, m in msgs:
        if m["market"] != market or m["kind"] != "execs" or m["block"] <= first_wm:
            continue
        for e in m["body"]["execs"]:
            if e["tid"] in seen:
                continue
            seen.add(e["tid"])
            n = Decimal(e["px"]) * Decimal(e["sz"])
            takers = [p for p in e["participants"] if p["role"] == "taker"]
            assert len(takers) == 1 and e["complete"] is True and takers[0]["side"] == e["aggressor"]   # all classifiable
            tk = takers[0]
            eff = {k: Decimal(v) for k, v in tk["effect"].items()}
            if tk["side"] == "B":
                kind = ("flip_to_long" if eff["cover"] > 0 and eff["new_long"] > 0 else
                        "open_long" if eff["new_long"] > 0 else "close_short")
            else:
                kind = ("flip_to_short" if eff["exit"] > 0 and eff["new_short"] > 0 else
                        "open_short" if eff["new_short"] > 0 else "close_long")
            usd[kind] += n
            fills += 1
            flips += kind.startswith("flip")
            vol += n
            acc[tk["user"]] = acc.get(tk["user"], Decimal(0)) + n
    return usd, acc, fills, flips, vol


def _money(v) -> str:
    """The report's money format, as the schema states it (node.flow_map.readings.format)."""
    v = abs(float(v))
    if v >= 999_500:
        return f"${v / 1e6:.2f}M"
    if v >= 999.5:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _expected_sentences(usd, acc):
    """The four sentences from the independent totals, under the schema's rules."""
    buy = usd["open_long"] + usd["close_short"] + usd["flip_to_long"]
    sell = usd["close_long"] + usd["open_short"] + usd["flip_to_short"]
    net, ol, cs, os_, cl = buy - sell, usd["open_long"], usd["close_short"], usd["open_short"], usd["close_long"]
    flips = usd["flip_to_long"] + usd["flip_to_short"]
    assert net != 0 and buy > 0 and sell > 0
    side = "buy" if net > 0 else "sell"
    s1 = f"Taker flow ran {_money(net)} to the {side} side on net — {_money(buy)} bought against {_money(sell)} sold."
    if side == "sell":
        assert cl != os_
        s2 = (f"That selling is longs leaving, not shorts arriving: {_money(cl)} of it closed existing longs against only "
              f"{_money(os_)} opening new shorts. An unwind, not a bet." if cl > os_ else
              f"That selling is shorts arriving, not longs leaving: {_money(os_)} of it opened new shorts against only "
              f"{_money(cl)} closing existing longs. A bet, not an unwind.")
    else:
        assert ol != cs
        s2 = (f"That buying is new longs, not shorts covering: {_money(ol)} opened fresh long risk against {_money(cs)} "
              f"closing shorts. Position building." if ol > cs else
              f"That buying is shorts covering, not new longs: {_money(cs)} closed existing shorts against {_money(ol)} "
              f"opening fresh long risk. Short covering, not position building.")
    s3 = (f"Across the window, {_money(ol + os_)} opened risk and {_money(cl + cs)} closed it, with {_money(flips)} in "
          f"single fills that flipped a position from one side to the other." if flips > 0 else
          f"Across the window, {_money(ol + os_)} opened risk and {_money(cl + cs)} closed it; no single fill flipped a "
          f"position from one side to the other.")
    ranked = sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))
    assert len(ranked) >= 5
    top, top5 = ranked[0][1], sum(v for _, v in ranked[:5])
    s4 = (f"The most active taker account took {float(top / (buy + sell) * 100):.1f}% of the aggressive flow "
          f"({_money(top)}); the top five took {float(top5 / (buy + sell) * 100):.1f}%.")
    return [s1, s2, s3, s4]


@pytest.mark.parametrize("market", ["BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"])
def test_recorded_window_against_an_independent_count(market):
    """The 120 s recording lies inside one quarter hour: one in-progress bucket with 119.9 s of coverage holds every
    fill, the seven before it are uncovered with zero fills, and every number equals the count made here from the raw
    JSON, each tid once and the taker side only (so buy + sell is the window's volume, never twice it)."""
    _, msgs = read_recording(EXECS)
    c = _client_from(msgs)
    last_rx = msgs[-1][0]
    fm = _map(c, market, last_rx)
    assert fm["available"] is True and fm["provenance"]["mode"] == "LIVE" and fm["freshness"]["state"] == "LIVE"
    _check_identities(fm)
    usd, acc, fills, flips, vol = _independent(msgs, market)
    assert fills > 0 and c.markets[market].flow.counters["duplicates"] == 0
    w = fm["window"]
    first_bt = next(m["block_time_ms"] for _, m in msgs if m["market"] == market and m["kind"] == "watermark")
    last_bt = max(m["block_time_ms"] for _, m in msgs if m["market"] == market)
    assert w["end_ms"] == last_bt and w["start_ms"] == last_bt - last_bt % BUCKET_MS - 7 * BUCKET_MS
    assert w["coverage_s"] == round((last_bt - first_bt) / 1000, 1) == 119.9 and w["complete"] is False
    assert w["uncovered"] == [{"start_ms": w["start_ms"], "end_ms": first_bt,
                               "why": "before the node fills were observed"}]
    for b in fm["buckets"][:7]:
        assert b["fills"] == 0 and b["coverage_s"] == 0 and b["complete"] is False and b["accounts"] == 0
        assert b["buy_usd"] == b["sell_usd"] == 0 and b["top_account_share_pct"] is None
        assert "before the node fills were observed" in b["reason"]
    last = fm["buckets"][-1]
    assert last["in_progress"] is True and last["coverage_s"] == 119.9 and last["complete"] is False
    assert "in progress" in last["reason"] and "not covered" in last["reason"]
    r = lambda d: round(float(d), 2)
    for row in (last, fm["totals"]):
        assert row["fills"] == fills and row["flip_fills"] == flips
        for k in KINDS:
            assert row[f"{k}_usd"] == r(usd[k]), k
        assert row["buy_usd"] == r(usd["open_long"] + usd["close_short"] + usd["flip_to_long"])
        assert row["sell_usd"] == r(usd["close_long"] + usd["open_short"] + usd["flip_to_short"])
        assert row["unclassified_usd"] == 0 and row["unclassified_fills"] == 0 and row["unreadable_fills"] == 0
    assert fm["totals"]["aggressive_usd"] == r(vol)                   # each trade once: buy + sell is the volume
    assert fm["totals"]["buy_usd"] + fm["totals"]["sell_usd"] == pytest.approx(r(vol), abs=0.011)
    # the tell of double counting: with both sides present the buy share is never exactly 50.0%
    if fm["totals"]["buy_usd"] > 0 and fm["totals"]["sell_usd"] > 0:
        assert round(fm["totals"]["buy_usd"] / fm["totals"]["aggressive_usd"] * 100, 1) != 50.0
    ranked = sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))
    top5 = sum((v for _, v in ranked[:5]), Decimal(0))
    ac = fm["accounts"]
    assert ac["distinct"] == len(ranked) == last["accounts"] and ac["basis_usd"] == r(vol)
    assert ac["top"] == {"address": ranked[0][0], "notional_usd": r(ranked[0][1]), "share_pct": round(float(ranked[0][1] / vol * 100), 2)}
    assert ac["top5_share_pct"] == round(float(top5 / vol * 100), 2) and last["top_account_share_pct"] == ac["top"]["share_pct"]
    assert ac["unattributed_usd"] == 0
    # the four sentences, chosen by the report's rules on these numbers
    assert [x["kind"] for x in fm["readings"]] == ["net", "composition", "risk", "concentration"]
    assert [x["text"] for x in fm["readings"]] == _expected_sentences(usd, acc)
    assert fm["readings"][0]["basis"]["net_usd"] == fm["totals"]["net_usd"]
    assert fm["readings"][3]["basis"]["top"] == ac["top"]
    # the tape rides next to it and the whole view is JSON
    json.dumps(panels.market_view(c, market, OPTS, last_rx, last_rx * 10 ** 6), default=str)


def test_recorded_hype_falls_in_the_reports_unwind_branch():
    """HYPE in the recorded window: net selling where closing longs outweighs opening shorts, the report's own HYPE
    verdict ("longs leaving, not shorts arriving ... An unwind, not a bet"), with its liquidation fills counted as the
    exits they are and the word nowhere in the block."""
    _, msgs = read_recording(EXECS)
    c = _client_from(msgs)
    fm = _map(c, "HYPE", msgs[-1][0])
    t = fm["totals"]
    assert t["net_usd"] < 0 and t["close_long_usd"] > t["open_short_usd"]
    assert fm["readings"][1]["basis"]["label"] == "unwind" and fm["readings"][1]["basis"]["dominant"] == "close_long"
    assert fm["readings"][1]["text"].startswith("That selling is longs leaving, not shorts arriving:")
    liq = sum(1 for _, m in msgs if m["market"] == "HYPE" and m["kind"] == "execs"
              for e in m["body"]["execs"] if e.get("liquidation"))
    assert liq > 0 and "liquidation" not in json.dumps(fm)


# --- synthetic fills at chosen block times --------------------------------------------------------------

def _eight_buckets(c=None, market=SP, replay_session=None):
    """Nine quarter hours of fills from T0 (one per bucket, bigger each time; bucket 3 also gets both flips and the
    two other kinds), ending 2 minutes into the ninth: the window then holds buckets 1..8 and bucket 0 is pruned."""
    c = c or client()
    f = Feed(c, market, replay_session=replay_session)
    f.hello()
    f.wm(T0)                                    # the observed window opens here
    tid = 0
    for k in range(9):
        bs = T0 + k * BUCKET_MS
        fills = [_fill(tid + 1, "open_long", bs + 60_000, sz=str(k + 1), user=A, maker=B),
                 _fill(tid + 2, "close_long", bs + 120_000, sz=str(2 * (k + 1)), user=B, maker=A)]
        tid += 2
        if k == 3:
            fills += [_fill(tid + 1, "flip_to_long", bs + 200_000, px="50", sz="2", split=("1", "1"), user=C, maker=B),
                      _fill(tid + 2, "flip_to_short", bs + 210_000, sz="3", split=("2", "1"), user=C, maker=A),
                      _fill(tid + 3, "close_short", bs + 220_000, sz="0.5", user=D, maker=A),
                      _fill(tid + 4, "open_short", bs + 230_000, px="100", sz="1.5", user=D, maker=B)]
            tid += 4
        f.execs(bs + 60_000, fills[:1])
        f.wm(bs + 90_000)
        f.execs(bs + 120_000, fills[1:])
        f.wm(bs + 300_000)
        f.wm(bs + 600_000)
    end = T0 + 8 * BUCKET_MS + 720_000
    f.wm(end)
    return c, f, end


def test_eight_bucket_shape_dir_mapping_flips_and_pruning():
    c, f, end = _eight_buckets()
    fm = _map(c, SP, end + 100)
    assert fm["available"] is True
    _check_identities(fm)
    w = fm["window"]
    assert w == {"start_ms": T0 + BUCKET_MS, "end_ms": end, "bucket_s": 900, "buckets": 8, "coverage_s": 7 * 900 + 720.0,
                 "complete": True, "uncovered": [], "note": flow_map.WINDOW_NOTE}
    st = c.markets[SP].flow_map
    assert T0 not in st.buckets and min(st.buckets) == T0 + BUCKET_MS and len(st.buckets) == 8   # bucket 0 pruned
    for i, b in enumerate(fm["buckets"]):
        k = i + 1
        base = {"open_long_usd": 100.0 * (k + 1), "close_long_usd": 200.0 * (k + 1)}
        extra = ({"flip_to_long_usd": 100.0, "flip_to_short_usd": 300.0, "close_short_usd": 50.0, "open_short_usd": 150.0}
                 if k == 3 else {})
        want = {"open_long_usd": 0.0, "close_short_usd": 0.0, "open_short_usd": 0.0, "close_long_usd": 0.0,
                "flip_to_long_usd": 0.0, "flip_to_short_usd": 0.0} | base | extra
        for key, v in want.items():
            assert b[key] == v, (k, key)
        assert b["flip_usd"] == (400.0 if k == 3 else 0.0) and b["flip_fills"] == (2 if k == 3 else 0)
        assert b["buy_usd"] == want["open_long_usd"] + want["close_short_usd"] + want["flip_to_long_usd"]
        assert b["sell_usd"] == want["close_long_usd"] + want["open_short_usd"] + want["flip_to_short_usd"]
        assert b["risk_added_usd"] == want["open_long_usd"] + want["open_short_usd"]      # flips in neither risk sum
        assert b["risk_removed_usd"] == want["close_long_usd"] + want["close_short_usd"]
        assert b["fills"] == (6 if k == 3 else 2) and b["accounts"] == (4 if k == 3 else 2)
        assert b["unclassified_fills"] == 0 and b["unreadable_fills"] == 0
        if k < 8:
            assert b["complete"] is True and b["coverage_s"] == 900 and b["in_progress"] is False and b["reason"] is None
        else:
            assert b["complete"] is False and b["in_progress"] is True and b["coverage_s"] == 720.0
            assert b["reason"] == "in progress: 720 s of its 900 s have passed"
    b3 = fm["buckets"][2]
    assert (b3["buy_usd"], b3["sell_usd"], b3["net_usd"]) == (550.0, 1250.0, -700.0)
    assert b3["top_account_share_pct"] == round(800 / 1800 * 100, 2)          # B's two closes: 800 of 1800
    t = fm["totals"]
    assert t["fills"] == 2 * 8 + 4 and t["flip_fills"] == 2 and t["flip_usd"] == 400.0
    assert t["open_long_usd"] == 100.0 * sum(range(2, 10)) and t["close_long_usd"] == 200.0 * sum(range(2, 10))
    assert fm["counters"] == {"fills": 2 * 9 + 4, "late_dropped": 0}
    # account shares over the window: A opened longs, B closed them (twice the notional), C the flips, D the rest
    ac = fm["accounts"]
    total = t["aggressive_usd"]
    assert ac["distinct"] == 4 and ac["top"]["address"] == B and ac["top"]["notional_usd"] == 200.0 * sum(range(2, 10))
    assert ac["top"]["share_pct"] == round(ac["top"]["notional_usd"] / total * 100, 2) and ac["top5_share_pct"] == 100.0
    assert [x["kind"] for x in fm["readings"]] == ["net", "composition", "risk", "concentration"]
    assert fm["readings"][3]["text"].endswith("all 4 accounts together took 100.0%.")


def test_direction_comes_from_the_effect_never_from_price_and_each_fill_counts_once():
    """Two buys at falling prices are both Open Long; a retried execution (same tid, later message) is counted once;
    an address on both sides of a self-trade is counted once, as the taker; summing both legs would make buying
    equal selling, which never happens here."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    hi = _fill(1, "open_long", T0 + 1_000, px="100", sz="2", user=A, maker=B)
    lo = _fill(2, "open_long", T0 + 2_000, px="90", sz="2", user=A, maker=B)
    selfie = _fill(3, "close_long", T0 + 3_000, px="100", sz="1", user=C, maker=C)
    f.execs(T0 + 1_000, [hi])
    f.execs(T0 + 2_000, [lo, hi])                    # hi retried in a later message
    f.execs(T0 + 3_000, [selfie, selfie])            # and twice in one
    f.wm(T0 + 4_000)
    fm = _map(c, SP, T0 + 4_100)
    t = fm["totals"]
    assert t["open_long_usd"] == 380.0 and t["buy_usd"] == 380.0 and t["sell_usd"] == 100.0 and t["fills"] == 3
    assert c.markets[SP].flow.counters["duplicates"] == 2
    both_legs = 2 * (380.0 + 100.0)                  # what summing every participant would give: buy = sell = 480
    assert t["buy_usd"] != both_legs / 2 and round(t["buy_usd"] / t["aggressive_usd"] * 100, 1) != 50.0
    ac = fm["accounts"]
    assert ac["distinct"] == 2 and ac["top"] == {"address": A, "notional_usd": 380.0, "share_pct": round(380 / 480 * 100, 2)}


def test_partial_coverage_before_the_feed_and_a_sequence_gap_inside_a_bucket():
    """The feed is first observed 5 minutes into a quarter hour; a line is lost 100 s into the next one. The fills
    before the gap stay counted, the fills between the gap and the next watermark are not (never counted blind), the
    hole is stated on the bucket and the window with the gap's own reason, and nothing is scaled or filled in. The
    accumulator survives the gap (the panel state built on the book doesn't)."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    B0, B1 = T0, T0 + BUCKET_MS
    f.wm(B0 + 300_000)                                               # observed from 5 minutes in
    f.execs(B0 + 400_000, [_fill(1, "open_long", B0 + 400_000, sz="10", user=A)])
    f.wm(B0 + 500_000)
    f.execs(B1 + 60_000, [_fill(2, "open_long", B1 + 60_000, sz="5", user=A)])
    f.wm(B1 + 100_000)
    st = c.markets[SP].flow_map
    ps = c.markets[SP].panels
    f.skip()                                                         # a lost line
    gap_line = f.execs(B1 + 200_000, [_fill(3, "close_long", B1 + 200_000, sz="7", user=B)])
    assert c.markets[SP].counters["gaps"] == 1
    withheld = _map(c, SP, B1 + 200_100)
    assert withheld["available"] is False and "node fills INVALID" in withheld["reason"] and "sequence gap" in withheld["reason"]
    assert set(withheld) == {"available", "reason", "stream", "provenance", "freshness"}
    f.execs(B1 + 250_000, [_fill(4, "open_short", B1 + 250_000, sz="3", user=B)])   # still before the next watermark
    f.wm(B1 + 300_000)                                               # the observed window restarts here
    f.execs(B1 + 400_000, [_fill(5, "close_long", B1 + 400_000, sz="2", user=B)])
    f.wm(B1 + 500_000)
    end = B1 + 600_000
    f.wm(end)
    fm = _map(c, SP, end + 100)
    assert fm["available"] is True
    _check_identities(fm)
    assert c.markets[SP].flow_map is st and c.markets[SP].panels is not ps      # kept over the gap; panels dropped
    assert c.markets[SP].flow.counters["before_window"] == 2
    gap_reason = f"sequence gap: expected {gap_line['seq'] - 1}, got {gap_line['seq']}"
    w = fm["window"]
    assert w["start_ms"] == B1 - 7 * BUCKET_MS and w["end_ms"] == end and w["complete"] is False
    assert w["coverage_s"] == 600 + 400.0
    assert w["uncovered"] == [{"start_ms": w["start_ms"], "end_ms": B0 + 300_000,
                               "why": "before the node fills were observed"},
                              {"start_ms": B1 + 100_000, "end_ms": B1 + 300_000, "why": gap_reason}]
    b0, b1 = fm["buckets"][6], fm["buckets"][7]
    assert b0["start_ms"] == B0 and b0["coverage_s"] == 600.0 and b0["complete"] is False and b0["in_progress"] is False
    assert b0["reason"] == "300 s of it not covered by node fills: before the node fills were observed"
    assert b0["buy_usd"] == 1000.0 and b0["fills"] == 1
    assert b1["start_ms"] == B1 and b1["coverage_s"] == 400.0 and b1["complete"] is False and b1["in_progress"] is True
    assert b1["reason"] == f"in progress: 600 s of its 900 s have passed; 200 s of it not covered by node fills: {gap_reason}"
    assert b1["buy_usd"] == 500.0 and b1["sell_usd"] == 200.0 and b1["fills"] == 2        # 700 and 300 in the hole: not counted
    assert all(b["fills"] == 0 and b["coverage_s"] == 0 for b in fm["buckets"][:6])
    assert fm["totals"]["buy_usd"] == 1500.0 and fm["totals"]["sell_usd"] == 200.0


def test_fills_known_false_is_a_hole_with_its_own_reason():
    """An executions message whose fills record wasn't read ends the observed window (PROTOCOL.md 6.7): the map is
    withheld until the next watermark, and the span between is uncovered, with that reason, on the bucket."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    f.execs(T0 + 10_000, [_fill(1, "open_long", T0 + 10_000, user=A)])
    f.wm(T0 + 20_000)
    unknown = f.execs(T0 + 30_000, [], fills_known=False)
    assert _map(c, SP, T0 + 30_100)["available"] is False
    f.wm(T0 + 50_000)
    f.execs(T0 + 60_000, [_fill(2, "close_long", T0 + 60_000, user=B)])
    f.wm(T0 + 70_000)
    fm = _map(c, SP, T0 + 70_100)
    why = f"executions unknown at block {unknown['block']} (fills record not read)"
    assert fm["window"]["uncovered"] == [
        {"start_ms": T0 - 7 * BUCKET_MS, "end_ms": T0, "why": "before the node fills were observed"},
        {"start_ms": T0 + 20_000, "end_ms": T0 + 50_000, "why": why}]
    last = fm["buckets"][-1]
    assert last["coverage_s"] == 40.0 and last["fills"] == 2 and why in last["reason"]
    assert fm["window"]["coverage_s"] == 40.0 and fm["window"]["complete"] is False


def test_unclassifiable_fills_are_counted_apart_never_guessed():
    """A record without a single identified taker (two makers, or an aggressor that isn't the taker's side), one with
    the taker's position effect missing, and an incomplete one: each is unclassified with its reason, in none of the
    numbers; a record that can't be read at all is counted as unreadable in its bucket, which is then incomplete. The
    net sentence says the unclassified notional could turn it."""
    c = client()
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    ok = _fill(1, "open_long", T0 + 1_000, sz="10", user=A)
    no_taker = _fill(2, "open_long", T0 + 2_000, sz="4", user=A)
    for p in no_taker["participants"]:
        p["role"] = "maker"
    missing = _fill(3, "open_long", T0 + 3_000, sz="3", user=A)
    del missing["participants"][0]["effect"]
    missing["participants"][0]["effect_unavailable"] = "startPosition absent"
    incomplete = _fill(4, "close_long", T0 + 4_000, sz="2", user=B, complete=False)
    mismatch = _fill(5, "close_long", T0 + 5_000, sz="1", user=B, aggressor="B")   # the taker sold; aggressor says B
    unreadable = {"tid": 6, "px": "100", "time_ms": T0 + 6_000, "aggressor": "B", "complete": True, "participants": []}
    f.execs(T0 + 6_000, [ok, no_taker, missing, incomplete, mismatch, unreadable])
    f.wm(T0 + 7_000)
    fm = _map(c, SP, T0 + 7_100)
    _check_identities(fm)
    t, last = fm["totals"], fm["buckets"][-1]
    assert t["buy_usd"] == 1000.0 and t["sell_usd"] == 0 and t["fills"] == 1
    assert t["unclassified_usd"] == 1000.0 and t["unclassified_fills"] == 4 and t["unreadable_fills"] == 1
    assert last["complete"] is False and "1 fill record(s) in it couldn't be read, so its totals are unknown" in last["reason"]
    assert fm["accounts"] == {"top": {"address": A, "notional_usd": 1000.0, "share_pct": 100.0}, "top5_share_pct": 100.0,
                              "distinct": 1, "unattributed_usd": 0.0, "basis_usd": 1000.0, "basis": flow_map.SHARES_BASIS}
    kinds = [x["kind"] for x in fm["readings"]]
    assert kinds == ["net", "composition", "risk", "concentration", "unclassified"]
    net = fm["readings"][0]
    assert net["basis"]["net_in_doubt"] is True
    assert net["text"] == ("Among the fills that could be classified, taker flow ran $1K to the buy side on net ($1K bought "
                           "against $0 sold), but $1K couldn't be classified: enough to turn it.")
    unc = fm["readings"][4]
    assert unc["text"] == ("Another $1K in 4 fill(s) couldn't be classified (no single identified taker (2); startPosition "
                           "absent (1); the execution record is incomplete or conflicting (1)) and is in none of these "
                           "numbers. 1 fill record(s) in the window couldn't be read at all.")
    assert unc["basis"] == {"unclassified_usd": 1000.0, "unclassified_fills": 4, "unreadable_fills": 1,
                            "reasons": {"no single identified taker": 2, "startPosition absent": 1,
                                        "the execution record is incomplete or conflicting": 1}}


# --- every sentence branch ---------------------------------------------------------------------------------

def _bucket(fills=10, flip_fills=0, **usd):
    b = Bucket(0)
    for k, v in usd.items():
        b.usd[k] = Decimal(v)
    b.fills, b.flip_fills = fills, flip_fills
    return b


def _ranked(*amounts):
    return [("0x" + f"{i + 1:040x}", Decimal(a)) for i, a in enumerate(amounts)]


def test_money_and_percent_formats_follow_the_report():
    assert [money(x) for x in (5_760_000, 13_400_000, 711_000, 3_000, 850, 0)] == ["$5.76M", "$13.40M", "$711K", "$3K", "$850", "$0"]
    assert money(999_500) == "$1.00M" and money(999_499) == "$999K" and money(999.5) == "$1K" and money(999.4) == "$999"
    assert money(1_234_000_000) == "$1.23B" and money(-5_760_000) == "$5.76M"
    assert pct_text(Decimal("10.46")) == "10.5%" and pct_text(37.94) == "37.9%"


def test_sentences_sell_side_unwind_as_the_reports_hype_card():
    """The report's HYPE numbers (built here so that they add up exactly): net selling, closing longs over opening
    shorts, the unwind verdict, flips apart, top account and top five."""
    t = _bucket(fills=1000, flip_fills=12, open_long=6_750_000, close_short=6_350_000, flip_to_long=300_000,
                close_long=15_290_000, open_short=3_450_000, flip_to_short=420_000)
    ranked = _ranked(3_420_000, 3_000_000, 2_500_000, 2_000_000, 1_420_000, 900_000, 100_000)
    out = readings(t, ranked, Decimal(32_560_000))
    assert [x["kind"] for x in out] == ["net", "composition", "risk", "concentration"]
    assert [x["text"] for x in out] == [
        "Taker flow ran $5.76M to the sell side on net — $13.40M bought against $19.16M sold.",
        "That selling is longs leaving, not shorts arriving: $15.29M of it closed existing longs against only $3.45M "
        "opening new shorts. An unwind, not a bet.",
        "Across the window, $10.20M opened risk and $21.64M closed it, with $720K in single fills that flipped a position "
        "from one side to the other.",
        "The most active taker account took 10.5% of the aggressive flow ($3.42M); the top five took 37.9%.",
    ]
    assert out[0]["basis"] == {"side": "sell", "net_usd": -5_760_000.0, "buy_usd": 13_400_000.0, "sell_usd": 19_160_000.0,
                               "unclassified_usd": 0.0, "net_in_doubt": False}
    assert out[1]["basis"] == {"side": "sell", "dominant": "close_long", "label": "unwind", "close_long_usd": 15_290_000.0,
                               "open_short_usd": 3_450_000.0, "open_long_usd": 6_750_000.0, "close_short_usd": 6_350_000.0}
    assert out[2]["basis"] == {"risk_added_usd": 10_200_000.0, "risk_removed_usd": 21_640_000.0, "flip_usd": 720_000.0,
                               "flip_fills": 12}
    assert out[3]["basis"] == {"top": {"address": ranked[0][0], "notional_usd": 3_420_000.0, "share_pct": 10.5},
                               "top5_share_pct": 37.9, "distinct": 7, "basis_usd": 32_560_000.0}


def test_sentences_buy_side_position_building_as_the_reports_eth_card():
    t = _bucket(flip_fills=3, open_long=51_030_000, close_short=13_250_000, flip_to_long=450_000,
                close_long=44_000_000, open_short=15_170_000, flip_to_short=320_000)
    out = readings(t, _ranked(44_620_000, 10_000_000, 6_000_000, 3_000_000, 2_980_000, 1_000_000), Decimal(124_220_000))
    assert out[0]["text"] == "Taker flow ran $5.24M to the buy side on net — $64.73M bought against $59.49M sold."
    assert out[1]["text"] == ("That buying is new longs, not shorts covering: $51.03M opened fresh long risk against $13.25M "
                              "closing shorts. Position building.")
    assert out[1]["basis"]["dominant"] == "open_long" and out[1]["basis"]["label"] == "position building"
    assert out[3]["text"] == "The most active taker account took 35.9% of the aggressive flow ($44.62M); the top five took 53.6%."


def test_sentences_mirror_branches_ties_flat_no_flips_and_few_accounts():
    # sell side, shorts arriving: a bet; no flips; two accounts
    t = _bucket(open_long=500_000, close_short=500_000, close_long=1_000_000, open_short=4_000_000)
    out = readings(t, _ranked(4_500_000, 1_500_000), Decimal(6_000_000))
    assert [x["text"] for x in out] == [
        "Taker flow ran $4.00M to the sell side on net — $1.00M bought against $5.00M sold.",
        "That selling is shorts arriving, not longs leaving: $4.00M of it opened new shorts against only $1.00M closing "
        "existing longs. A bet, not an unwind.",
        "Across the window, $4.50M opened risk and $1.50M closed it; no single fill flipped a position from one side to the other.",
        "The most active taker account took 75.0% of the aggressive flow ($4.50M); all 2 accounts together took 100.0%.",
    ]
    assert out[1]["basis"]["dominant"] == "open_short" and out[1]["basis"]["label"] == "bet"
    # buy side, shorts covering; one account
    t = _bucket(open_long=1_000_000, close_short=3_000_000, close_long=500_000, open_short=500_000)
    out = readings(t, _ranked(5_000_000), Decimal(5_000_000))
    assert out[1]["text"] == ("That buying is shorts covering, not new longs: $3.00M closed existing shorts against $1.00M "
                              "opening fresh long risk. Short covering, not position building.")
    assert out[1]["basis"]["dominant"] == "close_short" and out[1]["basis"]["label"] == "short covering"
    assert out[3]["text"] == "The only identified taker account took 100.0% of the aggressive flow ($5.00M)."
    # flat net; no identified account
    t = _bucket(open_long=1_000_000, close_short=1_000_000, close_long=1_000_000, open_short=1_000_000)
    out = readings(t, [], Decimal(4_000_000))
    assert out[0]["text"] == "Taker flow ran flat on net — $2.00M bought against $2.00M sold." and out[0]["basis"]["side"] == "flat"
    assert out[1]["text"] == ("Neither side led: $1.00M opened fresh long risk and $1.00M closed shorts on the buy side; "
                              "$1.00M closed existing longs and $1.00M opened new shorts on the sell side.")
    assert out[1]["basis"]["label"] == "flat" and out[1]["basis"]["dominant"] is None
    assert out[3]["text"] == "No taker account could be identified behind the aggressive flow."
    assert out[3]["basis"] == {"top": None, "top5_share_pct": None, "distinct": 0, "basis_usd": 4_000_000.0}
    # equal components on the net side
    t = _bucket(open_long=500_000, close_short=500_000, close_long=2_000_000, open_short=2_000_000)
    assert readings(t, _ranked(5_000_000), Decimal(5_000_000))[1]["text"] == (
        "That selling split evenly: $2.00M of it closed existing longs and $2.00M opened new shorts.")
    t = _bucket(open_long=2_000_000, close_short=2_000_000, close_long=500_000, open_short=500_000)
    out = readings(t, _ranked(5_000_000), Decimal(5_000_000))
    assert out[1]["text"] == "That buying split evenly: $2.00M opened fresh long risk and $2.00M closed shorts."
    assert out[1]["basis"]["label"] == "even"
    # nothing classified
    assert readings(_bucket(fills=0), [], Decimal(0)) == [{"kind": "empty", "text": "No taker fills in the window.", "basis": {"fills": 0}}]


# --- withheld, replay, the plugin and serving time --------------------------------------------------------

def test_withheld_while_the_flow_family_is_not_usable_like_the_tape():
    c = client()
    f = Feed(c, SP)
    f.hello()
    v = panels.market_view(c, SP, OPTS, T0, T0 * 10 ** 6)
    assert v["families"]["flow"]["state"] == "INVALID"
    assert v["flow_map"]["available"] is False and v["tape"]["available"] is False
    assert v["flow_map"]["reason"] == v["tape"]["reason"] == "node fills INVALID: no watermark yet"
    assert set(v["flow_map"]) == {"available", "reason", "stream", "provenance", "freshness"}
    assert v["flow_map"]["stream"] == "node fills" and v["flow_map"]["provenance"]["mode"] == "LIVE"
    assert v["flow_map"]["freshness"] == v["families"]["flow"]
    f.wm(T0)
    assert _map(c, SP, T0 + 100)["available"] is True                 # observed, with nothing in it yet
    fm = _map(c, SP, T0 + 100)
    assert fm["totals"]["fills"] == 0 and fm["readings"] == [{"kind": "empty", "text": "No taker fills in the window.", "basis": {"fills": 0}}]
    assert fm["accounts"]["top"] is None and fm["accounts"]["top5_share_pct"] is None


def test_replay_view_carries_the_map_labelled_replay():
    c, f, end = _eight_buckets(client(mode="REPLAY"), replay_session="r" * 32)
    v = panels.market_view(c, SP, OPTS, end + 100, (end + 100) * 10 ** 6)
    fm = v["flow_map"]
    assert fm["available"] is True and fm["provenance"]["mode"] == "REPLAY" and fm["freshness"]["state"] == "REPLAY"
    assert fm["provenance"]["replay_session"] == "r" * 32 and fm["totals"]["fills"] == 20
    _check_identities(fm)
    live = _map(_eight_buckets()[0], SP, end + 100)
    assert live["buckets"] == fm["buckets"] and live["totals"] == fm["totals"] and live["readings"] == fm["readings"]


def test_through_the_plugin_snapshot_tab_and_replay_slot(surfaces):
    """The block rides in the snapshot's node views (live and replay) and the tab's snapshot route shows the same;
    a replay started next to the live feed has its own map, never mixed."""
    plugin = surfaces([SP])
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c, f, end = _eight_buckets(plugin.attach(client()))
    r, fr, _ = _eight_buckets(plugin.attach(client("recording:abc", mode="REPLAY")), replay_session="s" * 32)
    fr.execs(end + 1_000, [_fill(99, "open_long", end + 1_000, sz="1000", user=D)])    # only the replay has this fill
    fr.wm(end + 2_000)
    view = plugin.read(SP, end + 3_000)
    fm = view["flow_map"]
    assert fm["available"] is True and fm["provenance"]["mode"] == "LIVE"
    tab = plugin.last["tab"]["node"]
    assert tab["live"]["flow_map"] == json.loads(json.dumps(fm, default=str))
    assert "order flow map" in " ".join(tab["panels"])
    # the node_book tool carries the same map, so the agent can quote its readings (Nov 10)
    assert plugin.last["node_book"]["flow_map"] == json.loads(json.dumps(fm, default=str))
    rep = plugin.read(SP, end + 3_000, slot="replay")["flow_map"]
    assert rep["provenance"]["mode"] == "REPLAY" and rep["freshness"]["state"] == "REPLAY"
    assert rep["totals"]["fills"] == fm["totals"]["fills"] + 1 and rep["totals"]["open_long_usd"] == fm["totals"]["open_long_usd"] + 100_000
    assert plugin.last["tab"]["node"]["replay"]["flow_map"] == json.loads(json.dumps(rep, default=str))
    assert plugin.last["tab"]["node"]["live"]["flow_map"]["totals"] == fm["totals"]            # live unchanged


def test_latest_snapshot_keeps_the_map_labelled_once_the_flow_lapses_by_serving_time(surfaces):
    """Serving-time aging (as the tape), changed by Kevin, Nov 10 (a late family labels, it doesn't withhold): a
    snapshot whose node fills are LIVE when built, served 3 s later past the delayed limit (2 s here), keeps the map
    with its buckets and readings as built, its freshness the flow family's aged status (BEHIND, "by the time this
    was served", the served age and the delay advanced) and an "as of" note that states the block time and the
    delay; the tape is served the same way. Within the limit it stays LIVE, its freshness carrying the served age and
    no note; a historical read is unchanged."""
    from hl_desk.util import iso_ms
    plugin = surfaces([SP])
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c, f, end = _eight_buckets(plugin.attach(client(cfg={"delayed_after_ms": 2_000, "stale_after_ms": 4_000})))
    for k in range(1, 7):
        f.wm(end + 1_000 * k)          # a live feed: a watermark every second, so the 5 s average delay is well under 2 s
    built = end + 6_100
    view = plugin.read(SP, built)
    fm = view["flow_map"]
    assert fm["available"] is True and fm["freshness"]["state"] == "LIVE" and fm["as_of_note"] is None
    assert view["tape"].get("available") is not False and view["tape"]["freshness"] == "LIVE"
    snap = plugin.last["snapshot"]
    words = ("freshness", "as_of_note")
    same = lambda a, b: {k: a[k] for k in a if k not in words} == {k: b[k] for k in b if k not in words}
    late = age_snapshot_for_serving(dict(snap, served={"latest": True, "age_s": 3.0}))["markets"][SP]["node"]["live"]
    fl = late["families"]["flow"]
    assert fl["state"] == "BEHIND" and fl["aged_by_ms"] == 3_000 and "by the time this was served" in fl["reason"]
    w = late["flow_map"]
    assert w["available"] is True and w["freshness"] == fl and same(w, fm)
    assert w["buckets"] == fm["buckets"] and w["readings"] == fm["readings"]
    note = f"as of the node's fills at {iso_ms(fl['watermark_block_time_ms'])}, {fl['delay_ms'] / 1000:.1f} s behind"
    assert w["as_of_note"] == note
    assert late["tape"].get("available") is not False and late["tape"]["freshness"] == "BEHIND"      # served together
    assert late["tape"]["as_of_note"] == note
    soon = age_snapshot_for_serving(dict(snap, served={"latest": True, "age_s": 0.5}))["markets"][SP]["node"]["live"]["flow_map"]
    assert soon["available"] is True and soon["freshness"]["state"] == "LIVE" and soon["freshness"]["aged_by_ms"] == 500
    assert soon["as_of_note"] is None and soon["buckets"] == fm["buckets"] and soon["readings"] == fm["readings"]
    hist = age_snapshot_for_serving(dict(snap, served={"latest": False, "age_s": 9.0}))["markets"][SP]["node"]["live"]["flow_map"]
    assert hist == fm
    # the same on a bare view: a lapsed flow family keeps the map, labelled; an unknown served age reads STALE and
    # keeps it too; within the limit there is no note
    bare = aged_view(view, 3_000)["flow_map"]
    assert bare["available"] is True and bare["freshness"]["state"] == "BEHIND"
    assert bare["as_of_note"].endswith(f", {fl['delay_ms'] / 1000:.1f} s behind")
    unknown = aged_view(view, None)["flow_map"]
    assert unknown["available"] is True and unknown["freshness"]["state"] == "STALE"
    assert unknown["as_of_note"].startswith("as of the node's fills at ")
    assert aged_view(view, 500)["flow_map"]["as_of_note"] is None
