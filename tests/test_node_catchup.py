"""A feed replaying what it buffered after a connection is loading, not lagging (decision of Nov 11): the family carries
catching_up while the node's block time advances at more than twice the clock's pace, its note says "catching up ...
still to replay", and the flag ends once the replay is over, after which the ordinary delay rule applies."""
from hl_desk.node import panels
from hl_desk.node.client import catching_up
from hl_desk.node.serving import aged_status
from pathlib import Path

from node_helpers import client, hello_from, read_recording

LIVE = Path(__file__).parent / "fixtures" / "node" / "owner-pos-HYPE-live-120s.jsonl.gz"
LIMITS = {"stale_after_ms": 10 ** 12, "delayed_after_ms": 10_000}   # a real delayed limit, no staleness


def test_the_rule_on_synthetic_histories():
    ms = 1_000_000
    # a replay: in 2 s of clock the offset fell by 100 s (block time outran the clock), delay still 72 s
    hist = [(0, 177_000), (1_000 * ms, 120_000), (2_000 * ms, 72_000)]
    assert catching_up(hist, 2_100 * ms, 10_000) is True
    # a feed merely behind: the offset holds, block time advances at the clock's pace
    hist = [(0, 72_000), (1_000 * ms, 72_100), (2_000 * ms, 71_900)]
    assert catching_up(hist, 2_100 * ms, 10_000) is False
    # a replay that already brought the delay under the delayed limit reads LIVE anyway
    hist = [(0, 60_000), (1_000 * ms, 20_000), (2_000 * ms, 3_000)]
    assert catching_up(hist, 2_100 * ms, 10_000) is False
    # quiet for over 2 s: whatever happened before, the replay is over
    hist = [(0, 177_000), (1_000 * ms, 72_000)]
    assert catching_up(hist, 3_500 * ms, 10_000) is False
    assert catching_up([(0, 177_000)], 100 * ms, 10_000) is False


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
    assert note.startswith("catching up: the node's book at ") and note.endswith(" s still to replay"), note
    assert "replaying what it buffered" in st["reason"]
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
