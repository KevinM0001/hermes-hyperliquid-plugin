"""Late node data is served with its state, not withheld (Kevin, Nov 10).

The hl-node ran 73 s behind the chain for an hour on Nov 10 and every node panel went dark. Kevin's decision:
present the information even if delayed, label it delayed and show the time increasing, caveat that node
information may be delayed, keep it subtle. The rule, on the recorded xyz window: a family BEHIND and then STALE
still serves the ladder, walls, thinning, the stop map, the tape, the Flow Deck's node buckets (the flow map) and
the wallet split, each with the family's state and delay_ms and an "as of" note that states the delay; a latest
snapshot that has lapsed by the time it is served is served the same way, its delay advanced; INVALID (a sequence
gap) and UNKNOWN (clock skew) still withhold, as does no data. The expected states are counted here from the
recorded receipt times, independently of the client.
"""

from __future__ import annotations

import json

from hl_desk.node import panels
from hl_desk.util import iso_ms

from node_helpers import XYZ, by_market, client, hello_from, jsonable, read_recording, surfaces

SP = "xyz:SP500"
LIMITS = {"delayed_after_ms": 2_000, "stale_after_ms": 4_000}
# (block, the family it is built on, the label of its "as of" note)
BLOCKS = (("ladder", "book", "book"), ("walls", "book", "book"), ("thinning", "book", "book"),
          ("stops", "triggers", "waiting stops"), ("tape", "flow", "fills"))
WORDS = ("freshness", "as_of_note")


def _steady(plugin, skew_ms=0, drop_seq=None):
    """The recorded SP500 lines received 400 ms after their blocks (published at 300 ms): a steady feed. skew_ms
    puts this machine's clock behind the producer's; drop_seq loses one line (a sequence gap)."""
    c = plugin.attach(client(cfg=LIMITS))
    h, msgs = read_recording(XYZ)
    ms = by_market(msgs, SP)
    start = ms[0][1]["block_time_ms"]
    c.on_line(dict(hello_from(h), pub_ms=start + 300), start + 400 - skew_ms, (start + 400) * 10 ** 6)
    for _, m in ms:
        now = m["block_time_ms"] + 400
        if m["seq"] != drop_seq:
            c.on_line(dict(m, pub_ms=m["block_time_ms"] + 300), now - skew_ms, now * 10 ** 6)
    return c, ms, now


def _blocks(plugin) -> dict:
    """The five blocks as node_book and node_stops serve them."""
    return {k: (plugin.last["node_stops"]["stops"] if k == "stops" else plugin.last["node_book"][k]) for k, _, _ in BLOCKS}


def _numbers(block: dict) -> dict:
    return {k: v for k, v in block.items() if k not in WORDS}


def _expected_delay(watermarks, t, window=5_000):
    """The node feed's delay at t, counted here: over (t - window, t], the age of the latest watermark received at or
    before each instant, averaged over the covered time (test_node_protocol._avg_delay's rule)."""
    from fractions import Fraction
    total, covered = Fraction(0), 0
    got = [(rx, bt) for rx, bt in watermarks if rx <= t]
    for i, (rx, bt) in enumerate(got):
        a, b = max(rx, t - window), (got[i + 1][0] if i + 1 < len(got) else t)
        if b > a:
            total += (b - a) * (Fraction(a + b, 2) - bt)
            covered += b - a
    return float(total / covered)


def _expected_state(delay_ms: float) -> str:
    return "STALE" if delay_ms > LIMITS["stale_after_ms"] else "BEHIND" if delay_ms > LIMITS["delayed_after_ms"] else "LIVE"


def _check_late(block: dict, st: dict, label: str, live: dict) -> None:
    """A late block: the family's state and its "as of" note beside the numbers, which are the LIVE build's own."""
    assert block.get("available") is not False
    assert block["freshness"] == st["state"]
    assert block["as_of_note"] == (f"as of the node's {label} at {iso_ms(st['watermark_block_time_ms'])}, "
                                   f"{st['delay_ms'] / 1000:.1f} s behind")
    assert _numbers(block) == _numbers(live), label


