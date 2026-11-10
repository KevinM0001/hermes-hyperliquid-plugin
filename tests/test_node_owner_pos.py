"""Owner positions and the whole-position size estimate (Kevin, Nov 10; PROTOCOL.md 4.8, revision 2.3).

A whole-position TP/SL rests with no stated size: the venue decides it when it fires, from the owner's position
then. The feed's opt-in position stream, "<market>.owner_pos", carries the signed position of every owner of such
an order, as the node's state has it, and the plugin derives the estimate under the Plugin session's rules: |pos|
when the order's side closes the position, less the owner's stated same-side orders that fire first (then an upper
bound), one position counted once per side and kind at its nearest trigger; a flat owner, a wrong-side order or an
owner missing from the stream leave the size unknown with the reason. Stated figures never change; the estimate
rides beside them, labelled, as of the older of the two families' blocks, and never enters the size-outlier test,
the exclusions or the census ratios. No per-account position ever leaves the plugin.

The fixtures are the engine session's hand-made recordings in the revision-2.3 shapes (synthetic values, not node
data): one with a snapshot, a set, a change and a drop; one where the producer declares positions unavailable.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

from hl_desk.node import panels, serving
from hl_desk.node.transport import handshake
from hl_desk.node.triggers import (EST_COUNTED_NEARER, EST_FLAT, EST_MISSING, EST_NOT_YET, EST_NO_OWNER, EST_SET_ASIDE, EST_UPPER, EST_WRONG_SIDE,
                                   Trigger, _cadd, _cbucket, estimates, fires_on)

from node_helpers import FIX, NO_AGE, OPTS, client, hello_from, jsonable, read_recording, surfaces

HAND = FIX / "owner-pos-handmade.jsonl.gz"
HAND_UNAVAILABLE = FIX / "owner-pos-handmade-unavailable.jsonl.gz"
M = "BTC"
S = M + ".owner_pos"
SP = "xyz:SP500"


def _play(path, cfg=NO_AGE, skip=None, hello_patch=None, drop_stream=False):
    """The recording through a live client: hello from its header, then every line (skip: a line's index to lose;
    drop_stream: a producer without the stream sends none of its lines). Returns the client, the header, the lines
    and the read time."""
    h, msgs = read_recording(path)
    c = client(cfg=cfg)
    hello = hello_from(h)
    if hello_patch:
        hello = hello_patch(hello)
    c.on_line(hello, msgs[0][0] - 1, 0)
    for i, (rx, m) in enumerate(msgs):
        if i == skip or (drop_stream and m.get("market") == S):
            continue
        c.on_line(m, rx, (i + 1) * 10 ** 9)
    return c, h, msgs, (msgs[-1][0] + 100, (len(msgs) + 2) * 10 ** 9)


def _view(c, now):
    return panels.market_view(c, M, OPTS, *now)


def _tail(positions):
    return {k[-4:]: str(v) for k, v in (positions or {}).items()}


def test_the_handshake_names_the_position_streams_and_resumes_them():
    c = client(cfg=NO_AGE)
    hs = handshake(c, [SP, M])
    assert hs["markets"] == [SP, M, SP + ".owner_pos", S]
    assert "resume" not in hs
    quiet = client(cfg=NO_AGE | {"owner_positions": False})
    assert handshake(quiet, [SP, M])["markets"] == [SP, M]
    c, h, msgs, now = _play(HAND)
    hs = handshake(c, [M])
    assert hs["markets"] == [M, S] and hs["resume"]["next"] == {M: c.markets[M].last_seq + 1, S: c.markets[M].opos.last_seq + 1}


def test_the_stream_plays_through_snapshot_set_change_and_drop():
    h, msgs = read_recording(HAND)
    assert h["markets"] == [M, S] and h.get("allowlist_version") == "livefeed-v2.1"
    c = client(cfg=NO_AGE)
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    mf = c.markets[M]
    ps = mf.opos
    assert ps.offered and not ps.fam.valid
    seen = []
    for i, (rx, m) in enumerate(msgs):
        c.on_line(m, rx, (i + 1) * 10 ** 9)
        if m.get("market") == S and m["kind"] in ("opos_snapshot", "opos_delta"):
            seen.append((m["kind"], m["block"] - msgs[0][1]["block"], _tail(ps.positions)))
    assert seen == [
        ("opos_snapshot", 0, {"0001": "0.5", "0002": "-1.25", "0004": "0"}),         # U4 flat is a fact, "0"
        ("opos_delta", 1, {"0001": "0.5", "0002": "-1.25", "0004": "0", "0005": "0.2"}),   # U5's first order: set
        ("opos_delta", 2, {"0001": "0.5", "0002": "-1", "0004": "0", "0005": "0.2"}),      # U2 bought 0.25
        ("opos_delta", 3, {"0001": "0.5", "0004": "0", "0005": "0.2"}),                    # U2's last order fired: drop
    ]
    assert ps.fam.valid and ps.counters["snapshots_applied"] == 1 and ps.counters["sets"] == 2 and ps.counters["drops"] == 1
    assert ps.counters["gaps"] == 0 and ps.counters["deltas_unapplied"] == 0 and ps.block == mf.block
    assert ps.basis.startswith("node state snapshot")
    now = (msgs[-1][0] + 100, (len(msgs) + 2) * 10 ** 9)
    st = c.family_status(M, "owner_pos", *now)
    assert st["state"] == "LIVE" and st["watermark_block"] == ps.block and st["family"] == "owner_pos"
    assert c.family_status(M, "triggers", *now)["state"] == "LIVE" and c.family_status(M, "book", *now)["state"] == "LIVE"
    # the stream is no market: it never appears among the client's markets
    assert set(c.markets) == {M}


def _t(oid, side, kind, px, sz, user, whole=False):
    return Trigger(oid, side, kind, Decimal(px), Decimal(sz), whole, user, None)


def test_the_estimate_rules_from_constructed_orders_and_positions():
    L, Sh, F, Mi = ("0x" + c * 40 for c in "abcd")
    orders = {g.oid: g for g in [
        _t(1, "A", "stop", "99", "0", L, True),            # the long's sell stop, fires on a fall
        _t(2, "A", "stop", "98", "0", L, True),            # a second whole-position sell stop, farther: counted at 1
        _t(3, "A", "stop", "99.5", "0.5", L),              # a stated sell stop nearer to firing: closes first
        _t(4, "A", "take_profit", "105", "0", L, True),    # the long's sell take-profit, fires on a rise
        _t(5, "B", "stop", "101", "0", Sh, True),          # the short's buy stop
        _t(6, "A", "stop", "97", "0", Sh, True),           # a sell stop by the short: would add, not close
        _t(7, "B", "stop", "102", "0", F, True),           # a flat owner
        _t(8, "A", "stop", "96", "0", Mi, True),           # an owner missing from the stream
        _t(9, "A", "stop", "95", "0", None, True),         # no owner at all
        _t(10, "B", "take_profit", "90", "1", Sh),         # stated: never estimated
    ]}
    pos = {L: Decimal("2"), Sh: Decimal("-3"), F: Decimal("0")}
    e = estimates(orders, pos)
    assert set(e) == {1, 2, 4, 5, 6, 7, 8, 9}                                  # every whole-position order, no stated one
    assert e[1] == {"sz": Decimal("1.5"), "reason": EST_UPPER, "upper_bound": True}      # 2 less the stated 0.5 that fires first
    assert e[2] == {"sz": Decimal(0), "reason": EST_COUNTED_NEARER, "upper_bound": False}
    assert e[4] == {"sz": Decimal("2"), "reason": None, "upper_bound": False}              # the other move: the whole position
    assert e[5] == {"sz": Decimal("3"), "reason": None, "upper_bound": False}
    assert e[6] == {"sz": None, "reason": EST_WRONG_SIDE, "upper_bound": False}
    assert e[7] == {"sz": None, "reason": EST_FLAT, "upper_bound": False}
    assert e[8] == {"sz": None, "reason": EST_MISSING, "upper_bound": False}
    assert e[9] == {"sz": None, "reason": EST_NO_OWNER, "upper_bound": False}
    # a stated order larger than the position leaves nothing: 0 estimated, said as an upper bound
    e2 = estimates({3: _t(3, "A", "stop", "99.5", "5", L), 1: orders[1]}, pos)
    assert e2[1] == {"sz": Decimal(0), "reason": EST_UPPER, "upper_bound": True}


def test_the_view_serves_the_estimate_beside_the_stated_figures():
    c, h, msgs, now = _play(HAND)
    v = _view(c, now)
    assert v["families"]["owner_pos"]["state"] == "LIVE" and v["families"]["triggers"]["state"] == "LIVE"
    st = v["stops"]
    e = st["estimate"]
    assert e["available"] is True and e["same_block"] is True and e["positions_block"] == e["triggers_block"] == c.markets[M].block
    assert e["owners"] == 3 and e["whole_position_orders"] == 4 and e["estimated"] == 3 and e["counted_at_nearer"] == 0
    assert e["unknown"] == {EST_FLAT: 1} and e["defects"] == 0 and e["upper_bound"] == 0
    assert e["as_of_note"].startswith("if triggered now, from the owners' positions at ") and "block" in e["basis"]
    assert e["freshness"] == "LIVE" and "never enters the size-outlier test" in e["rule"]
    # per band: U5's stop (0.2 x 74500), U1's stop (0.5 x 74000) and take-profit (0.5 x 78000); U4's flat owner unknown
    rows = {(d, r["k"]): r for d, side in st["map"]["sides"].items() for r in side["bands"] if r["orders"]}
    by_est = {k: (r["estimate"]["notional_usd"], r["estimate"]["unknown_reasons"]) for k, r in rows.items()}
    assert by_est == {("fall", 1): (14900.0, {}), ("fall", 2): (37000.0, {}), ("rise", 3): (None, {EST_FLAT: 1}),
                      ("rise", 7): (39000.0, {})}
    for r in rows.values():                                   # the stated figures: unchanged, and unknown stays unknown
        assert r["notional_usd"] is None and r["sized_orders"] == 0 and r["unknown_size_orders"] == 1 and r["sz"] is None
        assert r["estimate"]["label"].startswith("estimated: if triggered now")
        assert r["total_est_usd"] == r["estimate"]["notional_usd"]
    # the by-estimate note names the largest estimated band on each side, since no band has a stated cluster
    notes = {x["fires_on"]: x for x in st["clusters_by_estimate"]}
    assert notes["fall"]["k"] == 2 and "about $37K" in notes["fall"]["text"] and "sell-side" in notes["fall"]["text"].lower()
    assert notes["rise"]["k"] == 7 and "about $39K" in notes["rise"]["text"]
    assert st["clusters"] == [] and st["map"]["sides"]["fall"]["by_estimate_note"] == notes["fall"]["text"]
    # a band of whole-position orders only reads its estimate in its sentence
    text = panels._cluster_text(rows[("fall", 2)])
    assert "every one whole-position, no stated size: about $37K if they fired at their owners' positions as of" in text
    # the census and the exclusions know nothing of estimates (stated sizes only)
    assert st["census"]["unsized_total"] == 4 and "estimate" not in json.dumps(st["exclusions"])
    assert all("estimate" not in k and "est_" not in k for k in st["census"])
    # nothing per account leaves the plugin: no positions map anywhere in the served stops block or the families
    served = json.dumps(jsonable({"stops": st, "families": v["families"]}))
    assert '"positions"' not in served and '"pos"' not in served and "-1.25" not in served


def test_unavailable_old_producer_gap_and_no_stream_withhold_the_estimate_alone():
    # the producer declares positions unavailable: the family and the estimate say why; the stated map stands
    c, h, msgs, now = _play(HAND_UNAVAILABLE)
    v = _view(c, now)
    st = v["families"]["owner_pos"]
    assert st["state"] == "INVALID" and "fills record not read" in st["reason"]
    e = v["stops"]["estimate"]
    assert e["available"] is False and "fills record not read" in e["reason"]
    assert v["families"]["triggers"]["state"] == "LIVE" and v["stops"]["rows"] is not None     # the stated panel stands
    if v["stops"]["map"]["available"]:
        assert all(r["estimate"] is None for side in v["stops"]["map"]["sides"].values() for r in side["bands"])
    # an old producer: the stream is unknown to it, the feed carries no positions
    def old(hello):
        hello = dict(hello, markets={k: x for k, x in hello["markets"].items() if k != S})
        hello["unknown_markets"] = [S]
        hello["allowlist_version"] = "livefeed-v2.0"
        return hello
    c, h, msgs, now = _play(HAND, hello_patch=old, drop_stream=True)
    v = _view(c, now)
    assert v["families"]["owner_pos"]["state"] == "INVALID" and "carries no owner positions" in v["families"]["owner_pos"]["reason"]
    assert "livefeed-v2.0" in v["stops"]["estimate"]["reason"] and v["stops"]["map"]["available"] is True   # this recording has a book
    assert c.markets[M].opos.counters["snapshots_applied"] == 0 and c.family_status(M, "triggers", *now)["state"] == "LIVE"
    # a gap in the stream: owner_pos INVALID alone, the market's families untouched, the estimate withheld
    idx = next(i for i, (_, m) in enumerate(msgs) if m.get("market") == S and m["kind"] == "opos_delta")
    c, h, msgs, now = _play(HAND, skip=idx)
    v = _view(c, now)
    assert "stream gap" in v["families"]["owner_pos"]["reason"] and v["families"]["owner_pos"]["state"] == "INVALID"
    assert v["families"]["triggers"]["state"] == "LIVE" and v["families"]["book"]["state"] == "LIVE"
    assert v["stops"]["estimate"]["available"] is False and "stream gap" in v["stops"]["estimate"]["reason"]
    assert c.markets[M].opos.counters["gaps"] == 1 and c.markets[M].counters["gaps"] == 0
    # a feed that was never asked for the stream: no owner_pos family, and the estimate says so
    def unasked(hello):
        return dict(hello, markets={k: x for k, x in hello["markets"].items() if k != S})
    c, h, msgs, now = _play(HAND, hello_patch=unasked, drop_stream=True)
    v = _view(c, now)
    assert "owner_pos" not in v["families"] and v["stops"]["estimate"]["reason"] == "this feed carries no owner position stream"


def test_serving_time_aging_keeps_the_estimate_with_its_as_of():
    c, h, msgs, now = _play(HAND, cfg={"delayed_after_ms": 10 ** 9, "stale_after_ms": 2 * 10 ** 9})
    v = _view(c, now)
    assert v["families"]["owner_pos"]["state"] == "LIVE" and v["stops"]["estimate"]["available"] is True
    aged = serving.aged_view(v, 1.5 * 10 ** 9)
    assert aged["families"]["owner_pos"]["state"] == "BEHIND" and aged["families"]["triggers"]["state"] == "BEHIND"
    assert aged["stops"]["estimate"] == dict(v["stops"]["estimate"], freshness="BEHIND") and aged["stops"]["freshness"] == "BEHIND"
    assert aged["stops"]["map"]["sides"]["fall"]["bands"][2]["estimate"] == v["stops"]["map"]["sides"]["fall"]["bands"][2]["estimate"]


def test_the_estimate_pass_is_cheap_and_cached(monkeypatch):
    # 20,000 waiting triggers, half whole-position, 7,500 owners: one pass well under a tenth of a second
    users = ["0x" + f"{i:040x}" for i in range(7_500)]
    orders = {}
    for i in range(20_000):
        u = users[i % len(users)]
        whole = i % 2 == 0
        orders[i] = Trigger(i, "A" if i % 3 else "B", "stop" if i % 5 else "take_profit", Decimal(70_000 + (i % 400) * 5),
                            Decimal(0) if whole else Decimal("0.01"), whole, u, None)
    pos = {u: Decimal(1 if i % 4 else -1) for i, u in enumerate(users)}
    t0 = time.perf_counter()
    e = estimates(orders, pos)
    dt = time.perf_counter() - t0
    assert len(e) == 10_000 and dt < 0.25, dt
    # through the view: computed once per (triggers change, positions change), then served from the cache
    c, h, msgs, now = _play(HAND)
    calls = []
    real = panels.estimates
    monkeypatch.setattr(panels, "estimates", lambda orders, positions, *a: calls.append(1) or real(orders, positions, *a))
    _view(c, now)
    _view(c, (now[0] + 1_000, now[1] + 10 ** 9))
    assert len(calls) == 1


def test_through_the_plugin_surfaces_the_estimate_reaches_chat_and_the_tab(surfaces):
    plugin = surfaces([M])
    plugin.desk.collector = {"state": "connected", "note": "offline recorded feed"}
    h, msgs = read_recording(HAND)
    c = plugin.attach(client(cfg=NO_AGE))
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    for i, (rx, m) in enumerate(msgs):
        c.on_line(m, rx, (i + 1) * 10 ** 9)
    now = (msgs[-1][0] + 100, (len(msgs) + 2) * 10 ** 9)
    plugin.read(M, *now)
    ns = plugin.last["node_stops"]["stops"]
    assert ns["estimate"]["available"] is True and ns["estimate"]["owners"] == 3 and ns["clusters_by_estimate"]
    tab = plugin.last["tab"]["node"]["live"]
    assert tab["stops"]["estimate"] == ns["estimate"] and tab["families"]["owner_pos"]["state"] == "LIVE"
    status = plugin.last["node_status"]["markets"][M]["families"]
    assert status["owner_pos"]["state"] == "LIVE"
    served = json.dumps(plugin.last["snapshot"])
    assert '"positions"' not in served and "-1.25" not in served          # no per-account position is ever served


# --- the producer's own recording (the recorded Nov 7 window replayed offline, HYPE and its stream, 30 s) ----------

REAL = FIX / "owner-pos-HYPE-30s.jsonl.gz"


def test_the_producers_recording_gives_real_estimates_beside_the_stated_clusters():
    """Engine's real-data recording (made by the actual producer at its branch commit, checked against an independent
    calculation: 194,855 position checks, 0 mismatches): 1,751 covered owners, then 4 sets and 7 drops. Every number
    pinned here is the plugin's reading of it, so a change in the rules or the client shows up."""
    from node_helpers import feed
    h, msgs = read_recording(REAL)
    assert h["markets"] == ["HYPE", "HYPE.owner_pos"] and h["allowlist_version"] == "livefeed-v2.1"
    c = client(cfg=NO_AGE)
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    feed(c, msgs)
    mf, ps = c.markets["HYPE"], c.markets["HYPE"].opos
    assert len(ps.positions) == 1_751 and ps.counters["sets"] == 4 and ps.counters["drops"] == 7 and ps.counters["gaps"] == 0
    assert mf.fam["book"].valid and ps.block == mf.block
    rx = msgs[-1][0] + 100
    now = (rx, (rx - msgs[0][0]) * 10 ** 6)
    t0 = time.perf_counter()
    v = panels.market_view(c, "HYPE", OPTS, *now)
    assert time.perf_counter() - t0 < 1.0                     # a full build with the estimate, under load
    assert {k: f["state"] for k, f in v["families"].items()} == {k: "LIVE" for k in ("book", "triggers", "depth", "bands", "flow", "owner_pos")}
    st, e = v["stops"], v["stops"]["estimate"]
    assert e["available"] and e["same_block"] and e["defects"] == 0
    # 28 whole-position orders are set aside (already true at the price, or implausible: the map excludes them), 3 owners flat
    assert (e["owners"], e["whole_position_orders"], e["estimated"], e["counted_at_nearer"], e["upper_bound"]) == (1_751, 2_081, 2_046, 4, 46)
    assert e["unknown"] == {EST_SET_ASIDE: 28, EST_FLAT: 3} and e["as_of_note"].endswith("Z") and "snapshot 1212920000" in e["basis"]
    # the clusters: stated first, the estimate second, both walk shares, the whole-position count and how many estimated
    top = st["clusters"][0]
    assert top["fires_on"] == "fall" and top["k"] == 8 and top["lower_bound"] is True
    assert top["notional_usd"] == 4_197_893.01 and top["estimate"]["notional_usd"] == 796_120.01 and top["total_est_usd"] == 4_994_013.02
    assert top["walk"]["ratio_pct"] == 21.17 and top["walk"]["ratio_pct_est"] == 25.18
    assert top["text"] == ("Sell-side triggers at 73.7885 to 73.4042 (-4.0% to -4.5%): at least $4.20M stated, about $4.99M with "
                           "whole-position orders at their owners' positions as of 20:48:26Z, across 94 orders from 88 accounts; the "
                           "bids at and below it total $19.83M, so this cluster is at least 21% of the liquidity it would walk, about "
                           "25% with the estimate; 33 of 94 (35%) whole-position, 33 estimated")
    assert [(x["fires_on"], x["k"]) for x in st["clusters"]] == [("fall", 8), ("fall", 1), ("rise", 13), ("rise", 11)]   # ranked by stated notional
    # the estimate's largest buy-side band differs from the stated cluster: one sentence says so
    assert [(x["fires_on"], x["k"]) for x in st["clusters_by_estimate"]] == [("rise", 14)]
    assert st["clusters_by_estimate"][0]["text"].startswith("by the estimate, the largest buy-side trigger band is 82.2434 to 82.6277 (+7.0% to +7.5%), about $3.01M")
    # over the bands: the estimate nearly doubles the in-band interest; the stated figures and the exclusions are untouched
    rows = [r for side in st["map"]["sides"].values() for r in side["bands"]]
    assert round(sum(r["notional_usd"] or 0 for r in rows)) == 14_863_567 and round(sum((r["estimate"] or {}).get("notional_usd") or 0 for r in rows)) == 14_433_865
    assert sum(1 for r in rows if (r["estimate"] or {}).get("orders")) == 39 and not any((r["estimate"] or {}).get("outsized") for r in rows)
    assert st["exclusions"]["size_outlier"]["orders"] == 0 and st["census"]["unsized_total"] == 2_081
    # a band's estimate is the sum over its whole-position orders of the per-order estimate at the trigger price
    est = panels.owner_estimates(mf, v["families"])[0]
    from hl_desk.node.triggers import fires_on
    mid = mf.book.mid()
    want = Decimal(0)
    for g in mf.triggers.orders.values():
        if g.position_tpsl and fires_on(g.kind, g.side) == "fall" and g.trigger_px < mid:
            k = int((mid - g.trigger_px) / mid / Decimal("0.005"))
            if k == 8 and est[g.oid]["sz"] is not None:
                want += est[g.oid]["sz"] * g.trigger_px
    assert round(float(want), 2) == top["estimate"]["notional_usd"]       # the served figure is rounded to cents
    served = json.dumps(jsonable(v))
    assert '"positions"' not in served                           # nothing per account leaves the plugin


