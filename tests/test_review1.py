"""Regression tests for Codex review 1 (commit b21d205), one per finding, built from its reproductions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hl_desk import book as bk, owner, schema, tools
from hl_desk.config import DeskConfig
from hl_desk.runtime import LATEST_MAX_AGE_S, Runtime
from hl_desk.store import SnapshotStore

from helpers import A, B, C, NS, T0, SnapRT, book_msg, make_desk, open_segment, trade, build
from test_collector_local import COIN, LocalHyperliquid, _wait

ROOT = Path(__file__).resolve().parent.parent


def _in_other_process(code: str, timeout: float = 60.0) -> dict:
    """Run code in a separate Python process (as another Hermes process of the same profile would)."""
    prog = "import json, sys; sys.path.insert(0, %r)\n" % str(ROOT) + textwrap.dedent(code)
    out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# --- finding 1: snapshots by ID, drill-down and controls across processes -----------------

def test_other_process_reads_snapshot_by_id_drilldown_and_stops_the_owner(tmp_path):
    hl = LocalHyperliquid()
    hl.start()
    cfg = DeskConfig(markets=[COIN], ws_url=f"ws://127.0.0.1:{hl.ws_port}",
                     info_url=f"http://127.0.0.1:{hl.http.server_address[1]}/info", depth_poll_s=0.3)
    rt = Runtime(cfg_factory=lambda: cfg, data_dir_factory=lambda: tmp_path)
    try:
        rt.start()

        def ready():
            s, _ = rt.snapshot(None)
            if not s:
                return None
            p = s["markets"][COIN]["wallets"]["1m"]
            return s if p.get("buyers") and p["buyers"]["listed"] else None

        snap = _wait(ready)
        assert snap, "no wallet panel"
        sid = snap["snapshot_id"]
        panel = snap["markets"][COIN]["wallets"]["1m"]
        addr = panel["buyers"]["listed"][0]["address"]
        time.sleep(3)   # the owner moves on to newer snapshots; the old one stays retained
        got = _in_other_process(f"""
            from pathlib import Path
            from hl_desk.runtime import Runtime
            rt = Runtime(data_dir_factory=lambda: Path({str(tmp_path)!r}))
            s, err = rt.snapshot({sid!r})
            w = rt.wallet_trades({sid!r}, {COIN!r}, 1, {addr!r})
            st = rt.status()
            print(json.dumps({{"sid": s and s["snapshot_id"], "err": err, "served": s and s["served"],
                              "totals": w.get("totals"), "werr": w.get("error"), "status": st}}))
        """)
        assert got["sid"] == sid and got["err"] is None
        assert got["served"]["latest"] is False
        assert got["totals"]["bought_usd"] == panel["wallets"][addr]["bought_usd"]
        assert got["totals"]["sold_usd"] == panel["wallets"][addr]["sold_usd"]
        assert got["status"]["owner"] is False and got["status"]["owner_pid"] == os.getpid()
        stop = _in_other_process(f"""
            from pathlib import Path
            from hl_desk.runtime import Runtime
            rt = Runtime(data_dir_factory=lambda: Path({str(tmp_path)!r}))
            print(json.dumps(rt.stop()["action"]))
        """)
        assert stop["ok"] and "owner" in stop["message"]
        assert _wait(lambda: not rt.collector.alive(), timeout=15)
        assert not owner.sock_path(tmp_path).exists()
    finally:
        rt.stop()
        hl.stop()


# --- finding 2: a dead or silent owner is never served as current --------------------------

def test_killed_owner_leaves_nothing_that_reads_as_current(tmp_path):
    code = textwrap.dedent(f"""
        import sys, time; sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from hl_desk.runtime import Runtime
        from hl_desk.config import DeskConfig
        rt = Runtime(cfg_factory=lambda: DeskConfig(info_url="http://127.0.0.1:9/info", ws_url="ws://127.0.0.1:9/ws"),
                     data_dir_factory=lambda: Path({str(tmp_path)!r}))
        print(rt.start()["action"]["message"], flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "started"
        assert _wait(lambda: owner.request(owner.sock_path(tmp_path), {"op": "ping"}) is not None, timeout=10)
    finally:
        proc.send_signal(signal.SIGKILL)       # no final "stopped" publication, socket file left behind
        proc.wait(10)
    assert owner.sock_path(tmp_path).exists()
    rt = Runtime(cfg_factory=lambda: DeskConfig(info_url="http://127.0.0.1:9/info", ws_url="ws://127.0.0.1:9/ws"),
                 data_dir_factory=lambda: tmp_path)
    snap, err = rt.snapshot(None)
    assert snap is None and "isn't running" in err
    assert rt.status()["collector"]["state"] == "stopped"
    try:
        assert rt.start()["action"]["message"] == "started"   # the dead owner's lock and socket don't block
    finally:
        rt.stop()


class _Stand:
    def __init__(self, store, state="connected"):
        self.store = store
        self.desk = type("D", (), {"collector": {"state": state}})()

    def alive(self):
        return True

    def stop(self, timeout=10.0):
        return True


def test_latest_older_than_the_limit_is_refused_and_ids_are_labeled_historical(tmp_path):
    rt = Runtime(data_dir_factory=lambda: tmp_path)
    rt.store = SnapshotStore(tmp_path)
    rt.collector = _Stand(rt.store)
    desk = make_desk()
    desk.collector = {"state": "connected"}
    open_segment(desk.markets["BTC"], T0)
    # Snapshots are stamped as the live collector stamps them: the owner's monotonic clock (their served age).
    late = int((LATEST_MAX_AGE_S + 0.5) * 1e9)
    old = desk.build_snapshot(time.monotonic_ns() - late, int(time.time() * 1000) - late // 10 ** 6)
    rt.store.publish(old, frozen=desk.frozen_last)
    snap, err = rt.snapshot(None)
    assert snap is None and "isn't publishing" in err
    snap, err = rt.snapshot(old["snapshot_id"])
    assert err is None and snap["served"]["latest"] is False and snap["served"]["age_s"] > LATEST_MAX_AGE_S
    fresh = desk.build_snapshot(time.monotonic_ns(), int(time.time() * 1000))
    rt.store.publish(fresh, frozen=desk.frozen_last)
    snap, err = rt.snapshot(None)
    assert err is None and snap["served"]["latest"] is True
    stopped_snap = dict(fresh, snapshot_id="hd-x-stop", collector={"state": "stopped"})
    rt.store.publish(stopped_snap)
    snap, err = rt.snapshot(None)
    assert snap is None and "stopped" in err


# --- finding 3: only an advancing watermark is trade progress ----------------------------------

def test_late_trade_does_not_refresh_trades_freshness():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 100_000, 100.0, 1.0, "B", A, B, 1)], 0, T0)
    at = 31 * NS
    assert build(desk, at)["markets"]["BTC"]["freshness"]["trades"]["status"] == "unverified"
    ms.on_trades([trade("BTC", T0 + 90_000, 100.0, 1.0, "B", A, B, 2)], at, T0)   # late, different tid
    snap = build(desk, at)
    f = snap["markets"]["BTC"]["freshness"]["trades"]
    assert f["status"] == "unverified" and f["source_time_ms"] == T0 + 100_000
    assert snap["markets"]["BTC"]["flow"]["values"]["volume"]["value"] is None
    assert ms.store.counters["late"] == 1                     # still counted in the execution set
    ms.on_trades([trade("BTC", T0 + 100_000, 100.0, 1.0, "A", A, B, 3)], at, T0)  # same time: not progress
    assert build(desk, at)["markets"]["BTC"]["freshness"]["trades"]["status"] == "unverified"
    ms.on_trades([trade("BTC", T0 + 100_001, 100.0, 1.0, "A", A, B, 4)], at, T0)  # advances: progress
    assert build(desk, at)["markets"]["BTC"]["freshness"]["trades"]["status"] == "fresh"


