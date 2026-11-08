# Response to Codex review 1 (commit b21d205eac6d19390a3b18de2b24b344090cff78)

Codex's verdict was REVISE: 9 blocking findings and 1 deferred. All ten are fixed in this revision; finding 9 is fixed in the tree, and its history part is answered below. Each section quotes the finding and gives what changed (file:line at this revision) and the test that reproduces Codex's case. Kevin also decided four metric-definition findings from GPT review R11 for this round; they are in the last section, as his decisions.

The suite now has 140 tests: 139 pass and 1 is skipped (the deck-log compatibility check, which needs `HL_DECK_LIVE_LOG`). The official runs in `evidence/logs/` set that variable, so all 140 run there. `plugins doctor . --ci` and `plugins validate . --install-deps` pass. Live checks were made in the contest Hermes v0.23.0, as described below.

## 1. BLOCKING: "The shared snapshot/control contract does not work between Hermes processes."

**Changed.** The state owner now runs an owner service, `hl_desk/owner.py`:
- **Socket.** `OwnerServer` (line 35) is a Unix socket, `owner.sock`, with mode 0600, in the profile's plugin data directory. It runs on the collector's own event loop.
- **Requests.** Other processes of the profile call `request()` (line 85) to ask for a snapshot by ID, status, a wallet's executions from a retained snapshot, stop and restart.
- **Runtime.** `hl_desk/runtime.py` routes every read and control through the owner when this process isn't it: `_serve` line 194, `status` 225, `snapshot`, `wallet_trades` 273, `stop` 168, `restart` 180.
- **Start.** `start` finds a live owner in another process and doesn't start a second one.
- **No file fallback.** The latest-snapshot file is gone (`hl_desk/store.py`). `dashboard/plugin_api.py` has no file fallback; it always goes through the runtime, in a worker thread.

**Tests.** `tests/test_review1.py::test_other_process_reads_snapshot_by_id_drilldown_and_stops_the_owner` uses a second Python process. It reads a retained snapshot by ID after the owner has moved on, opens a wallet (its totals equal the panel row), reads the owner's status, and stops the owner's collector. `tests/test_collector_local.py` checks that the socket exists with mode 0600 and is removed on stop.

**Live, in Hermes v0.23.0.**
- Setup: the isolated contest dashboard owned the collector after its Start button was pressed. A separate Hermes process loaded the installed plugin through `hermes_cli.plugins.get_plugin_manager()` and dispatched every registered tool.
- Reads: all six tools returned `ok` with the dashboard's copied snapshot ID, and the wallet drill-down totals equaled the panel row.
- Controls: `/hl status` in that process reported the owner's live collector. `/hl stop` there stopped the dashboard's collector, and the tab then reported "the collector isn't running for this profile".

## 2. BLOCKING: "A dead collector's saved snapshot remains 'connected' and 'fresh.'"

**Changed.**
- **No saved copy.** No snapshot is saved to disk any more, so a dead owner leaves nothing that can be read as current. A process with no owner to ask gets "the collector isn't running".
- **Latest reads.** In the owner, `Runtime._local_snapshot` (`hl_desk/runtime.py:234`) refuses a latest read when the latest snapshot is more than `LATEST_MAX_AGE_S = 5.0` s old (line 290), or when the collector is stopped.
- **By ID.** A snapshot read by ID is labeled `served: {latest: false, age_s, note: "a retained historical snapshot: its freshness statuses are as of its build time, not now"}` (`_served`, line 293).
- **Tab.** It clears the shown snapshot on any `ok: false`. It also hides a snapshot whose build time hasn't advanced for `STALE_MS = 5000` (`dashboard/src/app.js:47`), saying why.

**Tests.**
- `test_killed_owner_leaves_nothing_that_reads_as_current` SIGKILLs an owner process, so there is no final publication and a stale socket file is left. The next reader gets "isn't running", and a new start succeeds.
- `test_latest_older_than_the_limit_is_refused_and_ids_are_labeled_historical`.

## 3. BLOCKING: "An unseen late trade refreshes freshness without source progress."

**Changed.** `MarketState.on_trades` (`hl_desk/state.py:141`) counts trades progress only when the trades watermark strictly advances. A late or same-time trade is counted in the execution set, as the counter `late` records, but never refreshes freshness.

**Test.** `test_late_trade_does_not_refresh_trades_freshness` is Codex's reproduction: T0+90 s arrives at 31 s after T0+100 s, and the status stays `unverified` with values null. A same-time trade doesn't refresh either, and an advancing one does.

## 4. BLOCKING: "Burst rearming both duplicates an episode and misses a new episode."

**Changed.** `DeckEngine.check_burst` (`hl_desk/deck.py:190`) excludes exactly the newly added executions, by identity, when computing the pre-message rolling sum. It no longer excludes "all trades at the final watermark". The schema's `alerts.burst.reset_rule` states this.

