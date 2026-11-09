"""Regression tests for Codex review 4 (commit 364fb76), one per finding, from its reproductions."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hl_desk import clock, runtime

from helpers import A, B, NS, T0, build, make_desk, open_segment, trade

ROOT = Path(__file__).resolve().parent.parent


# --- BLOCKING 2: an open wallet window stops showing account values once they are older than the limit ----

def _live_account_state_js() -> str:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    m = re.search(r"// <pure:liveAccountState>.*?\n(.*?)  // </pure:liveAccountState>", src, re.S)
    assert m, "liveAccountState markers missing from app.js"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_open_wallet_account_expires_after_its_limit():
    fresh = {"status": "fresh", "source_age_s": 0.465, "source_max_age_s": 10.0, "clock_bound_s": 0.05}
    cases = {
        "at_open": [fresh, 1000.0, 1000.0],
        "after_9s": [fresh, 1000.0, 10000.0],
        "after_15s": [fresh, 1000.0, 16019.0],           # Codex's run: 15.019 s after the request
        "no_receipt_time": [fresh, None, 5000.0],
        "delayed_stays_delayed": [{"status": "delayed", "reason": "old", "source_age_s": 40}, 1000.0, 1000.0],
    }
    script = _live_account_state_js() + "\nconst cases = " + json.dumps(cases) + ";\n" + \
        "const out = {}; for (const k in cases) { out[k] = liveAccountState(...cases[k]); }\n" + \
        "process.stdout.write(JSON.stringify(out));\n"
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(r.stdout)
    assert out["at_open"]["status"] == "fresh" and out["at_open"]["expired"] is False
    assert out["after_9s"]["status"] == "fresh" and abs(out["after_9s"]["age"] - 9.465) < 1e-9
    assert out["after_15s"]["expired"] is True and out["after_15s"]["status"] == "expired"
    assert "15.5 s old" in out["after_15s"]["reason"] and "no longer shown as current" in out["after_15s"]["reason"]
    assert out["no_receipt_time"]["expired"] is True
    assert out["delayed_stays_delayed"] == {"status": "delayed", "reason": "old", "age": 40, "expired": False}


def test_wallet_window_withholds_expired_values_and_can_fetch_again():
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    drill = src[src.index("  function WalletDrill(props) {"):src.index("    return h(\"div\", { className: \"hd-drill-backdrop\"")]
    assert "setInterval(function () { setTick(function (n) { return n + 1; }); }, 1000)" in drill
    assert "const nowPerf = performance.now();" in drill
    assert "live.expired ? { value: null, reason: live.reason } : metric" in drill
    assert "expired, not current" in drill and "Fetch account again" in drill
    assert "requested_perf: requestedPerf" in src


# --- BLOCKING 3: the rolling 15-minute cutoff never truncates a candle -----------------------------------

def test_oldest_candle_holds_every_trade_of_its_interval():
    desk = make_desk()
    ms = desk.markets["BTC"]
    open_segment(ms, T0)
    for i, (dt, px, sz) in enumerate(((30, 90, 10), (31, 100, 1), (44, 110, 1), (931, 100, 1))):
        ms.on_trades([trade("BTC", T0 + dt * 1000, px, sz, "B", A, B, 100 + i)], i * NS, T0 + dt * 1000)
    candles = build(desk, 4 * NS)["markets"]["BTC"]["chart"]["candles"]
    first = candles[0]
    assert first["t"] == T0 + 30_000                     # the boundary at or before watermark - 15 min
    assert (first["open"], first["high"], first["low"], first["close"]) == (90.0, 110.0, 90.0, 110.0)
    assert first["volume_usd"] == 1110.0 and first["complete"] is True
    assert candles[-1]["t"] == T0 + 930_000 and candles[-1]["complete"] is False


# --- DEFERRED 4: reading the clock bound runs no external command ----------------------------------------

def test_clock_bound_lookup_launches_no_subprocess():
    seen, armed = [], [True]

    def hook(event, args):
        if armed[0] and event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"):
            seen.append((event, args))

    sys.addaudithook(hook)                  # audit hooks can't be removed; this one is disarmed below
    try:
        clock._CACHE.update(at=-1e9, value=None, basis=None)
        clock.os_bound()
    finally:
        armed[0] = False
    assert seen == []


# --- DEFERRED 5: inside Hermes, the profile directory always wins over HL_DESK_DATA_DIR -------------------

def test_data_dir_override_is_ignored_inside_hermes(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profile" / "plugin-data" / "hyperliquid-desk"
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: profile_dir
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setenv("HL_DESK_DATA_DIR", str(tmp_path / "override"))
    assert runtime.default_data_dir() == profile_dir
    assert not (tmp_path / "override").exists()


def test_data_dir_override_outside_hermes_only(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)     # import fails, as outside Hermes
    monkeypatch.setenv("HL_DESK_DATA_DIR", str(tmp_path / "override"))
    assert runtime.default_data_dir() == tmp_path / "override"
    monkeypatch.delenv("HL_DESK_DATA_DIR")
    with pytest.raises(RuntimeError, match="not running inside Hermes"):
        runtime.default_data_dir()
