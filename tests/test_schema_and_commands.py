"""The versioned schema is complete, /hl never starts anything by itself, and hl_deck's log format is compatible."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from hl_desk import commands, deck, runtime as rt_mod, schema
from hl_desk.collector import THREAD_NAME

from helpers import use_standalone_storage

REQUIRED = {"formula", "units", "inputs", "side_convention", "window", "reset_rule", "completeness_rule"}


def test_schema_version_and_every_metric_fully_defined():
    s = schema.load()
    assert re.fullmatch(r"\d+\.\d+\.\d+", s["version"])
    for name, m in s["metrics"].items():
        missing = REQUIRED - set(m) - ({"window"} if "window_s" in m else set())
        assert not missing, (name, missing)
    for name in ("deck.impact", "deck.pressure", "flow.large_print", "wallets.ranked_list"):
        assert "threshold" in s["metrics"][name]
    burst = s["alerts"]["burst"]
    assert {"formula", "threshold", "units", "inputs", "side_convention", "window", "reset_rule", "completeness_rule"} <= set(burst)
    assert burst["threshold"]["min_notional_usd_by_market"]["BTC"] == 10_000_000
    states = {r["state"] for r in s["regimes"]["rules"]}
    for st in ("QUIET", "THIN", "ABSORBING", "BALANCED", "TRENDING-UP", "TRENDING-DOWN"):
        assert st in states
    assert "UNRESOLVED" not in states and "UNRESOLVED" not in s["regimes"]["meanings"]
    assert set(s["freshness"]["fields"]) >= {"l2Book", "trades", "bbo", "activeAssetCtx", "clock_skew"}


def test_hl_command_reports_and_never_autostarts(monkeypatch, tmp_path):
    import threading
    use_standalone_storage(monkeypatch, tmp_path)
    rt_mod._slot.reset()
    out = commands.handle_hl("")
    assert "collector: stopped" in out and "/hl start" in out
    assert commands.handle_hl("status") == out
    assert commands.handle_hl("help").startswith("Usage: /hl")
    assert commands.handle_hl("bogus").startswith("Usage: /hl")
    assert not [t for t in threading.enumerate() if t.name == THREAD_NAME]
    assert list(tmp_path.iterdir()) == []
    rt_mod._slot.reset()


# The deck's own output log exists only on the author's machine; point HL_DECK_LIVE_LOG at a copy of it.
LOG = Path(os.environ.get("HL_DECK_LIVE_LOG", "/nonexistent/deck-live.log"))
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.mark.skipif(not LOG.exists(), reason="set HL_DECK_LIVE_LOG to a copy of hl_deck's output log")
def test_deck_live_log_is_format_compatible():
    """Compatibility only (values come from node fills and aren't reproducible on public data):
    every regime name and event line in hl_deck's log maps onto this plugin's vocabulary and event fields."""
    known = {"QUIET", "THIN", "SPARSE", "BALANCED", "ABSORBING", "VACUUM-UP", "VACUUM-DOWN", "PULLED-OFFERS",
             "PULLED-BIDS", "TRENDING-UP", "TRENDING-DOWN"}
    ev = re.compile(r"^\s+(\d\d:\d\d:\d\d)\s+(\S+)\s+([A-Z-]+)\s+->\s+([A-Z-]+)\s+\(held\s+(\d+)s\)")
    hdr = re.compile(r"^\s{2}(BTC|HYPE)\s+[0-9.]+\s+([A-Z-]+)\s+(?:\d\d:\d\d:\d\dZ\s+)?\((\d+)s\)")
    seen_states, events = set(), 0
    with LOG.open(errors="replace") as fh:
        for i, line in enumerate(fh):
            if i > 400_000:
                break
            line = ANSI.sub("", line)
            m = ev.match(line)
            if m:
                events += 1
                seen_states |= {m.group(3), m.group(4)}
                ours = f"{m.group(3)} -> {m.group(4)} (held {int(m.group(5))}s)"
                assert re.fullmatch(r"[A-Z-]+ -> [A-Z-]+ \(held \d+s\)", ours)
                continue
            m = hdr.match(line)
            if m:
                seen_states.add(m.group(2))
    assert events > 0
    assert seen_states <= known, seen_states - known
    assert known <= set(schema.load()["regimes"]["meanings"])
