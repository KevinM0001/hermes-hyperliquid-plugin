"""Node-mode bursts: the burst calibration's rule on node fills, its thresholds and its rank (Kevin's choice A).

The rule's cases are PLAN.md's own ("The burst rule", its tests), on constructed inputs; ranks are checked
against counts made here from the reference file, never against the module's helpers.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from hl_desk.config import from_settings
from hl_desk.node import calibration as cal_mod
from hl_desk.node.bursts import NodeBurstRule

A = "0x" + "a" * 40
B = "0x" + "b" * 40
T = 1_800_000_000_000
THR = 100_000.0


def ex(tid, usd, t, side="B", **over):
    """A valid execution of `usd` notional (price 100) at time t, taker on `side`."""
    other = "A" if side == "B" else "B"
    sz = str(Decimal(str(usd)) / 100)
    eff = lambda s: {"cover": "0", "new_long": sz} if s == "B" else {"exit": "0", "new_short": sz}
    e = {"tid": tid, "px": "100", "sz": sz, "time_ms": t, "aggressor": side, "complete": True,
         "participants": [{"user": A, "side": side, "role": "taker", "oid": 1, "effect": eff(side)},
                          {"user": B, "side": other, "role": "maker", "oid": 2, "effect": eff(other)}]}
    e.update(over)
    return e


def rule(thr=THR, start=T):
    r = NodeBurstRule("BTC", {"usd": thr, "source": "manual", "label": "manual: test"})
    r.start(start)
    return r


def feed(r, *blocks):
    """blocks: (offset ms, [executions]) in order, one evaluation each; block numbers count up."""
    for i, (off, execs) in enumerate(blocks):
        r.on_block(100 + i, T + off, execs)
    return [(e["at_ms"] - T, e["side"], e["aggressive_notional_usd"]) for e in reversed(r.events)]


# --- the rule (PLAN.md, "The burst rule") ---------------------------------------------

def test_warm_up_keeps_the_crossing_armed():
    """$100,100 at 59 s (its window starts before the segment: incomplete, so it neither fires nor disarms), then
    $100 at 61 s: one $100,200 burst."""
    assert feed(rule(), (59_000, [ex(1, 100_100, T + 59_000)]), (61_000, [ex(2, 100, T + 61_000)])) == [
        (61_000, "buying", 100_200.0)]


def test_a_second_block_at_the_same_time_doesnt_refire_and_a_later_crossing_does():
    r = rule()
    got = feed(r, (60_000, [ex(1, 100_100, T + 60_000)]),        # (0, 60 s]: complete, fires
               (60_000, [ex(2, 100, T + 60_000)]),               # same time: before = $100,100, disarmed
               (125_000, [ex(3, 100_000, T + 125_000)]))         # earlier ones left the window: re-arm, fire
    assert got == [(60_000, "buying", 100_100.0), (125_000, "buying", 100_000.0)]


def test_the_sides_are_separate():
    got = feed(rule(), (60_000, [ex(1, 60_000, T + 60_000, "B"), ex(2, 60_000, T + 60_000, "A")]),
               (61_000, [ex(3, 50_000, T + 61_000, "A")]))
    assert got == [(61_000, "selling", 110_000.0)]


def test_a_gap_rearms_and_starts_a_new_segment_with_warm_up():
    r = rule()
    assert feed(r, (60_000, [ex(1, 150_000, T + 60_000)])) == [(60_000, "buying", 150_000.0)]
    r.boundary("sequence gap")
    r.start(T + 70_000)
    r.on_block(200, T + 100_000, [ex(2, 200_000, T + 100_000)])  # window starts before the new segment
    assert len(r.events) == 1
    r.on_block(201, T + 131_000, [ex(3, 1, T + 131_000)])         # (71 s, 131 s]: complete; re-armed by the gap
    assert [(e["at_ms"] - T, e["aggressive_notional_usd"]) for e in r.events][0] == (131_000, 200_001.0)
    assert r.counters["gaps"] == 1


def test_an_incomplete_window_keeps_its_executions_and_never_fires_or_disarms():
    bad = ex(9, 1_000_000, T + 61_000, complete=False)
    got = feed(rule(), (61_000, [bad]),
               (90_000, [ex(1, 150_000, T + 90_000)]),           # the bad record is in this window: no fire
               (121_500, [ex(2, 1, T + 121_500)]))               # it has left; the $150,000 is kept; still armed
    assert got == [(121_500, "buying", 150_001.0)]


def test_an_incomplete_window_never_rearms():
    def run(with_bad):
        r = rule()
        blocks = [(60_000, [ex(1, 150_000, T + 60_000)])]           # fires, disarms
        if with_bad:
            blocks.append((125_000, [ex(9, 5, T + 125_000, complete=False)]))
        blocks += [(126_000, [ex(2, 150_000, T + 126_000)]), (185_500, [ex(3, 1, T + 185_500)])]
        return feed(r, *blocks)
    assert run(False) == [(60_000, "buying", 150_000.0), (126_000, "buying", 150_000.0)]
    # The bad record makes (65 s, 125 s] and (66 s, 126 s] incomplete: neither re-arms, so at 185.5 s, with the
    # $150,000 still in the window (before is above the threshold), the side is still disarmed.
    assert run(True) == [(60_000, "buying", 150_000.0)]


REJECTIONS = {
    "an unreadable execution": lambda t: [{k: v for k, v in ex(9, 50_000, t).items() if k != "px"}],
    "complete false": lambda t: [ex(9, 50_000, t, complete=False)],
    "repeated records disagree (conflict)": lambda t: [ex(9, 50_000, t, conflict=True)],
    "not exactly one taker and one maker": lambda t: [ex(9, 50_000, t, participants=[
        dict(ex(9, 1, t)["participants"][0]), dict(ex(9, 1, t)["participants"][1], role="taker")])],
    "not one buyer and one seller": lambda t: [ex(9, 50_000, t, participants=[
        dict(ex(9, 1, t)["participants"][0]), dict(ex(9, 1, t)["participants"][1], side="B")])],
    "a fill time other than the block time": lambda t: [ex(9, 50_000, t - 1)],
    "address is missing": lambda t: [ex(9, 50_000, t, participants=[
        dict(ex(9, 1, t)["participants"][0], user=None), ex(9, 1, t)["participants"][1]])],
}


@pytest.mark.parametrize("why", sorted(REJECTIONS))
def test_a_rejected_record_makes_its_windows_incomplete(why):
    """Each rejected record is counted by reason, never added, and its windows never fire: $50,000 valid plus
    $60,000 more crosses $100,000 without it (the control), and doesn't with it."""
    control = rule()
    control.on_block(100, T + 61_000, [ex(1, 50_000, T + 61_000)])
    control.on_block(101, T + 62_000, [ex(2, 60_000, T + 62_000)])
    assert [e["aggressive_notional_usd"] for e in control.events] == [110_000.0]
    r = rule()
    r.on_block(100, T + 61_000, [ex(1, 50_000, T + 61_000)] + REJECTIONS[why](T + 61_000))
    r.on_block(101, T + 62_000, [ex(2, 60_000, T + 62_000)])
    assert not r.events and r.counters["incomplete_windows"] == 2
    assert any(why in k for k in r.counters["rejected"]), r.counters
    assert sum(s[2] for s in r.samples) == 110_000 and all(s[3].tid != 9 for s in r.samples)


