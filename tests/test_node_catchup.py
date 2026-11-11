"""A feed replaying what it buffered after a connection is loading, not lagging (decision of Nov 11): the family carries
catching_up while the node's block time advances at more than twice the clock's pace over the delay's own window, its
reason and note quote the same averaged delay as its state ("catching up ... behind and falling"), the flag and the
state end together (never on a LIVE family, never plainly "delayed" with a falling figure between the replay and
LIVE), and a served status or delay block older than 3 s drops the claim for the plain reason of its own state."""
from hl_desk.node import panels
from hl_desk.node.client import catching_up, delay_state
from hl_desk.node.serving import aged_status, aged_view
from pathlib import Path

from node_helpers import client, hello_from, read_recording
from test_h1_review2 import _age_shown
import pytest
import shutil

LIVE = Path(__file__).parent / "fixtures" / "node" / "owner-pos-HYPE-live-120s.jsonl.gz"
LIMITS = {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10_000}   # a real delayed limit, no staleness
PROD = {"delay_window_ms": 5_000, "delayed_after_ms": 10_000, "stale_after_ms": 30_000}   # the client's defaults


def test_the_rule_on_synthetic_histories():
    ms = 1_000_000
    # a replay: in 2 s of clock the offset fell by 100 s (block time outran the clock); the averaged delay is 120 s
    hist = [(0, 177_000), (1_000 * ms, 120_000), (2_000 * ms, 72_000)]
    assert catching_up(hist, 2_100 * ms, 120_000, PROD) is True
    # a feed merely behind: the offset holds, block time advances at the clock's pace
    hist = [(0, 72_000), (1_000 * ms, 72_100), (2_000 * ms, 71_900)]
    assert catching_up(hist, 2_100 * ms, 72_000, PROD) is False
    # the flag follows the averaged delay the state is judged by: a replay whose latest line is already under the limit
    # is still catching up while the average is above it (the family reads BEHIND), and never once the average is under
    hist = [(0, 60_000), (1_000 * ms, 20_000), (2_000 * ms, 3_000)]
    assert catching_up(hist, 2_100 * ms, 38_000, PROD) is True
    assert catching_up(hist, 2_100 * ms, 9_000, PROD) is False
    assert catching_up(hist, 2_100 * ms, None, PROD) is False
    # quiet for over 2 s: whatever happened before, the replay is over
    hist = [(0, 177_000), (1_000 * ms, 72_000)]
    assert catching_up(hist, 3_500 * ms, 100_000, PROD) is False
    assert catching_up([(0, 177_000)], 100 * ms, 177_000, PROD) is False
    # a feed's ordinary receipt jitter (the live fixture's steady part swings under 1 s between watermarks) never reads
    # as a replay, from the first samples of a history on
    hist = [(0, 72_300), (165 * ms, 72_000), (435 * ms, 71_353), (705 * ms, 71_900), (975 * ms, 72_100)]
    for n in range(2, len(hist) + 1):
        assert catching_up(hist[:n], hist[n - 1][0] + 50 * ms, 72_000, PROD) is False, n


def test_a_replay_that_ends_live_stays_catching_up_until_the_family_is_live():
    """A connection at the chain head replays its backlog to live within 3 s. The 5 s average trails the replay, so the
    family stays STALE then BEHIND for a few seconds after the last buffered line: those seconds read 'catching up'
    too, never plainly 'delayed' with a large falling figure (review of Nov 11); the flag ends exactly when LIVE does."""
    ms = 1_000_000
    dense = [(0, 60_000), (1_000 * ms, 40_000), (2_000 * ms, 20_000), (3_000 * ms, 8_000)]
    dense += [(t * 1_000 * ms, 5_000) for t in range(4, 12)]       # then at the clock's pace, 5 s behind
    # the same replay delivered as three sparse lines (a rate-limited pipe): the rate test spans the same interval as
    # the average, so no plain "delayed" tail opens between the replay's last line and LIVE either
    sparse = [(0, 100_000), (2_500 * ms, 50_000), (4_500 * ms, 5_000)] + [(t * 1_000 * ms + 500 * ms, 5_000) for t in range(5, 12)]
    c = client(cfg=PROD)
    for hist, from_tenth in ((dense, 10), (sparse, 26)):           # from the second sample on
        seen = []
        for tenth in range(from_tenth, 115):
            now = tenth * 100 * ms
            d = c.delay("X", now, hist=hist)
            state, _ = delay_state(d["delay_ms"], c.cfg)
            seen.append((tenth / 10, state, d["catching_up"], d["delay_ms"]))
            assert d["catching_up"] is (state != "LIVE"), seen[-1]
        states = [s for _, s, _, _ in seen]
        assert states[0] == "STALE" and "BEHIND" in states and states[-1] == "LIVE", states
        assert states.index("LIVE") > states.index("BEHIND") > states.index("STALE")
        assert all(s == "LIVE" for s in states[states.index("LIVE"):]), states