# --- finding 4: burst re-arming uses exactly the pre-message set ---------------------------------

def _bursts(ms):
    return [e for e in ms.deck.events if e["type"] == "burst"]


def test_burst_does_not_repeat_within_one_episode_at_the_same_timestamp():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 61_000, 100.0, 100_000.0, "B", A, B, 1)], 0, T0)   # $10M: fires
    assert len(_bursts(ms)) == 1
    ms.on_trades([trade("BTC", T0 + 61_000, 1.0, 1.0, "B", C, B, 2)], 0, T0)           # $1 more, same time
    assert len(_bursts(ms)) == 1


def test_burst_new_episode_in_one_message_fires():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 61_000, 100.0, 100_000.0, "B", A, B, 1)], 0, T0)
    assert len(_bursts(ms)) == 1
    ms.on_trades([trade("BTC", T0 + 122_000, 100.0, 100_000.0, "B", A, B, 2),
                  trade("BTC", T0 + 123_000, 1.0, 1.0, "B", C, B, 3)], 0, T0)
    assert len(_bursts(ms)) == 2
    assert _bursts(ms)[0]["aggressive_notional_usd"] == pytest.approx(10_000_001.0)


# --- finding 5: drill-down is immutable and complete for every retained snapshot ------------------

def _rt_with(desk, snaps_and_frozen, tmp_path):
    rt = Runtime(data_dir_factory=lambda: tmp_path)
    rt.store = SnapshotStore(tmp_path)
    rt.collector = _Stand(rt.store)
    for s, f in snaps_and_frozen:
        rt.store.publish(s, frozen=f)
    return rt