def _latest(plugin, monkeypatch, tool, read_at_ms):
    """A read of the LATEST snapshot at read_at_ms (wall and monotonic alike, as Surfaces stamps snapshots), through
    chat and the tab's tool route, which must agree."""
    from hl_desk import runtime, tools
    monkeypatch.setattr(runtime.time, "time", lambda: read_at_ms / 1000)
    monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: int(read_at_ms * 10 ** 6))
    via_chat = json.loads(tools.run_tool(tool, {"market": SP}, plugin.rt))
    via_tab = plugin.http.get(f"/api/plugins/hyperliquid-desk/tool/{tool}", params={"market": SP}).json()
    strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
    assert via_chat["ok"] and strip(via_chat) == strip(via_tab), via_chat
    return via_chat


def test_a_family_behind_then_stale_serves_its_panels_with_the_state_and_delay(surfaces, monkeypatch):
    """The steady feed is LIVE. Its snapshot read as the latest 2.5 s and 4.5 s later has lapsed to BEHIND and then
    STALE by the time it is served: node_book and node_stops serve the same numbers with that state, the delay
    advanced by the served age, and an "as of" note stating the block time and the delay, through chat and the
    tab's tool route. Then heartbeats keep arriving and blocks don't, so the market's delay (the 5 s average of
    the latest watermark's age, counted here independently) grows past the delayed and then the stale limit at the
    build: at every step the ladder, walls, thinning, stops and tape are served with the numbers the LIVE build
    had, the family's state and delay_ms beside them and the "as of" note, and so are the Flow Deck's node buckets
    and the wallet split over the recorded fills, with the flow family's freshness; the market's delay object is
    as before."""
    plugin = surfaces([SP])
    plugin.desk.collector = {"state": "connected", "note": "offline recorded feed"}
    c, ms, now = _steady(plugin)
    wms = [(m["block_time_ms"] + 400, m["block_time_ms"]) for _, m in ms if m["kind"] == "watermark" and m.get("block_time_ms")]
    v = plugin.read(SP, now)
    fam = v["families"]
    assert all(fam[f]["state"] == "LIVE" for f in ("book", "triggers", "flow")) and fam["book"]["delay_ms"] < 1_000
    assert abs(fam["book"]["delay_ms"] - _expected_delay(wms, now)) <= 1
    live = _blocks(plugin)
    assert live["ladder"]["bids"] and live["ladder"]["asks"] and live["tape"]["rows"]
    assert isinstance(live["stops"]["rows"], list) and live["stops"]["accounted"]["waiting_triggers"] >= 0
    assert live["thinning"]["available"] is True and live["thinning"]["note"] == panels.THIN_NOTE
    for k, _, _ in BLOCKS:
        assert live[k]["freshness"] == "LIVE" and live[k]["as_of_note"] is None, k
    sid = plugin.last["snapshot"]["snapshot_id"]
    cov = c.markets[SP].flow.coverage()
    span = (cov["since_ms"], cov["through_ms"])
    takers = sorted({r["taker"] for r in live["tape"]["rows"] if r["taker"]})
    assert takers
    live_map = panels.deck_buckets(c, SP, *span, now, now * 10 ** 6)
    live_split = panels.wallet_buckets(c, SP, takers, *span, now, now * 10 ** 6)
    assert live_map["freshness"]["state"] == "LIVE" and live_map["coverage"]["covers"] is True
    assert live_split["available"] is True and live_split["freshness"]["state"] == "LIVE"
    assert set(live_split["wallets"]) >= set(takers)

    # Serving time: the LIVE snapshot, read as the latest 2.5 s and then 4.5 s after it was built.
    for after_ms in (2_500, 4_500):
        bound = fam["book"]["delay_ms"] + after_ms
        want = _expected_state(bound)
        assert want == ("BEHIND" if after_ms == 2_500 else "STALE")
        book = _latest(plugin, monkeypatch, "node_book", now + after_ms)
        stops = _latest(plugin, monkeypatch, "node_stops", now + after_ms)
        assert book["snapshot_id"] == stops["snapshot_id"] == sid and book["served"]["latest"] is True
        for res in (book, stops):
            for f in ("book", "triggers", "flow"):
                fst = res["families"][f]
                assert fst["state"] == want and fst["delay_ms"] == bound and fst["aged_by_ms"] == after_ms, (f, fst)
                assert "by the time this was served" in fst["reason"]
        served = {k: (stops["stops"] if k == "stops" else book[k]) for k, _, _ in BLOCKS}
        for key, f, label in BLOCKS:
            _check_late(served[key], book["families"][f], label, jsonable(live[key]))
        assert book["book"]["valid"] is True
    monkeypatch.undo()

    # Heartbeats keep arriving, blocks don't: the delay grows, the states follow it, the data stays.
    states, prev = [], fam["book"]["delay_ms"]
    for k in range(1, 11):
        t = now + k * 1000
        c.on_line({"v": 2, "control": "heartbeat", "epoch": c.epoch, "session": c.session, "pub_ms": t}, t, t * 10 ** 6)
        v = plugin.read(SP, t)
        st = v["families"]["book"]
        want = _expected_state(_expected_delay(wms, t))
        assert st["state"] == want and abs(st["delay_ms"] - _expected_delay(wms, t)) <= 1 and st["delay_ms"] > prev
        assert v["delay"]["state"] == want and v["delay"]["delay_ms"] == st["delay_ms"]   # the delay object as before
        prev = st["delay_ms"]
        states.append(want)
        got = _blocks(plugin)
        for key, f, label in BLOCKS:
            fst = v["families"][f]
            assert fst["state"] == want and fst["delay_ms"] == st["delay_ms"], (key, fst)
            if want == "LIVE":
                assert got[key]["freshness"] == "LIVE" and got[key]["as_of_note"] is None
            else:
                _check_late(got[key], fst, label, live[key])
        assert v["book"]["valid"] is True and v["book"]["reason"] is None
        # the flow map and the wallet split: the same numbers, the flow family's freshness beside them
        m = panels.deck_buckets(c, SP, *span, t, t * 10 ** 6)
        s = panels.wallet_buckets(c, SP, takers, *span, t, t * 10 ** 6)
        for late, first in ((m, live_map), (s, live_split)):
            assert late["freshness"]["state"] == want and late["freshness"]["delay_ms"] == st["delay_ms"]
            assert {x: y for x, y in late.items() if x != "freshness"} == {x: y for x, y in first.items() if x != "freshness"}
    assert states[0] == "LIVE" and "BEHIND" in states and states[-1] == "STALE"
    assert states == sorted(states, key=("LIVE", "BEHIND", "STALE").index)            # each state in order


