"""A relay sends its hello once, at connect. When the producer restarts, the relay starts a new session on the same
connection: the next line carries the new epoch, the client replaces its partition, and no hello follows. A position
stream the hello announced stays announced through that change (its family waits for the new session's own snapshot,
never reading as a feed without positions), a stream the connection never announced stays absent, and a hello on a
later connection decides the views by name, so nothing stale from an earlier connection survives it."""

from __future__ import annotations

import copy

from hl_desk.node import panels
from hl_desk.node.client import PosStream
from node_helpers import FIX, OPTS, client, hello_from, read_recording

HAND = FIX / "owner-pos-handmade.jsonl.gz"
M = "BTC"
S = M + ".owner_pos"
FRESH = PosStream().fam.reason                                  # a stream announced, nothing of it served yet
NO_STREAM = "this feed carries no owner position stream"       # the estimate's reason when the feed has no stream
BLOCK_MS = 70                                                  # the fixture's block spacing


def _relay_hello(h: dict, views: dict, epoch: int | None = None, session: str | None = None, start: dict | None = None) -> dict:
    """The relay's hello from the recording's header: the view of each market and stream on this connection."""
    hello = hello_from(h)
    hello["markets"] = {m: {"start_seq": (start or {}).get(m, 1), "next_seq": (start or {}).get(m, 1), "oldest_seq": 1,
                            "view": v, "snapshot": "in_stream" if v == "full" else "none"} for m, v in views.items()}
    if epoch is not None:
        hello["epoch"], hello["session"] = epoch, session
    return hello


def _in_session(msg: dict, epoch: int, session: str, market: str | None = None, block_shift: int = 0) -> dict:
    """The recorded line as a later producer session sends it: its own epoch and session, the sequence as recorded (a
    session numbers from its own start), the block and times moved on; optionally under another market's name."""
    out = copy.deepcopy(msg)
    out["epoch"], out["session"] = epoch, session
    if market is not None:
        stream = msg["market"].endswith(".owner_pos")
        out["market"] = market + ".owner_pos" if stream else market
        if "snapshot_id" in out.get("body", {}):
            out["body"]["snapshot_id"] = out["body"]["snapshot_id"].replace(M, market, 1)
    out["block"] += block_shift
    out["block_time_ms"] += block_shift * BLOCK_MS
    out["pub_ms"] += block_shift * BLOCK_MS
    return out


def _play(c, lines: list, t: int) -> tuple[int, tuple]:
    """Feed the lines (received 100 ms after publication, a second apart on the monotonic clock); returns the next
    line's index and the time to read the client at."""
    for m in lines:
        t += 1
        c.on_line(m, m["pub_ms"] + 100, t * 10 ** 9)
    return t, (lines[-1]["pub_ms"] + 200, (t + 1) * 10 ** 9)


def _tail(positions):
    return {k[-4:]: str(v) for k, v in (positions or {}).items()}


def test_a_new_producer_session_on_the_same_connection_keeps_the_position_stream_announced():
    h, msgs = read_recording(HAND)
    lines = [m for _, m in msgs]
    ep, ss = h["producer"]["epoch"], h["producer"]["session"]
    c = client("relay")
    c.on_connect(lines[0]["pub_ms"])
    c.on_line(_relay_hello(h, {M: "full", S: "full"}), lines[0]["pub_ms"], 0)
    t, now = _play(c, lines[:9], 0)                       # through block 2: the snapshot, a set, their watermarks
    old = c.markets[M].opos
    assert old.offered and old.fam.valid and _tail(old.positions) == {"0001": "0.5", "0002": "-1.25", "0004": "0", "0005": "0.2"}
    assert panels.families(c, M, *now)["owner_pos"]["state"] == "LIVE"

    # The producer restarts: the relay starts a new session on the same connection, with no hello. Its snapshot holds
    # a different set, so the positions served afterwards are shown to be the new session's.
    ep2, ss2 = ep + 60_000, "1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"
    new = [_in_session(m, ep2, ss2, block_shift=1000) for m in lines]
    snap = next(m for m in new if m["market"] == S and m["kind"] == "opos_snapshot")
    snap["body"]["positions"][0][1] = "0.75"

    t, now = _play(c, new[:1], t)                         # the first line of the new epoch: the partition is replaced
    assert c.epoch == ep2 and c.session == ss2 and (ep, ss) in c.retired
    mf = c.markets[M]
    ps = mf.opos
    assert ps is not old and ps.offered is True and ps.unavailable is None
    assert ps.last_seq is None and ps.expected_start is None and ps.run_start is None   # nothing resumes across sessions
    assert ps.positions is None and not ps.fam.valid
    assert f"epoch {ep2}" in mf.flow.stream                # the fresh feed is labelled with the new session, not the old
    st = panels.families(c, M, *now)["owner_pos"]
    assert st["state"] == "INVALID" and st["reason"] == FRESH and "carries no owner position" not in st["reason"]

    t, now = _play(c, new[1:3], t)                        # the triggers snapshot and its watermark: the stops are served
    v = panels.market_view(c, M, OPTS, *now)
    assert v["families"]["owner_pos"]["reason"] == FRESH
    est = v["stops"]["estimate"]
    assert est["available"] is False and FRESH in est["reason"] and est["reason"] != NO_STREAM

    t, now = _play(c, new[3:5], t)                        # the new session's own snapshot and its watermark
    assert ps.fam.valid and ps.counters["snapshots_applied"] == 1 and ps.counters["gaps"] == 0
    assert _tail(ps.positions) == {"0001": "0.75", "0002": "-1.25", "0004": "0"}
    v = panels.market_view(c, M, OPTS, *now)
    assert v["families"]["owner_pos"]["state"] == "LIVE" and v["stops"]["estimate"]["available"] is True

    t, now = _play(c, new[5:9], t)                        # and the session plays on: the set applies
    assert _tail(ps.positions) == {"0001": "0.75", "0002": "-1.25", "0004": "0", "0005": "0.2"}
    assert ps.counters["deltas_unapplied"] == 0 and panels.families(c, M, *now)["owner_pos"]["state"] == "LIVE"