def test_an_identity_repeated_in_a_later_block_is_rejected_and_a_copy_in_its_own_block_counts_once():
    r = rule()
    r.on_block(100, T + 61_000, [ex(1, 60_000, T + 61_000)])
    r.on_block(100, T + 61_000, [ex(1, 60_000, T + 61_000)])     # the same block's record again: counted once
    assert sum(s[2] for s in r.samples) == 60_000 and not r.counters["rejected"]
    r.on_block(101, T + 62_000, [ex(1, 60_000, T + 62_000)])     # a later block: rejected
    assert not r.events and "an execution identity repeated in a later block" in r.counters["rejected"]
    r.on_block(102, T + 62_500, [ex(2, 1, T + 62_500, px="100", sz="0.01"),
                                 dict(ex(2, 1, T + 62_500), sz="0.02")])   # disagreeing copies in one block
    assert "conflicting copies of an execution in one block" in r.counters["rejected"]


def test_unread_fills_and_a_backwards_block_time_are_unreadable_records():
    r = rule()
    r.on_block(100, T + 61_000, [], fills_known=False)
    r.on_block(101, T + 62_000, [ex(1, 150_000, T + 62_000)])
    assert not r.events and r.counters["rejected"] == {"the block's fills weren't read (fills_known false)": 1}
    r = rule()
    r.on_block(100, T + 61_000, [ex(1, 50_000, T + 61_000)])
    r.on_block(101, T + 60_000, [ex(2, 90_000, T + 60_000)])     # runs backwards: rejected, never added
    r.on_block(102, T + 62_000, [ex(3, 60_000, T + 62_000)])     # $110,000 valid, but the window is incomplete
    assert not r.events and r.counters["rejected"] == {"a block time ran backwards": 1}
    assert sum(s[2] for s in r.samples) == 110_000


