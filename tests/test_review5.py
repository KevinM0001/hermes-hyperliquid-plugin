"""Regression tests for Codex review 5 (commit cd83e6f), one per finding, from its reproductions."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hl_desk import runtime, tools

from helpers import A, B, NS, T0, SnapRT, build, make_desk, open_segment, trade, use_standalone_storage

ROOT = Path(__file__).resolve().parent.parent


# --- BLOCKING 2: delivery time counts, so an answer that arrives too old is withheld at once --------------

def _run_live_account_state(cases: dict) -> dict:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:liveAccountState>.*?\n(.*?)  // </pure:liveAccountState>", src, re.S).group(1)
    script = fn + "\nconst cases = " + json.dumps(cases) + ";\nconst out = {};\n" + \
        "for (const k in cases) { out[k] = liveAccountState(...cases[k]); }\nprocess.stdout.write(JSON.stringify(out));\n"
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_answer_delayed_in_delivery_is_expired_on_arrival():
    fresh = {"status": "fresh", "source_age_s": 0.485, "source_max_age_s": 10.0, "clock_bound_s": 0.749}
    # Codex's run: requested at t=1000 ms, held 12.011 s in delivery, rendered on arrival.
    out = _run_live_account_state({"held": [fresh, 1000.0, 13011.0], "prompt": [fresh, 1000.0, 1300.0]})
    assert out["held"]["expired"] is True and out["held"]["status"] == "expired"
    assert abs(out["held"]["age"] - 12.496) < 1e-9
    assert out["prompt"]["status"] == "fresh"


def test_tab_ages_the_account_from_the_request_and_renders_with_the_current_clock():
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    load = src[src.index("    function loadWallet(req, refresh) {"):src.index("    function openWallet(address) {")]
    assert load.index("const requestedPerf = performance.now();") < load.index("SDK.fetchJSON(url)")
    assert "requested_perf: requestedPerf" in load
    assert "liveAccountState(curFr, d.requested_perf, nowPerf)" in src
    assert "const nowPerf = performance.now();" in src     # read at render time, not from a once-a-second state


# --- BLOCKING 3: an unverified CVD slice has an unknown base, never zero ---------------------------------

def test_unverified_cvd_slice_base_is_null_with_reason():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for k in range(1, 932):                     # one $100 aggressive buy per second through T0+931 s
        ms.on_trades([trade("BTC", T0 + k * 1000, 100.0, 1.0, "B", A, B, k)], k * NS, T0 + k * 1000)
    fresh = build(desk, 931 * NS + NS // 5, wall_ms=T0 + 931_200)
    base = json.loads(tools.run_tool("hl_flow", {"market": "BTC"}, SnapRT(fresh)))["cvd_last_6_minutes"]["slice"]["cumulative_base_usd"]
    assert base == {"value": 57000.0, "unit": "USD"}           # 19 earlier complete buckets x 30 trades x $100
    stale = build(desk, 931 * NS + int(31.2 * NS), wall_ms=T0 + 962_200)
    assert stale["markets"]["BTC"]["freshness"]["trades"]["status"] != "fresh"
    base = json.loads(tools.run_tool("hl_flow", {"market": "BTC"}, SnapRT(stale)))["cvd_last_6_minutes"]["slice"]["cumulative_base_usd"]
    assert base["value"] is None and base["reason"].startswith("trades ")


def test_cvd_slice_base_is_zero_only_when_nothing_precedes_it():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for k in range(1, 61):
        ms.on_trades([trade("BTC", T0 + k * 1000, 100.0, 1.0, "B", A, B, k)], k * NS, T0 + k * 1000)
    snap = build(desk, 60 * NS + NS // 5, wall_ms=T0 + 60_200)
    base = json.loads(tools.run_tool("hl_flow", {"market": "BTC"}, SnapRT(snap)))["cvd_last_6_minutes"]["slice"]["cumulative_base_usd"]
    assert base["value"] == 0.0 and "no complete bucket precedes" in base["note"]


# --- DEFERRED 4: tests that rely on the standalone storage path isolate it, even inside Hermes ------------

def test_standalone_storage_helper_wins_over_an_importable_hermes(monkeypatch, tmp_path):
    hermes_storage = types.ModuleType("plugins.plugin_storage")
    hermes_storage.plugin_data_dir = lambda name: tmp_path / "hermes-profile"
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", hermes_storage)   # as in Hermes's environment
    assert runtime.default_data_dir() == tmp_path / "hermes-profile"
    use_standalone_storage(monkeypatch, tmp_path / "data")
    assert runtime.default_data_dir() == tmp_path / "data"
