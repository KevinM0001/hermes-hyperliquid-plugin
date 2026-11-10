"""Hosted relay mode (handoff 1d step 7): the relay's views, node data only while it is used, and the relay's
refusals shown in plain words.

The relay serves each subscriber full data for the market it has open plus the executions of all four markets, or
the executions only (profile hl-relay-2). Filtered-out lines arrive as `filtered` ranges, so a lost line is still a
jump. These tests feed the client the relay's executions view of a recorded executions stream (the fixture's real
lines, with the ranges the relay builds between them), and run the transport against a small relay stand-in on
loopback that speaks the same handshake, switch, view, refusal and close messages.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hl_desk.config import HOSTED_RELAY_URL, DeskConfig, from_settings
from hl_desk.node import transport
from hl_desk.node.demand import Demand
from hl_desk.node.manager import NodeManager
from hl_desk.node.transport import IDLE_REASON, check_relay_url, relay_handshake, run_relay
from node_helpers import EXECS, client, read_recording

MARKETS = ["BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"]


def recorded():
    header, msgs = read_recording(EXECS)
    return header, msgs


def execs_view(msgs):
    """The relay's executions view of a recorded executions stream: a `filtered` range for every run of numbers
    between two delivered lines of a market, as the relay builds it (same session, the newest producer pub_ms)."""
    last, out = {}, []
    for rx, m in msgs:
        mk, seq = m["market"], m["seq"]
        if mk in last and seq > last[mk] + 1:
            out.append((rx, {"v": 2, "control": "filtered", "epoch": m["epoch"], "session": m["session"],
                             "pub_ms": m["pub_ms"], "market": mk, "from": last[mk] + 1, "through": seq - 1}))
        out.append((rx, m))
        last[mk] = seq
    return out


def relay_hello(header, msgs, views: dict, snapshot="none"):
    p = header["producer"]
    first = {}
    for _, m in msgs:
        first.setdefault(m["market"], m["seq"])
    markets = {mk: {"start_seq": first[mk], "next_seq": first[mk], "oldest_seq": 1, "view": v,
                    "snapshot": snapshot if v == "execs" else "pending"} for mk, v in views.items()}
    return {"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "started_ms": p["started_ms"],
            "pub_ms": p["started_ms"], "mode": "LIVE", "limits": header["limits"], "markets": markets}


def feed(c, pairs):
    for rx, m in pairs:
        c.on_line(m, rx, rx * 1_000_000)


# --- the client: filtered versus lost -----------------------------------------------------------------------------
def test_filtered_ranges_keep_an_executions_view_contiguous_and_a_lost_line_is_a_gap():
    header, msgs = recorded()
    stream = execs_view(msgs)
    c = client("relay")
    feed(c, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + stream)
    assert c.views == {m: "execs" for m in MARKETS}
    assert c.counters["filtered"] > 100
    end = msgs[-1][0]
    for m in MARKETS:
        mf = c.markets[m]
        assert mf.counters["gaps"] == 0, m
        assert c.family_status(m, "flow", end, end * 1_000_000)["state"] != "INVALID", m
        assert c.family_status(m, "book", end, end * 1_000_000)["reason"].startswith("executions only"), m
        assert mf.flow.execs, m            # the executions themselves arrived

    # The same stream with one BTC execution lost upstream: the range after it starts beyond last + 1.
    lost = next(i for i, (_, m) in enumerate(stream) if m.get("market") == "BTC" and m.get("kind") == "execs")
    c2 = client("relay")
    feed(c2, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + stream[:lost] + stream[lost + 1:])
    assert c2.markets["BTC"].counters["gaps"] == 1
    assert all(c2.markets[m].counters["gaps"] == 0 for m in MARKETS if m != "BTC")


def test_a_switch_to_full_keeps_the_flow_window_only_when_continuous():
    header, msgs = recorded()
    stream = execs_view(msgs)
    half = stream[: len(stream) // 2]
    ep, ss = header["producer"]["epoch"], header["producer"]["session"]

    def view(c, nxt_delta):
        mf = c.markets["BTC"]
        return {"v": 2, "control": "view", "epoch": ep, "session": ss, "pub_ms": 1, "market": "BTC", "view": "full",
                "next_seq": mf.last_seq + 1 + nxt_delta, "oldest_seq": 1, "start_seq": 5, "snapshot": "in_stream"}

    ok = client("relay")
    feed(ok, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + half)
    flow_before = ok.markets["BTC"].fam["flow"].valid
    ok.on_line(view(ok, 0), half[-1][0], half[-1][0] * 1_000_000)
    mf = ok.markets["BTC"]
    assert ok.views["BTC"] == "full" and mf.counters["gaps"] == 0
    assert mf.fam["flow"].valid == flow_before          # carried across: the sequence reached next_seq - 1
    assert mf.last_seq == 4 and mf.expected_start == 5     # restarts at start_seq, position start_seq - 1
    assert "switched to full data" in mf.fam["book"].reason

    short = client("relay")
    feed(short, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + half)
    short.on_line(view(short, 2), half[-1][0], half[-1][0] * 1_000_000)   # two numbers never arrived
    mf = short.markets["BTC"]
    assert mf.counters["gaps"] == 1 and not mf.fam["flow"].valid


def test_every_hello_reconciles_the_views():
    header, msgs = recorded()
    stream = execs_view(msgs)[:400]
    c = client("relay")
    feed(c, [(msgs[0][0], relay_hello(header, msgs, {"BTC": "full", "HYPE": "execs", "xyz:SP500": "execs",
                                                      "xyz:XYZ100": "execs"}))] + stream)
    # A reconnect that no longer carries HYPE, and BTC in executions view only.
    again = relay_hello(header, msgs, {"BTC": "execs", "xyz:SP500": "execs", "xyz:XYZ100": "execs"})
    c.on_line(again, stream[-1][0], stream[-1][0] * 1_000_000)
    assert "HYPE" not in c.views and c.markets["HYPE"].last_seq is None
    assert not c.markets["HYPE"].fam["flow"].valid
    assert c.views["BTC"] == "execs" and not c.markets["BTC"].fam["book"].valid

    # A resumed reconnect that changes BTC to executions only: nothing lost, but the full-data families stop at once.
    r = client("relay")
    feed(r, [(msgs[0][0], relay_hello(header, msgs, {"BTC": "full", "HYPE": "execs", "xyz:SP500": "execs",
                                                      "xyz:XYZ100": "execs"}))] + stream)
    resumed = relay_hello(header, msgs, {m: "execs" for m in MARKETS})
    for m in MARKETS:
        resumed["markets"][m].update(snapshot="resumed", start_seq=r.markets[m].last_seq + 1)
    flow_before = r.markets["HYPE"].fam["flow"].valid
    r.on_line(resumed, stream[-1][0], stream[-1][0] * 1_000_000)
    assert r.views["BTC"] == "execs" and r.markets["BTC"].fam["book"].reason.startswith("executions only")
    assert r.markets["HYPE"].fam["flow"].valid == flow_before and r.markets["HYPE"].counters["gaps"] == 0


def test_the_handshake_names_the_view_and_resumes_only_what_can_continue():
    header, msgs = recorded()
    c = client("relay")
    assert relay_handshake(c, "BTC", MARKETS) == {"v": 2, "markets": ["BTC"], "execs": MARKETS, "start": "snapshot"}
    feed(c, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + execs_view(msgs)[:300])
    hs = relay_handshake(c, "BTC", MARKETS)
    # BTC is wanted in full but was received through filtered ranges only: never resumed (the relay starts it from
    # a snapshot); the executions markets resume from their next number.
    assert "BTC" not in hs["resume"]["next"]
    assert set(hs["resume"]["next"]) == {"HYPE", "xyz:SP500", "xyz:XYZ100"}
    assert "resume" not in relay_handshake(c, None, [])     # nothing wanted: nothing to resume


# --- the setting -----------------------------------------------------------------------------------------------------
def test_the_hosted_relay_is_the_default_and_can_be_turned_off():
    unset = from_settings(lambda k, d=None: None)
    assert unset.node_feed == HOSTED_RELAY_URL and unset.node_feed_is_hosted_default
    assert from_settings({"node_feed": "hosted"}.get).node_feed == HOSTED_RELAY_URL
    for off in ("off", "OFF", "none", ""):
        assert from_settings({"node_feed": off}.get).node_feed == ""
    own = from_settings({"node_feed": "wss://relay.example.org/v1/feed"}.get)
    assert own.node_feed == "wss://relay.example.org/v1/feed" and not own.node_feed_is_hosted_default
    assert from_settings({}.get).node_alerts is False
    assert from_settings({"node_alerts": True}.get).node_alerts is True
    assert DeskConfig().node_feed == ""          # outside Hermes (no settings): public mode only


def test_the_relay_url_is_bound_to_its_tls_origin():
    assert check_relay_url("wss://relay.example.org/v1/feed") is None
    assert check_relay_url("ws://127.0.0.1:9000/v1/feed") is None
    assert "wss://" in check_relay_url("ws://relay.example.org/v1/feed")
    assert "user name or password" in check_relay_url("wss://u:p@relay.example.org/v1/feed")
    assert "isn't set in this version" in check_relay_url(HOSTED_RELAY_URL)
    assert check_relay_url("https://relay.example.org/") is not None


def test_demand_timeline():
    d = Demand(MARKETS, linger_s=60, grace_s=5)
    assert d.want(0.0) is None
    d.panel("HYPE", now=100.0)
    assert d.want(101.0) == ("HYPE", MARKETS)
    assert d.status(103.0)["panel_on_screen"] and not d.status(106.0)["panel_on_screen"]
    assert d.want(159.5) == ("HYPE", MARKETS)          # within 60 s of the last poll from a visible tab
    assert d.status(159.5)["unsubscribes_in_s"] == 0.5
    assert d.want(160.5) is None                       # then nothing: the connection closes
    alerts = Demand(MARKETS, alerts_in_background=True, linger_s=60, grace_s=5)
    assert alerts.want(0.0) == (None, MARKETS)         # background alerts: the four markets' executions only
    alerts.panel("BTC", now=10.0)
    assert alerts.want(11.0) == ("BTC", MARKETS) and alerts.want(71.0) == (None, MARKETS)


# --- the transport against a relay stand-in ---------------------------------------------------------------------------
class RelayStandIn:
    """Speaks the relay's subscriber side on loopback: handshake, hello with views, a switch answered with view lines,
    refusals at the upgrade, a redirect, and the stop switch's relay line and close 4001."""

    def __init__(self, refuse=None, redirect=False, stop_after_hello=False):
        self.refuse, self.redirect, self.stop_after_hello = refuse, redirect, stop_after_hello
        self.requests, self.handshakes, self.switches, self.closed = 0, [], [], 0
        self.epoch, self.session = 1794106250035, "56cbe07228c59971c078c1ae284f078f"

    async def __aenter__(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.handler, "127.0.0.1", 0, process_request=self.process_request)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1/feed"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def process_request(self, connection, request):
        self.requests += 1
        if self.refuse:
            code, status = self.refuse
            r = connection.respond(status, json.dumps({"relay": "hl-relay-2", "refused": code,
                                                       "message": transport.REFUSALS[code]}))
            r.headers["X-Relay-Refusal"] = code
            return r
        if self.redirect:
            r = connection.respond(302, "")
            r.headers["Location"] = self.url.replace("/v1/feed", "/elsewhere")
            return r
        return None

    def _ctl(self, **kw):
        return json.dumps({"v": 2, "epoch": self.epoch, "session": self.session, "pub_ms": 1} | kw)

    async def handler(self, ws):
        try:
            hs = json.loads(await ws.recv())
            self.handshakes.append(hs)
            full = set(hs.get("markets") or [])
            views = {m: "full" for m in full} | {m: "execs" for m in hs.get("execs") or [] if m not in full}
            await ws.send(self._ctl(control="hello", mode="LIVE", started_ms=1, limits={}, markets={
                m: {"view": v, "start_seq": 10, "next_seq": 10, "oldest_seq": 1,
                    "snapshot": "pending" if v == "full" else "none"} for m, v in views.items()}))
            if self.stop_after_hello:
                await ws.send(json.dumps({"v": 2, "control": "relay", "relay": "hl-relay-2", "relay_ms": 1,
                                          "publisher": "connected", "state": "stopped",
                                          "message": transport.REFUSALS["stopped"]}))
                await ws.close(4001, "stopped")
                return
            async for raw in ws:
                sw = json.loads(raw)["switch"]
                self.switches.append(sw)
                nfull = set(sw.get("markets") or [])
                new = {m: "full" for m in nfull} | {m: "execs" for m in sw.get("execs") or [] if m not in nfull}
                for m in sorted(set(views) | set(new)):
                    if views.get(m) != new.get(m):
                        await ws.send(self._ctl(control="view", market=m, view=new.get(m, "none"), next_seq=10,
                                                oldest_seq=1, start_seq=10, snapshot="pending"))
                views = new
        finally:
            self.closed += 1