def test_nothing_is_evaluated_before_the_segment_starts():
    r = NodeBurstRule("BTC", {"usd": THR, "source": "manual", "label": "m"})
    r.on_block(100, T + 61_000, [ex(1, 500_000, T + 61_000)])
    assert r.counters["evaluations"] == 0 and not r.samples


# --- the calibration: thresholds, labels, rank -----------------------------------------

def _tiny(tmp_path, sums=(1.0, 2.0, 2.0, 3.0), ref=True, sha=None):
    raw = json.dumps({"markets": {"BTC": {"buying_sums_usd_sorted": list(sums), "selling_sums_usd_sorted": [5.0]}}}).encode()
    d = {"version": "t.1", "dated": "2026-11-08", "measured_label": "Nov 3 to 7", "measured_utc": ["a", "b"],
         "target_per_day": 10, "near_tolerance": 0.25, "reference_file": "ref.json.gz",
         "source": {"reference": {"file": "ref.json", "sha256": sha or hashlib.sha256(raw).hexdigest()}},
         "markets": {"BTC": {"threshold_usd": 3e7, "achieved_per_day": 10.6, "relative_error": 0.06, "near_target": True},
                     "HYPE": {"threshold_usd": 5e6, "achieved_per_day": 14.6025, "relative_error": 0.46, "near_target": False}}}
    (tmp_path / "cal.json").write_text(json.dumps(d))
    if ref:
        (tmp_path / "ref.json.gz").write_bytes(gzip.compress(raw))
    return cal_mod.Calibration(tmp_path / "cal.json")


def test_rank_is_the_upper_tail_with_ties_counted_at_or_above(tmp_path):
    c = _tiny(tmp_path)
    r = c.rank("BTC", "buying", 2.0)
    assert r["available"] and r["upper_tail"] == 0.75 and r["label"] == "a top 75% minute, Nov 3 to 7"
    assert c.rank("BTC", "buying", 0.5)["upper_tail"] == 1.0
    top = c.rank("BTC", "buying", 4.0)
    assert top["upper_tail"] == 0.0 and top["label"] == "larger than every minute in the reference, Nov 3 to 7"
    assert c.rank("BTC", "selling", 5.0)["upper_tail"] == 1.0
    assert c.rank("HYPE", "buying", 1.0) == {"available": False, "reason": "the burst reference has no buying sums for HYPE"}


