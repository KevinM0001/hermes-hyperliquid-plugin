"""Metadata and fixture checks for each market (Handoff 1 step 1)."""

from __future__ import annotations

import pytest

from helpers import recorded_records, recorded_replayer, registry

MARKETS = ["BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"]


@pytest.mark.parametrize("market", MARKETS)
def test_metadata_check_passes(market):
    reg = registry()
    res = reg.check(market)
    assert res["supported"] is True, res
    assert res["meta"]["dex"] == ("xyz" if market.startswith("xyz:") else "")
    assert res["meta"]["sz_decimals"] >= 0


@pytest.mark.parametrize("market,why", [("NOTACOIN", "not in the meta universe"),
                                        ("xyz:NOPE", "not in the meta universe"),
                                        ("zzz:BTC", "not in perpDexs")])
def test_unknown_markets_are_unsupported(market, why):
    res = registry().check(market)
    assert res["supported"] is False and why in res["reason"]


def test_perp_dexs_include_default_and_xyz():
    reg = registry()
    assert reg.dexes[0] == "" and "xyz" in reg.dexes


@pytest.mark.parametrize("market", MARKETS)
def test_fixture_check(market):
    """The recorded public feed for this market has every channel, and replays to a fresh, non-null book."""
    seen = set()
    for r in recorded_records():
        m = r.get("msg") or {}
        ch = m.get("channel")
        d = m.get("data")
        if ch == "trades" and d and d[0]["coin"] == market:
            seen.add("trades")
        elif ch in ("l2Book", "bbo", "activeAssetCtx") and d.get("coin") == market:
            seen.add(ch if ch != "l2Book" else f"l2Book:{len(d['levels'][0])}")
        elif r.get("http") == "l2Book" and r["request"]["coin"] == market:
            seen.add(f"info:{r['request']['nSigFigs']}")
    assert {"trades", "bbo", "activeAssetCtx", "l2Book:5", "info:4", "info:3"} <= seen
    assert any(s.startswith("l2Book:") and s != "l2Book:5" for s in seen)
    rp = recorded_replayer([market])
    rp.run_until(rp.records[len(rp.records) // 4]["recv_mono_ns"])
    ms = rp.snapshot()["markets"][market]
    assert ms["freshness"]["book_fast"]["status"] == "fresh"
    assert ms["book"]["top"]["best_bid"]["value"] is not None
    assert ms["book"]["top"]["best_bid"]["value"] < ms["book"]["top"]["best_ask"]["value"]
    assert ms["provenance"]["mode"] == "REPLAY"
