# Response to Codex review 6 (commit cb9c9d7b84d4ad822e815609a821fdedac9835df)

Codex's verdict was REVISE: 3 blocking findings. Kevin settled finding 1, and the commit now under review follows his decision. Findings 2 and 3 are fixed in `6593e389430e259947ad6636ed87630317dd4f73` on `main`. The commit under review has the same tree as the tip of `main`. Each section quotes the finding and gives what changed (file:line) and the test that reproduces Codex's case. The new tests are in `tests/test_review6.py`.

The suite now has 179 tests (4 new). The official runs at `6593e38` are logged in `evidence/logs/`, with the commands in `evidence/BUILD_REPORT.md`.

## 1. BLOCKING: "Private operational information remains in the reviewed commit's reachable history."

**Resolved by Kevin's decision.** Codex's required outcome was "review a clean publication commit, or have the user explicitly change the history scope." Kevin chose the first, a clean publication commit (his option 2, Nov 8). His instruction:
- commit the current files as a new commit with **no parent**, with author and committer `KevinM0001 <210251245+KevinM0001@users.noreply.github.com>`, and use that identity for every commit in this repo from now on;
- leave the existing commits untouched;
- Codex reviews that commit, and on ACCEPT it is tagged `v1-public-btc`;
- nothing is pushed.

How the commit under review was made:
- **One commit, no parent.** It was created with `git commit-tree` from the tree of the tip of `main`. That tree contains the fixes below, this response, the logs and the build report. `git rev-list --count <commit>` is 1, and `git cat-file -p <commit>` shows a `tree` line and no `parent` line. Its tree hash is identical to the tip of `main`'s.
- **Its own branch.** It sits on the branch `v1-public`. `main` and every earlier commit are unchanged; none of them is reachable from it.
- **Identity.** Author and committer are both KevinM0001 with the address above.
- **Scanned.** Its single tree was checked with the same private-detail scan as before: home paths, storage and mount paths, coordination paths and local ports. The scan found nothing.

The development commits cited in this file and the earlier response files (`b21d205…` through `6593e38…`, on `main`) are named only as provenance. None of them is part of the reviewed history.

## 2. BLOCKING: "A delayed main snapshot is displayed as current and fresh after its freshness limits have expired."

**Changed** in `dashboard/src/app.js`:
- **The rule.** The new pure function `snapshotValidity` (line 1325) decides whether a served snapshot may still be shown as current. Its age is `served.age_s` plus all the time since the tab sent the request, which is an upper bound that includes delivery delay and time on screen. The snapshot is withheld, with the reason, in two cases:
  - that age passes 5 s (`LATEST_MAX_S`, line 47, the backend's latest-snapshot limit);
  - any status it shows as fresh would have lapsed at that age. Those are the chosen market's fields and every market's Flow Deck trades. A status lapses when its progress age plus the snapshot's age passes `max_age_s`, or when its source age plus that age plus the clock bound passes `source_max_age_s`.
- **When it is checked.** `poll` records the request time before it sends the request (line 1579). It runs the check on arrival, so an answer that arrives already too old is never shown (line 1589). While a snapshot is on screen, the check repeats every 250 ms (lines 1627–1633), and it keeps running while the page is hidden.
- **What it replaces.** The timer that measured only time since arrival is gone.
- **Historical snapshots.** The tab shows only the latest snapshot. A snapshot labeled historical is never shown as current; the tools keep their `historical` label.
- **Schema.** `freshness.in_the_tab` (`schema/metrics.v1.json:163`) states the rule.

**Tests.**
- `test_snapshot_delayed_in_delivery_is_withheld_on_arrival` runs the tab's own `snapshotValidity` under node in six cases:
  - Codex's case, a snapshot 12.842 s old when displayed: withheld, past the 5 s limit;
  - a prompt answer: shown;
  - a fast book whose 3 s limit lapses while the snapshot is on screen: withheld, with the reason naming `BTC book_fast`;
  - a source-time lapse: withheld;
  - an answer that isn't the latest: withheld;
  - a missing request time: withheld.
- `test_tab_checks_validity_on_arrival_and_while_shown` checks the wiring: the request time is taken before the fetch, the check runs before `setResp`, and the check repeats on the timer.

**Live, in Hermes v0.23.0 at `6593e38`.**
- **Normal operation.** For 60 s, sampled twice a second, the panels were on screen in all 148 samples; the tab never blanked.
- **Delayed answer.** Chrome's request interception held one real `/snapshot` answer for 12.0 s, then delivered it unchanged, as in Codex's reproduction. 0.5 s later the tab showed no panels and read: "the latest snapshot is now about 12.3 s old (limit 5 s), counting the time since the tab asked for it, so its values aren't shown as current". The next normal answer restored the panels within 4 s. There were no browser exceptions (`evidence/screenshots/review6-snapshot-delayed-delivery-live.png`).

## 3. BLOCKING: "A late execution changes the snapshot's execution set but leaves its Flow Deck card numerically wrong for the stated window."

**Changed** in `hl_desk/deck.py`, implementing Codex's first option: recompute with coherent latch and event treatment.
- **Saved state.** Every evaluated tick is kept with the latch state from before it, and with the events it produced, for the last `REVISABLE_TICKS` = 8 ticks (line 23; `_tick` line 199, `_emit` line 209). That covers every tick whose 90 s window can still contain a trade.
- **Late trades.** `advance` (line 141) now receives the message's new trades (`hl_desk/state.py:160`). A trade whose time is at or before the last evaluated tick is late. `_revise` (line 158) takes back the events of the ticks from the earliest one whose window contains it. It restores the latch state saved before that tick and evaluates those ticks again. Window numbers, live classification, pending and confirmed latch, and regime-change events are then re-derived from the execution set.
- **Withdrawn changes.** A regime change that the re-evaluation no longer supports is reported as a `regime_change_withdrawn` event (line 188). The card carries `revised` with the late-trade count, the latest late trade's time and the ticks re-evaluated (line 193).
- **Much older trades.** A trade later than the retained ticks (about 2 minutes) falls outside every displayed card's window. Older latch steps stay as they were evaluated.
- **Schema and docs.** The new `deck.late_trades` entry (`schema/metrics.v1.json:924`) states this, and so do the README, `docs/SNAPSHOT.md` and the commentary skill.

**Tests.**
- `test_late_trade_updates_the_displayed_card` is Codex's case: $100,000 buys at T0+31 s, +91 s and +121 s, then at T0+125 s a $900,000 buy timestamped T0+100 s. The card for tick T0+120 s now has 3 trades, net flow $1,100,000, volume $1,100,000, and `revised.late_trades` = 1.
- `test_late_delivery_ends_in_the_same_state_as_ordered_delivery` delivers 10 minutes of regime-changing trades twice: in order, and with about 8% of them held 20–60 s. Both deliveries end in the same card metrics, live classification, latch and regime-change events. In this run that meant 53 re-evaluations and 8 regime changes.