def test_rank_is_unavailable_when_the_file_isnt_loaded(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for c in (_tiny(tmp_path / "a", ref=False), _tiny(tmp_path / "b", sha="0" * 64)):
        r = c.rank("BTC", "buying", 2.0)
        assert r["available"] is False and "isn't loaded" in r["reason"] and "upper_tail" not in r
        assert str(tmp_path) not in r["reason"] and "/" not in r["reason"]           # no file path in a reason
        assert c.threshold("BTC", {})["usd"] == 3e7                  # the thresholds still load
    gone = cal_mod.Calibration(tmp_path / "missing.json")
    assert gone.rank("BTC", "buying", 2.0)["available"] is False
    th = gone.threshold("BTC", {})
    assert th["usd"] is None and th["source"] == "unavailable" and "isn't loaded" in th["reason"]
    assert th["reason"] == "the burst calibration file isn't loaded (missing.json: No such file or directory)"
    assert gone.threshold("BTC", {"BTC": 2e6})["usd"] == 2e6           # a manual threshold needs no file


def test_labels_state_the_achieved_rate_when_not_near_the_target():
    c = cal_mod.load()
    assert c.threshold("BTC", {})["label"] == "automatic: about 10 a day, measured Nov 3 to 7"
    assert c.threshold("xyz:XYZ100", {})["label"] == "automatic: about 10 a day, measured Nov 3 to 7"
    hype, sp = c.threshold("HYPE", {}), c.threshold("xyz:SP500", {})
    assert hype["label"].startswith("automatic: 14.6 a day, measured Nov 3 to 7") and not hype["near_target"]
    assert sp["label"].startswith("automatic: 7.2 a day, measured Nov 3 to 7") and not sp["near_target"]
    for th in (hype, sp, c.threshold("BTC", {})):
        assert th["version"] == "2026-11-08.1" and th["dated"] == "2026-11-08" and th["target_per_day"] == 10
        assert th["measured_utc"] == ["2026-11-03T00:00:00.000Z", "2026-11-08T00:00:00.000Z"]
    assert {m: c.threshold(m, {})["usd"] for m in ("BTC", "HYPE", "xyz:SP500", "xyz:XYZ100")} == {
        "BTC": 30_000_000, "HYPE": 5_000_000, "xyz:SP500": 5_000_000, "xyz:XYZ100": 5_000_000}
    man = c.threshold("BTC", {"BTC": 25_000_000.0})
    assert man == {"usd": 25_000_000.0, "source": "manual", "label": "manual: $25,000,000 (setting node_burst_min_usd)"}


def test_the_manual_override_setting_and_switching_back_to_automatic():
    cfg = from_settings({"node_burst_min_usd": {"BTC": 25_000_000, "HYPE": "auto", "xyz:SP500": "3000000",
                                                "xyz:XYZ100": 0, "ETH": "inf", "SOL": float("nan"), "X": True}}.get)
    assert cfg.node_burst_min_usd == {"BTC": 25_000_000.0, "xyz:SP500": 3_000_000.0}
    c = cal_mod.load()
    assert c.threshold("HYPE", cfg.node_burst_min_usd)["source"] == "automatic"
    assert c.threshold("xyz:XYZ100", cfg.node_burst_min_usd)["source"] == "automatic"
    assert from_settings({}.get).node_burst_min_usd == {}
    assert from_settings({"node_burst_min_usd": {}}.get).node_burst_min_usd == {}


def test_the_shipped_files_agree_with_each_other():
    c = cal_mod.load()
    assert c.error is None and c.reference_error is None
    d = c.data
    raw = gzip.decompress((cal_mod.HERE / d["reference_file"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == d["source"]["reference"]["sha256"]
    for m, x in d["markets"].items():
        assert x["threshold_usd"] in d["grid_usd"]
        assert x["near_target"] is (abs(x["relative_error"]) <= d["near_tolerance"])
        assert abs(abs(x["achieved_per_day"] / d["target_per_day"] - 1) - x["relative_error"]) < 1e-3   # unsigned
        ref = json.loads(raw)["markets"][m]
        for side in ("buying", "selling"):
            s = ref[f"{side}_sums_usd_sorted"]
            assert s == sorted(s) and len(s) > 20_000
    text = cal_mod.CALIBRATION_FILE.read_text() + raw.decode()
    for leak in ("/mnt", "/srv", "/home", "/tmp", "files_read", "capture"):
        assert leak not in text, leak


SRC = Path(os.environ.get("BURST_CALIBRATION_SOURCE") or "/nonexistent")


@pytest.mark.skipif(not SRC.is_dir(), reason="needs BURST_CALIBRATION_SOURCE: the folder with the calibration's history "
                                              "and reference files")
def test_the_shipped_files_match_the_calibration_run():
    c = cal_mod.load()
    d = c.data
    hist_raw = (SRC / d["source"]["history"]["file"]).read_bytes()
    ref_raw = (SRC / d["source"]["reference"]["file"]).read_bytes()
    assert hashlib.sha256(hist_raw).hexdigest() == d["source"]["history"]["sha256"]
    assert hashlib.sha256(ref_raw).hexdigest() == d["source"]["reference"]["sha256"]
    assert gzip.decompress((cal_mod.HERE / d["reference_file"]).read_bytes()) == ref_raw
    h = json.loads(hist_raw)
    assert h["range_utc"] == d["measured_utc"] and h["evaluation_unit"] == d["evaluation_unit"]
    assert h["settings"]["near_target"] == d["near_tolerance"] and h["settings"]["grid_usd"] == d["grid_usd"]
    for m, x in d["markets"].items():
        (sel,) = [s for s in h["bursts"][m]["selection"] if s["target_per_day"] == 10]
        assert sel["threshold_usd"] == x["threshold_usd"] and sel["near_target"] is x["near_target"]
        assert round(sel["achieved_per_day"], 4) == x["achieved_per_day"]
        assert round(sel["relative_error"], 4) == x["relative_error"]


# --- real recorded node fills, through the feed client, against a count made here ------------

LOW = {"BTC": 250_000.0, "HYPE": 100_000.0, "xyz:SP500": 40_000.0, "xyz:XYZ100": 10_000.0}


def _independent(msgs, market, thr):
    """PLAN.md's rule written out directly: the segment starts at the market's first watermark; each later execs
    message is one evaluation at its block time; sums are exact decimals."""
    seg, seen, kept, armed, out = None, set(), [], {"B": True, "A": True}, []
    for _, m in msgs:
        if m["market"] != market:
            continue
        if m["kind"] == "watermark":
            seg = seg or (m["block"], m["block_time_ms"])
            continue
        if seg is None or m["block"] <= seg[0]:
            continue
        w, new = m["block_time_ms"], []
        for e in m["body"]["execs"]:
            ps = e["participants"]
            assert e["complete"] and not e.get("conflict") and e["time_ms"] == w and e["tid"] not in seen
            assert sorted(p["role"] for p in ps) == ["maker", "taker"] and sorted(p["side"] for p in ps) == ["A", "B"]
            seen.add(e["tid"])
            new.append((w, next(p["side"] for p in ps if p["role"] == "taker"), Decimal(e["px"]) * Decimal(e["sz"])))
        if w - 60_000 >= seg[1]:
            for side in "BA":
                before = sum((n for t, s, n in kept if s == side and t > w - 60_000), Decimal(0))
                total = before + sum((n for _t, s, n in new if s == side), Decimal(0))
                if before < Decimal(str(thr)):
                    armed[side] = True
                if total >= Decimal(str(thr)) and armed[side]:
                    armed[side] = False
                    out.append((w, {"B": "buying", "A": "selling"}[side], round(float(total), 2)))
        kept += new
    return out


def test_recorded_node_fills_against_an_independent_computation():
    """The 120 s recording of four markets, through the feed client with the plugin's burst attachment, at low
    manual thresholds (every record in it is valid): the same bursts as the rule written out here, each ranked
    as counted here from the shipped reference."""
    from hl_desk.config import DeskConfig
    from hl_desk.node.manager import NodeManager

    from node_helpers import EXECS, client, read_recording
    h, msgs = read_recording(EXECS)
    mgr = NodeManager(DeskConfig(markets=list(LOW), node_burst_min_usd=dict(LOW)))
    c = mgr.attach_bursts(client("sock-a"))
    p = h["producer"]
    c.on_line({"v": 2, "control": "hello", "epoch": p["epoch"], "session": p["session"], "mode": "LIVE",
               "markets": {}}, p["started_ms"], 0)
    for rx, m in msgs:
        c._market(m["market"]).last_seq = m["seq"] - 1        # this fixture keeps only execs and watermarks
        c.on_line(m, rx, rx * 10 ** 6)
    ref = json.loads(gzip.decompress((cal_mod.HERE / cal_mod.load().data["reference_file"]).read_bytes()))
    fired = 0
    for market, thr in LOW.items():
        r = c.markets[market].flow.bursts
        got = [(e["at_ms"], e["side"], e["aggressive_notional_usd"]) for e in reversed(r.events)]
        assert got == _independent(msgs, market, thr), market
        assert r.counters["rejected"] == {} and r.counters["gaps"] == 0
        for e in r.events:
            sums = ref["markets"][market][f"{e['side']}_sums_usd_sorted"]
            assert e["rank"]["upper_tail"] == sum(1 for x in sums if x >= e["aggressive_notional_usd"]) / len(sums)
            assert e["threshold"]["source"] == "manual" and e["provenance"]["source"] == "sock-a"
        fired += len(got)
    assert fired >= 3


def test_a_block_without_a_time_makes_the_windows_around_it_incomplete():
    r = rule()
    r.on_block(100, T + 61_000, [ex(1, 50_000, T + 61_000)])
    r.on_block(101, None, [ex(2, 500_000, T + 61_500)])           # can't be placed: never added
    r.on_block(102, T + 130_000, [ex(3, 150_000, T + 130_000)])   # the next block's windows are incomplete too
    assert not r.events and r.counters["rejected"] == {"a block without a block time": 2}
    assert r.armed == {"buying": True, "selling": True}            # incomplete windows never disarm
    r.on_block(103, T + 190_500, [ex(4, 150_000, T + 190_500)])    # both marks have left (130.5 s, 190.5 s]
    assert [e["aggressive_notional_usd"] for e in r.events] == [150_000.0]