def test_invalid_and_unknown_still_withhold(surfaces):
    """The same feed with one line lost after the last snapshot (a sequence gap: the book and the triggers INVALID
    with nothing to recover from) and the same feed received on a clock 2.5 s behind the producer's (every family
    UNKNOWN): the panels built on those families are withheld with the state and the reason, as are the Flow
    Deck's node buckets and the wallet split when the node fills are UNKNOWN."""
    ms = by_market(read_recording(XYZ)[1], SP)
    last_snapshot = max(m["seq"] for _, m in ms if m["kind"].endswith("_snapshot"))
    gap = next(m["seq"] for _, m in ms if m["kind"] == "book_delta" and m["seq"] > last_snapshot)
    for case, kw, want, families in (("gap", {"drop_seq": gap}, "INVALID", ("book", "triggers")),
                                     ("skew", {"skew_ms": 2_500}, "UNKNOWN", ("book", "triggers", "flow"))):
        plugin = surfaces([SP])
        plugin.desk.collector = {"state": "connected", "note": "offline recorded feed"}
        c, _, now = _steady(plugin, **kw)
        if case == "gap":
            assert c.markets[SP].counters["gaps"] == 1 and c.markets[SP].book is None
        v = plugin.read(SP, now)
        fam = v["families"]
        assert all(fam[f]["state"] == want for f in families), fam
        assert v["book"]["valid"] is False and want in v["book"]["reason"]
        got = _blocks(plugin)
        for key, f, _ in BLOCKS:
            if f not in families:
                continue
            assert got[key]["available"] is False and want in got[key]["reason"], (case, key, got[key])
            assert fam[f]["reason"] in got[key]["reason"], (case, key)
            assert "freshness" not in got[key] and "as_of_note" not in got[key]
        if "flow" in families:
            m = panels.deck_buckets(c, SP, now - 60_000, now, now, now * 10 ** 6)
            s = panels.wallet_buckets(c, SP, ["0x" + "a" * 40], now - 60_000, now, now, now * 10 ** 6)
            for res in (m, s):
                assert res["available"] is False and f"node fills {want}" in res["reason"], (case, res)
                assert res["freshness"]["state"] == want and "new_long" not in res and "wallets" not in res
