"""Node mode through the plugin itself: settings, the optional key, the transports, the node_* tools and the tab,
and replay beside live data (Handoff 1c Part B steps 11-13). Every intermediate tool and tab result is checked."""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import time
import types
from decimal import Decimal
from pathlib import Path

import pytest

from hl_desk import config as cfg_mod
from hl_desk.config import DeskConfig
from hl_desk.node import panels
from hl_desk.node.client import FeedClient
from hl_desk.node.manager import NodeManager
from hl_desk.node.replay import Recording, ReplayPlayer
from hl_desk.node.transport import handshake, parse_source, run_feed

from helpers import make_desk
from node_helpers import EXECS, OPTS, RESTART, XYZ, by_market, hello_from, jsonable, read_recording, read_tap, surfaces

import hl_desk
ROOT = Path(hl_desk.__file__).resolve().parent.parent          # the plugin repo, wherever these tests run from
SP, XY = "xyz:SP500", "xyz:XYZ100"


# --- step 11: the optional key, and loading without any node input ------------------------------

def _manifest_block(text: str, key: str) -> dict:
    """The flat `key: value` lines of one config_schema entry (the manifest is plain YAML; no parser needed)."""
    lines = text.splitlines()
    i = lines.index(f"  {key}:")
    out = {}
    for ln in lines[i + 1:]:
        if not ln.startswith("    "):
            break
        k, _, v = ln.strip().partition(": ")
        out[k] = v
    return out


def test_the_key_is_an_optional_hermes_secret_never_required():
    text = (ROOT / "plugin.yaml").read_text()
    key = _manifest_block(text, "node_key")
    assert key["type"] == "secret" and key["env"] == "HL_DESK_NODE_KEY" and key["required"] == "false"
    assert "requires_env" not in text                       # requires_env would disable the plugin without it
    for name in ("node_status", "node_book", "node_stops"):
        assert f"  - {name}" in text


def test_the_key_is_read_only_through_hermes_secret_scope(monkeypatch):
    monkeypatch.setenv("HL_DESK_NODE_KEY", "from-the-environment")
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)              # not inside Hermes
    assert cfg_mod.node_key() is None                                         # never read from os.environ directly
    fake = types.ModuleType("agent.secret_scope")
    calls = []
    fake.get_secret = lambda name, default=None: calls.append(name) or "from-hermes"
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    assert cfg_mod.node_key() == "from-hermes" and calls == ["HL_DESK_NODE_KEY"]
    # Hermes refusing the read (no secret scope on a multiplexing gateway) is shown as such, never as "not set",
    # and only the error's type is kept: the plugin connects to a public relay without a key.

    class UnscopedSecretError(RuntimeError):
        pass

    def refuse(name, default=None):
        raise UnscopedSecretError("no profile secret scope active")
    fake.get_secret = refuse
    m = NodeManager(DeskConfig(markets=["BTC"], node_feed="wss://relay.example/v1/feed"), key_getter=cfg_mod.node_key)
    assert m.status()["configured"]["key"] == "unreadable (UnscopedSecretError); connecting without it"


def test_no_key_no_socket_no_replay_public_mode_still_works():
    desk = make_desk(markets=("BTC",))
    snap = desk.build_snapshot(time.monotonic_ns(), int(time.time() * 1000))
    node = snap["markets"]["BTC"]["node"]
    assert node["live"]["available"] is False and "node_feed" in node["live"]["reason"] and node["replay"] is None
    assert snap["markets"]["BTC"]["deck"]["card"]["activity_buckets"]["new_long"]["value"] is None
    m = NodeManager(DeskConfig(markets=["BTC"]))
    assert m.live is None and m.player is None
    assert m.status()["configured"] == {"node_feed": None, "node_replay_file": None, "key": "not set"}