# --- the independent review's findings (Nov 10 evening), each pinned ---------------------------------------------

def test_the_outsized_flag_names_no_owner_and_no_size():
    """A per-account position never leaves the plugin: the outsized flag carries the notional and the note only."""
    L = "0x" + "a" * 40
    g = _t(1, "A", "stop", "74000", "0", L, True)
    b = _cbucket()
    _cadd(b, g, {1: {"sz": Decimal("10"), "reason": None, "upper_bound": False}})
    meta = panels.MAP_SIDES["fall"]
    dband = {"lo": Decimal("73500"), "hi": Decimal("74000")}
    row = panels._map_row("fall", meta, 2, b, dband, Decimal("1000"), Decimal("5000"), {"available": True, "as_of_ms": 1})
    assert row["estimate"]["outsized"] == {"notional_usd": 740000.0, "note": row["estimate"]["outsized"]["note"]}
    assert "user" not in row["estimate"]["outsized"] and "sz" not in row["estimate"]["outsized"]
    assert "0x" not in json.dumps(row["estimate"])


def test_the_streams_own_watermarks_bound_its_freshness():
    """A silent position stream beside a live market reads BEHIND by its own last watermark's age; the market's
    families stay LIVE. Before: the family's delay came from the market's watermarks."""
    c, h, msgs, now = _play(HAND, cfg={"delayed_after_ms": 2_000, "stale_after_ms": 10 ** 12})
    mf = c.markets[M]
    last = msgs[-1][1]
    seq, block, bt = mf.last_seq, mf.block, mf.block_time_ms
    sess = last["session"]
    for k in range(1, 4):                                      # the market goes on: fresh watermarks every second
        seq += 1
        bt2 = bt + 1_000 * k
        c.on_line({"v": 2, "epoch": last["epoch"], "session": sess, "market": M, "seq": seq, "kind": "watermark", "block": block + k,
                   "block_time_ms": bt2, "pub_ms": bt2 + 10, "body": {"through_seq": seq - 1}}, bt2 + 50, (len(msgs) + 1 + k) * 10 ** 9)
    at = (bt + 3_100, (len(msgs) + 4) * 10 ** 9 + 100 * 10 ** 6)
    assert c.family_status(M, "book", *at)["state"] == "LIVE" and c.family_status(M, "triggers", *at)["state"] == "LIVE"
    st = c.family_status(M, "owner_pos", *at)
    assert st["state"] == "BEHIND" and st["delay_ms"] > 2_000
    assert panels.market_view(c, M, OPTS, *at)["stops"]["estimate"]["freshness"] == "BEHIND"