def test_a_connection_that_never_announced_the_stream_has_none_after_the_change():
    h, msgs = read_recording(HAND)
    lines = [m for _, m in msgs if not m["market"].endswith(".owner_pos")]
    ep, ss = h["producer"]["epoch"], h["producer"]["session"]
    c = client("relay")
    c.on_connect(lines[0]["pub_ms"])
    c.on_line(_relay_hello(h, {M: "full"}), lines[0]["pub_ms"], 0)
    t, now = _play(c, lines[:7], 0)
    assert S not in c.views and not c.markets[M].opos.offered
    assert "owner_pos" not in panels.families(c, M, *now)
    assert panels.market_view(c, M, OPTS, *now)["stops"]["estimate"]["reason"] == NO_STREAM

    ep2, ss2 = ep + 60_000, "1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"
    new = [_in_session(m, ep2, ss2, block_shift=1000) for m in lines]
    t, now = _play(c, new[:1], t)
    assert c.epoch == ep2 and not c.markets[M].opos.offered and S not in c.views
    assert "owner_pos" not in panels.families(c, M, *now)
    t, now = _play(c, new[1:7], t)
    assert "owner_pos" not in panels.families(c, M, *now)
    assert panels.market_view(c, M, OPTS, *now)["stops"]["estimate"]["reason"] == NO_STREAM


def test_a_later_hello_decides_the_views_by_name_so_nothing_stale_survives_a_reconnect():
    """Connection 1 carries BTC in full with its stream, and SOL executions only. The connection drops and the producer
    restarts; connection 2's hello opens HYPE with its stream, keeps BTC executions only and lists neither SOL nor BTC's
    stream. That hello arrives after the session change has already replaced the partition, so it must drop the views
    by name; otherwise the next change without a hello would offer BTC a stream this connection doesn't carry."""
    h, msgs = read_recording(HAND)
    lines = [m for _, m in msgs]
    ep, ss = h["producer"]["epoch"], h["producer"]["session"]
    c = client("relay")
    c.on_connect(lines[0]["pub_ms"])
    c.on_line(_relay_hello(h, {M: "full", S: "full", "SOL": "execs"}), lines[0]["pub_ms"], 0)
    t, now = _play(c, lines[:9], 0)
    assert c.views == {M: "full", S: "full", "SOL": "execs"} and c.markets[M].opos.fam.valid

    wall = now[0]
    c.on_disconnect("the relay closed the connection", wall)
    c.on_connect(wall + 1_000)
    ep2, ss2 = ep + 60_000, "1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"
    hello2 = _relay_hello(h, {"HYPE": "full", "HYPE.owner_pos": "full", M: "execs"}, epoch=ep2, session=ss2, start={M: 7})
    c.on_line(hello2, wall + 1_000, (t + 1) * 10 ** 9)
    assert c.epoch == ep2 and set(c.markets) == {"HYPE", M}
    assert c.views == {"HYPE": "full", "HYPE.owner_pos": "full", M: "execs"}      # no BTC.owner_pos, no SOL
    assert c.markets["HYPE"].opos.offered and not c.markets[M].opos.offered

    hype = [_in_session(m, ep2, ss2, market="HYPE", block_shift=1000) for m in lines]
    btc_execs = [_in_session(m, ep2, ss2, block_shift=1000) for m in lines if m["market"] == M and m["kind"] == "execs"]
    t, now = _play(c, hype[:5] + btc_execs[:1], t + 1)
    assert c.markets["HYPE"].opos.fam.valid and panels.families(c, "HYPE", *now)["owner_pos"]["state"] == "LIVE"
    assert "owner_pos" not in panels.families(c, M, *now) and c.markets[M].last_seq == 7

    # The producer restarts again, this time with the connection held: session 3's first lines come with no hello.
    ep3, ss3 = ep + 120_000, "2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d"
    hype3 = [_in_session(m, ep3, ss3, market="HYPE", block_shift=2000) for m in lines]
    btc3 = [_in_session(m, ep3, ss3, block_shift=2000) for m in lines if m["market"] == M and m["kind"] == "execs"]
    t, now = _play(c, hype3[:1] + btc3[:1], t)
    assert c.epoch == ep3 and set(c.markets) == {"HYPE", M}
    assert c.views == {"HYPE": "full", "HYPE.owner_pos": "full", M: "execs"}
    hps, bps = c.markets["HYPE"].opos, c.markets[M].opos
    assert hps.offered is True and hps.last_seq is None and not hps.fam.valid
    assert panels.families(c, "HYPE", *now)["owner_pos"]["reason"] == FRESH
    assert bps.offered is False and bps.unavailable is None and "owner_pos" not in panels.families(c, M, *now)
    assert "SOL" not in c.markets and "SOL" not in c.views

    t, now = _play(c, hype3[1:5], t)                      # session 3's own snapshot: HYPE's family is back, BTC still has none
    assert hps.fam.valid and panels.families(c, "HYPE", *now)["owner_pos"]["state"] == "LIVE"
    assert _tail(hps.positions) == {"0001": "0.5", "0002": "-1.25", "0004": "0"}
    assert "owner_pos" not in panels.families(c, M, *now)
