"""Regression tests for Codex h1 review 1 (commit 56c65e3), one per blocking finding, from its reproductions.

B1: a latest public tool read re-judges each public freshness status at its served age, and withholds what the build
    gates on a lapsed field; a historical read (by ID) stays as of its build time.
B2: the tab ages the snapshot it shows the same way (app.js ageShown): lapsed values and statuses are withheld in
    place, with the reason, and the page keeps its layout.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import B, C, NS, T0, book_msg, open_segment, trade
from node_helpers import surfaces  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
MS = 1_000_000


def _feed(plugin):
    """BTC: trades every second from T0+1 s to T0+20 s, a 5-level fast book and a 20-level book at T0+20.1 s."""
    ms = plugin.desk.markets["BTC"]
    open_segment(ms, T0)
    for k in range(1, 21):
        ms.on_trades([trade("BTC", T0 + k * 1000, 100.0, 1.0, "B" if k % 2 else "A", B, C, k)], k * 1000 * MS, T0 + k * 1000)
    five = [(100.0 - i * 0.1, 1.0, 2) for i in range(5)], [(100.1 + i * 0.1, 1.0, 3) for i in range(5)]
    full = [(100.0 - i * 0.1, 1.0, 2) for i in range(20)], [(100.1 + i * 0.1, 1.0, 3) for i in range(20)]
    ms.on_book(book_msg("BTC", T0 + 20_100, *five), 20_100 * MS, T0 + 20_100)
    ms.on_book(book_msg("BTC", T0 + 20_100, *full), 20_100 * MS, T0 + 20_100)
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    return ms


def _publish(plugin, at_ms):
    snap = plugin.desk.build_snapshot(at_ms * MS, T0 + at_ms)
    plugin.rt.store.publish(snap, frozen=plugin.desk.frozen_last)
    return snap


def _read(plugin, monkeypatch, tool, at_ms, **args):
    """The tool through chat and the tab's tool route, at at_ms on both clocks (as the snapshots were stamped)."""
    from hl_desk import runtime, tools
    monkeypatch.setattr(runtime.time, "time", lambda: (T0 + at_ms) / 1000)
    monkeypatch.setattr(runtime.time, "monotonic_ns", lambda: at_ms * MS)
    args = {"market": "BTC", **args}
    via_chat = json.loads(tools.run_tool(tool, args, plugin.rt))
    via_tab = plugin.http.get(f"/api/plugins/hyperliquid-desk/tool/{tool}", params=args).json()
    strip = lambda r: {k: v for k, v in r.items() if k not in ("served", "historical")}
    assert strip(via_chat) == strip(via_tab)
    return via_chat


def test_b1_latest_public_reads_are_judged_at_serving_time(surfaces, monkeypatch):
    """Codex's reproduction: the fast book is fresh at build time with a progress age of 2.9 s (max 3 s); read as the
    latest snapshot 1 s later, it is stale with the reason, and the top of book is withheld (never 99.9 as current),
    while the 20-level book (max 15 s) and trades stay fresh with their values. A historical read keeps them."""
    plugin = surfaces(["BTC"])
    _feed(plugin)
    snap = _publish(plugin, 23_000)                     # 2.9 s after the books
    built = snap["markets"]["BTC"]["freshness"]["book_fast"]
    assert built["status"] == "fresh" and abs(built["progress_age_s"] - 2.9) < 1e-6
    assert snap["markets"]["BTC"]["book"]["top"]["best_bid"]["value"] == 100.0
    m = _read(plugin, monkeypatch, "hl_market", 24_000)
    assert m["ok"] and m["served"]["latest"] is True and abs(m["served"]["age_s"] - 1.0) < 1e-6 and "historical" not in m
    fast = m["status"]["fields"]["book_fast"]
    assert fast["status"] == "stale" and "by the time this was served" in fast["reason"] and abs(fast["progress_age_s"] - 3.9) < 1e-6
    assert m["top_of_book"]["best_bid"]["value"] is None and "stale" in m["top_of_book"]["best_bid"]["reason"]
    assert all(m["top_of_book"][k]["value"] is None for k in ("best_ask", "mid", "spread", "spread_bps"))
    assert m["status"]["fields"]["book_full"]["status"] == "fresh" and m["levels"]["bids"]   # not lapsed: kept
    assert any(u["status"] == "stale" and "by the time this was served" in u["reason"] for u in m["unavailable"])
    hist = _read(plugin, monkeypatch, "hl_market", 24_000, snapshot_id=snap["snapshot_id"])
    assert hist["historical"] and hist["status"]["fields"]["book_fast"]["status"] == "fresh"
    assert hist["top_of_book"]["best_bid"]["value"] == 100.0