def test_parse_source_and_resume_handshake():
    assert parse_source("") is None and parse_source("relay.example") is None
    assert parse_source("/run/l4/feed.sock") == ("unix", "/run/l4/feed.sock")
    assert parse_source("unix:/run/l4/feed.sock") == ("unix", "/run/l4/feed.sock")
    assert parse_source("wss://relay.example/v1/feed") == ("ws", "wss://relay.example/v1/feed")
    h, msgs = read_recording(XYZ)
    c = FeedClient("s", "LIVE")
    c.on_line(hello_from(h), 0, 0)
    for rx, m in msgs[:50]:
        c.on_line(m, rx, rx * 10 ** 6)
    hs = handshake(c, [SP, XY])
    assert hs["v"] == 2 and hs["start"] == "snapshot" and hs["resume"]["epoch"] == c.epoch
    assert hs["resume"]["next"] == {mk: c.markets[mk].last_seq + 1 for mk in (SP, XY)}


# --- transports: a real local Unix socket and a real local WebSocket server ---------------------------

def _lines_for_server():
    lines = read_tap(RESTART)
    hellos = [i for i, o in enumerate(lines) if o.get("control") == "hello"]
    return lines[hellos[1]:]                       # one producer session: hello, snapshots, events, heartbeats


def test_unix_socket_transport_reads_the_real_stream(tmp_path, monkeypatch):
    lines = _lines_for_server()
    # AF_UNIX paths are limited to 107 bytes and a temp folder can be deeper than that: the socket is bound by a short
    # name in the test's folder, and reached by its full path when that fits, else by a short path to the same file.
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / "feed.sock")
    if len(path.encode()) > 100:
        path = "/proc/self/cwd/feed.sock"
    got = {}

    async def main():
        async def serve(reader, writer):
            got["handshake"] = json.loads(await reader.readline())
            for o in lines:
                writer.write((json.dumps(o) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()

        server = await asyncio.start_unix_server(serve, path="feed.sock")
        c = FeedClient("unix:" + path, "LIVE")
        stop = asyncio.Event()
        task = asyncio.create_task(run_feed(c, path, [XY], stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if c.markets.get(XY) and c.markets[XY].book is not None:
                break
        stop.set()
        task.cancel()                                # as the collector does on stop: a blocked read is cancelled
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        return c

    c = asyncio.run(main())
    assert got["handshake"] == {"v": 2, "markets": [XY], "start": "snapshot"}
    assert c.markets[XY].book is not None and c.epoch == lines[0]["epoch"]


def test_websocket_transport_sends_the_key_only_as_a_header_and_shows_a_refusal():
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    lines = _lines_for_server()
    seen = []

    async def main(refuse: bool, key):
        async def handler(conn):
            seen.append({"path": conn.request.path, "auth": conn.request.headers.get("Authorization"),
                         "handshake": json.loads(await conn.recv())})
            for o in lines:
                await conn.send(json.dumps(o))
            await asyncio.sleep(0.3)

        def process_request(conn, request):
            if refuse:
                return Response(503, "Service Unavailable", Headers({"Retry-After": "60"}),
                                b"The node-data relay is full right now. Public data keeps working; try again later.")
            return None

        async with serve(handler, "127.0.0.1", 0, process_request=process_request, max_size=2 ** 26) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/v1/feed"
            c = FeedClient(url, "LIVE")
            stop = asyncio.Event()
            task = asyncio.create_task(run_feed(c, url, [XY], stop, key=key))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if refuse and c.connection.get("state") == "refused":
                    break
                if not refuse and c.markets.get(XY) and c.markets[XY].book is not None:
                    break
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return c

    c = asyncio.run(main(False, "k3y"))
    assert seen[-1]["auth"] == "Bearer k3y" and seen[-1]["path"] == "/v1/feed" and "k3y" not in json.dumps(seen[-1]["handshake"])
    assert c.markets[XY].book is not None
    c = asyncio.run(main(False, None))
    assert seen[-1]["auth"] is None
    c = asyncio.run(main(True, None))
    assert c.connection["state"] == "refused" and "full right now" in c.connection["reason"]


# --- the node_* tools and the tab, at every intermediate snapshot --------------------------------

@pytest.fixture()
def harness(surfaces):
    plugin = surfaces([SP, XY])
    c = FeedClient("unix:/nonexistent/feed.sock", "LIVE", {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10 ** 15})
    c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
    plugin.attach(c)
    return plugin


def test_every_intermediate_tool_and_tab_result(harness):
    """Two markets interleaved, line by line: after every line the snapshot, the tab's routes and the node tools
    (chat and tab) agree, and a periodic snapshot swaps atomically: while its parts arrive node_book shows the
    whole old book, and after the last part the snapshot's, whole."""
    plugin = harness
    c = plugin.mgr.live
    h, msgs = read_recording(XYZ)
    c.on_line(hello_from(h), h["producer"]["started_ms"], 0)
    pending = {}
    stops_compared = swaps_checked = 0
    for rx, m in msgs:
        mk = m["market"]
        if m["kind"] == "book_snapshot" and m["body"]["part"] == 1 and c.markets.get(mk) and c.markets[mk].book:
            plugin.read(mk, rx)
            pending[mk] = {"ladder": plugin.last["node_book"]["ladder"], "rows": []}
        c.on_line(m, rx, rx * 10 ** 6)
        view = plugin.read(mk, rx)
        lad = plugin.last["node_book"]["ladder"]
        ps = pending.get(mk)
        if ps is not None and m["kind"] == "book_snapshot":
            ps["rows"].extend(m["body"]["orders"])
            if m["body"]["part"] < m["body"]["parts"]:
                for key in ("bids", "asks"):
                    assert lad[key] == ps["ladder"][key], "a read during the snapshot's parts sees the whole old book"
            else:
                lv = {"B": {}, "A": {}}
                for oid, side, px, sz, user, t in ps["rows"]:
                    e = lv[side].setdefault(Decimal(px), [Decimal(0), 0])
                    e[0] += Decimal(sz)
                    e[1] += 1
                for side, key in (("B", "bids"), ("A", "asks")):
                    want = [(px, lv[side][px][0], lv[side][px][1]) for px in sorted(lv[side], reverse=(side == "B"))[:20]]
                    got = [(Decimal(r["px"]), Decimal(str(r["total_sz"])), r["orders"]) for r in lad[key]]
                    assert got == want, "after the last part node_book shows the snapshot's book, whole"
                swaps_checked += 1
                del pending[mk]
        if view.get("families", {}).get("triggers", {}).get("state") == "LIVE":
            assert plugin.last["node_stops"]["stops"]["rows"] is not None
            stops_compared += 1
    assert swaps_checked == 2 and stops_compared > 300


def _tab_validity(payload: dict, elapsed_ms: float) -> dict:
    """The tab's own snapshotValidity (extracted from app.js), on a payload asked for elapsed_ms ago."""
    import re
    import subprocess
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:snapshotValidity>.*?\n(.*?)  // </pure:snapshotValidity>", src, re.S).group(1)
    script = fn + "\nprocess.stdout.write(JSON.stringify(snapshotValidity(" + json.dumps(payload) + \
        ", 1000, " + str(1000 + elapsed_ms) + ", 5)));\n"
    return json.loads(subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True).stdout)


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node to run the tab's pure function")
def test_the_tab_withholds_node_values_once_their_freshness_lapses(harness):
    """Review 5, B2, with Kevin's Nov 8 delay rule. A real tab payload with the node book LIVE (lines received 400 ms
    after their blocks) is current when it arrives; 2.1 s after the tab asked for it, its delay bound (the 5 s
    average when built plus the time since) passes the 2 s delayed limit, so the tab's own gate says it isn't current
    (the tab keeps it on screen and says why: it never blanks for a delay). The bound is never below what the client
    itself then reads. Statuses that aren't LIVE (REPLAY, BEHIND) aren't aged."""
    plugin = harness
    c = plugin.mgr.live
    c.cfg.update({"delayed_after_ms": 2_000, "stale_after_ms": 4_000})
    h, msgs = read_recording(XYZ)
    ms = by_market(msgs, XY)[:300]
    start = ms[0][1]["block_time_ms"]
    c.on_line(dict(hello_from(h), pub_ms=start + 300), start + 400, (start + 400) * 10 ** 6)
    for _, m in ms:
        rx = m["block_time_ms"] + 400
        c.on_line(dict(m, pub_ms=m["block_time_ms"] + 300), rx, rx * 10 ** 6)
    plugin.read(XY, rx)
    sid = plugin.last["snapshot"]["snapshot_id"]
    payload = plugin.http.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": XY, "snapshot_id": sid}).json()
    fam = payload["market"]["node"]["live"]["families"]
    assert fam["book"]["state"] == "LIVE" and fam["book"]["delay_ms"] < 1_000 and fam["book"]["delayed_after_ms"] == 2_000
    payload["served"] = dict(payload["served"], latest=True, age_s=0.0)   # as served the moment it was built
    assert _tab_validity(payload, 100)["ok"] is True
    late = _tab_validity(payload, 2_100)
    assert late["ok"] is False and "node book was LIVE when the snapshot was built" in late["reason"]
    assert "delayed above 2 s" in late["reason"]
    now = c.family_status(XY, "book", rx + 2_100, (rx + 2_100) * 10 ** 6)
    assert now["delay_ms"] <= fam["book"]["delay_ms"] + 2_100                # the tab's bound covers the client
    no_delay = json.loads(json.dumps(payload))                            # review 6, B1: LIVE without its delay
    no_delay["market"]["node"]["live"]["families"]["book"].pop("delay_ms")
    gone = _tab_validity(no_delay, 100)
    assert gone["ok"] is False and "delay or limit isn't in the snapshot" in gone["reason"]
    for state in ("REPLAY", "BEHIND"):
        other = json.loads(json.dumps(payload))
        for f in other["market"]["node"]["live"]["families"].values():
            f["state"] = state
        assert _tab_validity(other, 2_100)["ok"] is True


def _latest(plugin, monkeypatch, tool, market, read_at_ms, mono_at_ms=None):
    """A tool read of the LATEST snapshot (no snapshot_id) at read_at_ms on the wall clock and mono_at_ms on the
    monotonic clock (the same as the wall clock unless given; Surfaces stamps snapshots that way), through chat and
    the tab's tool route."""
    from hl_desk import runtime, tools
    mono = read_at_ms if mono_at_ms is None else mono_at_ms
    monkeypatch.setattr(runtime.time, "time", lambda: read_at_ms / 1000)
    monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: int(mono * 10 ** 6))
    via_chat = json.loads(tools.run_tool(tool, {"market": market}, plugin.rt))
    via_tab = plugin.http.get(f"/api/plugins/hyperliquid-desk/tool/{tool}", params={"market": market}).json()
    strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
    assert strip(via_chat) == strip(via_tab)
    return via_chat


@pytest.mark.parametrize("publish_after,read_after", [(1_900, 1_500), (0, 3_000), (0, 4_500)])
def test_latest_node_tool_reads_are_judged_at_serving_time(surfaces, monkeypatch, publish_after, read_after):
    """Review 7, B1, with Kevin's Nov 8 delay rule (delayed above 2 s, stale above 4 s here). A snapshot whose node
    book is LIVE when built (1.9 s after the last line, or at once), read as the latest snapshot 1.5, 3 or 4.5 s
    later: its delay bound (the 5 s average when built plus the time since) passes a limit, so node_book,
    node_status and node_stops don't call it LIVE and withhold every number built on it. The bound is never below
    what the client itself reads then. Within the limit it stays LIVE with its delay and ages advanced; a
    historical read (by snapshot_id) stays as of its build time."""
    from hl_desk.node.client import delay_state
    plugin = surfaces([XY])
    plugin.desk.collector = {"state": "connected", "note": "offline recorded feed"}
    c = plugin.attach(FeedClient("unix:/run/feed.sock", "LIVE", {"delayed_after_ms": 2_000, "stale_after_ms": 4_000}))
    c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
    h, msgs = read_recording(XYZ)
    ms = by_market(msgs, XY)[:300]
    start = ms[0][1]["block_time_ms"]
    c.on_line(dict(hello_from(h), pub_ms=start + 300), start + 400, (start + 400) * 10 ** 6)
    for _, m in ms:
        now = m["block_time_ms"] + 400                          # received 400 ms after the block, published at 300
        c.on_line(dict(m, pub_ms=m["block_time_ms"] + 300), now, now * 10 ** 6)
    built = now + publish_after
    view = plugin.read(XY, built)
    assert view["families"]["book"]["state"] == "LIVE" and view["families"]["triggers"]["state"] == "LIVE"
    sid = plugin.last["snapshot"]["snapshot_id"]
    read_at = built + read_after
    bound = view["families"]["book"]["delay_ms"] + read_after
    want, _ = delay_state(bound, view["families"]["book"])
    assert want in ("BEHIND", "STALE")
    assert c.family_status(XY, "book", read_at, read_at * 10 ** 6)["delay_ms"] <= bound   # what the client says now
    book = _latest(plugin, monkeypatch, "node_book", XY, read_at)
    assert book["ok"] and book["served"]["latest"] is True
    fb = book["families"]["book"]
    assert fb["state"] == want and fb["delay_ms"] == bound and "by the time this was served" in fb["reason"]
    for k in ("ladder", "walls", "thinning"):
        assert book[k]["available"] is False and f"book {want}" in book[k]["reason"], k
    assert book["tape"]["available"] is False
    status = _latest(plugin, monkeypatch, "node_status", XY, read_at)
    assert all(f["state"] != "LIVE" for f in status["markets"][XY]["families"].values())
    assert status["markets"][XY]["delay"]["delay_ms"] == bound and status["markets"][XY]["delay"]["state"] == want
    stops = _latest(plugin, monkeypatch, "node_stops", XY, read_at)
    assert stops["stops"]["available"] is False and f"triggers {want}" in stops["stops"]["reason"]
    # within the limit: still LIVE, its delay and ages advanced by the time since it was built
    if publish_after == 0:
        ok = _latest(plugin, monkeypatch, "node_book", XY, built + 500)
        fb = ok["families"]["book"]
        assert fb["state"] == "LIVE" and fb["aged_by_ms"] == 500 and ok["ladder"]["bids"]
        assert fb["watermark_age_ms"] == view["families"]["book"]["watermark_age_ms"] + 500
        assert fb["delay_ms"] == view["families"]["book"]["delay_ms"] + 500
    # a historical read is as of its build time, and says so
    hist = json.loads(__import__("hl_desk.tools", fromlist=["run_tool"]).run_tool(
        "node_book", {"market": XY, "snapshot_id": sid}, plugin.rt))
    assert hist["families"]["book"]["state"] == "LIVE" and hist["served"]["latest"] is False and "historical" in hist


def test_latest_reads_age_on_the_monotonic_clock_not_the_wall_clock(surfaces, monkeypatch):
    """Review 8, finding 1. A snapshot LIVE when built is read 2.5 s later on the monotonic clock, while the wall
    clock was corrected back by 2.4 s (wall time says 0.1 s). The served age is the monotonic 2.5 s: node_book's
    delay bound passes the 2 s delayed limit, so it reads BEHIND (delayed) with its numbers withheld, and the
    tab's own gate says the latest snapshot isn't current; the bound is never below what the client reads. A
    snapshot whose age can't be established on the monotonic clock isn't served as latest."""
    plugin = surfaces([XY])
    plugin.desk.collector = {"state": "connected", "note": "offline recorded feed"}
    c = plugin.attach(FeedClient("unix:/run/feed.sock", "LIVE", {"delayed_after_ms": 2_000, "stale_after_ms": 4_000}))
    c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
    h, msgs = read_recording(XYZ)
    ms = by_market(msgs, XY)[:300]
    start = ms[0][1]["block_time_ms"]
    c.on_line(dict(hello_from(h), pub_ms=start + 300), start + 400, (start + 400) * 10 ** 6)
    for _, m in ms:
        now = m["block_time_ms"] + 400
        c.on_line(dict(m, pub_ms=m["block_time_ms"] + 300), now, now * 10 ** 6)
    view = plugin.read(XY, now)
    d0 = view["families"]["book"]["delay_ms"]
    assert view["families"]["book"]["state"] == "LIVE" and d0 < 1_000
    mono_at, wall_at = now + 2_500, now + 100                    # 2.5 s elapsed; the wall clock stepped back 2.4 s
    assert c.family_status(XY, "book", wall_at, mono_at * 10 ** 6)["delay_ms"] <= d0 + 2_500
    book = _latest(plugin, monkeypatch, "node_book", XY, wall_at, mono_at)
    assert book["served"]["latest"] is True and abs(book["served"]["age_s"] - 2.5) < 1e-6
    fb = book["families"]["book"]
    assert fb["state"] == "BEHIND" and fb["aged_by_ms"] == 2_500 and fb["delay_ms"] == d0 + 2_500
    assert book["ladder"]["available"] is False
    tab = plugin.http.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": XY}).json()
    assert tab["ok"] and abs(tab["served"]["age_s"] - 2.5) < 1e-6
    gate = _tab_validity(tab, 0)
    assert gate["ok"] is False and "node book was LIVE" in gate["reason"]
    # a monotonic stamp from another clock (ahead of now): the age can't be established, so no latest answer
    refused = _latest(plugin, monkeypatch, "node_book", XY, wall_at, now - 1)
    assert refused["ok"] is False and "can't be established" in refused["error"]


def test_deck_card_buckets_count_node_fills_only(harness):
    """Step 13: the card's numbers count public trades; its activity buckets count node fills over the same window,
    each percentage over the node fills' own aggressive notional. Neither stream is added to the other."""
    from helpers import NS, open_segment, trade
    desk, mgr = harness.desk, harness.mgr
    h, msgs = read_recording(EXECS)
    hype = [(rx, m) for rx, m in msgs if m["market"] == "xyz:SP500"]
    p = h["producer"]
    mgr.live.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
                      "markets": {}}, p["started_ms"], 0)
    for rx, m in hype:
        mf = mgr.live._market(m["market"])
        mf.last_seq = m["seq"] - 1
        mgr.live.on_line(m, rx, rx * 10 ** 6)
    flow = mgr.live.markets[SP].flow
    start = flow.since_ms
    ms = desk.markets[SP]
    open_segment(ms, start)
    for k in range(1, 112):                               # public trades every second over the same span
        t = start + k * 1000
        ms.on_trades([trade(SP, t, 6800.0, 0.01, "B" if k % 3 else "A", "0x" + "1" * 40, "0x" + "2" * 40, k)],
                     k * NS, t)
    snap = desk.build_snapshot(111 * NS, start + 111_200)
    card = snap["markets"][SP]["deck"]["card"]
    assert card["complete"]
    ab = card["activity_buckets"]
    assert ab["stream"] == "node fills" and ab["available"]
    lo, hi = card["window_start_ms"], card["window_end_ms"]
    want = {"new_long": Decimal(0), "cover": Decimal(0), "new_short": Decimal(0), "exit": Decimal(0)}
    agg = {"B": Decimal(0), "A": Decimal(0)}
    for rx, m in hype:
        if m["kind"] != "execs" or m["block"] <= flow.since_block:
            continue
        for e in m["body"]["execs"]:
            if not lo <= e["time_ms"] <= hi:
                continue
            tk = [x for x in e["participants"] if x["role"] == "taker"][0]
            px = Decimal(e["px"])
            agg[tk["side"]] += px * Decimal(e["sz"])
            for k, v in tk["effect"].items():
                want[k] += Decimal(v) * px
    for k in want:
        assert Decimal(str(ab[k]["value"])) == want[k], k
    assert Decimal(str(ab["buying_aggressive_usd"]["value"])) == agg["B"]
    public_volume = Decimal(str(card["numbers"]["volume_usd"]["value"]))
    assert public_volume != agg["B"] + agg["A"]           # the two streams are never summed into one number