**Tests.** `test_burst_does_not_repeat_within_one_episode_at_the_same_timestamp` (reproduction A: no second alert) and `test_burst_new_episode_in_one_message_fires` (reproduction B: the $10,000,001 episode fires).

## 5. BLOCKING: "Wallet drill-down for a retained snapshot is neither immutable nor complete."

**Changed.**
- **Frozen sets.** Every published snapshot keeps the exact execution set its panels used: an immutable tuple of trades per market, `DeskState.build_snapshot` → `frozen_last`. `SnapshotStore` holds it beside the snapshot, and drops it with the snapshot.
- **Drill-down.** It reads only that frozen set, through `tools.drilldown` (`hl_desk/tools.py:257`), in the owner, or through the owner service from any other process.

**Tests.** `test_drilldown_unchanged_by_a_later_late_trade` (reproduction A: still $200, 2 executions) and `test_drilldown_complete_after_old_trades_leave_the_live_store` (reproduction B: 90 fills, $9,000, after the live store pruned the oldest).

## 6. BLOCKING: "The six-minute CVD tool result misstates its cumulative origin."

**Changed.**
- **Snapshot.** The CVD now carries `cumulative_from_ms` and `cumulative_from`, the start of the first complete bucket as a real timestamp, plus a `cumulative_rule` (`hl_desk/flow.py`).
- **`hl_flow` slice.** The 12-bucket slice adds `slice: {buckets_in_snapshot, buckets_shown, cumulative_base_usd, note}` (`hl_desk/tools.py:139`), so values keep the same meaning under one snapshot ID and the origin is explicit.

