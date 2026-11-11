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


# ---- the user's second look at the Desk page (review of Nov 11): three small fixes

def test_folded_who_is_aggressive_fills_its_grid_rows():
    """Folded, Who is aggressive keeps the two grid rows it spans on the wide layout, so its box reaches the Watch
    card instead of leaving the right column blank; Flow notes folded still sits at the top of its own row."""
    assert ".hd-row-m > .hd-p-flow { grid-column: 2; grid-row: 2 / 4;" in CSS
    assert "align-items: stretch" in re.search(r"\.hd-row-m \{([^}]*)\}", CSS).group(1)
    assert ".hd-p-flow.hd-folded" not in CSS
    assert ".hd-row-m > .hd-p-notes.hd-folded { align-self: start; }" in CSS


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_wallet_split_window_sentence_has_one_shape_every_poll():
    """The sentence under the split's title: the plugin's label when the node's window is behind the panel's, else the
    same sentence built in the tab and ending "the panel's own window"; nothing without a usable window."""
    fn = re.search(r"// <pure:splitWindowLabel>.*?\n(.*?)  // </pure:splitWindowLabel>", APP, re.S).group(1)
    t0 = 1794416414730                                   # 2026-11-11T17:00:14.730Z
    behind = "these wallets' node split over the node's last 5 min, 17:00:14 to 17:05:14 UTC, 3 s behind the panel's window"
    cases = {"behind": {"available": True, "as_of_note": "x", "window_label": behind,
                        "window": {"start_exclusive_ms": t0, "end_ms": t0 + 300_000}},
             "own": {"available": True, "as_of_note": None, "window_label": None,
                     "window": {"start_exclusive_ms": t0, "end_ms": t0 + 300_000}},
             "own_short": {"available": True, "window": {"start_exclusive_ms": t0, "end_ms": t0 + 90_000}},
             "no_window": {"available": True}, "half_window": {"available": True, "window": {"end_ms": t0}},
             "unavailable": {"available": False, "reason": "r", "window_label": behind}, "absent": None}
    script = (fn + "\nconst cases = " + json.dumps(cases) + ";\n"
              "const out = {}; for (const k in cases) out[k] = splitWindowLabel(cases[k]);\n"
              "process.stdout.write(JSON.stringify(out));\n")
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(r.stdout) == {
        "behind": behind,
        "own": "these wallets' node split over the node's last 5 min, 17:00:14 to 17:05:14 UTC, the panel's own window",
        "own_short": "these wallets' node split over the node's last 90 s, 17:00:14 to 17:01:44 UTC, the panel's own window",
        "no_window": None, "half_window": None, "unavailable": None, "absent": None}


def test_wallet_split_header_keeps_the_chip_and_the_provenance_on_their_lines():
    """Line 1: NODE, the title and its parenthesis, the flow chip pinned at the right; line 2: the window sentence
    (always, through splitWindowLabel) then the provenance. Both lines refuse to wrap, so no tile changes line
    between polls; a long title or sentence truncates with an ellipsis instead."""
    split = _component("WalletSplit")
    assert "const label = splitWindowLabel(s);" in split
    assert 'h("div", { className: "hd-wt-title hd-wsplit-head" },' in split
    assert 'h("div", { className: "hd-wsplit-line" },' in split and 'h("div", { className: "hd-wsplit-line hd-wsplit-sub" },' in split
    assert split.count("h(FamChip, {") == 1 and split.index("h(FamChip, {") < split.index("hd-wsplit-sub")
    assert 'h("span", { className: "hd-dim hd-wsplit-win", title: s.as_of_note || label }, label)' in split
    assert split.index("hd-wsplit-win") > split.index("hd-wsplit-sub") and split.index("hd-wsplit-prov") > split.index("hd-wsplit-win")
    assert '" \\u00b7 " + s.window_label' not in split                      # the old trailing clause on line 1 is gone
    for cls in ("hd-wsplit-line", "hd-wsplit-name", "hd-wsplit-win"):
        rule = re.search(r"\." + cls + r" \{([^}]*)\}", CSS).group(1)
        assert "white-space: nowrap" in rule or "flex-wrap: nowrap" in rule, cls
    assert "text-overflow: ellipsis" in re.search(r"\.hd-wsplit-name \{([^}]*)\}", CSS).group(1)
    assert "flex: none" in re.search(r"\.hd-wsplit-prov \{([^}]*)\}", CSS).group(1)


def test_the_footer_drops_the_two_sentences():
    assert "Unavailable values show their reason, never 0." not in APP
    assert "A panel built on the node book shows INVALID while the book is invalid or crossed." not in APP
    assert 'h("span", { className: "hd-dim" }, "Descriptive public data, not trade advice.")' in APP