def test_a_dropped_subscriber_discards_a_half_assembled_position_snapshot():
    h, msgs = read_recording(HAND)
    c = client(cfg=NO_AGE)
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    first = next(m for _, m in msgs if m.get("market") == S and m["kind"] == "opos_snapshot")
    part = dict(first, body=dict(first["body"], parts=2, positions=first["body"]["positions"][:1]))
    c.on_line(part, msgs[0][0], 10 ** 9)
    ps = c.markets[M].opos
    assert ps.assembly is not None and ps.assembly.next_part == 2
    c.on_line({"v": 2, "control": "dropped", "reason": "slow subscriber", "last_sent": {}}, msgs[0][0] + 1, 2 * 10 ** 9)
    assert ps.assembly is None and ps.counters["snapshots_discarded"] == 1 and "dropped" in ps.fam.reason


def test_an_owner_missing_between_the_markets_line_and_the_streams_is_not_a_defect():
    """The market's trig_delta adding U5's whole-position order lands one line before the stream's set for U5: at that
    moment the families stand at different blocks, so U5 reads "not in the position stream yet", not a feed defect;
    once the set arrives the blocks agree and U5 is estimated."""
    h, msgs = read_recording(HAND)
    c = client(cfg=NO_AGE)
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    idx = next(i for i, (_, m) in enumerate(msgs) if m.get("market") == S and m["kind"] == "opos_delta")
    for i, (rx, m) in enumerate(msgs[:idx]):
        c.on_line(m, rx, (i + 1) * 10 ** 9)
    now = (msgs[idx - 1][0] + 100, (idx + 1) * 10 ** 9)
    e = panels.market_view(c, M, OPTS, *now)["stops"]["estimate"]
    assert e["same_block"] is False and e["defects"] == 0 and e["unknown"].get(EST_NOT_YET) == 1 and EST_MISSING not in e["unknown"]
    assert "different blocks" in e["basis"]
    for i, (rx, m) in enumerate(msgs[idx:], idx):
        c.on_line(m, rx, (i + 1) * 10 ** 9)
    now = (msgs[-1][0] + 100, (len(msgs) + 2) * 10 ** 9)
    e = panels.market_view(c, M, OPTS, *now)["stops"]["estimate"]
    assert e["same_block"] is True and e["defects"] == 0 and EST_NOT_YET not in e["unknown"] and e["estimated"] == 3