**Test.** `test_hl_flow_cvd_slice_states_its_cumulative_origin` (Codex's +31 s … +931 s series): base plus the shown deltas reproduces every shown cumulative value.

## 7. BLOCKING: "Missing book fields become zero counts or falsely complete depth."

**Changed.**
- **Parsing.** `parse_levels` (`hl_desk/book.py:18`) returns `(levels, problems)`. A missing `n` gives `api_count: null` with `api_count_reason`, not 0.
- **Malformed levels.** A level with a missing or invalid px or sz is reported, and makes that side's totals unavailable (`problem_reason`, line 32): exact band, approximate band and observed depth. A malformed best level makes the top of book unavailable.

**Tests.** `test_missing_api_count_is_null_with_reason`, `test_missing_level_size_invalidates_that_sides_totals` (Codex's 99.75 case) and `test_malformed_best_level_makes_the_top_unavailable`.

## 8. BLOCKING: "Chart metrics lack schema entries, and candle volume rounds each fill."

**Changed.**
- **Rounding.** Candle volume is summed exactly and rounded once (`hl_desk/state.py:345` and `:361`).
- **Schema entries.** New entries cover `chart.candle` and `chart.price_line`, plus `book.bbo_consistency`, `flow.trade_count`, `context.mid`, `context.prev_day_px`, `deck.price`, `wallets.unique_execution_notional` and `wallets.executions`. Every published number now has an entry.

**Tests.** `test_candle_volume_is_summed_exactly_then_rounded_once` (3 × 75800.1 × 0.00001 → $2.27) and `test_every_published_metric_has_a_schema_entry`.

## 9. BLOCKING: "Committed evidence contains private information about Kevin's machine/workflow."

**Changed, in the tree (Kevin's decision: scrub now).**
- **What was removed.** All private operational details are out of `evidence/`: the node's storage layout and devices, coordination paths and channels, local server and port details, and process details.
- **Paths.** Folder paths are shortened to `~`.
- **Preflight log.** `evidence/logs/preflight.log` is now a labeled, redacted record that keeps the commands and conclusions.
- **Build report.** `evidence/BUILD_REPORT.md` was rewritten without those details.
- **Deck-log test.** It no longer names a private directory; it reads `HL_DECK_LIVE_LOG`.

**Disputed: rewriting reachable history.** Per Kevin, earlier commits never become public. The published history is rebuilt from the accepted tree before the first push, so the commits that carry the old evidence (`b8d1c91…` and `805e52d…`) are never published. Kevin also asked that the existing commits not be rewritten (decision of Nov 7, recorded in the build report). Nothing in this repository has a remote or has been pushed (`git remote` prints nothing). I've left history as it is and made the tree to be published clean.

## 10. DEFERRED: "The visible CVD origin is rendered as '(no time).'"

**Changed.** Fixed with finding 6: `cumulative_from` is a timestamp again, and the tab prints it as UTC time. When no bucket is complete yet, it says "the first complete bucket".

## Kevin's decisions on GPT review R11 findings 7 to 10 (made in this round)

Kevin accepted these four findings from GPT's review of the metric definitions (GPT review R11, provided separately) and asked for them in this round, with tests.

**(7) Freshness needs a recent source timestamp under a known clock bound.**
- **Decision.** A value is fresh only when its feed is still advancing and its source timestamp is recent, judged with a clock relationship whose uncertainty is known. Otherwise it is delayed or unverified, never fresh.
- **Clock bound.** `hl_desk/clock.py` (`ClockBound`, line 60; `os_bound`, line 38) reads the kernel's maximum clock error via a read-only `adjtimex` call while NTP-synchronized. Where the OS can't report a bound, the `clock_error_bound_ms` setting supplies one.
- **Rule.** `FieldTracker._source_recency` (`hl_desk/freshness.py:104`):
  - **fresh:** receipt progress within its limit, and source_age + bound ≤ the source-age limit;
  - **delayed:** source_age − bound exceeds the limit;
  - **unverified:** the bound is unknown, the source time is ahead of local time beyond the bound, or the age is within the bound of the limit.
- **Limits.** Source-age limits are in the schema: fast book 5 s, 20-level and info books 20 s, trades 35, 65 or 125 s.
- **Context fields.** `activeAssetCtx` has no source timestamp, so mark, oracle, funding, OI and volume are now always unverified and null, with their receipt age. That is the consequence of this decision; the README states it.
- **Tests.**
  - `test_delayed_backlog_advancing_an_hour_behind_is_never_fresh`: GPT's case, before and after a reconnect.
  - `test_source_age_limit_with_the_clock_bound`: 4.95 s fresh, 5.0 s unverified, 5.051 s delayed.
  - `test_unknown_clock_bound_means_unverified`, `test_source_time_ahead_of_local_time_breaks_the_clock_relationship` and `test_clock_skew_withholds_freshness_instead_of_being_ignored`.
  - `test_context_is_never_fresh_without_a_source_timestamp` and `test_ctx_receipt_age_per_market_and_never_fresh`.

**(8) PRESSURE.**
- **Decision.** PRESSURE becomes aggressive buy notional over aggressive sell notional, flips included, with the same $25K floor on selling, in both modes. So TRENDING can fire in public mode, and UNRESOLVED goes away. The data check verifies public trade semantics for every flow number before release. hl_deck's flip-free ratio survives only as a separately named legacy comparison, if at all. The schema's node-mode opening and closing buckets split flips by finding 8's formulas; Part B implements them.
- **Code.** `window_metrics` (`hl_desk/deck.py:57`) and `classify`; UNRESOLVED is removed from code, schema, skill, README and tab.
- **Schema.** `deck.pressure`; `deck.pressure_legacy_hl_deck` (defined, not computed, never used by the classifier); and `deck.activity_buckets`, with finding 8's position-effect formulas, flips split, for node mode and Part B. The data-check note is under `conventions.data_check`.
- **Tests.** `test_pressure_is_aggressive_buy_over_sell_with_the_25k_floor`, `test_trending_with_public_pressure_at_its_thresholds` and `test_unresolved_is_gone_everywhere`.

**(9) Regime explanations and card layout.**
- **Decision.** Replace the regime explanations with finding 9's wording, never claiming a cause the numbers can't show. Show the live classification and any pending change separately from the confirmed regime.
- **Wording.** Schema `regimes.meanings` holds GPT's replacement text word for word, with `meanings_source`. IMPACT is labeled "absolute observed trade-price change per $1M of net aggressive flow", a descriptive ratio.
- **Card.** It now has `confirmed_regime`, `live_classification` (with its own meaning and a sparse flag) and `pending: {state, ticks, of}` (`hl_desk/deck.py:260`). The tab shows "confirmed", "now" and "pending" on separate lines. The skill tells the agent never to describe the current numbers with the confirmed label.
- **Tests.** `test_meanings_are_the_adopted_wording_and_claim_no_cause`, `test_card_reports_confirmed_live_and_pending_separately` and `test_sparse_is_a_flag_on_the_live_classification_only`.

**(10) Exact versus approximate depth.**
- **Decision.** Exact band depth comes only from full-precision levels; coarse buckets are labeled approximate.
- **Code.** `depth_bands` (`hl_desk/book.py:142`) returns, per band:
  - `exact`, from the full-precision 20-level book only;
  - `approximate`, from the nSigFigs books via `_approx_side` (line 120), with `approx_usd`, a `range_usd` covering the boundary buckets, the aggregation, the bucket width and the representative-price `convention`.
- **Imbalance.** An approximate imbalance appears only when both sides come from one aggregated book.
- **Tab.** It shows exact bands as before, plus a separate line marked "approx".
- **Tests.** `test_exact_band_counts_only_full_precision_levels_inside_the_edge` and `test_coarse_buckets_are_a_labeled_approximation_with_a_range`.

## Other changes in this round

- `bucket_width` now measures spacing within each side, so the spread is no longer mistaken for a bucket.
- The tape marks trades from an earlier coverage segment ("before gap").
- The deck-log compatibility test reads its log from `HL_DECK_LIVE_LOG`.