# --- step 12: replay paused, seeked, restarted and ended beside live data -----------------------

def test_replay_paused_seeked_restarted_and_ended_beside_live(surfaces):
    """A replay beside a live feed, both in the plugin, while the live feed keeps receiving lines: after every
    replay step, node_status, node_book and node_stops with source=replay (chat and tab) show REPLAY with the
    replay's own status, and the live results equal those of a reference client fed the same live lines with no
    replay at all."""
    h, msgs = read_recording(XYZ)
    plugin = surfaces([SP, XY])
    age = {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10 ** 15}
    live = plugin.attach(FeedClient("unix:/run/feed.sock", "LIVE", age))
    ref = FeedClient("unix:/run/feed.sock", "LIVE", age)                   # the same live lines, never a replay
    plugin.mgr.attach_bursts(ref)                                         # with the plugin's node-mode bursts
    for c in (live, ref):
        c.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
        c.on_line(hello_from(h), 0, 0)
        for rx, m in msgs[:600]:
            c.on_line(m, rx, rx * 10 ** 6)
    now = msgs[599][0]
    plugin.read(XY, now, slot="live")
    assert plugin.last["tab"]["node"]["live"]["families"]["book"]["state"] == "LIVE"
    live_lines = iter(msgs[600:])
    live_fed = 0

    def live_step(n=3):
        nonlocal now, live_fed
        for rx, m in itertools.islice(live_lines, n):
            for c in (live, ref):
                c.on_line(m, rx, rx * 10 ** 6)
            now = rx
            live_fed += 1

    rc = plugin.attach(FeedClient("recording:x", "REPLAY"))
    rc.listeners.append(lambda ev: panels.on_client_event(ev, OPTS))
    rec = Recording.load(str(XYZ))
    clock = [100.0]
    pl = ReplayPlayer(rec, rc, speed=2.0, clock=lambda: clock[0])

    def read_replay():
        live_step()
        v = plugin.read(XY, now, slot="replay")
        for name in ("node_status", "node_book", "node_stops"):
            r = plugin.last[name]
            assert r["source"] == "replay"
            if name != "node_status":
                assert r["mode"] == "REPLAY" and r["replay"] == jsonable(v.get("replay"))
        plugin.read(XY, now, slot="live")                                         # live: as if no replay ran
        assert plugin.last["tab"]["node"]["live"] == jsonable(panels.market_view(ref, XY, OPTS, now, now * 10 ** 6))
        assert plugin.last["node_book"]["mode"] == "LIVE"
        return v

    pl.start()
    s1 = pl.session
    statuses = []
    for _ in range(40):
        clock[0] += 0.25
        pl.tick()
        v = read_replay()
        statuses.append(v["replay"])
    assert rc.replay["state"] == "playing" and rc.replay["recording_start_ms"] == rec.start_ms
    pl.pause()
    pos = rc.replay["position_ms"]
    clock[0] += 30
    pl.tick()
    assert rc.replay["state"] == "paused" and rc.replay["position_ms"] == pos
    assert read_replay()["replay"]["state"] == "paused"
    target = rec.start_ms + 62_000
    pl.seek(target)
    s2 = pl.session
    assert s2 != s1 and s1 in rc.retired_replay
    first = rc.counters["retired_replay_ignored"]
    rc.on_line(dict(msgs[10][1], mode="REPLAY", replay_session=s1), 0, 0)      # a late line of the old session
    assert rc.counters["retired_replay_ignored"] == first + 1
    pl.tick()
    assert rc.markets[XY].book is not None and rc.markets[XY].book.through_seq >= 503   # restarted at the 60 s pair
    assert read_replay()["provenance"]["replay_session"] == s2
    pl.start()                                                                         # restart: a third session
    assert pl.session not in (s1, s2)
    pl.set_speed(1000.0)
    for _ in range(50):
        clock[0] += 1
        pl.tick()
        read_replay()
    assert pl.state == "ended" and rc.replay["state"] == "ended"
    v = read_replay()
    assert v["replay"]["state"] == "ended"
    assert all(f["state"] in ("REPLAY", "INVALID") for f in v["families"].values())     # the end stays REPLAY
    assert v["provenance"]["mode"] == "REPLAY"                 # node_book's mode REPLAY is checked in read_replay
    # The live feed kept moving beside the replay, and matched the reference at every read above.
    assert live_fed > 250 and live.markets[XY].last_seq == ref.markets[XY].last_seq > msgs[599][1]["seq"]


