"""Kevin's review of Nov 11, the Desk page cards: one fold toggle for every card that folds (What the node sees, Who
is aggressive, Flow notes, Alerts), the stored-choice rule behind it, and the regrouped figures at the top of Who is
aggressive. Structural checks on the tab source and its stylesheet (as tests/test_review4.py does for the wallet
window) and the tab's pure foldOpen run under node (as tests/test_h1_review1.py runs ageShown)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "dashboard" / "src" / "app.js").read_text()
CSS = (ROOT / "dashboard" / "dist" / "style.css").read_text()


def _component(name: str) -> str:
    """The text of one top-level tab function: from its definition to the next blank line that a top-level
    definition or comment follows (the tab separates its functions that way)."""
    start = APP.index(f"  function {name}(")
    m = re.compile(r"\n\n  (?:function |const |//)").search(APP, start)
    return APP[start:m.start()] if m else APP[start:]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_fold_open_rule_stored_choice_wins_else_the_default():
    fn = re.search(r"// <pure:foldOpen>.*?\n(.*?)  // </pure:foldOpen>", APP, re.S).group(1)
    cases = {"opened": ["open", False], "closed": ["closed", True], "no_choice": [None, True],
             "no_choice_closed_card": [None, False], "garbage": ["yes", True], "empty": ["", False]}
    script = (fn + "\nconst cases = " + json.dumps(cases) + ";\n"
              "const out = {}; for (const k in cases) out[k] = foldOpen(...cases[k]);\n"
              "process.stdout.write(JSON.stringify(out));\n")
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(r.stdout) == {"opened": True, "closed": False, "no_choice": True,
                                    "no_choice_closed_card": False, "garbage": True, "empty": False}


def test_one_fold_toggle_rendered_by_the_card_frame():
    """The toggle is one component, rendered by Panel beside the badges; the four folding cards hand their fold to
    Panel and none draws a toggle of its own. It reads COLLAPSE or EXPAND and is a bordered chip in the stylesheet."""
    panel = _component("Panel")
    assert APP.count("h(FoldButton, {") == 1 and "h(FoldButton, {" in panel
    assert ('h("div", { className: "hd-badges" },\n'
            '        folds ? h(FoldButton, { open: props.open, onToggle: props.onToggle }) : null,\n'
            '        props.badges || null)') in panel
    fold = _component("FoldButton")
    assert 'open ? "COLLAPSE" : "EXPAND"' in fold and '"aria-expanded": open' in fold and 'className: "hd-fold-btn"' in fold
    assert 'open ? "collapse" : "expand"' not in APP
    for name in ("NodeBrief", "FlowPanel", "FlowNotes", "DeckPanel"):
        body = _component(name)
        assert "open: open, onToggle: props.onToggle" in body, name
        assert "hd-fold-btn" not in body and "FoldButton" not in body, name
    rule = re.search(r"\.hd-fold-btn \{([^}]*)\}", CSS).group(1)
    assert "border: 1px solid" in rule and "text-transform: uppercase" in rule and "var(--hd-mono)" in rule


def test_each_card_keeps_its_choice_in_local_storage_guarded():
    hook = _component("useFold")
    assert "try { stored = window.localStorage.getItem(key); } catch (e)" in hook
    assert 'try { window.localStorage.setItem(key, o ? "closed" : "open"); } catch (e)' in hook
    assert "return foldOpen(stored, defaultOpen);" in hook
    app = _component("AppInner")
    assert 'useFold("hyperliquid-desk.brief", false)' in app                 # the node card keeps its default (folded)
    for key in ("flow", "notes", "alerts"):
        assert f'useFold("hyperliquid-desk.fold.{key}", true)' in app        # the other three start open
    assert "open: briefOpen, onToggle: toggleBrief" in app
    assert "h(FlowPanel, { m: m, open: flowOpen, onToggle: toggleFlow })" in app
    assert "h(FlowNotes, { resp: resp, m: m, open: notesOpen, onToggle: toggleNotes })" in app
    assert "h(DeckPanel, { resp: resp, open: alertsOpen, onToggle: toggleAlerts })" in app


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to read the tab's table")
def test_who_is_aggressive_figures_are_net_volume_trades_then_buy_sell():
    rows = re.search(r"const FLOW_ROWS = (\[.*?\n  \]);", APP, re.S).group(1)
    r = subprocess.run(["node", "-"], input="process.stdout.write(JSON.stringify(" + rows + "));",
                       capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(r.stdout) == [[["net", "net"], ["volume", "volume"], ["trade_count", "trades"]],
                                    [["buy_aggressive_notional", "buy aggressor"], ["sell_aggressive_notional", "sell aggressor"]]]
    fig = _component("FlowFigures")
    assert "(props.open ? FLOW_ROWS : FLOW_ROWS.slice(0, 1))" in fig                      # folded: the first row only
    assert "compact: true" in fig and 'className: "hd-note hd-flow-why"' in fig             # reasons written once below
    panel = _component("FlowPanel")
    assert "h(FlowFigures, { v: v, open: open })" in panel
    assert 'open ? h(Fragment, null,\n      h("div", { className: "hd-window" }' in panel   # the rest needs EXPAND
    grid = re.search(r"\.hd-flow-top \{([^}]*)\}", CSS).group(1)
    assert "display: grid" in grid and "grid-template-columns" in grid


def test_folded_notes_and_alerts_keep_one_line():
    notes = _component("FlowNotes")
    assert "const stateShown = open ? state : (shown.length ? [] : state.slice(0, 1));" in notes
    assert "const noteShown = open ? shown : shown.slice(0, 1);" in notes
    assert 'listCount("newest", noteShown.length, "notes", notes.length, "in this snapshot")' in notes
    deck = _component("DeckPanel")
    assert "events.slice(0, open ? 40 : 1)" in deck
    assert 'open ? h("div", { className: "hd-events-head" }' in deck