def test_drilldown_unchanged_by_a_later_late_trade(tmp_path):
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    ms.on_trades([trade("BTC", T0 + 100_000, 100.0, 1.0, "B", A, B, 1)], 0, T0)
    ms.on_trades([trade("BTC", T0 + 120_000, 100.0, 1.0, "B", A, B, 2)], 0, T0)
    snap = build(desk, 1)
    rt = _rt_with(desk, [(snap, desk.frozen_last)], tmp_path)
    ms.on_trades([trade("BTC", T0 + 110_000, 1000.0, 1.0, "B", A, B, 3)], 0, T0)   # late, after the snapshot
    panel = snap["markets"]["BTC"]["wallets"]["5m"]
    assert panel["wallets"][A]["bought_usd"] == 200.0
    d = rt.wallet_trades(snap["snapshot_id"], "BTC", 5, A)
    assert d["totals"]["bought_usd"] == 200.0 and len(d["executions"]) == 2


def test_drilldown_complete_after_old_trades_leave_the_live_store(tmp_path):
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    tid = 0
    for k in range(1, 101):                   # $100 buys every 10 s through +1000 s
        tid += 1
        ms.on_trades([trade("BTC", T0 + k * 10_000, 100.0, 1.0, "B", A, B, tid)], 0, T0)
    snap = build(desk, 1)
    rt = _rt_with(desk, [(snap, desk.frozen_last)], tmp_path)
    panel = snap["markets"]["BTC"]["wallets"]["15m"]
    for k in range(101, 113):                 # continue to +1120 s: the live store prunes the oldest
        tid += 1
        ms.on_trades([trade("BTC", T0 + k * 10_000, 100.0, 1.0, "B", A, B, tid)], 0, T0)
    assert min(t.time for t in ms.store.trades) > panel["window"]["start_exclusive_ms"] + 10_000
    d = rt.wallet_trades(snap["snapshot_id"], "BTC", 15, A)
    assert d["totals"]["bought_usd"] == panel["wallets"][A]["bought_usd"] == 9000.0
    assert len(d["executions"]) == 90


# --- finding 6: the hl_flow CVD slice states its origin ---------------------------------------

def test_hl_flow_cvd_slice_states_its_cumulative_origin():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i, k in enumerate(range(31_000, 932_000, 30_000), start=1):   # $100 buys at +31 s, +61 s, ... +931 s
        ms.on_trades([trade("BTC", T0 + k, 100.0, 1.0, "B", A, B, i)], 0, T0)
    snap = build(desk, 1)
    cvd_full = snap["markets"]["BTC"]["flow"]["cvd"]
    first_complete = next(b for b in cvd_full["buckets"] if b["complete"])
    assert cvd_full["cumulative_from_ms"] == first_complete["start_ms"]
    out = json.loads(tools.run_tool("hl_flow", {"market": "BTC"}, SnapRT(snap)))
    cvd = out["cvd_last_6_minutes"]
    shown = [b for b in cvd["buckets"] if b["complete"]]
    base = cvd["slice"]["cumulative_base_usd"]
    assert base["unit"] == "USD" and base["value"] is not None
    running = base["value"]
    for b in shown:
        running += b["delta_usd"]
        assert b["cumulative_usd"] == pytest.approx(running)
    assert cvd["cumulative_from"] and "not from the first bucket shown" in cvd["slice"]["note"]


