"""Every numeric freshness threshold at its boundary, mirror-side regime exits, partial values as null,
the wallet drill-down on recorded info responses, and one collector per profile in one process."""

from __future__ import annotations

import json
import time

import pytest

from hl_desk import deck, schema, tools
from hl_desk.collector import Collector
from hl_desk.config import DeskConfig
from hl_desk.runtime import Runtime
from hl_desk.store import SnapshotStore

from helpers import A, B, INFO, NS, T0, SnapRT, book_msg, make_desk, open_segment, recorded_replayer, trade, build

FAST = [(100.0 - i * 0.1, 1.0, 1) for i in range(5)]
FAST_ASK = [(100.1 + i * 0.1, 1.0, 1) for i in range(5)]
FULL = [(100.0 - i * 0.1, 1.0, 1) for i in range(20)]
FULL_ASK = [(100.1 + i * 0.1, 1.0, 1) for i in range(20)]


def status_at(desk, now_ns, field, market="BTC"):
    return build(desk, now_ns)["markets"][market]["freshness"][field]["status"]


@pytest.mark.parametrize("age,expected", [(15.0, "fresh"), (15.001, "stale")])
def test_full_book_max_age_15s(age, expected):
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FULL, FULL_ASK), 0, T0)
    assert status_at(desk, int(age * NS), "book_full") == expected


@pytest.mark.parametrize("nsf,field", [(4, "book_sf4"), (3, "book_sf3"), (2, "book_sf2")])
@pytest.mark.parametrize("age,expected", [(15.0, "fresh"), (15.001, "stale")])
def test_info_depth_books_max_age_15s(nsf, field, age, expected):
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_depth_poll(nsf, book_msg("BTC", T0, FULL, FULL_ASK), 0, T0)
    assert status_at(desk, int(age * NS), field) == expected


@pytest.mark.parametrize("market,limit", [("HYPE", 60.0), ("xyz:SP500", 120.0), ("xyz:XYZ100", 120.0)])
def test_trades_max_age_per_market(market, limit):
    desk = make_desk(markets=(market,))
    ms = desk.markets[market]
    open_segment(ms, T0)
    ms.on_trades([trade(market, T0 + 1, 10.0, 1.0, "B", A, B, 1)], 0, T0)
    assert status_at(desk, int(limit * NS), "trades", market) == "fresh"
    assert status_at(desk, int((limit + 0.001) * NS), "trades", market) == "unverified"


@pytest.mark.parametrize("market,limit", [("HYPE", 20.0), ("xyz:SP500", 45.0), ("xyz:XYZ100", 45.0)])
def test_ctx_receipt_age_per_market_and_never_fresh(market, limit):
    desk = make_desk(markets=(market,))
    ms = desk.markets[market]
    ms.on_connect(1)
    ms.on_ctx({"coin": market, "ctx": {"markPx": "10", "oraclePx": "10", "funding": "0", "openInterest": "1",
                                       "dayNtlVlm": "1", "prevDayPx": "10"}}, 0, T0)
    m = build(desk, int(limit * NS))["markets"][market]
    snap = m["freshness"]["ctx"]
    assert snap["status"] == "unverified" and "no source timestamp" in snap["reason"]
    # at the limit the values are still served, marked unverified with the receipt age (schema 1.3.0)
    assert m["context"]["mark"]["value"] == 10.0 and m["context"]["mark"]["status"] == "unverified"
    assert m["context"]["mark"]["received_age_s"] == limit
    m = build(desk, int((limit + 0.001) * NS))["markets"][market]
    snap = m["freshness"]["ctx"]
    assert snap["status"] == "unverified" and "no changed context" in snap["reason"]
    assert m["context"]["mark"]["value"] is None and "no changed context" in m["context"]["mark"]["reason"]


@pytest.mark.parametrize("offset_ms,suspected", [(2000, False), (2001, True), (-2001, True)])
def test_clock_skew_warn_threshold(offset_ms, suspected):
    assert schema.freshness_field("clock_skew")["warn_abs_ms"] == 2000
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0 + offset_ms)
    clock = build(desk, 1)["markets"]["BTC"]["clock"]
    assert clock["offset_ms"] == offset_ms and clock["clock_skew_suspected"] is suspected


@pytest.mark.parametrize("gap_ms,usable", [(1500, True), (1501, False)])
def test_depth_mid_must_be_within_1500ms(gap_ms, usable):
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, FAST, FAST_ASK), 0, T0)
    deep_bids = [(100.0 - i * 0.5, 1.0, 1) for i in range(20)]
    deep_asks = [(100.1 + i * 0.5, 1.0, 1) for i in range(20)]
    ms.on_depth_poll(4, book_msg("BTC", T0 + gap_ms, deep_bids, deep_asks), 0, T0 + gap_ms)
    obs = build(desk, 1)["markets"]["BTC"]["book"]["depth"]["observed"]["nSigFigs=4"]
    assert ("bid" in obs) is usable
    if not usable:
        assert obs["reason"] == "no full-precision mid within 1.5 s of this book"


@pytest.mark.parametrize("age,stalled", [(14.99, False), (15.001, True)])   # live clock: 15.0 itself drifts past
def test_stall_reconnect_threshold(age, stalled, tmp_path):
    assert schema.freshness_field("l2Book")["stall_reconnect_s"] == 15.0
    c = Collector(DeskConfig(markets=["BTC"]), SnapshotStore(tmp_path))
    c.desk = make_desk()
    tr = c.desk.markets["BTC"].trackers["book_fast"]
    tr.on_progress(time.monotonic_ns() - int(age * NS), source_ms=T0)
    assert c._stalled() is stalled