async def _until(cond, timeout=8.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def test_node_data_flows_only_while_a_panel_is_on_screen_and_switches_with_the_market():
    async def run():
        async with RelayStandIn() as relay:
            d = Demand(MARKETS, linger_s=1.0, grace_s=0.3)
            c = client(relay.url)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(c, relay.url, d, stop))
            await asyncio.sleep(0.6)
            assert relay.requests == 0 and c.connection["state"] == "idle"      # nothing on screen: no connection

            d.panel("BTC")
            assert await _until(lambda: relay.handshakes)
            assert relay.handshakes[0] == {"v": 2, "markets": ["BTC"], "execs": MARKETS, "start": "snapshot"}
            assert await _until(lambda: c.views.get("BTC") == "full")

            # The user opens HYPE: one switch, then HYPE full and BTC executions only.
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.5:
                d.panel("HYPE")
                await asyncio.sleep(0.1)
            assert relay.switches == [{"markets": ["HYPE"], "execs": MARKETS}]
            assert c.views["HYPE"] == "full" and c.views["BTC"] == "execs"

            # The tab is hidden: 1 s after its last poll the subscription ends and the client forgets the old state.
            assert await _until(lambda: relay.closed == 1, timeout=4)
            assert c.connection["state"] == "idle" and c.connection["reason"] == IDLE_REASON
            assert c.epoch is None and c.views == {}

            # Shown again: a new subscription from a fresh snapshot (nothing resumed).
            d.panel("HYPE")
            assert await _until(lambda: len(relay.handshakes) == 2)
            assert relay.handshakes[1] == {"v": 2, "markets": ["HYPE"], "execs": MARKETS, "start": "snapshot"}
            stop.set()
            await task
    asyncio.run(run())


