"""Candle history per interval (hl_desk/candles.py): the store, the websocket channel, the route and the snapshot
additions, with canned venue answers in the documented candleSnapshot shape (prices and volume as strings, as the
venue sends them). Nothing reaches the network: the store's fetch and executor are stand-ins, so the loading, loaded
and failed states are reached deliberately and every age is read off a fixed clock.
"""

from __future__ import annotations

import json

import pytest

from helpers import A, B, T0, build, make_desk, open_segment, trade, use_standalone_storage

from hl_desk import candles as cd
from hl_desk.candles import INTERVAL_MS, CandleStore

H = INTERVAL_MS["1h"]
OPEN = 1_794_254_400_000          # 2026-11-09T14:00Z, the 1h candle in progress at NOW (as the venue answered that day)
NOW = 1_794_255_205_717           # 2026-11-09T14:13:25.717Z


def venue(t, o, h, l, c, v, n, coin="BTC", interval="1h"):
    """A candle as the venue sends it; candleSnapshot and the websocket candle channel share the shape."""
    return {"t": t, "T": t + INTERVAL_MS[interval] - 1, "s": coin, "i": interval, "o": str(o), "h": str(h), "l": str(l),
            "c": str(c), "v": str(v), "n": n}


# The venue's answer at NOW: three closed hours and the hour in progress.
ROWS = [venue(OPEN - 3 * H, 74000, 74100, 73900, 74050, "100.5", 10),
        venue(OPEN - 2 * H, 74050, 74200, 74000, 74150, "200.25", 20),
        venue(OPEN - H, 74150, 74300, 74100, 74250, "300.125", 30),
        venue(OPEN, 74250, 74400, 74200, 74350, "40.5", 4)]


class Clock:
    def __init__(self, ms=NOW):
        self.ms, self.mono = ms, 10 ** 12

    def now(self):
        return self.ms, self.mono

    def advance(self, ms):
        self.ms += ms
        self.mono += ms * 1_000_000


class Jobs:
    """A stand-in executor: a fetch runs only when the test says so, so the loading state is observable."""

    def __init__(self):
        self.queued = []

    def submit(self, fn, *args):
        self.queued.append((fn, args))

    def run(self):
        jobs, self.queued = self.queued, []
        for fn, args in jobs:
            fn(*args)
        return len(jobs)