def test_replay_commands_through_the_manager():
    mgr = NodeManager(DeskConfig(markets=[SP, XY], node_replay_file=str(XYZ)))
    assert mgr.player is None                                  # never started automatically
    assert mgr.replay_command("pause")["ok"] is False
    r = mgr.replay_command("start")
    assert r["ok"] and r["replay"]["state"] == "playing" and r["replay"]["partial_history"] is True
    assert mgr.replay_command("pause")["replay"]["state"] == "paused"
    assert mgr.replay_command("speed", 4.0)["replay"]["speed"] == 4.0
    assert mgr.replay_command("seek", Recording.load(str(XYZ)).start_ms + 30_000)["ok"]
    assert mgr.replay_command("stop") == {"ok": True, "replay": None} and mgr.replay_client is None
    assert mgr.replay_command("start", "/nonexistent.jsonl")["ok"] is False


def test_a_configured_feed_that_serves_a_replay_is_never_live():
    """A replayer behind node_feed: its lines carry mode REPLAY. They're shown only in the replay area, labelled
    REPLAY, and never feed the Flow Deck or wallet metrics (step 12)."""
    h, msgs = read_recording(XYZ)
    mgr = NodeManager(DeskConfig(markets=[SP, XY], node_feed="unix:/run/replayer.sock"))
    rs = "f" * 32
    hello = dict(hello_from(h), mode="REPLAY", replay_session=rs)
    mgr.live.on_line(hello, 0, 0)
    for rx, m in msgs[:300]:
        mgr.live.on_line(dict(m, mode="REPLAY", replay_session=rs), rx, rx * 10 ** 6)
    assert mgr.live.mode == "REPLAY" and mgr.live_client() is None
    v = mgr.views(msgs[299][0], msgs[299][0] * 10 ** 6)[XY]
    assert v["live"]["available"] is False and "REPLAY" in v["live"]["reason"]
    assert v["replay"]["provenance"]["mode"] == "REPLAY"
    assert all(f["state"] in ("REPLAY", "INVALID") for f in v["replay"]["families"].values())
    desk = make_desk(markets=(SP, XY))
    desk.node = mgr
    snap = desk.build_snapshot(time.monotonic_ns(), int(time.time() * 1000))
    assert snap["markets"][XY]["deck"]["card"]["activity_buckets"]["new_long"]["value"] is None
    ab = snap["markets"][XY]["deck"]["card"]["activity_buckets"]
    assert ab.get("stream") != "node fills" and ab["new_long"]["reason"].startswith("needs node mode")