def test_background_alerts_keep_the_four_markets_executions_only():
    async def run():
        async with RelayStandIn() as relay:
            d = Demand(MARKETS, alerts_in_background=True)
            c = client(relay.url)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(c, relay.url, d, stop))
            assert await _until(lambda: relay.handshakes)
            assert relay.handshakes[0] == {"v": 2, "markets": [], "execs": MARKETS, "start": "snapshot"}
            assert await _until(lambda: c.views == {m: "execs" for m in MARKETS})
            stop.set()
            await task
    asyncio.run(run())


@pytest.mark.parametrize("code,status,state", [("at_capacity", 503, "full"), ("stopped", 503, "stopped"),
                                               ("per_ip_limit", 429, "refused"), ("rate_limited", 429, "refused")])
def test_a_refusal_is_shown_with_the_relays_reason(code, status, state):
    async def run():
        async with RelayStandIn(refuse=(code, status)) as relay:
            d = Demand(MARKETS)
            d.panel("BTC")
            c = client(relay.url)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(c, relay.url, d, stop))
            assert await _until(lambda: c.connection.get("refused") == code)
            assert c.connection["state"] == state and c.connection["reason"] == transport.REFUSALS[code]
            await asyncio.sleep(1.0)
            assert relay.requests == 1          # a refusal waits at least 15 s before trying again
            stop.set()
            await task
    asyncio.run(run())