def test_an_order_the_map_excludes_at_the_price_never_takes_the_position():
    """A stale bracket (a sell stop above the price, already true at it) of the same owner, side and kind is set
    aside: the waiting in-band order carries the position; without the reference price both would be ordered by
    trigger price alone and the stale one would fire first."""
    from hl_desk.node.triggers import EST_SET_ASIDE
    L = "0x" + "a" * 40
    orders = {g.oid: g for g in [_t(1, "A", "stop", "76000", "0", L, True),       # above the mid: already true, set aside
                                  _t(2, "A", "stop", "74000", "0", L, True)]}      # the waiting one, in a band below
    pos = {L: Decimal("2")}
    e = estimates(orders, pos, EST_MISSING, Decimal("75005"))
    assert e[1] == {"sz": None, "reason": EST_SET_ASIDE, "upper_bound": False}
    assert e[2] == {"sz": Decimal("2"), "reason": None, "upper_bound": False}
    e2 = estimates(orders, pos)                                                   # no reference price: by price alone
    assert e2[1]["sz"] == Decimal("2") and e2[2]["reason"] == EST_COUNTED_NEARER


def test_a_periodic_snapshot_that_differs_from_the_tracked_set_is_counted():
    c, h, msgs, now = _play(HAND)
    ps = c.markets[M].opos
    first = next(m for _, m in msgs if m.get("market") == S and m["kind"] == "opos_snapshot")
    seq = ps.last_seq + 1
    body = dict(first["body"], snapshot_id=S + ":9", through_seq=seq - 1, positions=[[u, str(v)] for u, v in sorted(ps.positions.items())], total_owners=len(ps.positions))
    c.on_line(dict(first, seq=seq, body=body, block=ps.block, block_time_ms=ps.block_time_ms), now[0], now[1] + 10 ** 9)
    assert ps.counters["snapshot_mismatches"] == 0 and ps.counters["snapshots_applied"] == 2
    seq += 1
    changed = [[u, str(v)] for u, v in sorted(ps.positions.items())]
    changed[0][1] = "9"
    body = dict(body, snapshot_id=S + ":10", through_seq=seq - 1, positions=changed)
    c.on_line(dict(first, seq=seq, body=body, block=ps.block, block_time_ms=ps.block_time_ms), now[0] + 1, now[1] + 2 * 10 ** 9)
    assert ps.counters["snapshot_mismatches"] == 1 and ps.positions[changed[0][0]] == Decimal("9") and ps.fam.valid


