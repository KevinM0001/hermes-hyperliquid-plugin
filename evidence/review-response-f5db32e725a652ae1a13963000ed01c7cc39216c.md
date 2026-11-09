# Response to Codex h1 review 5 of `f5db32e725a652ae1a13963000ed01c7cc39216c` (REVISE, 1 blocking)

Kevin decided (Nov 9, 07:55Z) to stop after this review's verdict. Nothing in the code was changed in response. The blocking finding is **deferred by Kevin**, and is quoted in full below so the next review treats it as known. The only change is one line in `docs/DEMO.md` (`e8044519c0483811753a18663f35828012ceb990`), as he asked.

## B1 (BLOCKING): node ladder reports a complete-looking 10-second zero after losing its change history

**Deferred by Kevin (Nov 9, 07:55Z): not fixed.**

Codex's finding, quoted:

> `NodeBook` starts with an empty change deque. After a gap, a replacement snapshot can establish a valid current book, but cannot reconstruct the preceding ten seconds of adds/fills/cancels. `ladder()` nevertheless defaults every missing change category to zero and publishes those numbers under `change_10s`, with no observed start, incomplete-window flag or reason. The tab renders them as numeric values under the same ten-second label. The book's LIVE status and full current-order coverage do not establish coverage of this historical interval.

> Reproduction […]: 1. At T, deliver a valid BTC book snapshot and an applied watermark. 2. At T+1 s, add an order of size 3 at bid price 99.9. After its watermark, `change_10s.added` at that level is 3. 3. Disconnect at T+1.5 s. Reconnect with a valid replacement snapshot at T+2 s, including that order; apply the T+2.1 s watermark. 4. Read at T+2.11 s. The book family is LIVE, source age 10 ms. Both `node_book` and the tab payload report `{"added":0.0,"absorbed":0.0,"pulled":0.0,"unknown":0.0}` for that level's last ten seconds. Initial startup also produces these zeros with only 100 ms observed.

> The complete ten-second interval is unknown; in this controlled stream it even contains the previously observed addition of 3. Reconnecting must not silently turn that interval into a zero. Return null with an incomplete-coverage reason until ten contiguous seconds have been observed, or expose a separately named partial statistic with its actual start and coverage. Do not retain pre-gap changes as if the missing interval were covered. The schema's current family-validity gate also needs to distinguish current-book validity from change-window completeness.

**Agreed that it's a real defect.**
- **Where:** `hl_desk/node/panels.py` `ladder()` (the `change_10s` defaults) and the tab's change columns.
- **When:** for the first 10 s after the node feed starts or reconnects. After that, the columns are correct. The current book itself (sizes, order counts, largest orders) is correct throughout.
- **The proposed fix, not built:**
  - the book remembers when its change history starts;
  - until 10 contiguous seconds are covered, the four columns are withheld with the reason ("measuring: N s of 10" in the tab);
  - the schema separates change-window completeness from book validity;
  - the skill tells the agent to say so;
  - the other short-window node numbers (wall watch, thinning) get the same check;
  - a test runs Codex's reproduction.
  - About 2 h, including a review.
- **Interim, `e804451`:** `docs/DEMO.md`'s notes say to wait at least 10 s after pressing Start before opening the node ladder.

## D1 (DEFERRED): delivery/display aging uses different freshness classifications from a fresh build

Deferred until after the release, as in reviews 1 to 4. As the finding says, both paths withhold the affected values; the difference is the status word ("stale" or "delayed" against "unverified").

## D2 (DEFERRED): the documented demo's chat timing was not achieved

Deferred: Kevin hasn't decided yet. A read-only measurement of the three timed chat runs from reviews 4 and 5 is in `evidence/BURST_CALIBRATION.md`: about two-thirds of the time is the model writing a ~5,000 to 6,000-character answer.