def test_b1_lapsed_trades_withhold_flow_deck_and_wallets(surfaces, monkeypatch):
    """Trades fresh at build time (progress age 29.5 s of BTC's 30 s) and read 1 s later: hl_flow's values, the
    Flow Deck card (hl_alerts) and the wallet panel (hl_wallets) are withheld with the reason, as the build withholds
    them; the tape, a record of what was received, is kept."""
    plugin = surfaces(["BTC"])
    _feed(plugin)
    snap = _publish(plugin, 49_500)
    assert snap["markets"]["BTC"]["freshness"]["trades"]["status"] == "fresh"
    assert snap["markets"]["BTC"]["flow"]["values"]["volume"]["value"] > 0
    f = _read(plugin, monkeypatch, "hl_flow", 50_500)
    tr = f["status"]["fields"]["trades"]
    assert tr["status"] == "stale" and "by the time this was served" in tr["reason"]
    assert all(v["value"] is None and "trades stale" in v["reason"] for v in f["values"].values())
    assert f["cvd_last_6_minutes"]["reason"] and all(b["delta_usd"] is None for b in f["cvd_last_6_minutes"]["buckets"])
    assert f["large_prints"]["count"] is None and f["tape"]
    a = _read(plugin, monkeypatch, "hl_alerts", 50_500)
    card = a["cards"]["BTC"]["card"]
    assert card["regime"]["value"] is None and all(v["value"] is None for v in card["numbers"].values())
    w = _read(plugin, monkeypatch, "hl_wallets", 50_500, window_min=5)
    assert w["panel"]["buyers"] is None and "trades stale" in w["panel"]["reason"]
    st = _read(plugin, monkeypatch, "hl_status", 50_500)
    assert st["markets"]["BTC"]["freshness"]["trades"]["status"] == "stale"


# --- B2: the tab -----------------------------------------------------------------------------------

def _age_shown(payload: dict, age_s: float) -> dict:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:ageShown>.*?\n(.*?)  // </pure:ageShown>", src, re.S).group(1)
    script = fn + "\nprocess.stdout.write(JSON.stringify(ageShown(" + json.dumps(payload) + ", " + json.dumps(age_s) + ")));\n"
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_b2_the_tab_withholds_lapsed_values_in_place(surfaces):
    """Codex's reproduction in the tab: a real tab payload (the snapshot route's answer), held 14 s. The tab's own
    ageShown turns the fast book stale with the reason and withholds the top of book and its price lines; past 15 s
    the 20-level book's ladder and depth, past 30 s the trades-built flow values, card and wallet panels, in the
    shapes the panels already render as unavailable; at 0.5 s nothing is withheld (fresh ages
    advance). The Python and tab rules agree."""
    from hl_desk.serving_public import age_market
    plugin = surfaces(["BTC"])
    _feed(plugin)
    snap = _publish(plugin, 20_300)
    payload = plugin.http.get("/api/plugins/hyperliquid-desk/snapshot",
                              params={"market": "BTC", "snapshot_id": snap["snapshot_id"]}).json()
    m0 = payload["market"]
    assert m0["freshness"]["book_fast"]["status"] == "fresh" and m0["book"]["top"]["best_bid"]["value"] == 100.0
    same = _age_shown(payload, 0.5)
    assert same["market"]["book"] == m0["book"]                              # nothing lapsed: values kept
    for k, f in m0["freshness"].items():                                     # statuses kept, fresh ages as of now
        g = same["market"]["freshness"][k]
        assert g["status"] == f["status"]
        if f["status"] == "fresh":
            assert abs(g["progress_age_s"] - (f["progress_age_s"] + 0.5)) < 1e-6 and g["aged_by_s"] == 0.5
    late = _age_shown(payload, 14.0)["market"]
    fr = late["freshness"]
    assert fr["book_fast"]["status"] == "stale" and "while on screen" in fr["book_fast"]["reason"]
    assert late["book"]["top"]["best_bid"]["value"] is None and "stale" in late["book"]["top"]["best_bid"]["reason"]
    assert not [ln for ln in late["chart"]["price_lines"] if ln["kind"] in ("best_bid", "best_ask")]
    assert fr["book_full"]["status"] == "fresh" and late["book"]["ladder"]["bids"]           # 15 s: not lapsed yet
    assert fr["trades"]["status"] == "fresh" and late["flow"]["values"]["volume"]["value"] is not None   # 30 s: not lapsed
    full = _age_shown(payload, 15.5)["market"]
    assert full["freshness"]["book_full"]["status"] == "stale" and full["book"]["ladder"]["bids"] == []
    assert full["book"]["ladder"]["reason"] and full["book"]["depth"]["observed"]["full precision"]["reason"]
    later = _age_shown(payload, 31.0)["market"]
    assert later["freshness"]["trades"]["status"] == "stale"
    assert all(v["value"] is None for v in later["flow"]["values"].values())
    assert later["deck"]["card"]["regime"]["value"] is None
    assert all(p["buyers"] is None and p["reason"] for p in later["wallets"].values())
    # the backend's serving rule gives the same statuses and withheld top of book at the same age
    py = age_market(m0, 14.0, "while on screen")
    assert {k: v["status"] for k, v in py["freshness"].items()} == {k: v["status"] for k, v in fr.items()}
    assert py["book"]["top"]["best_bid"]["value"] is None and py["book"]["ladder"]["bids"] == late["book"]["ladder"]["bids"]
    assert [ln["kind"] for ln in py["chart"]["price_lines"]] == [ln["kind"] for ln in late["chart"]["price_lines"]]


def test_b2_the_app_shows_the_aged_view_on_arrival_and_while_shown():
    """The app renders ageShown's view of the snapshot, on arrival and every second while it stays on screen (and
    only re-renders when a status changes); it never blanks the page for a delay."""
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    poll = src[src.index("    const poll = useCallback(function (force) {"):src.index("    }, []);", src.index("    const poll = useCallback"))]
    assert poll.index("const view = ageShown(data, shownAge(data, requestedPerf));") < poll.index("setResp(view);")
    tick = src[src.index("    // The snapshot on screen keeps aging:"):]
    tick = tick[:tick.index("}, []);")]
    assert "setResp(ageShown(s.data, shownAge(s.data, s.requestedPerf)));" in tick
    assert "setResp(null)" not in tick
