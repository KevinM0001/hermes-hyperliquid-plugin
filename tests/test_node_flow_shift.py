"""The Flow Deck card's node buckets and the wallet split show the last covered window while the node runs behind
(Kevin, Nov 10, after the Plugin session's note on the late-data rule).

Their window is the present (the card's 90 s, the wallet panel's minutes), and a node behind the chain hasn't
processed through it, so both read "not processed through the window's end yet" even when every other node panel
shows late data. Now: when a window fails only for that reason, the buckets cover the last window of the same
length the node's fills do cover, ending at the processed-through block time, served with that window's own
bounds, the requested window, the shift, a label and an "as of" note. Every other uncovered reason still withholds
(fills observed only since later, an unreadable execution, retention), as do INVALID and UNKNOWN; a covered window
is never shifted and carries no note. Sums are checked against counts made here from the executions' own prices
and sizes.
"""

from __future__ import annotations

from hl_desk.node import panels
from hl_desk.node.flow import NOT_THROUGH, RETENTION_MS, NodeFlow
from hl_desk.util import iso_ms

from node_helpers import NO_AGE, client
from test_node_flow import A, B, _exec, _p

SP = "xyz:SP500"
T0 = 1_800_000_000_000
MONO0 = T0 - 200_000                              # the monotonic clock's zero, before every line here
SESSION = "0" * 31 + "2"
CARD = (T0 + 15_000 - 1, T0 + 105_000)            # the card's window as state.py asks for it: (start - 1, end]
LEN = CARD[1] - CARD[0]
READ = T0 + 111_200


def _buy(tid, px, sz, t):
    """A taker buy of sz at px: the taker covers 1 and opens the rest long; the maker exits sz."""
    return _exec(tid, px, sz, t, "B", [_p(A, "B", "taker", {"cover": "1", "new_long": str(int(sz) - 1)}),
                                       _p(B, "A", "maker", {"exit": sz, "new_short": "0"})])


def _now(t=READ):
    return t, (t - MONO0) * 10 ** 6


class _Feed:
    """A live node client fed protocol v2 lines for SP500: hello, a first watermark at since_ms (the fills' window
    opens there), then execs bodies each followed by a watermark at its block time."""

    def __init__(self, since_ms, cfg=NO_AGE):
        self.c = client(cfg=cfg)
        self.seq = 0
        self.c.on_line({"v": 2, "control": "hello", "epoch": 7, "session": SESSION, "started_ms": since_ms - 1_000,
                        "pub_ms": since_ms - 1_000, "mode": "LIVE", "markets": {SP: {"start_seq": 1, "snapshot": "pending"}}},
                       since_ms - 950, (since_ms - 950 - MONO0) * 10 ** 6)
        self.watermark(1, since_ms)

    def _line(self, kind, body, block, bt):
        self.seq += 1
        m = {"v": 2, "epoch": 7, "session": SESSION, "market": SP, "seq": self.seq, "kind": kind, "block": block,
             "block_time_ms": bt, "pub_ms": bt + 10, "body": body}
        rx = bt + 50
        self.c.on_line(m, rx, (rx - MONO0) * 10 ** 6)

    def watermark(self, block, bt):
        self._line("watermark", {"through_seq": self.seq}, block, bt)

    def execs(self, block, bt, execs):
        self._line("execs", {"execs": execs}, block, bt)
        self.watermark(block, bt)

    def card(self, start=CARD[0], end=CARD[1], at=READ):
        return panels.deck_buckets(self.c, SP, start, end, *_now(at))

    def split(self, start=CARD[0], end=CARD[1], at=READ):
        return panels.wallet_buckets(self.c, SP, [A, B], start, end, *_now(at))


def _three_fills(since_ms=T0 - 120_000):
    """Fills observed since since_ms: one before the window the shift will reach, three inside it (block 12 is the
    node's last)."""
    f = _Feed(since_ms)
    f.execs(9, T0 - 40_000, [_buy(4, "1000", "2", T0 - 40_000)])     # buying 2000, before the shifted window
    f.execs(10, T0 + 60_000, [_buy(1, "100", "4", T0 + 60_000)])     # buying 400: cover 100, new long 300
    f.execs(11, T0 + 61_000, [_buy(2, "200", "3", T0 + 61_000)])     # buying 600: cover 200, new long 400
    f.execs(12, T0 + 62_000, [_buy(3, "50", "2", T0 + 62_000)])      # buying 100: cover 50, new long 50
    return f, T0 + 62_000


def _usd(x):
    return float(x["value"] if isinstance(x, dict) else x)