def test_the_live_recording_starts_by_catching_up_then_reads_behind():
    """The live recording opens with a snapshot 177 s old and replays to 72 s behind within 3 s (the runner it was
    taken from lagged the chain by 72 s then): catching up first, then plainly behind."""
    h, msgs = read_recording(LIVE)
    c = client(cfg=LIMITS)
    c.on_line(hello_from(h), msgs[0][0] - 1, 0)
    t0 = msgs[0][0]
    seen_catching, seen_behind = None, None
    for rx, m in msgs:
        c.on_line(m, rx, rx * 10 ** 6)
        el = rx - t0
        if m.get("market") != "HYPE" or m.get("kind") != "watermark":
            continue
        st = c.family_status("HYPE", "book", rx, rx * 10 ** 6)
        if 800 <= el <= 2_500 and seen_catching is None:
            seen_catching = (el, st, panels.as_of_note("book", st))
        if el >= 12_000:
            seen_behind = (el, st, panels.as_of_note("book", st))
            break
    el, st, note = seen_catching
    assert st["catching_up"] is True and st["state"] in ("BEHIND", "STALE"), (el, st)
    assert note.startswith("catching up: the node's book at ") and note.endswith(" s behind and falling"), note
    assert "replaying what it buffered" in st["reason"]
    # one figure per judgement: the reason quotes the averaged delay the note, the chip and delay_ms show
    assert f"the delay is {st['delay_ms'] / 1000:.1f} s, 5 s average, and falling" in st["reason"], st["reason"]
    assert note.endswith(f"{st['delay_ms'] / 1000:.1f} s behind and falling"), (note, st["delay_ms"])
    el, st, note = seen_behind
    assert st["catching_up"] is False and st["state"] in ("BEHIND", "STALE"), (el, st)
    assert note.startswith("as of the node's book at ") and " s behind" in note, note
    # a served copy of the catching-up status (BEHIND) keeps the claim for 3 s, then reads plainly behind again
    _, st_c, _ = seen_catching
    young = aged_status(st_c, 1_000)
    assert young["catching_up"] is True and "replaying what it buffered" in young["reason"]
    assert panels.as_of_note("book", young).startswith("catching up: ")
    for age in (3_500, None):
        old = aged_status(st_c, age)
        assert old["catching_up"] is False and old["state"] == st_c["state"], old
        assert "catching up" not in old["reason"] and "delayed above" in old["reason"], old["reason"]
        note = panels.as_of_note("book", old)
        assert note.startswith("as of the node's book at ") and note.endswith(" s behind"), note


STALE_ST = {"family": "book", "state": "STALE", "catching_up": True, "delay_ms": 41_200, "watermark_block_time_ms": 1_762_000_000_000,
            "delay_window_ms": 5_000, "delayed_after_ms": 10_000, "stale_after_ms": 30_000,
            "reason": "catching up: the feed is replaying what it buffered, so the node's time advances faster than the clock "
                      "(the delay is 41.2 s, 5 s average, and falling)"}
DELAY_BLOCK = {"available": True, "state": "STALE", "catching_up": True, "delay_ms": 41_200, "current_ms": 20_900, "window_ms": 5_000,
               "delayed_after_ms": 10_000, "stale_after_ms": 30_000, "reason": STALE_ST["reason"]}


def test_a_served_stale_status_and_the_delay_block_drop_the_claim_on_the_same_rule():
    """After 3 s the family gets the plain reason of its own state (stale, not delayed), and the market's delay block,
    which the tab's counter reads, drops the flag with the families (review of Nov 11)."""
    for age in (3_500, None):
        old = aged_status(STALE_ST, age)
        assert old["catching_up"] is False and old["state"] == "STALE"
        assert old["reason"] == delay_state(41_200, STALE_ST)[1]
        assert "stale above 30 s" in old["reason"] and "heartbeats don't count" in old["reason"], old["reason"]
    assert aged_status(STALE_ST, 1_000)["catching_up"] is True
    view = {"families": {"book": STALE_ST}, "delay": DELAY_BLOCK}
    assert aged_view(view, 1_000)["delay"]["catching_up"] is True
    for age in (3_500, None):
        aged = aged_view(view, age)
        assert aged["delay"]["catching_up"] is False and aged["families"]["book"]["catching_up"] is False, (age, aged["delay"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the tab's pure function")
def test_the_tab_mirrors_the_3s_drop_for_a_stale_family_and_the_delay_block():
    """The tab's ageShown, the same rule: a catching-up family held on screen past 3 s reads its own plain reason
    (the stale wording for STALE, as serving.py words it) and the delay block loses the flag at the same moment."""
    payload = {"market": {"node": {"live": {"families": {"book": STALE_ST}, "delay": DELAY_BLOCK}}}}
    young = _age_shown(payload, 1.0)["market"]["node"]["live"]
    assert young["families"]["book"]["catching_up"] is True and young["delay"]["catching_up"] is True
    old = _age_shown(payload, 3.5)["market"]["node"]["live"]
    assert old["families"]["book"]["catching_up"] is False and old["delay"]["catching_up"] is False
    assert old["families"]["book"]["reason"] == delay_state(41_200, STALE_ST)[1], old["families"]["book"]["reason"]
    behind = {"market": {"node": {"live": {"families": {"book": dict(STALE_ST, state="BEHIND", delay_ms=15_000)},
                                           "delay": dict(DELAY_BLOCK, state="BEHIND", delay_ms=15_000)}}}}
    old = _age_shown(behind, 3.5)["market"]["node"]["live"]
    assert old["families"]["book"]["reason"] == delay_state(15_000, STALE_ST)[1], old["families"]["book"]["reason"]