# --- finding 7: missing book fields are never zero or silently dropped -------------------------

def test_missing_api_count_is_null_with_reason():
    levels, problems = bk.parse_levels([{"px": "100", "sz": "2"}])
    assert levels == [(100.0, 2.0, None)] and problems == []
    row = bk.ladder_rows(levels, [])["bids"][0]
    assert row["api_count"] is None and "no n field" in row["api_count_reason"]


def test_missing_level_size_invalidates_that_sides_totals():
    desk = make_desk()
    ms = desk.markets["BTC"]
    ms.on_connect(1)
    ms.on_book(book_msg("BTC", T0, [(99.95, 1.0, 1)] * 1 + [(99.94 - i * 0.01, 1.0, 1) for i in range(4)],
                        [(100.05 + i * 0.01, 1.0, 1) for i in range(5)]), 0, T0)
    raw = {"coin": "BTC", "time": T0, "levels": [
        [{"px": "99.95", "sz": "1", "n": 1}, {"px": "99.75"}, {"px": "99.5", "sz": "1", "n": 1}]
        + [{"px": str(99.4 - i * 0.1), "sz": "1", "n": 1} for i in range(17)],
        [{"px": str(100.05 + i * 0.1), "sz": "1", "n": 1} for i in range(20)]]}
    ms.on_depth_poll(4, raw, 0, T0)
    snap = build(desk, 1)
    bands = snap["markets"]["BTC"]["book"]["depth"]["bands"]
    b025 = bands[0]["approximate"]
    assert b025["bid"]["status"] == "unavailable"
    assert any("missing or invalid sz" in t["reason"] for t in b025["bid"]["tried"])
    obs = snap["markets"]["BTC"]["book"]["depth"]["observed"]["nSigFigs=4"]
    assert obs["bid"]["notional_usd"]["value"] is None and obs["ask"]["notional_usd"]["value"] is not None


def test_malformed_best_level_makes_the_top_unavailable():
    top = bk.top_of_book([(99.0, 1.0, 1)], [(101.0, 1.0, 1)], {"bid": "1 returned bid level(s) had a missing or invalid px"})
    assert top["best_bid"]["value"] is None and top["mid"]["value"] is None and top["best_ask"]["value"] == 101.0


# --- finding 8: chart metrics in the schema, volume rounded once ------------------------------

def test_candle_volume_is_summed_exactly_then_rounded_once():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i in range(3):
        ms.on_trades([trade("BTC", T0 + 16_000 + i, 75800.1, 0.00001, "B", A, B, i + 1)], 0, T0)
    ms.on_trades([trade("BTC", T0 + 31_000, 75800.1, 0.00001, "B", A, B, 9)], 0, T0)
    candle = build(desk, 1)["markets"]["BTC"]["chart"]["candles"][0]
    assert candle["volume_usd"] == 2.27 and candle["complete"] is True


def test_every_published_metric_has_a_schema_entry():
    s = schema.load()["metrics"]
    published = ["book.best_bid", "book.best_ask", "book.mid", "book.spread", "book.spread_bps", "book.level",
                 "book.depth_band", "book.depth_imbalance", "book.observed_depth", "book.bbo_consistency",
                 "flow.buy_aggressive_notional", "flow.sell_aggressive_notional", "flow.net", "flow.volume",
                 "flow.trade_count", "flow.cvd_bucket", "flow.large_print",
                 "context.mark", "context.oracle", "context.mid", "context.funding", "context.open_interest",
                 "context.day_notional_volume", "context.prev_day_px",
                 "deck.price", "deck.move_bps", "deck.net_flow", "deck.volume", "deck.impact", "deck.pressure",
                 "deck.activity_buckets", "deck.top_wallet", "deck.top_wallet_net_aggressive", "deck.accounts",
                 "deck.trade_count", "wallets.window", "wallets.unique_execution_notional", "wallets.executions",
                 "wallets.bought", "wallets.sold", "wallets.aggressive_bought", "wallets.aggressive_sold",
                 "wallets.participation", "wallets.net_bought", "wallets.buy_share", "wallets.sell_share",
                 "wallets.ranked_list", "chart.candle", "chart.price_line"]
    missing = [k for k in published if k not in s]
    assert not missing, missing