class Venue:
    """Canned candleSnapshot answers, one per call in order (an Exception instance is raised); records each range."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, market, interval, start_ms, end_ms):
        self.calls.append((market, interval, start_ms, end_ms))
        ans = self.answers.pop(0) if self.answers else []
        if isinstance(ans, Exception):
            raise ans
        return ans


def make_store(answers, clock=None):
    clock = clock or Clock()
    jobs, src = Jobs(), Venue(answers)
    return CandleStore("http://127.0.0.1:9/info", fetch=src, executor=jobs, now=clock.now), jobs, src, clock


SUB = {"type": "candle", "coin": "BTC", "interval": "1h"}


# --- the store ------------------------------------------------------------------------------------------------

def test_first_request_fetches_the_most_recent_5000_and_the_answer_has_the_agreed_shape():
    store, jobs, src, clock = make_store([ROWS])
    store.request("BTC", "1h")
    assert store.take_pending() == [SUB]                      # the websocket subscription, on first request
    v = store.view("BTC", "1h")
    assert v == {"ok": False, "state": "loading", "market": "BTC", "interval": "1h", "reason": v["reason"]}
    assert "running" in v["reason"]
    store.request("BTC", "1h")
    assert len(jobs.queued) == 1                                # one fetch at a time
    assert jobs.run() == 1 and src.calls == [("BTC", "1h", NOW - 5000 * H, NOW)]
    clock.advance(2_500)
    v = store.view("BTC", "1h")
    assert v["ok"] is True and v["market"] == "BTC" and v["interval"] == "1h"
    assert set(v) == {"ok", "market", "interval", "candles", "history", "in_progress", "source", "provenance"}
    assert [c["state"] for c in v["candles"]] == ["closed"] * 3 + ["in_progress"]
    assert v["candles"][0] == {"time": OPEN - 3 * H, "close_time": OPEN - 2 * H - 1, "open": 74000.0, "high": 74100.0,
                               "low": 73900.0, "close": 74050.0, "volume_base": 100.5, "trades": 10, "state": "closed"}
    assert all(isinstance(c[k], float) for c in v["candles"] for k in ("open", "high", "low", "close", "volume_base"))
    assert "volume_usd" not in v["candles"][0] and "notional" not in json.dumps(v)     # nothing invented: base volume only
    ip = v["in_progress"]
    assert ip["status"] == "unverified" and ip["received_age_s"] == 2.5
    assert ip["reason"] == "candle updates carry no source timestamp; received 2.5 s ago"
    assert ip["from"] == "candleSnapshot" and ip["time"] == OPEN and ip["close_time"] == OPEN + H - 1
    h = v["history"]
    assert h["count"] == 3 and h["available"] == 3 and h["first_ms"] == OPEN - 3 * H and h["last_closed_ms"] == OPEN - H
    assert h["fetched_at_ms"] == NOW and h["note"] == "the venue keeps only the most recent 5000 candles per interval"
    assert v["source"] == cd.SOURCE and "candleSnapshot" in v["source"] and "wss://api.hyperliquid.xyz/ws" in v["source"]
    p = v["provenance"]
    assert p["mode"] == "LIVE" and p["market"] == "BTC" and p["interval"] == "1h" and p["source"] == cd.SOURCE
    assert p["websocket"]["subscription"] == SUB and p["last_fetch"]["items"] == 4 and p["last_fetch"]["weight"] == 21
    clock.advance(1_000)                                        # the age is computed when served, never cached
    assert store.view("BTC", "1h")["in_progress"]["received_age_s"] == 3.5


def test_limit_returns_the_most_recent_candles_with_the_in_progress_one_last():
    store, jobs, src, clock = make_store([ROWS])
    store.request("BTC", "1h")
    jobs.run()
    v = store.view("BTC", "1h", limit=2)
    assert [(c["time"], c["state"]) for c in v["candles"]] == [(OPEN - H, "closed"), (OPEN, "in_progress")]
    assert v["history"]["count"] == 1 and v["history"]["available"] == 3 and v["history"]["first_ms"] == OPEN - H
    assert [c["state"] for c in store.view("BTC", "1h", limit=0)["candles"]] == ["in_progress"]     # clamped to 1
    assert len(store.view("BTC", "1h", limit=10 ** 6)["candles"]) == 4


def test_merge_sorts_dedupes_by_open_time_and_the_latest_receipt_wins():
    store, jobs, src, clock = make_store([])
    store.merge_snapshot("BTC", "1h", [ROWS[2], ROWS[0], ROWS[1], ROWS[0]], NOW, 10 ** 12)
    v = store.view("BTC", "1h")
    assert [c["time"] for c in v["candles"]] == [OPEN - 3 * H, OPEN - 2 * H, OPEN - H]
    assert v["in_progress"] == {"status": "unavailable", "received_age_s": None, "reason": v["in_progress"]["reason"]}
    revised = dict(ROWS[0], c="74060.0", n=11)
    store.merge_snapshot("BTC", "1h", [revised], NOW + 1000, 10 ** 12 + 10 ** 9)         # a later answer revises a candle
    assert store.view("BTC", "1h")["candles"][0]["close"] == 74060.0
    store.merge_snapshot("BTC", "1h", [ROWS[0]], NOW + 500, 10 ** 12 + 5 * 10 ** 8)       # an earlier one never does
    assert store.view("BTC", "1h")["candles"][0]["close"] == 74060.0
    assert store.view("BTC", "1h")["history"]["available"] == 3
    bad = [{"t": OPEN - 4 * H, "T": OPEN - 3 * H - 1, "o": "x", "h": "1", "l": "1", "c": "1", "v": "1"}, {"t": "1"}, 7]
    store.merge_snapshot("BTC", "1h", bad, NOW + 2000, 10 ** 12 + 2 * 10 ** 9)            # malformed: dropped, counted
    v = store.view("BTC", "1h")
    assert v["history"]["available"] == 3 and v["history"]["malformed_dropped"] == 3


def test_in_progress_detection_at_receipt_and_the_websocket_updates():
    store, jobs, src, clock = make_store([ROWS])
    store.request("BTC", "1h")
    jobs.run()
    clock.advance(4_000)                                        # an update of the same candle replaces it and its receipt
    assert store.on_ws_candle(venue(OPEN, 74250, 74500, 74200, 74480, "55.75", 6), clock.mono, clock.ms) == 1
    v = store.view("BTC", "1h")
    ip, last = v["in_progress"], v["candles"][-1]
    assert ip["received_age_s"] == 0.0 and ip["from"] == "websocket" and ip["time"] == OPEN
    assert last["state"] == "in_progress" and last["close"] == 74480.0 and last["volume_base"] == 55.75
    assert v["history"]["available"] == 3 and v["provenance"]["websocket"]["messages"] == 1
    clock.advance(H)                                            # the next hour's first update closes the one it replaces
    store.on_ws_candle([venue(OPEN + H, 74480, 74490, 74470, 74485, "1.5", 2)], clock.mono, clock.ms)   # list form too
    v = store.view("BTC", "1h")
    assert [c["time"] for c in v["candles"]] == [OPEN - 3 * H, OPEN - 2 * H, OPEN - H, OPEN, OPEN + H]
    assert v["candles"][3]["state"] == "closed" and v["candles"][3]["close"] == 74480.0
    assert v["in_progress"]["time"] == OPEN + H and v["history"]["last_closed_ms"] == OPEN
    # a late update of an older candle revises the closed one and never becomes the in-progress candle
    store.on_ws_candle(venue(OPEN, 74250, 74500, 74200, 74490, "56.0", 7), clock.mono + 1, clock.ms)
    v = store.view("BTC", "1h")
    assert v["candles"][3]["close"] == 74490.0 and v["in_progress"]["time"] == OPEN + H
    # pairs never requested are ignored; malformed updates are dropped and counted
    assert store.on_ws_candle(venue(OPEN + H, 1, 1, 1, 1, "1", 1, coin="ETH"), clock.mono, clock.ms) == 0
    assert store.on_ws_candle(dict(venue(OPEN + H, 1, 1, 1, 1, "1", 1), o="nope"), clock.mono, clock.ms) == 0
    v = store.view("BTC", "1h")
    assert v["history"]["malformed_dropped"] == 1 and v["in_progress"]["time"] == OPEN + H
    clock.advance(2 * H)                                        # past its close time without final values: said so
    ip = store.view("BTC", "1h")["in_progress"]
    assert ip["status"] == "unverified" and "close time has passed" in ip["reason"] and ip["received_age_s"] == 2 * 3600.0


def test_refetch_only_when_the_newest_closed_candle_is_older_than_one_interval_and_at_most_every_30_s():
    later = [ROWS[3], venue(OPEN + H, 74350, 74360, 74300, 74310, "9.0", 3)]
    store, jobs, src, clock = make_store([ROWS, later])
    store.request("BTC", "1h")
    jobs.run()
    clock.advance(20_000)
    store.request("BTC", "1h")
    assert not jobs.queued                                      # 13:00's candle isn't older than one interval yet
    clock.advance(H)                                            # 15:13: it is
    store.request("BTC", "1h")
    assert len(jobs.queued) == 1
    jobs.run()
    assert src.calls[1] == ("BTC", "1h", OPEN - H, clock.ms)    # incremental: from the newest closed candle on
    v = store.view("BTC", "1h")
    assert [c["time"] for c in v["candles"]][-2:] == [OPEN, OPEN + H] and v["history"]["available"] == 4
    assert v["history"]["last_closed_ms"] == OPEN and v["in_progress"]["time"] == OPEN + H and v["history"]["fetches"] == 2
    assert v["provenance"]["last_fetch"]["why"] == "the newest closed candle is older than one interval"
    # the 30 s floor: a history that stays an interval old (empty answers) isn't asked for twice within 30 s
    store2, jobs2, src2, clock2 = make_store([ROWS, [], []])
    store2.request("BTC", "1h")
    jobs2.run()
    clock2.advance(2 * H)
    store2.request("BTC", "1h")
    jobs2.run()
    assert len(src2.calls) == 2
    clock2.advance(10_000)
    store2.request("BTC", "1h")
    assert not jobs2.queued
    clock2.advance(25_000)
    store2.request("BTC", "1h")
    assert len(jobs2.queued) == 1
    with pytest.raises(ValueError):
        store2.request("BTC", "2h")


def test_a_failed_fetch_is_the_error_form_and_is_retried_after_30_s():
    store, jobs, src, clock = make_store([ConnectionError("boom"), ROWS, ValueError("bad answer")])
    store.request("BTC", "1h")
    jobs.run()
    v = store.view("BTC", "1h")
    assert v["ok"] is False and v["state"] == "error" and v["reason"] == "candleSnapshot failed: ConnectionError: boom"
    assert v["market"] == "BTC" and v["interval"] == "1h" and v["next_retry_s"] == 30.0
    clock.advance(10_000)
    store.request("BTC", "1h")
    assert not jobs.queued and store.view("BTC", "1h")["next_retry_s"] == 20.0
    clock.advance(21_000)
    store.request("BTC", "1h")
    assert len(jobs.queued) == 1 and store.view("BTC", "1h")["state"] == "loading"
    jobs.run()
    v = store.view("BTC", "1h")
    assert v["ok"] is True and "last_error" not in v["history"]
    clock.advance(2 * H)                                        # a failed refetch keeps the history and says so beside it
    store.request("BTC", "1h")
    jobs.run()
    v = store.view("BTC", "1h")
    assert v["ok"] is True and v["history"]["available"] == 3 and "ValueError: bad answer" in v["history"]["last_error"]["reason"]


def test_subscriptions_are_resent_on_connect_and_pending_ones_sent_once():
    store, jobs, src, clock = make_store([[], [], []])
    store.request("BTC", "1h")
    store.request("xyz:SP500", "1m")
    assert store.take_pending() == [SUB, {"type": "candle", "coin": "xyz:SP500", "interval": "1m"}]
    assert store.take_pending() == []
    store.request("BTC", "5m")
    assert store.subscriptions_on_connect() == [SUB, {"type": "candle", "coin": "xyz:SP500", "interval": "1m"},
                                                {"type": "candle", "coin": "BTC", "interval": "5m"}]
    assert store.take_pending() == []                           # a connect sent everything; nothing stays pending


def test_feed_routes_candle_messages_to_the_desks_store():
    from hl_desk.feed import dispatch
    desk = make_desk()
    assert desk.candles is None
    assert dispatch(desk, {"channel": "candle", "data": venue(OPEN, 1, 1, 1, 1, "1", 1)}, 0, NOW) == "candle"
    store, jobs, src, clock = make_store([ROWS])
    desk.candles = store
    store.request("BTC", "1h")
    jobs.run()
    dispatch(desk, {"channel": "candle", "data": venue(OPEN, 74250, 74600, 74200, 74550, "70", 8)}, clock.mono, clock.ms)
    assert store.view("BTC", "1h")["candles"][-1] == {"time": OPEN, "close_time": OPEN + H - 1, "open": 74250.0, "high": 74600.0,
                                                      "low": 74200.0, "close": 74550.0, "volume_base": 70.0, "trades": 8,
                                                      "state": "in_progress"}


# --- the snapshot ------------------------------------------------------------------------------------------------

def test_15_s_candles_carry_the_base_volume_exactly():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i in range(3):
        ms.on_trades([trade("BTC", T0 + 16_000 + i, 75800.1, 0.00001, "B", A, B, i + 1)], 0, T0)
    ms.on_trades([trade("BTC", T0 + 31_000, 75800.1, 0.00001, "B", A, B, 9)], 0, T0)
    chart = build(desk, 1)["markets"]["BTC"]["chart"]
    candle = chart["candles"][0]
    assert candle["volume_base"] == 0.00003 and candle["volume_usd"] == 2.27       # the sizes' sum, exact; USD unchanged
    assert chart["intervals"] == {}                                                # no venue interval loaded


def test_the_snapshot_lists_the_intervals_loaded_so_far_without_a_cached_age():
    desk = make_desk()
    store, jobs, src, clock = make_store([ROWS, []])
    desk.candles = store
    store.request("BTC", "1h")
    store.request("BTC", "1m")
    assert build(desk, 1)["markets"]["BTC"]["chart"]["intervals"] == {}             # requested, not loaded yet
    jobs.run()
    iv = build(desk, 2)["markets"]["BTC"]["chart"]["intervals"]
    assert iv["1h"] == {"count": 3, "first_ms": OPEN - 3 * H, "last_ms": OPEN - H, "in_progress_status": "unverified",
                        "in_progress_received_ms": NOW, "fetched_at_ms": NOW}
    assert iv["1m"] == {"count": 0, "first_ms": None, "last_ms": None, "in_progress_status": "unavailable",
                        "in_progress_received_ms": None, "fetched_at_ms": NOW}
    assert not any("age" in k for k in iv["1h"])


# --- the route ----------------------------------------------------------------------------------------------------

class _Collector:
    """The owner's collector as the runtime reads it: alive, with the desk's markets and the candle store."""

    def __init__(self, store, markets):
        import types
        self.candles = store
        self.desk = types.SimpleNamespace(markets={m: object() for m in markets}, collector={"state": "connected"})

    def alive(self):
        return True


@pytest.fixture
def route(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hl_desk import runtime as rt_mod
    from node_helpers import load_plugin_api

    use_standalone_storage(monkeypatch, tmp_path)
    rt_mod._slot.reset()
    rt = rt_mod.get_runtime()
    store, jobs, src, clock = make_store([ROWS])
    rt.collector = _Collector(store, ["BTC", "xyz:SP500"])
    app = FastAPI()
    app.include_router(load_plugin_api().router, prefix="/api/plugins/hyperliquid-desk")
    yield TestClient(app), rt, store, jobs, src, clock
    rt_mod._slot.reset()


URL = "/api/plugins/hyperliquid-desk/candles"


def test_route_loading_then_loaded_then_bad_interval_and_market(route):
    http, rt, store, jobs, src, clock = route
    r = http.get(URL, params={"market": "BTC", "interval": "1h"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "state": "loading", "market": "BTC", "interval": "1h", "reason": r.json()["reason"]}
    assert len(jobs.queued) == 1 and src.calls == []            # the fetch is scheduled, not run on the request
    jobs.run()
    clock.advance(1_500)
    r = http.get(URL, params={"market": "BTC", "interval": "1h", "limit": 2})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    # the route serves the store's view, unchanged, plus the chart's taker flow per candle (Kevin's review of Nov 11),
    # which says why it has nothing when the collector has no node feed
    assert {k: v for k, v in body.items() if k != "taker_flow"} == json.loads(json.dumps(store.view("BTC", "1h", 2)))
    assert body["taker_flow"] == {"stream": "node fills", "interval": "1h", "available": False,
                                  "reason": "needs node mode: no node feed configured (setting node_feed)"}
    assert [c["state"] for c in body["candles"]] == ["closed", "in_progress"] and body["in_progress"]["received_age_s"] == 1.5
    assert set(body) == {"ok", "market", "interval", "candles", "history", "in_progress", "source", "provenance", "taker_flow"}
    r = http.get(URL, params={"market": "BTC", "interval": "2h"})
    assert r.status_code == 400 and r.json()["ok"] is False
    assert "'2h'" in r.json()["error"] and "1m, 5m, 15m, 30m, 1h, 4h, 1d" in r.json()["error"]
    r = http.get(URL, params={"market": "ETH", "interval": "1h"})
    assert r.status_code == 400 and r.json()["ok"] is False and "'ETH'" in r.json()["error"]
    assert not jobs.queued                                      # neither bad request touched the venue
    r = http.get(URL, params={"market": "BTC", "interval": "1h", "limit": 10 ** 6})
    assert len(r.json()["candles"]) == 4 and r.json()["history"]["available"] == 3


def test_route_error_form_after_a_failed_first_fetch_and_without_a_collector(route):
    http, rt, store, jobs, src, clock = route
    src.answers[:] = [ConnectionError("venue down")]
    assert http.get(URL, params={"market": "xyz:SP500", "interval": "1m"}).json()["state"] == "loading"
    jobs.run()
    r = http.get(URL, params={"market": "xyz:SP500", "interval": "1m"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "state": "error", "market": "xyz:SP500", "interval": "1m",
                        "reason": "candleSnapshot failed: ConnectionError: venue down", "failed_at": r.json()["failed_at"],
                        "next_retry_s": 30.0}
    rt.collector = None                                         # nothing running for this profile
    r = http.get(URL, params={"market": "BTC", "interval": "1h"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["state"] == "error"
    assert "isn't running" in r.json()["reason"]


# --- the chat tool ------------------------------------------------------------------------------------------------

def test_hl_candles_tool_gives_the_routes_answer_with_the_tool_envelope(route, tmp_path):
    from hl_desk import tools
    from hl_desk.store import SnapshotStore
    http, rt, store, jobs, src, clock = route
    rt.store = SnapshotStore(tmp_path)                           # run_tool resolves a snapshot first, as every tool does
    desk = make_desk()
    snap = build(desk, 1)
    rt.store.publish(snap, frozen=desk.frozen_last)
    args = {"market": "BTC", "interval": "1h", "limit": 2, "snapshot_id": snap["snapshot_id"]}
    first = json.loads(tools.run_tool("hl_candles", args, rt))
    assert first["ok"] is False and first["state"] == "loading" and first["tool"] == "hl_candles"
    jobs.run()
    clock.advance(500)
    r = json.loads(tools.run_tool("hl_candles", args, rt))
    via_tab = http.get(URL, params={"market": "BTC", "interval": "1h", "limit": 2}).json()
    assert r["ok"] is True and r["tool"] == "hl_candles" and r["disclaimer"] == tools.DISCLAIMER
    assert {k: v for k, v in r.items() if k in via_tab} == via_tab            # the same answer as the tab's route
    assert r["in_progress"]["received_age_s"] == 0.5 and "received N s ago" in r["note"]
    assert r["snapshot_id"] == snap["snapshot_id"] and r["served"]["latest"] is False and r["historical"]["as_of"] == snap["built_at"]
    default = json.loads(tools.run_tool("hl_candles", {"market": "BTC", "snapshot_id": snap["snapshot_id"]}, rt))
    assert default["ok"] is True and default["interval"] == "1h" and len(default["candles"]) == 4     # interval defaults to 1h
    for bad, why in (({"market": "BTC", "interval": "2h"}, "interval must be one of 1m, 5m, 15m, 30m, 1h, 4h, 1d"),
                     ({"market": "BTC", "interval": "1h", "limit": 501}, "limit must be a whole number from 1 to 500"),
                     ({"market": "ETH", "interval": "1h"}, "market 'ETH' isn't followed")):
        e = json.loads(tools.run_tool("hl_candles", dict(bad, snapshot_id=snap["snapshot_id"]), rt))
        assert e["ok"] is False and why in e["error"], e
    assert tools.SCHEMAS["hl_candles"]["parameters"]["required"] == ["market"]
    # a runtime without a candle store (a stand-in, or no collector): the error form, still with the served labels
    from helpers import SnapRT
    from hl_desk.runtime import _served
    served = _served(snap, snap["built_at_ms"] + 120_000, latest=False)
    e = json.loads(tools.run_tool("hl_candles", {"market": "BTC", "snapshot_id": snap["snapshot_id"]}, SnapRT(served)))
    assert e["ok"] is False and e["state"] == "error" and "candle store" in e["reason"] and e["served"]["age_s"] == 120.0