def test_a_stops_block_without_a_book_still_has_every_estimate_field():
    c, h, msgs, now = _play(HAND)
    mf = c.markets[M]
    st = panels.stop_map(mf, None, "no valid book", None, {"available": False, "reason": "x", "rule": panels.EST_RULE, "freshness": None})
    assert st["clusters_by_estimate"] == [] and st["estimate"]["available"] is False and st["map"]["available"] is False


def test_the_sentence_counts_the_orders_taken_at_a_nearer_trigger_and_the_total_matches_the_served_figures():
    c, h, msgs, now = _play(HAND)
    v = _view(c, now)
    row = next(r for side in v["stops"]["map"]["sides"].values() for r in side["bands"] if (r["estimate"] or {}).get("orders"))
    row = json.loads(json.dumps(jsonable(row)))
    row["notional_usd"], row["lower_bound"], row["sized_orders"] = 1000.004, True, 1          # a stated part beside the estimate
    row["estimate"]["counted_at_nearer"], row["estimate"]["unknown_orders"] = 2, 1
    row["orders"], row["unknown_size_orders"] = 5, 4
    row["total_est_usd"] = round(row["notional_usd"] + row["estimate"]["notional_usd"], 2)
    text = panels._cluster_text(row)
    assert "4 of 5 (80%) whole-position, 1 estimated, 2 counted at a nearer trigger, 1 unknown" in text
    for r in (r for side in v["stops"]["map"]["sides"].values() for r in side["bands"] if r.get("total_est_usd") is not None):
        assert r["total_est_usd"] == round((r["notional_usd"] or 0.0) + (r["estimate"]["notional_usd"] or 0.0), 2)
