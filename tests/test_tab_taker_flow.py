"""The tab's side of the Taker flow layer on every interval (Kevin's review of Nov 11, item 3; dashboard/src/app.js):
the chart draws one bar per candle from the plugin's per-candle block (the snapshot's chart.taker_flow for 15 s, the
candles route's taker_flow otherwise) with no 15m-only gate, and the tab's own aging (ageShown) treats chart.taker_flow
as the backend does: the bars stay, the flow family's state ages.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from node_helpers import client, surfaces  # noqa: F401  (fixture)
from test_node_flow_map import A, SP, T0, Feed, _fill
from test_node_taker_flow import _public_candles

ROOT = Path(__file__).resolve().parent.parent


def _age_shown(payload: dict, age_s: float) -> dict:
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    fn = re.search(r"// <pure:ageShown>.*?\n(.*?)  // </pure:ageShown>", src, re.S).group(1)
    script = fn + "\nprocess.stdout.write(JSON.stringify(ageShown(" + json.dumps(payload) + ", " + json.dumps(age_s) + ")));\n"
    r = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_the_tab_ages_the_15s_bars_freshness_and_keeps_the_bars(surfaces):
    """The tab's own aging (ageShown) treats chart.taker_flow as the backend does: on screen past the allowance the
    flow family it was built on reads BEHIND with the delay counted up, the bars stay; within the allowance the state
    is as served with the age counted."""
    plugin = surfaces([SP])
    plugin.desk.collector = {"state": "connected", "note": "offline test feed"}
    c = plugin.attach(client(cfg={"delayed_after_ms": 2_000, "stale_after_ms": 4_000}))
    f = Feed(c, SP)
    f.hello()
    f.wm(T0)
    _public_candles(plugin, SP, T0, 4)
    f.execs(T0 + 20_000, [_fill(1, "open_long", T0 + 20_000, user=A)])
    for k in range(1, 70):
        f.wm(T0 + 1000 * k)
    plugin.read(SP, T0 + 69_100)
    sid = plugin.last["snapshot"]["snapshot_id"]
    payload = plugin.http.get("/api/plugins/hyperliquid-desk/snapshot", params={"market": SP, "snapshot_id": sid}).json()
    tf0 = payload["market"]["chart"]["taker_flow"]
    assert tf0["available"] is True and tf0["freshness"]["state"] == "LIVE" and any(b["fills"] for b in tf0["bars"])
    soon = _age_shown(payload, 5.0)["market"]["chart"]["taker_flow"]
    assert soon["freshness"]["state"] == "LIVE" and soon["freshness"]["aged_by_ms"] == 5_000 and soon["bars"] == tf0["bars"]
    late = _age_shown(payload, 31.0)["market"]["chart"]["taker_flow"]     # 31 s past the 4 s stale limit
    assert late["freshness"]["state"] == "STALE" and "while on screen" in late["freshness"]["reason"]
    assert late["bars"] == tf0["bars"] and late["available"] is True
    assert late["freshness"]["state"] == _age_shown(payload, 31.0)["market"]["node"]["live"]["families"]["flow"]["state"]


def test_the_chart_draws_the_layer_on_every_interval_from_the_plugins_block():
    """The tab's chart reads the plugin's per-candle block (the snapshot's for 15 s, the route's otherwise) and no longer
    gates the layer on the 15m interval or reads the flow map's buckets; the toggle names what the bars are."""
    src = (ROOT / "dashboard" / "src" / "app.js").read_text()
    chart = src[src.index("function PriceChart"):src.index("// ------------------------------------------------------------------ node mode")]
    assert "15m only" not in chart and "flowOff" not in chart and "fm.buckets" not in chart
    assert 'const flowSrc = fromRoute ? (cd && cd.state === "ok" && cd.data ? (cd.data.taker_flow || null) : null) : (chart.taker_flow || null);' in chart
    assert "net taker flow per candle from the node's fills" in chart
    assert chart.index("a.series.setData(candles);") < chart.index("a.nflow.setData(data);")     # bars drawn after the candles stand
    assert "REGISTRY.register(PLUGIN, App)" in src