def test_the_cards_buckets_shift_to_the_last_window_the_fills_cover_with_exact_bounds_and_sums():
    f, through = _three_fills()
    b = f.card()
    assert b["available"] is True and b["coverage"]["covers"] is True and b["freshness"]["state"] == "LIVE"
    assert b["window"] == {"start_ms": through - LEN, "end_ms": through,
                           "requested": {"start_ms": CARD[0], "end_ms": CARD[1]}, "shifted_by_ms": CARD[1] - through}
    assert CARD[1] - through == 43_000
    assert b["as_of_note"] == f"as of the node's fills at {iso_ms(through)}, 43.0 s behind the card's window"
    assert b["window_label"] == (f"the node's last 90 s, {iso_ms(through - LEN)[11:19]} to {iso_ms(through)[11:19]} UTC, "
                                 "43 s behind the card's window")
    # the sums over exactly the three fills inside (through - 90 s, through], from their own prices and sizes;
    # the fill at T0 - 40 s (2000) is outside the shifted window and the window's bounds are exact
    buying = b["buying"]
    assert buying["available"] is True and _usd(buying["aggressive_usd"]) == 400 + 600 + 100
    assert _usd(buying["buckets"]["cover"]) == 100 + 200 + 50 and _usd(buying["buckets"]["new_long"]) == 300 + 400 + 50
    assert _usd(b["selling"]["aggressive_usd"]) == 0
    # the wallet split over the same shifted window, named as the wallets' own
    w = f.split()
    assert w["available"] is True and set(w["wallets"]) == {A, B}
    assert w["window"] == {"start_exclusive_ms": through - LEN, "end_ms": through, "shifted_by_ms": 43_000,
                           "requested": {"start_exclusive_ms": CARD[0], "end_ms": CARD[1]}}
    assert w["as_of_note"] == f"as of the node's fills at {iso_ms(through)}, 43.0 s behind the panel's window"
    assert w["window_label"].startswith("these wallets' node split over the node's last 90 s, ") and \
        w["window_label"].endswith("43 s behind the panel's window")
    # the shifted results equal the plain ones over the same bounds once the node has processed past them
    f.watermark(99, T0 + 111_000)
    plain = f.card(through - LEN, through)
    assert plain["window"]["shifted_by_ms"] == 0 and plain["as_of_note"] is None and plain["window_label"] is None
    skip = ("window", "as_of_note", "window_label", "coverage", "freshness")
    assert {k: v for k, v in plain.items() if k not in skip} == {k: v for k, v in b.items() if k not in skip}
    plain_w = f.split(through - LEN, through)
    assert plain_w["wallets"] == w["wallets"] and plain_w["as_of_note"] is None


def test_a_covered_window_is_served_as_asked_without_a_note():
    f, through = _three_fills()
    f.watermark(99, T0 + 111_000)                 # the node has processed through the card's window
    b = f.card()
    assert b["available"] is True
    assert b["window"] == {"start_ms": CARD[0], "end_ms": CARD[1], "requested": None, "shifted_by_ms": 0}
    assert b["as_of_note"] is None and b["window_label"] is None
    assert _usd(b["buying"]["aggressive_usd"]) == 1100
    w = f.split()
    assert w["window"] == {"start_exclusive_ms": CARD[0], "end_ms": CARD[1], "requested": None, "shifted_by_ms": 0}
    assert w["as_of_note"] is None and w["window_label"] is None


def test_only_the_one_reason_shifts_and_the_shifted_window_passes_every_check():
    # fills observed only since after the shifted window's start: withheld, both reasons said
    f, through = _three_fills(since_ms=T0 - 5_000)
    b = f.card()
    assert b["available"] is False and b["reason"].startswith(NOT_THROUGH) and "observed only since" in b["reason"]
    assert "window" not in b and "as_of_note" not in b
    w = f.split()
    assert w["available"] is False and w["reason"].startswith(NOT_THROUGH) and "observed only since" in w["reason"]
    # an unreadable execution inside the shifted window: withheld with that reason
    f, through = _three_fills()
    broken = _buy(5, "10", "2", T0 + 61_500)
    del broken["px"]
    f.execs(13, T0 + 61_500, [broken])
    f.watermark(14, T0 + 62_500)
    b = f.card()
    assert b["available"] is False and b["reason"].startswith(NOT_THROUGH) and "couldn't be read" in b["reason"]
    assert f.split()["available"] is False


def test_invalid_and_unknown_still_withhold_before_any_shift(monkeypatch):
    f, through = _three_fills()
    for state in ("INVALID", "UNKNOWN"):
        monkeypatch.setattr(f.c, "family_status", lambda market, fam, *a, _s=state, **k: {"family": fam, "state": _s, "reason": "test"})
        b = f.card()
        assert b["available"] is False and b["reason"] == f"node fills {state}: test" and "window" not in b
        w = f.split()
        assert w["available"] is False and w["reason"] == f"node fills {state}: test"


def test_fills_older_than_the_retention_never_count_as_covered_nor_does_a_shift_reach_them():
    f = NodeFlow("test")
    f.on_watermark(1, T0)
    f.on_execs({"execs": [_buy(1, "100", "4", T0 + 1_000)]}, 2, T0 + 1_000)
    f.on_watermark(2, T0 + 1_000)
    f.on_watermark(3, T0 + 20 * 60_000)                                # the node's last block, 20 min on
    execs, cov = f.window(T0 + 60_000, T0 + 20 * 60_000)              # starts 19 min before it: not kept whole
    assert cov["covers"] is False and "no longer kept" in cov["reason"] and "16 min" in cov["reason"]
    execs, cov = f.window(T0 + 20 * 60_000 - RETENTION_MS, T0 + 20 * 60_000)    # exactly at the cut: covered
    assert cov["covers"] is True and execs == []
    # a 17 min window ending after the node's last block: the shift re-runs every check and retention withholds
    execs, cov, win = f.covered_window(T0 + 25 * 60_000 - 17 * 60_000, T0 + 25 * 60_000)
    assert cov["covers"] is False and cov["reason"].startswith(NOT_THROUGH) and "no longer kept" in cov["reason"]
    assert win["shifted_by_ms"] == 0 and win["requested"] is None
    # the same window 15 min long shifts within retention
    execs, cov, win = f.covered_window(T0 + 25 * 60_000 - 15 * 60_000, T0 + 25 * 60_000)
    assert cov["covers"] is True and win["shifted_by_ms"] == 5 * 60_000
    assert win["start_ms"] == T0 + 5 * 60_000 and win["end_ms"] == T0 + 20 * 60_000 and execs == []
