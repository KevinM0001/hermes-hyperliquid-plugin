"""Regression tests for Codex h1 review 2 (commit 5bcd153), one per blocking finding, from its reproductions.

B1: a confirmed Flow Deck card whose trades lapse is withheld in exactly the build's own withheld shape (its last
    confirmed regime a string, with its since time and tick), at serving time and in the tab.
B2: node walls use the median of the documented definition (the mean of the two middle notionals for an even count).
B3: hl_market reports the largest returned level per side as a computed field (levels.largest), the same level as
    the chart's line, and the commentary skill names a largest level only from it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from helpers import A, B, T0
from hl_desk import deck
from hl_desk.trades import Trade

ROOT = Path(__file__).resolve().parent.parent


def _confirmed_engine():
    """A Flow Deck engine with ABSORBING confirmed on two ticks (as tests/test_deck.py builds one)."""
    eng = deck.DeckEngine("BTC", 10e6)
    eng.reset_segment(T0)
    rows = [Trade("BTC", T0 + i, 100.0, 5000.0, "B", i, f"h{i}", A, B, i) for i in range(6)]

    class S:
        def window(self, s, e, start_inclusive=True):
            return rows
    eng._tick(S(), T0 + 105_000, None)
    eng._tick(S(), T0 + 120_000, None)
    complete = eng.card(None)
    assert complete["confirmed_regime"]["state"] == "ABSORBING"
    return eng, complete


def test_b1_withheld_card_is_the_builds_own_shape():
    """Codex's crash: the confirmed regime object was put where the renderer expects its name. The serving-time
    withheld card now equals, key for key, the card the build itself makes when trades aren't fresh."""
    from hl_desk.serving_public import withheld_card
    eng, complete = _confirmed_engine()
    reason = "trades stale: no progress for 31.0 s (max 30 s), by the time this was served"
    built = eng.card(reason)
    mine = withheld_card(complete, reason)
    assert mine == built
    assert mine["last_confirmed"]["regime"] == "ABSORBING" and isinstance(mine["last_confirmed"]["regime"], str)
    assert mine["last_confirmed"]["since"] == complete["confirmed_regime"]["since"]
    assert mine["last_confirmed"]["as_of_tick_ms"] == complete["tick_ms"] == built["last_complete_tick_ms"]


def _age_shown(payload: dict, age_s: float) -> dict:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:ageShown>.*?\n(.*?)  // </pure:ageShown>", src, re.S).group(1)
    script = fn + "\nprocess.stdout.write(JSON.stringify(ageShown(" + json.dumps(payload) + ", " + json.dumps(age_s) + ")));\n"
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_b1_the_tab_withholds_a_confirmed_card_in_the_builds_shape():
    """The tab's ageShown, on the chosen market's card and on another market's Flow Deck summary: once the snapshot
    has sat on screen past the screen's allowance (30 s, Kevin, Nov 10), the withheld card has the build's keys, its
    last confirmed regime is the string the renderer draws (RegimeBadge), with the same since time and tick as the
    build's, and every number is null with the reason. Within the allowance the card is kept whole."""
    eng, complete = _confirmed_engine()
    built = eng.card("trades stale")
    trades = {"field": "trades", "status": "fresh", "progress_age_s": 29.5, "max_age_s": 30.0,
              "source_age_s": 29.6, "source_max_age_s": 35.0, "clock_bound_s": 0.05}
    payload = {"served": {"latest": True, "age_s": 0.0},
               "market": {"market": "BTC", "freshness": {"trades": trades}, "deck": {"card": complete},
                          "book": {}, "flow": {"values": {}, "cvd": {"buckets": []}, "large_prints": {}}, "wallets": {}},
               "deck": {"HYPE": {"trades_freshness": trades, "card": dict(complete, market="HYPE")}}}
    kept = _age_shown(payload, 1.0)                                            # within the allowance: whole
    assert kept["market"]["deck"]["card"]["regime"] == complete["regime"] and kept["deck"]["HYPE"]["card"]["numbers"] == complete["numbers"]
    out = _age_shown(payload, 31.0)                                            # past it: withheld in the build's shape
    for card in (out["market"]["deck"]["card"], out["deck"]["HYPE"]["card"]):
        assert set(card) == set(built)
        assert card["last_confirmed"] == built["last_confirmed"]
        assert isinstance(card["last_confirmed"]["regime"], str)
        assert card["regime"]["value"] is None and all(v["value"] is None for v in card["numbers"].values())
        assert card["confirmed_regime"] is None and card["live_classification"] is None


def test_b2_walls_use_the_documented_median():
    """Codex's fixture: four bids at 100 of 0.1, 0.2, 0.3 and 1.4 (notional $10, $20, $30, $140) and one ask at 101.
    The median of the four is $25, the threshold 5 x $25 = $125, so the $140 order is a wall; the upper-middle value
    ($30, threshold $150) had none."""
    from hl_desk.node import panels
    from hl_desk.node.book import NodeBook
    rows = [[1, "B", "100", "0.1", A, T0], [2, "B", "100", "0.2", A, T0], [3, "B", "100", "0.3", A, T0],
            [4, "B", "100", "1.4", B, T0], [5, "A", "101", "1", A, T0]]

    class MF:
        book = NodeBook.from_snapshot(rows)
    ps = panels.PanelState()
    panels._track_walls(MF, ps, T0 + 1000)
    cur = panels.walls(MF, ps)["current"]
    assert [w["oid"] for w in cur] == [4] and cur[0]["notional_usd"] == 140.0
    from hl_desk.util import median
    assert median([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("140")]) == Decimal("25")
    assert median([3, 1, 2]) == 2


def test_b3_largest_level_is_a_computed_field_and_the_skill_uses_it():
    """Codex's demo: the larger ask (28.65934 at 74,975) was in the result, but the narration named a smaller one.
    levels.largest gives each side's largest returned level, by the chart's own rule, and the skill tells the agent
    to name a largest level only from it."""
    from hl_desk import book as bk
    asks = [(74958.0, 10.36, 32), (74960.0, 2.0, 3), (74975.0, 28.65934, 8), (74980.0, 1.0, 1)]
    bids = [(74950.0, 1.0, 2), (74940.0, 5.0, 4), (74930.0, 5.0, 9)]
    lad = bk.ladder_rows(bids, asks)
    assert lad["largest"]["ask"] == {"px": 74975.0, "sz": 28.65934, "api_count": 8, "notional_usd": 2148734.02}
    assert lad["largest"]["bid"]["px"] == 74940.0          # a tie in size goes to the level nearer the top
    skill = (ROOT / "skills" / "hyperliquid-commentary" / "SKILL.md").read_text()
    assert "levels.largest.bid" in skill and "levels.largest.ask" in skill
    assert "Don't rank or compare rows yourself" in skill