def test_a_redirect_is_never_followed():
    async def run():
        async with RelayStandIn(redirect=True) as relay:
            d = Demand(MARKETS)
            d.panel("BTC")
            c = client(relay.url)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(c, relay.url, d, stop, key="a-key"))
            assert await _until(lambda: c.connection["state"] == "refused")
            assert "redirect" in c.connection["reason"] and relay.requests == 1
            stop.set()
            await task
    asyncio.run(run())


def test_the_stop_switch_is_shown_as_stopped():
    async def run():
        async with RelayStandIn(stop_after_hello=True) as relay:
            d = Demand(MARKETS)
            d.panel("BTC")
            c = client(relay.url)
            stop = asyncio.Event()
            task = asyncio.create_task(run_relay(c, relay.url, d, stop))
            assert await _until(lambda: c.connection["state"] == "stopped")
            assert c.connection["reason"] == transport.REFUSALS["stopped"]
            stop.set()
            await task
    asyncio.run(run())


def test_a_tool_call_subscribes_on_demand_and_releases_after_answering():
    async def run():
        async with RelayStandIn() as relay:
            m = NodeManager(DeskConfig(markets=MARKETS, node_feed=relay.url))
            assert m.relay_mode
            stop = asyncio.Event()
            tasks = m.tasks(stop)
            await asyncio.sleep(0.5)
            assert relay.requests == 0                       # no panel, no alerts: nothing subscribed
            view = m.views(int(time.time() * 1000), time.monotonic_ns())["BTC"]["live"]
            assert view["available"] is False and view["reason"] == IDLE_REASON
            lease = m.lease("HYPE")
            assert lease["via"] == "a subscription of its own"
            assert await _until(lambda: relay.handshakes)
            assert relay.handshakes[0] == {"v": 2, "markets": ["HYPE"], "execs": [], "start": "snapshot"}
            assert m.client_for("HYPE") is m.tool_clients["HYPE"]["client"]
            m.release(lease["id"])
            assert m.tool_clients == {}
            assert await _until(lambda: relay.closed == 1, timeout=4)
            st = m.status()["live"]["relay"]
            assert st["demand"]["subscription"] is None and st["tool_subscriptions"] == {}
            stop.set()
            await asyncio.gather(*tasks)
    asyncio.run(run())