def M(bps, net, vol=1e6, pressure=None):
    impact = abs(bps) / (abs(net) / 1e6) if abs(net) >= 100_000 else None
    return {"n": 50, "bps": bps, "net": net, "vol": vol, "impact": impact, "pressure": pressure}


def test_mirror_side_regimes_enter_and_leave():
    assert deck.classify(M(-5.0, 40_000)) == ("PULLED-BIDS", None)
    assert deck.classify(M(-4.999, 40_000))[0] != "PULLED-BIDS"
    assert deck.classify(M(-5.0, 39_999.99))[0] != "PULLED-BIDS"
    assert deck.classify(M(-20.0, -1_000_000)) == ("VACUUM-DOWN", "impact")
    assert deck.classify(M(-19.99, -1_000_000))[0] != "VACUUM-DOWN"
    assert deck.classify(M(-2.0, -149_999.99 / 1e6 * 1e6, vol=149_999.99)) == ("VACUUM-DOWN", "thin")
    assert deck.classify(M(-1.999, -100.0, vol=149_999.99)) == ("THIN", None)


def test_partial_and_unverified_values_are_null_with_reason():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0 + 10_000)
    for i in range(1, 8):
        ms.on_trades([trade("BTC", T0 + 10_000 + i * 10_000, 100.0, 3000.0, "B", A, B, i)], 0, T0)
    snap = build(desk, 1)
    cvd = snap["markets"]["BTC"]["flow"]["cvd"]["buckets"]
    first, last = cvd[0], cvd[-1]
    assert first["complete"] is False and first["delta_usd"] is None and "segment start" in first["reason"]
    assert last["complete"] is False and last["cumulative_usd"] is None and "watermark" in last["reason"]
    complete = [b for b in cvd if b["complete"]]
    assert complete and all(b["delta_usd"] is not None for b in complete)
    assert snap["markets"]["BTC"]["flow"]["large_prints"]["count"] == 7
    stale = build(desk, int(31 * NS))["markets"]["BTC"]["flow"]
    assert stale["large_prints"]["count"] is None and "unverified" in stale["large_prints"]["reason"]
    assert all(b["delta_usd"] is None for b in stale["cvd"]["buckets"])
    candles = snap["markets"]["BTC"]["chart"]["candles"]
    assert candles[-1]["complete"] is False


def test_wallet_drilldown_with_recorded_info_responses():
    rp = recorded_replayer(["BTC", "xyz:SP500"])
    rp.run_until(rp.records[int(len(rp.records) * 0.45)]["recv_mono_ns"])
    snap = rp.snapshot()
    recorded = {dex: {k: json.loads((INFO / f"{k}_{dex or 'default'}.json").read_text())
                      for k in ("clearinghouseState", "frontendOpenOrders")} for dex in ("", "xyz")}

    def current(user, dex):
        r = recorded[dex]
        assert r["clearinghouseState"]["request"]["dex"] == dex
        # The recorder stamped each answer when it arrived; both are in hand at the orders answer's time.
        return {"requested_ms": r["clearinghouseState"]["fetched_ms"] - 150,
                "state_received_ms": r["clearinghouseState"]["fetched_ms"],
                "orders_received_ms": r["frontendOpenOrders"]["fetched_ms"],
                "returned_ms": r["frontendOpenOrders"]["fetched_ms"],
                "clearinghouseState": r["clearinghouseState"]["response"],
                "frontendOpenOrders": r["frontendOpenOrders"]["response"]}

    for market, dex in (("BTC", ""), ("xyz:SP500", "xyz")):
        panel = snap["markets"][market]["wallets"]["1m"]
        addr = panel["buyers"]["listed"][0]["address"]
        out = json.loads(tools.run_tool("hl_wallets", {"market": market, "window_min": 1, "address": addr},
                                        SnapRT(snap, rp.desk, current)))
        cur = out["wallet"]["current"]
        assert cur["label"] == "current account state, not part of the snapshot" and cur["fetched_at"]
        assert cur["dex"] == (dex or "(default perp dex)")
        # Real answers carry their own time about 0.1 s before the fetch, so under the test clock bound they're fresh.
        fr = cur["account"]["freshness"]
        assert fr["status"] == "fresh" and fr["source_time_ms"] == recorded[dex]["clearinghouseState"]["response"]["time"]
        assert cur["account"]["positions"] == {"value": []}
        assert cur["open_orders"]["freshness"]["status"] == "unverified" and cur["open_orders"]["orders"]["value"] is None
        assert out["wallet"]["trades"]["totals"]["bought_usd"] == panel["wallets"][addr]["bought_usd"]


def _offline(tmp):
    return Runtime(cfg_factory=lambda: DeskConfig(info_url="http://127.0.0.1:9/info", ws_url="ws://127.0.0.1:9/ws"),
                   data_dir_factory=lambda: tmp)


def test_one_collector_per_profile_in_one_process(tmp_path):
    a, b = tmp_path / "profile-a", tmp_path / "profile-b"
    a.mkdir()
    b.mkdir()
    ra, rb = _offline(a), _offline(b)
    try:
        assert ra.start()["action"]["message"] == "started"
        # Another profile in the same process gets its own collector, never profile A's.
        assert rb.start()["action"]["message"] == "started"
        assert rb.collector is not ra.collector and rb.store is not ra.store
        # A second copy of the runtime for profile A adopts A's collector.
        ra2 = _offline(a)
        assert ra2.start()["action"]["message"] in ("already running", "adopted this profile's collector already running in this process")
        assert ra2.collector is ra.collector
        ra3 = _offline(a)
        assert ra3.status()["owner"] is True and ra3.collector is ra.collector
        rb2 = _offline(b)
        rb2.status()
        assert rb2.collector is rb.collector
    finally:
        ra.stop()
        rb.stop()
