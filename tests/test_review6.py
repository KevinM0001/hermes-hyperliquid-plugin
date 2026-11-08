"""Regression tests for Codex review 6 (commit cb9c9d7), one per finding, from its reproductions."""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import A, B, C, NS, T0, build, make_desk, open_segment, trade

ROOT = Path(__file__).resolve().parent.parent


# --- BLOCKING 2: a snapshot delayed in delivery, or kept on screen too long, is withheld -----------------

def _snapshot_validity(cases: dict) -> dict:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:snapshotValidity>.*?\n(.*?)  // </pure:snapshotValidity>", src, re.S).group(1)
    script = fn + "\nconst cases = " + json.dumps(cases) + ";\nconst out = {};\n" + \
        "for (const k in cases) { out[k] = snapshotValidity(...cases[k], 5); }\nprocess.stdout.write(JSON.stringify(out));\n"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


def _payload(age_s=0.3, fast_progress=0.4, trades_source=0.8, latest=True):
    fast = {"status": "fresh", "progress_age_s": fast_progress, "max_age_s": 3.0,
            "source_age_s": 0.81, "source_max_age_s": 5.0, "clock_bound_s": 0.05}
    trades = {"status": "fresh", "progress_age_s": 0.2, "max_age_s": 30.0,
              "source_age_s": trades_source, "source_max_age_s": 35.0, "clock_bound_s": 0.05}
    return {"served": {"latest": latest, "age_s": age_s},
            "market": {"market": "BTC", "freshness": {"book_fast": fast, "trades": trades,
                                                      "ctx": {"status": "unverified"}}},
            "deck": {"BTC": {"trades_freshness": trades}, "HYPE": {"trades_freshness": dict(trades, status="unverified")}}}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_snapshot_delayed_in_delivery_is_withheld_on_arrival():
    out = _snapshot_validity({
        "prompt": [_payload(), 1000.0, 1050.0],
        "held_12_8s": [_payload(), 1000.0, 13842.0],                 # Codex's run: 12.842 s old when displayed
        "fast_book_lapses_on_screen": [_payload(fast_progress=2.6), 1000.0, 1200.0],
        "source_lapses": [_payload(trades_source=34.0), 1000.0, 2500.0],
        "historical": [_payload(latest=False), 1000.0, 1050.0],
        "no_request_time": [_payload(), None, 1050.0],
    })
    assert out["prompt"]["ok"] is True
    assert out["held_12_8s"]["ok"] is False and "limit 5 s" in out["held_12_8s"]["reason"]
    assert out["fast_book_lapses_on_screen"]["ok"] is False and "BTC book_fast" in out["fast_book_lapses_on_screen"]["reason"]
    assert out["source_lapses"]["ok"] is False and "source time" in out["source_lapses"]["reason"]
    assert out["historical"]["ok"] is False and out["no_request_time"]["ok"] is False


def test_tab_checks_validity_on_arrival_and_while_shown():
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    poll = src[src.index("    const poll = useCallback(function (force) {"):src.index("    }, []);", src.index("    const poll = useCallback"))]
    assert poll.index("const requestedPerf = performance.now();") < poll.index("SDK.fetchJSON(url)")
    assert poll.index("snapshotValidity(data, requestedPerf, performance.now(), LATEST_MAX_S)") < poll.index("setResp(view)")
    assert "snapshotValidity(s.data, s.requestedPerf, performance.now(), LATEST_MAX_S)" in src
    assert "STALE_MS" not in src and "advancedAt" not in src


# --- BLOCKING 3: a late trade re-evaluates the ticks whose windows contain it -------------------------

def test_late_trade_updates_the_displayed_card():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i, dt in enumerate((31, 91, 121)):
        ms.on_trades([trade("BTC", T0 + dt * 1000, 100.0, 1000.0, "B", A, B, i + 1)], i * NS, T0 + dt * 1000)
    card = build(desk, 3 * NS, wall_ms=T0 + 121_200)["markets"]["BTC"]["deck"]["card"]
    assert card["tick_ms"] == T0 + 120_000 and card["numbers"]["trades"]["value"] == 2
    ms.on_trades([trade("BTC", T0 + 100_000, 100.0, 9000.0, "B", A, C, 9)], 4 * NS, T0 + 125_000)
    snap = build(desk, 4 * NS, wall_ms=T0 + 125_200)["markets"]["BTC"]
    card = snap["deck"]["card"]
    assert ms.store.counters["late"] == 1
    assert card["tick_ms"] == T0 + 120_000
    assert card["numbers"]["trades"]["value"] == 3
    assert card["numbers"]["net_flow_usd"]["value"] == 1_100_000.0 and card["numbers"]["volume_usd"]["value"] == 1_100_000.0
    assert card["revised"]["late_trades"] == 1 and card["revised"]["latest_late_trade_ms"] == T0 + 100_000


def _deliver(desk, batches):
    ms = desk.markets["BTC"]
    for i, (arrive_ms, trades) in enumerate(batches):
        ms.on_trades(trades, (i + 10) * NS, arrive_ms)


def _ticks_state(desk):
    d = desk.markets["BTC"].deck
    regime_events = [(e["type"], e["at_ms"], e.get("from"), e.get("to")) for e in d.events if e["type"] == "regime_change"]
    lt = d.last_tick
    return {"confirmed": d.confirmed, "since": d.since_ms, "pending": d.pending, "tick": lt["tick_ms"],
            "metrics": lt.get("metrics"), "live": lt.get("live"), "events": regime_events}


def test_late_delivery_ends_in_the_same_state_as_ordered_delivery():
    rng = random.Random(7)
    trades = []
    for k in range(1, 600):                       # 10 minutes of trades, regime-changing flow
        side = "B" if (k // 120) % 2 == 0 else "A"
        if rng.random() < 0.25:
            side = "A" if side == "B" else "B"
        size = rng.choice((1.0, 5.0, 40.0, 400.0, 2000.0))
        px = 100.0 + ((k // 120) % 2) * 0.5 + rng.random() * 0.1
        trades.append(trade("BTC", T0 + k * 1000, round(px, 4), size, side, A if side == "B" else B, B if side == "B" else A, k))
    ordered = [(t["time"], [t]) for t in trades]
    late_ids = {t["tid"] for t in trades if rng.random() < 0.08}
    delayed = []
    held: list = []
    for t in trades:
        if t["tid"] in late_ids:
            held.append((t["time"] + rng.randint(20, 60) * 1000, t))
            continue
        delayed.append((t["time"], [t]))
        due = [h for h in held if h[0] <= t["time"]]
        for h in due:
            held.remove(h)
            delayed.append((t["time"], [h[1]]))
    a, b = make_desk(), make_desk()
    for desk in (a, b):
        open_segment(desk.markets["BTC"], T0)
    _deliver(a, ordered)
    _deliver(b, delayed)
    b.markets["BTC"].on_trades([h[1] for h in held], 10_000 * NS, T0 + 700_000)   # anything still held
    _deliver(a, [(T0 + 700_000, [trade("BTC", T0 + 700_000, 100.0, 1.0, "B", A, B, 99_999)])])
    _deliver(b, [(T0 + 700_000, [trade("BTC", T0 + 700_000, 100.0, 1.0, "B", A, B, 99_999)])])
    assert b.markets["BTC"].store.counters["late"] > 0
    assert _ticks_state(a) == _ticks_state(b)