# --- a node tool's call subscribes on demand ---------------------------------------------------------------------------
class _RtStandIn:
    """The runtime surface run_tool uses, with a relay subscription that becomes valid after two snapshots."""

    def __init__(self, market):
        self.market, self.calls, self.polls = market, [], 0

    def node_lease(self, market):
        self.calls.append(("lease", market))
        return {"id": 7, "via": "a subscription of its own"}

    def node_release(self, lease):
        self.calls.append(("release", lease["id"]))

    def snapshot(self, snapshot_id=None):
        self.polls += 1
        state = "LIVE" if self.polls >= 3 else "INVALID"
        fams = {f: {"state": state, "reason": None if state == "LIVE" else "switched to full data"}
                for f in ("book", "triggers", "depth", "bands", "flow")}
        live = {"available": True, "families": fams, "provenance": {"mode": "LIVE"}, "book": {"valid": state == "LIVE"},
                "ladder": None, "walls": None, "thinning": None, "tape": None}
        snap = {"snapshot_id": f"hd-t-{self.polls}", "built_at": "2026-11-10T00:00:00.000Z", "built_at_ms": 0,
                "served": {"latest": True}, "schema": {}, "mode": "LIVE",
                "markets": {self.market: {"market": self.market, "node": {"live": live, "replay": None}}}}
        return snap, None


def test_a_node_tool_subscribes_for_its_call_and_releases_after_answering(monkeypatch):
    from hl_desk import tools
    monkeypatch.setattr(tools, "NODE_WAIT_POLL_S", 0.01)
    monkeypatch.setattr("hl_desk.node.serving.age_snapshot_for_serving", lambda s: s)
    monkeypatch.setattr("hl_desk.serving_public.age_snapshot", lambda s: s)
    rt = _RtStandIn("HYPE")
    out = json.loads(tools.run_tool("node_book", {"market": "HYPE"}, rt))
    assert rt.calls == [("lease", "HYPE"), ("release", 7)]          # released once it has answered
    assert out["families"]["book"]["state"] == "LIVE"                 # answered from the snapshot once valid
    assert rt.polls >= 3
    # A historical read (by snapshot ID) or a replay never subscribes.
    rt2 = _RtStandIn("HYPE")
    tools.run_tool("node_book", {"market": "HYPE", "source": "replay"}, rt2)
    tools.run_tool("node_book", {"market": "HYPE", "snapshot_id": "hd-x-1"}, rt2)
    assert rt2.calls == []


# --- the tab's visible poll is the signal ---------------------------------------------------------------------------
def test_the_tabs_visible_poll_is_what_keeps_relay_data_flowing(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from node_helpers import ROOT, load_plugin_api

    api = load_plugin_api()
    calls = []

    class Rt:
        def node_panel(self, market):
            calls.append(market)

        def status(self):
            return {}

    class RtModule:
        @staticmethod
        def get_runtime():
            return Rt()

    ms = {"provenance": {}, "freshness": {"trades": {}}, "deck": {"card": None, "events": []}}
    snap = {"snapshot_id": "hd-t-1", "built_at": "2026-11-10T00:00:00.000Z", "built_at_ms": 0, "served": {},
            "mode": "PUBLIC", "plugin_version": "1.0.0", "schema": {}, "collector": {"state": "connected"},
            "markets": {"BTC": dict(ms), "HYPE": dict(ms)}}

    async def fake_snapshot(snapshot_id):
        return snap, None

    async def fake_engine(name):
        return RtModule if name == "runtime" else None

    monkeypatch.setattr(api, "_snapshot", fake_snapshot)
    monkeypatch.setattr(api, "_engine_async", fake_engine)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hyperliquid-desk")
    http = TestClient(app)
    url = "/api/plugins/hyperliquid-desk/snapshot"
    assert http.get(url, params={"node": "visible", "market": "HYPE"}).json()["ok"] is True
    assert calls == ["HYPE"]
    http.get(url, params={"market": "HYPE"})                     # the chat strip's read: not a panel on screen
    assert calls == ["HYPE"]
    http.get(url, params={"node": "visible"})                    # the tab's first poll, before it chose a market
    assert calls == ["HYPE", "BTC"]
    # The tab is the only reader that sends it, and it polls only while visible (it returns early when hidden).
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    assert src.count('"/snapshot?node=visible"') == 1
    poll = src[src.index("const poll = useCallback(function (force) {"):src.index('"/snapshot?node=visible"')]
    assert "document.hidden) return;" in poll


def test_the_copy_after_a_switch_to_full_never_replays_blocks_into_the_flow():
    """A switch to full that keeps the flow window restarts the market from the relay's snapshot: the copy repeats
    executions and watermarks the flow already has. They advance the sequence but never reach the flow, its bursts or
    its panels again (a block time running backwards would reject the burst windows)."""
    header, msgs = recorded()
    btc = [(rx, m) for rx, m in msgs if m["market"] == "BTC"]
    half = len(btc) // 2
    ep, ss = header["producer"]["epoch"], header["producer"]["session"]

    def run(switch: bool):
        c = NodeManager(DeskConfig(markets=["BTC"])).attach_bursts(client("relay"))     # the plugin's burst rule
        c.on_line(relay_hello(header, msgs, {"BTC": "execs"}), btc[0][0], btc[0][0] * 1_000_000)
        feed(c, execs_view(btc[:half]))
        if not switch:
            feed(c, execs_view(btc)[len(execs_view(btc[:half])):])
            return c
        mf = c.markets["BTC"]
        start = 5
        c.on_line({"v": 2, "control": "view", "epoch": ep, "session": ss, "pub_ms": 1, "market": "BTC", "view": "full",
                   "next_seq": mf.last_seq + 1, "oldest_seq": 1, "start_seq": start, "snapshot": "pending"},
                  btc[half][0], btc[half][0] * 1_000_000)
        # The copy: the market's lines from an earlier point (here the last third already seen), numbered on from
        # start_seq as one contiguous full stream, then the rest live.
        copy = btc[half // 3:]
        for i, (rx, m) in enumerate(copy):
            c.on_line(dict(m, seq=start + i), rx, rx * 1_000_000)
        return c

    ref, sw = run(False), run(True)
    rule = sw.markets["BTC"].flow.bursts
    assert sw.counters["copy_repeats"] > 0
    assert "a block time ran backwards" not in rule.counters["rejected"]
    assert sw.markets["BTC"].counters["gaps"] == 0 and sw.markets["BTC"].fam["flow"].valid
    assert set(sw.markets["BTC"].flow.execs) == set(ref.markets["BTC"].flow.execs)


def test_public_bursts_keep_running_until_relay_data_flows():
    """With a relay, node-mode bursts replace public-trades bursts for a market only while its node fills flow: a
    paused, unreachable, full or stopped relay leaves the public bursts running. A local socket keeps Part B's rule."""
    header, msgs = recorded()
    relay = NodeManager(DeskConfig(markets=MARKETS, node_feed="wss://relay.example.org/v1/feed"))
    assert relay.relay_mode and not relay.node_bursts_for("BTC")          # idle: public bursts
    feed(relay.live, [(msgs[0][0], relay_hello(header, msgs, {m: "execs" for m in MARKETS}))] + execs_view(msgs))
    relay.live.connection = {"state": "connected", "since_wall_ms": 0, "reason": None}
    assert relay.node_bursts_for("BTC")                                   # node fills flowing: node bursts
    relay.live.connection = {"state": "stopped", "since_wall_ms": 0, "reason": transport.REFUSALS["stopped"]}
    assert not relay.node_bursts_for("BTC")                               # stopped: public bursts again
    assert NodeManager(DeskConfig(markets=MARKETS, node_feed="/run/feed.sock")).node_bursts_for("BTC")
    assert not NodeManager(DeskConfig(markets=MARKETS)).node_bursts_for("BTC")


def test_a_lease_that_is_never_released_ends_on_its_own(monkeypatch):
    from hl_desk.node import manager as mgr_mod
    monkeypatch.setattr(mgr_mod, "LEASE_MAX_S", 0.5)

    async def run():
        async with RelayStandIn() as relay:
            m = NodeManager(DeskConfig(markets=MARKETS, node_feed=relay.url))
            stop = asyncio.Event()
            tasks = m.tasks(stop)
            m.lease("HYPE")                                  # never released (a caller that errored or timed out)
            assert await _until(lambda: relay.handshakes)
            assert await _until(lambda: m.tool_clients == {} and relay.closed == 1, timeout=4)
            stop.set()
            await asyncio.gather(*tasks)
    asyncio.run(run())


def test_a_relay_without_its_publisher_is_said_beside_the_connection():
    from hl_desk.node.client import WAITING_REASON
    c = client("relay")
    c.on_connect(1)
    c.on_line({"v": 2, "control": "relay", "relay": "hl-relay-2", "relay_ms": 1, "publisher": "absent",
               "state": "waiting_for_publisher"}, 1, 1)
    assert c.connection == {"state": "connected", "since_wall_ms": 1, "reason": WAITING_REASON}
    c.on_line({"v": 2, "control": "relay", "relay": "hl-relay-2", "relay_ms": 2, "publisher": "connected",
               "state": "serving"}, 2, 2)
    assert c.connection["reason"] is None
    assert c.relay["state"] == "serving" and not c.markets      # a heartbeat is never a market's freshness
