# Build report: hyperliquid-desk v1 (Handoff 1)

Author: Claude Code · Nov 7–8, 2026 · prompt: Kevin's Handoff 1 prompt and its subagent notes, plus Kevin's later instructions (answers to the open questions, the review loop, and the decisions for the review-1 fix round). Private operational details are left out of this file at Kevin's request, and folder paths are written as `~`.

## Summary

hyperliquid-desk v1 is a Hermes Agent plugin on Hyperliquid's public data. It has:
- six read-only tools;
- a `/hl` command;
- a commentary skill;
- a dashboard tab;
- one explicitly started collector per Hermes profile, whose owner process answers the profile's other Hermes processes over a local socket.

It is pinned to and tested on **Hermes v0.23.0**, declared as `requires_hermes: ">=0.23.0"` in both `plugin.yaml` and the draft catalog entry.

Status at this revision: Codex review 7 **accepted** `a4d146c…`, the first parentless publication commit, with no blocking findings. Its one deferred item is a verification limit: the demo's chat narration couldn't run in a profile without a model provider. `docs/DEMO.md` now states that prerequisite.

`a4d146c` was not tagged. Kevin then asked (Nov 8) for a further-cleaned publication commit to be reviewed and tagged instead:
- the current files contain none of his home path, storage layout, coordination paths, local server, port or process details, and no email address but the GitHub no-reply one;
- they are committed as a new commit with no parent, with author and committer `KevinM0001 <210251245+KevinM0001@users.noreply.github.com>`, on the local branch `v1-public-candidate`;
- Codex review 8 is asked to review that exact commit, and on ACCEPT it is tagged `v1-public-btc`;
- nothing is pushed.

**The old history has not been deleted.** The local repository still holds the development history on `main`, `a4d146c` on `v1-public`, and their reflogs, untouched; none of it is reachable from the commit under review. Deleting it is a separate phase that Kevin will approve after review 8.

The plugin code is byte-identical to `a4d146c` and to `6593e38`, where the official checks in `evidence/logs/` ran: 179 tests on Python 3.14.9 and 3.12.3, under a long `--basetemp` and with Hermes's storage helper importable; doctor, validate and the bundle check. Since then only evidence, docs and one comment in the design mockup changed.

**History.** The commit under review has no parent, so its history is that one commit. Its SHA is in the session's final message, because a file can't contain its own commit's SHA. The build was developed on Nov 7–8 in 17 earlier commits, which remain in the local repository on other branches: the first build, a fix commit and a response commit for each review round below, and `a4d146c`. Commit SHAs cited in this report and the response files refer to those.

## Preflight (before the first write)

The check ran before anything was written in `~/contest`. It used `realpath ~/contest`, `findmnt -T` on `~/contest`, on the node's data storage and on each device it uses, and `git rev-parse` to make sure `~/contest` isn't inside a repo.
- `~/contest` is a real directory on the home filesystem.
- It isn't on the node's data storage or any device it uses.
- It isn't inside any repo.

`evidence/logs/preflight.log` keeps the checks and conclusions; details of the machine's storage are left out.

## Hermes release and install

- **Pinned release: v0.23.0** (released 2026-11-05, the latest stable; commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`). The same SemVer floor, `>=0.23.0`, is in `plugin.yaml` and `catalog/hyperliquid-desk.yaml`.
- **Install.** `git clone --depth 1 --branch v0.23.0` into `~/contest/hermes`, then `source ./activate`, the documented source install. It ran under `env -i` with `~/contest/hermes-env.sh`, which points HOME, HERMES_HOME (`~/contest/hermes-home`), the runtime directory, XDG, uv, pip, npm and TMPDIR all inside `~/contest`.
- **Executable,** always called by full path: `~/contest/hermes/.hermes/bin/hermes`. It reports `Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d`, Python 3.14.9.
- **Writes stay inside `~/contest`.** After the install, every file under the home directory that was newer than a marker and outside `~/contest` belonged to other software already running on the machine. Nothing came from this install.
- **The machine's own Hermes installation was never used or modified.** The contest dashboard always ran with `--isolated` on its own port, so it never attached to another Hermes server. It was stopped by its own process ID, never with `hermes dashboard --stop`.

## What was built (how each rule is met)

| Requirement | Where |
| --- | --- |
| Provenance (mode, source, market, snapshot ID, coverage) on every result | `hl_desk/state.py` `MarketState.build` → `provenance`; every tool's envelope (`hl_desk/tools.py` `_envelope`); the tab's provenance strip |
| Freshness separate from provenance | `hl_desk/freshness.py`, `hl_desk/clock.py`; schema `freshness`. Fresh needs an advancing feed **and** a recent source time under a known clock bound (kernel max error while NTP-synchronized, or the `clock_error_bound_ms` setting). Statuses: fresh, delayed, stale, unverified, warming_up, disconnected, stopped. Re-checked every second. Heartbeats never count. |
| l2Book vs bbo vs context | l2Book: source time advances (fast book every ~0.54 s, receipt limit 3 s, source limit 5 s; 20-level and info books ~5.4 s, 15 s and 20 s). bbo: no progress signal, always unverified, never shown. activeAssetCtx: no source time, so always unverified (Kevin's decision 7). Trades: 30, 60 or 120 s receipt limits; late trades never refresh. |
| Null with a reason, never zero; status beside provenance | `hl_desk/util.py` `val`/`na`; tools' `status` beside `provenance`; the tab's "unavailable" chips; the skill §3–4 |
| Aggregated levels, "API count", both doc descriptions | `hl_desk/book.py` `API_COUNT_NOTE`, `ladder_rows`; a missing `n` is a null API count with a reason |
| Depth bands | Exact only from full-precision levels that reach the band; coarse nSigFigs books give a labeled approximation with a range (Kevin's decision 10). Otherwise "observed depth within returned levels". |
| One versioned schema | `schema/metrics.v1.json` v1.0.0, 52 metric entries plus freshness, regimes and alerts. Every published number has an entry (tested). |
| Aggressor side, trade identity, once only, observed window | `hl_desk/trades.py`: identity (coin, time, tid); the subscription snapshot isn't counted; gaps reset the windows |
| Addresses kept, never attributed | trades, wallets, tape, the deck's top wallet; skill §7 |
| One state owner per profile; bounded worker with explicit start, shutdown and restart | `hl_desk/runtime.py`: Hermes `SingletonSlot` ("Thread-safe lazy singletons") and an exclusive `flock` in `plugin_data_dir` ("Store durable state"). The worker comes from `agent.memory_provider.spawn_context_thread` (docs "Background threads"), and is controlled by `/hl start|stop|restart` ("Register slash commands") and the tab's buttons. The owner service (`hl_desk/owner.py`) answers other processes of the profile on a 0600 Unix socket: snapshots by ID, frozen wallet execution sets, status, stop, restart. Never started by import, register, doctor or validate (tested; doctor blocks sockets during register). |
| No blocking work on the gateway event loop | dashboard routes are `async`, and every blocking call goes through `asyncio.to_thread` (statically checked by `test_routes_do_no_blocking_work_on_the_event_loop`); `/hl` is a sync handler, which Hermes runs in its executor; the collector has its own loop and thread |
| Step 1: metadata incl. HIP-3; unknown markets unsupported | `hl_desk/markets.py`; `tests/test_metadata.py` (check per market, fixture check per market, unknown markets) |
| Step 2: websocket collector, bounded dependency, backoff, visible disconnected state | `hl_desk/collector.py`; `pyproject.toml` `websockets>=13.0,<16` (Hermes pins 15.0.1); backoff 1–30 s with jitter |
| Step 3: six tools, JSON, never raise; `/hl` | `hl_desk/tools.py`, `hl_desk/commands.py`; `tests/test_tool_shapes.py` |
| Step 4: commentary skill | `skills/hyperliquid-commentary/SKILL.md` |
| Step 5: tab with every named panel; Lightweight Charts with price lines; node hooks; local readable bundle with license, notice and attribution | `dashboard/`; `scripts/build_dashboard.py --check`; `THIRD_PARTY_NOTICES.md`; `evidence/screenshots/` |
| Step 5a: Flow Deck | `hl_desk/deck.py`; schema `regimes`, `deck.*`, `alerts.burst` |
| Step 5b: wallets | `hl_desk/wallets.py`, `state._wallet_panel`, `tools.drilldown` (from the snapshot's frozen execution set) |

## Flow Deck port (step 5a)

- **Source.** Kevin named `hl_deck.py` (the Oct 9 version). It was read, never modified; it loads no settings file and imports no local module.
- **Ported** (definitions, not code):
  - the regime order and every threshold (THIN, VACUUM thin and impact, PULLED, ABSORBING, TRENDING, BALANCED, QUIET);
  - the 2-tick latch and the SPARSE rule (a flag on the live classification);
  - the 90 s window and 15 s tick;
  - the card numbers (move, net flow, volume, IMPACT with its $100K floor, PRESSURE, top taker wallet and share, accounts).
- **Changed by Kevin's decisions on Nov 8:**
  - PRESSURE is aggressive buy over aggressive sell notional, flips included, with a $25K selling floor, in both modes. So TRENDING fires on public data and the plugin's earlier UNRESOLVED state is gone.
  - The explanation texts are GPT's cause-free replacement wording, not hl_deck's GUIDE text.
  - The card shows the confirmed regime, the live classification and any pending change separately.
- **Plugin additions:**
  - the top wallet's net aggressive notional;
  - gap handling: a regime ends at a gap, and nothing spans one;
  - ticks on source time, classified only when the 90 s window lies inside one coverage segment.
- **Burst alerts** are the handoff's definition: ≥ $10M aggressive buying or selling within (W − 60 s, W] for BTC, with plugin defaults of $2M for HYPE and $1M for the xyz markets, configurable. They name the aggressive wallets ranked against the burst's total.
- **Not ported:** hl_deck's SELLING DONE, CONVICTION and whale GONE/BACK/FLIPPED alerts (schema `alerts.not_ported`).
- **Compatibility check.** `test_deck_live_log_is_format_compatible` reads the deck's own output log, a copy given through `HL_DECK_LIVE_LOG`. Every regime name and event line in it maps onto the plugin's vocabulary. Values aren't compared; they come from node data.

## Review loop

1. **Review 1** of `b21d205…`: Codex returned REVISE, with 9 blocking findings and 1 deferred, in Codex's review file (kept outside this repo).
2. **Response.** `evidence/review-response-b21d205eac6d19390a3b18de2b24b344090cff78.md` answers each finding (all fixed; finding 9's history part disputed with Kevin's statement). It also records Kevin's decisions on GPT review R11 findings 7–10, made in the same round.
3. **Review 2** of `16cb0e3…`: REVISE, with 6 blocking findings and 3 deferred, in Codex's review file (kept outside this repo).
4. **Response.** `evidence/review-response-16cb0e362810a5dde8fdcad514c3d66d7ad997eb.md` answers each finding:
   - findings 2–6 and the three deferred ones are fixed in `e7569a7`, with tests in `tests/test_review2.py`;
   - finding 1 is disputed a second time, with Kevin's statements and the fact that the repository has no remote. It names the two ways Kevin can settle it. If review 3 raises it unchanged, it goes to Kevin as WAITING-KEVIN.
5. **Review 3** of `556b093…`: REVISE, with 4 blocking findings and 4 deferred, in Codex's review file (kept outside this repo). Finding 1 was the history finding again, unchanged after two disputes, so it was posted to Kevin as WAITING-KEVIN.
6. **Response.** `evidence/review-response-556b09394569a488ae99245e71df5af16cb649ec.md` answers each finding:
   - findings 2–4 and the four deferred ones are fixed in `2a0ed13`, with tests in `tests/test_review3.py`;
   - finding 1 is referred to Kevin, with the two options he has.
7. **Review 4** of `364fb76…`: REVISE, with 3 blocking findings and 2 deferred, in Codex's review file (kept outside this repo). Finding 1, the history finding, is unchanged and still with Kevin, and a WAITING-KEVIN reminder was posted.
8. **Response.** `evidence/review-response-364fb76b45451b3df5bdccba1ae735722ca3719a.md` answers each finding: findings 2–3 and both deferred ones are fixed in `af160d4`, with tests in `tests/test_review4.py`.
9. **Review 5** of `cd83e6f…`: REVISE, with 3 blocking findings and 1 deferred, in Codex's review file (kept outside this repo). Finding 1 is unchanged and still with Kevin.
10. **Response.** `evidence/review-response-cd83e6f555c7895136cf7beb198632a6bff6b8ef.md` answers each finding: findings 2–3 and the deferred one are fixed in `04c3aeb`, with tests in `tests/test_review5.py`.
11. **Review 6** of `cb9c9d7…`: REVISE, with 3 blocking findings, in Codex's review file (kept outside this repo). Kevin then decided finding 1: review a parentless commit of the current files.
12. **Response.** `evidence/review-response-cb9c9d7b84d4ad822e815609a821fdedac9835df.md` records Kevin's decision and how the parentless commit was made and checked. Findings 2–3 are fixed in `6593e38`, with tests in `tests/test_review6.py`.
13. **Review 7** of `a4d146c…`, the first parentless publication commit: **ACCEPT**, with no blocking findings and one deferred verification limit (the demo's chat narration needs a model provider). Before tagging, Kevin asked for a further-cleaned parentless commit to be reviewed and tagged instead, so `a4d146c` was not tagged.
14. **Response.** `evidence/review-response-a4d146c334ae508ee9e23dd03b48dc29b28bfb4f.md` records Kevin's request and what changed since `a4d146c`. It states that the old history has not been deleted (that is a separate phase after review 8), and answers the deferred item.
15. **Next.** Review 8 is of the new parentless commit on `v1-public-candidate`; its SHA is in the session's final message.

## Live checks

All live checks ran in the contest Hermes v0.23.0, through an isolated contest dashboard and a separate Hermes process.
- **Install.** The plugin directory was copied into `~/contest/hermes-home/plugins/hyperliquid-desk` (the documented "drop a directory into plugins/" install) and enabled with `hermes plugins enable hyperliquid-desk`. The installed copy matches the commit under test.
- **Tab, live.** The tab's **Start** button was pressed and live data collected for about 2 minutes:
  - `evidence/screenshots/step7-tab-live-BTC-top.png` and `step7-tab-live-BTC-full.png` show every panel on live BTC data;
  - the Flow Deck shows "confirmed", "now" and "pending" lines;
  - depth shows exact bands and a labeled approximation;
  - mark, oracle and funding show unverified, because they have no source timestamp;
  - there were no browser console errors.
- **Tab, stopped.** **Stop** was then pressed; `step7-tab-after-stop.png` shows the result.
- **Cross-process** (Codex finding 1). A separate Hermes process loaded the plugin through Hermes's own plugin manager:
  - all six tools answered with the dashboard's copied snapshot ID;
  - the wallet drill-down equaled the panel row;
  - `/hl status` reported the owner's collector;
  - `/hl stop` stopped it, and the tab then reported that the collector isn't running.
- **Wallet drill-down** (Codex review 2, finding 5), at `e7569a7`. After the tab's Start, a wallet was opened from the BTC panel. The account state showed "fresh", with the answer's own time (0.126 s old when received, limit 10 s), and its positions. A liquidation price that Hyperliquid doesn't give showed as unavailable. Open orders showed as unavailable with the no-timestamp reason. The overlay now starts at the tab's edge, clear of the Hermes sidebar. There were no browser console errors. Stop was then pressed, and the dashboard process was stopped. Screenshot: `evidence/screenshots/review2-wallet-drilldown-live.png`.
- **Wallet drill-down, request time counted** (Codex review 3, finding 4), at `2a0ed13`. The account state showed "fresh · … · 0.575 s old when served, request time included (limit 10 s)". Open orders showed as unavailable, with the time that answer arrived. There were no browser console errors. Stop was then pressed, and the dashboard process was stopped. Screenshot: `evidence/screenshots/review3-wallet-drilldown-live.png`.
- **Wallet window aging** (Codex review 4, finding 2), at `af160d4`. 5 s after a wallet was opened, the account state was fresh ("now about 5.1 s old"). At 17 s it read "expired, not current", with every value unavailable and the reason. **Fetch account again** then showed a fresh answer with new values. There were no browser console errors. Stop was then pressed, and the dashboard process was stopped. Screenshot: `evidence/screenshots/review4-wallet-account-expired-live.png`.
- **Wallet answer delayed in delivery** (Codex review 5, finding 2), at `04c3aeb`. Chrome's request interception held the real `/wallets` answer for 12.0 s, then delivered it unchanged. The backend had assessed it as 0.6 s old. 0.7 s after arrival, the window read "expired, not current", about 13.4 s old, with every value withheld. Without the delay, a wallet opened fresh (0.456 s when served). There were no browser console errors. Stop was then pressed, and the dashboard process was stopped. Screenshot: `evidence/screenshots/review5-wallet-delayed-delivery-live.png`.
- **Snapshot delayed in delivery** (Codex review 6, finding 2), at `6593e38`.
  - **Normal operation.** For 60 s of live operation, sampled twice a second, the panels were on screen in all 148 samples; the tab never blanked.
  - **Delayed answer.** Chrome's request interception then held one real `/snapshot` answer for 12.0 s and delivered it unchanged. 0.5 s later the tab showed no panels and read "the latest snapshot is now about 12.3 s old (limit 5 s) … aren't shown as current". The next normal answer restored the panels within 4 s.
  - There were no browser exceptions. Stop was then pressed, and the dashboard process was stopped. Screenshot: `evidence/screenshots/review6-snapshot-delayed-delivery-live.png`.
- **Mainnet smoke** (first build, outside Hermes, 150 s): every feed for all four markets stayed fresh, every tool returned `ok`, and the stop was clean.

## Test, doctor and validate output (verbatim; folder paths shortened to `~`)

All final runs were made at `6593e389430e259947ad6636ed87630317dd4f73`, whose plugin files are identical to the reviewed commit's.
- **By the test-runner subagent:** the tests on Python 3.14.9 and on 3.12.3, the tests under a long `--basetemp`, the tests with the contest Hermes source on `PYTHONPATH` (Hermes's storage helper importable; a throwaway `HERMES_HOME` received no files), the bundle check, doctor, and the first validate attempt.
- **Validate.** Its first two attempts printed "Validation passed" and exited 0, but their dependency sync failed. Hermes's own dependency lock couldn't fetch a Hermes dependency's wheel (`kittentts` 0.8.1) from a GitHub release: GitHub's edge returned 504 Gateway Timeout, and the receipts say `outcome: failed`. Both attempts are kept, in `evidence/logs/plugins-validate.attempt1-github-504.log` and `attempt2`. Once that download answered again, I ran validate a third time, unchanged, and it passed with a successful dependency sync: `evidence/logs/plugins-validate.log`, below.

Each command's output was captured outside the plugin tree, then copied into `evidence/logs/`. The only edit to the logs is the mechanical replacement of the home-directory prefix with `~`. `git status --short` was empty, with HEAD unchanged, before and after every run, and no tracked file changed. Validate's isolated `register()` probe rewrote git-ignored `.pyc` files under `hl_desk/__pycache__/`.

### Tests, Python 3.14.9 (Hermes's pinned interpreter): `evidence/logs/pytest-py3.14.log`

```
$ cd ~/contest/hyperliquid-desk && HL_DECK_LIVE_LOG=~/contest/.tmp/deck-live.log PYTHONDONTWRITEBYTECODE=1 TMPDIR=~/contest/.tmp ~/contest/devenv314/bin/python -m pytest -c tests/pytest.ini -v -rs tests
============================= test session starts ==============================
platform linux -- Python 3.14.9, pytest-9.2.0, pluggy-1.7.0 -- ~/contest/devenv314/bin/python
rootdir: ~/contest/hyperliquid-desk/tests
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 179 items

tests/test_book.py::test_band_not_reached_falls_back_to_observed_depth PASSED [  0%]
tests/test_book.py::test_exact_band_counts_only_full_precision_levels_inside_the_edge PASSED [  1%]
tests/test_book.py::test_coarse_buckets_are_a_labeled_approximation_with_a_range PASSED [  1%]
tests/test_book.py::test_empty_side_is_null_with_reason_not_zero PASSED  [  2%]
tests/test_book.py::test_empty_side_through_the_snapshot PASSED          [  2%]
tests/test_book.py::test_levels_are_labeled_api_count_and_aggregated PASSED [  3%]
tests/test_book.py::test_fast_and_full_books_are_told_apart_by_level_count PASSED [  3%]
tests/test_collector_local.py::test_collector_end_to_end_with_reconnect PASSED [  4%]
tests/test_consistency.py::test_tab_and_tools_return_the_same_numbers_for_one_snapshot_id PASSED [  5%]
tests/test_consistency.py::test_unknown_snapshot_and_stopped_collector PASSED [  5%]
tests/test_consistency.py::test_routes_do_no_blocking_work_on_the_event_loop PASSED [  6%]
tests/test_deck.py::test_quiet_when_no_trades PASSED                     [  6%]
tests/test_deck.py::test_thin_vs_vacuum_below_the_volume_floor PASSED    [  7%]
tests/test_deck.py::test_pulled_offers_and_bids_at_their_thresholds PASSED [  7%]
tests/test_deck.py::test_vacuum_by_impact_and_absorbing PASSED           [  8%]
tests/test_deck.py::test_trending_with_public_pressure_at_its_thresholds PASSED [  8%]
tests/test_deck.py::test_pressure_is_aggressive_buy_over_sell_with_the_25k_floor PASSED [  9%]
tests/test_deck.py::test_regime_order_first_match_wins PASSED            [ 10%]
tests/test_deck.py::test_window_metrics_match_independent_values PASSED  [ 10%]
tests/test_deck.py::test_latch_needs_two_ticks_and_events_carry_held_time PASSED [ 11%]
tests/test_deck.py::test_flip_flop_does_not_confirm PASSED               [ 11%]
tests/test_deck.py::test_incomplete_window_is_not_classified_and_numbers_are_null PASSED [ 12%]
tests/test_deck.py::test_gap_ends_the_regime_and_nothing_spans_it PASSED [ 12%]
tests/test_deck.py::test_sparse_is_a_flag_on_the_live_classification_only PASSED [ 13%]
tests/test_deck.py::test_card_reports_confirmed_live_and_pending_separately PASSED [ 13%]
tests/test_deck.py::test_burst_threshold_below_at_above[9999999.99-False] PASSED [ 14%]
tests/test_deck.py::test_burst_threshold_below_at_above[10000000.0-True] PASSED [ 15%]
tests/test_deck.py::test_burst_threshold_below_at_above[10000000.01-True] PASSED [ 15%]
tests/test_deck.py::test_burst_fires_once_then_rearms_after_falling_below PASSED [ 16%]
tests/test_deck.py::test_burst_never_spans_a_gap PASSED                  [ 16%]
tests/test_deck.py::test_meanings_are_the_adopted_wording_and_claim_no_cause PASSED [ 17%]
tests/test_deck.py::test_top_wallet_that_buys_and_sells_equally_shows_a_near_zero_net PASSED [ 17%]
tests/test_deck.py::test_top_wallet_net_aggressive_on_the_card_and_in_hl_alerts PASSED [ 18%]
tests/test_deck.py::test_unresolved_is_gone_everywhere PASSED            [ 18%]
tests/test_freshness.py::test_thresholds_are_the_schema_values PASSED    [ 19%]
tests/test_freshness.py::test_fast_book_max_age[0.0-fresh] PASSED        [ 20%]
tests/test_freshness.py::test_fast_book_max_age[3.0-fresh] PASSED        [ 20%]
tests/test_freshness.py::test_fast_book_max_age[3.001-stale] PASSED      [ 21%]
tests/test_freshness.py::test_frozen_source_behind_a_live_connection_turns_stale PASSED [ 21%]
tests/test_freshness.py::test_rechecked_without_any_new_message PASSED   [ 22%]
tests/test_freshness.py::test_quiet_market_values_are_null_not_zero PASSED [ 22%]
tests/test_freshness.py::test_quiet_window_with_real_zero_selling_is_a_real_zero PASSED [ 23%]
tests/test_freshness.py::test_bbo_is_never_fresh PASSED                  [ 24%]
tests/test_freshness.py::test_context_is_never_fresh_without_a_source_timestamp PASSED [ 24%]
tests/test_freshness.py::test_disconnect_makes_every_field_disconnected PASSED [ 25%]
tests/test_freshness.py::test_warming_up_before_the_first_message PASSED [ 25%]
tests/test_freshness.py::test_clock_skew_withholds_freshness_instead_of_being_ignored PASSED [ 26%]
tests/test_freshness.py::test_delayed_backlog_advancing_an_hour_behind_is_never_fresh PASSED [ 26%]
tests/test_freshness.py::test_source_age_limit_with_the_clock_bound[4950-fresh] PASSED [ 27%]
tests/test_freshness.py::test_source_age_limit_with_the_clock_bound[5000-unverified] PASSED [ 27%]
tests/test_freshness.py::test_source_age_limit_with_the_clock_bound[5051-delayed] PASSED [ 28%]
tests/test_freshness.py::test_unknown_clock_bound_means_unverified PASSED [ 29%]
tests/test_freshness.py::test_source_time_ahead_of_local_time_breaks_the_clock_relationship PASSED [ 29%]
tests/test_lifecycle.py::test_import_and_register_start_nothing PASSED   [ 30%]
tests/test_lifecycle.py::test_provides_tools_matches_registered PASSED   [ 30%]
tests/test_lifecycle.py::test_explicit_start_stop_restart_and_visible_disconnected_state PASSED [ 31%]
tests/test_lifecycle.py::test_one_state_owner_per_profile PASSED         [ 31%]
tests/test_metadata.py::test_metadata_check_passes[BTC] PASSED           [ 32%]
tests/test_metadata.py::test_metadata_check_passes[HYPE] PASSED          [ 32%]
tests/test_metadata.py::test_metadata_check_passes[xyz:SP500] PASSED     [ 33%]
tests/test_metadata.py::test_metadata_check_passes[xyz:XYZ100] PASSED    [ 34%]
tests/test_metadata.py::test_unknown_markets_are_unsupported[NOTACOIN-not in the meta universe] PASSED [ 34%]
tests/test_metadata.py::test_unknown_markets_are_unsupported[xyz:NOPE-not in the meta universe] PASSED [ 35%]
tests/test_metadata.py::test_unknown_markets_are_unsupported[zzz:BTC-not in perpDexs] PASSED [ 35%]
tests/test_metadata.py::test_perp_dexs_include_default_and_xyz PASSED    [ 36%]
tests/test_metadata.py::test_fixture_check[BTC] PASSED                   [ 36%]
tests/test_metadata.py::test_fixture_check[HYPE] PASSED                  [ 37%]
tests/test_metadata.py::test_fixture_check[xyz:SP500] PASSED             [ 37%]
tests/test_metadata.py::test_fixture_check[xyz:XYZ100] PASSED            [ 38%]
tests/test_review1.py::test_other_process_reads_snapshot_by_id_drilldown_and_stops_the_owner PASSED [ 39%]
tests/test_review1.py::test_killed_owner_leaves_nothing_that_reads_as_current PASSED [ 39%]
tests/test_review1.py::test_latest_older_than_the_limit_is_refused_and_ids_are_labeled_historical PASSED [ 40%]
tests/test_review1.py::test_late_trade_does_not_refresh_trades_freshness PASSED [ 40%]
tests/test_review1.py::test_burst_does_not_repeat_within_one_episode_at_the_same_timestamp PASSED [ 41%]
tests/test_review1.py::test_burst_new_episode_in_one_message_fires PASSED [ 41%]
tests/test_review1.py::test_drilldown_unchanged_by_a_later_late_trade PASSED [ 42%]
tests/test_review1.py::test_drilldown_complete_after_old_trades_leave_the_live_store PASSED [ 43%]
tests/test_review1.py::test_hl_flow_cvd_slice_states_its_cumulative_origin PASSED [ 43%]
tests/test_review1.py::test_missing_api_count_is_null_with_reason PASSED [ 44%]
tests/test_review1.py::test_missing_level_size_invalidates_that_sides_totals PASSED [ 44%]
tests/test_review1.py::test_malformed_best_level_makes_the_top_unavailable PASSED [ 45%]
tests/test_review1.py::test_candle_volume_is_summed_exactly_then_rounded_once PASSED [ 45%]
tests/test_review1.py::test_every_published_metric_has_a_schema_entry PASSED [ 46%]
tests/test_review2.py::test_malformed_best_bid_never_yields_a_covered_band PASSED [ 46%]
tests/test_review2.py::test_malformed_best_level_blocks_older_mids_for_later_depth_books PASSED [ 47%]
tests/test_review2.py::test_trade_missing_an_essential_field_is_a_gap_not_a_zero[side] PASSED [ 48%]
tests/test_review2.py::test_trade_missing_an_essential_field_is_a_gap_not_a_zero[users] PASSED [ 48%]
tests/test_review2.py::test_trade_missing_an_essential_field_is_a_gap_not_a_zero[tid] PASSED [ 49%]
tests/test_review2.py::test_trade_with_a_bad_side_or_address_is_rejected PASSED [ 49%]
tests/test_review2.py::test_every_tool_keeps_the_served_age_and_historical_label PASSED [ 50%]
tests/test_review2.py::test_hour_old_account_state_is_delayed_and_null PASSED [ 50%]
tests/test_review2.py::test_fresh_account_state_shows_values_and_missing_positions_are_unknown_not_empty PASSED [ 51%]
tests/test_review2.py::test_open_orders_are_never_presented_as_current PASSED [ 51%]
tests/test_review2.py::test_schema_describes_exact_approximate_cvd_slice_and_account_metrics PASSED [ 52%]
tests/test_review2.py::test_pressure_is_not_declared_node_only PASSED    [ 53%]
tests/test_review2.py::test_long_data_dir_gets_a_short_private_socket PASSED [ 53%]
tests/test_review2.py::test_readme_describes_in_memory_snapshots_only PASSED [ 54%]
tests/test_review3.py::test_malformed_gap_restarts_freshness_before_any_new_trade PASSED [ 54%]
tests/test_review3.py::test_dispatcher_routes_a_message_whose_first_record_has_no_coin PASSED [ 55%]
tests/test_review3.py::test_unattributable_trades_message_ends_every_markets_coverage PASSED [ 55%]
tests/test_review3.py::test_record_naming_another_coin_is_invalid_for_the_subscription PASSED [ 56%]
tests/test_review3.py::test_slow_account_requests_count_as_age PASSED    [ 56%]
tests/test_review3.py::test_account_without_a_serving_time_is_unverified PASSED [ 57%]
tests/test_review3.py::test_negative_or_zero_sizes_and_fractional_counts_are_rejected PASSED [ 58%]
tests/test_review3.py::test_skill_does_not_call_pressure_node_only PASSED [ 58%]
tests/test_review3.py::test_cvd_rule_and_clock_note_are_current PASSED   [ 59%]
tests/test_review3.py::test_lifecycle_expects_the_path_sock_path_returns PASSED [ 59%]
tests/test_review4.py::test_open_wallet_account_expires_after_its_limit PASSED [ 60%]
tests/test_review4.py::test_wallet_window_withholds_expired_values_and_can_fetch_again PASSED [ 60%]
tests/test_review4.py::test_oldest_candle_holds_every_trade_of_its_interval PASSED [ 61%]
tests/test_review4.py::test_clock_bound_lookup_launches_no_subprocess PASSED [ 62%]
tests/test_review4.py::test_data_dir_override_is_ignored_inside_hermes PASSED [ 62%]
tests/test_review4.py::test_data_dir_override_outside_hermes_only PASSED [ 63%]
tests/test_review5.py::test_answer_delayed_in_delivery_is_expired_on_arrival PASSED [ 63%]
tests/test_review5.py::test_tab_ages_the_account_from_the_request_and_renders_with_the_current_clock PASSED [ 64%]
tests/test_review5.py::test_unverified_cvd_slice_base_is_null_with_reason PASSED [ 64%]
tests/test_review5.py::test_cvd_slice_base_is_zero_only_when_nothing_precedes_it PASSED [ 65%]
tests/test_review5.py::test_standalone_storage_helper_wins_over_an_importable_hermes PASSED [ 65%]
tests/test_review6.py::test_snapshot_delayed_in_delivery_is_withheld_on_arrival PASSED [ 66%]
tests/test_review6.py::test_tab_checks_validity_on_arrival_and_while_shown PASSED [ 67%]
tests/test_review6.py::test_late_trade_updates_the_displayed_card PASSED [ 67%]
tests/test_review6.py::test_late_delivery_ends_in_the_same_state_as_ordered_delivery PASSED [ 68%]
tests/test_schema_and_commands.py::test_schema_version_and_every_metric_fully_defined PASSED [ 68%]
tests/test_schema_and_commands.py::test_hl_command_reports_and_never_autostarts PASSED [ 69%]
tests/test_schema_and_commands.py::test_deck_live_log_is_format_compatible PASSED [ 69%]
tests/test_thresholds_and_review.py::test_full_book_max_age_15s[15.0-fresh] PASSED [ 70%]
tests/test_thresholds_and_review.py::test_full_book_max_age_15s[15.001-stale] PASSED [ 70%]
tests/test_thresholds_and_review.py::test_info_depth_books_max_age_15s[15.0-fresh-4-book_sf4] PASSED [ 71%]
tests/test_thresholds_and_review.py::test_info_depth_books_max_age_15s[15.0-fresh-3-book_sf3] PASSED [ 72%]
tests/test_thresholds_and_review.py::test_info_depth_books_max_age_15s[15.001-stale-4-book_sf4] PASSED [ 72%]
tests/test_thresholds_and_review.py::test_info_depth_books_max_age_15s[15.001-stale-3-book_sf3] PASSED [ 73%]
tests/test_thresholds_and_review.py::test_trades_max_age_per_market[HYPE-60.0] PASSED [ 73%]
tests/test_thresholds_and_review.py::test_trades_max_age_per_market[xyz:SP500-120.0] PASSED [ 74%]
tests/test_thresholds_and_review.py::test_trades_max_age_per_market[xyz:XYZ100-120.0] PASSED [ 74%]
tests/test_thresholds_and_review.py::test_ctx_receipt_age_per_market_and_never_fresh[HYPE-20.0] PASSED [ 75%]
tests/test_thresholds_and_review.py::test_ctx_receipt_age_per_market_and_never_fresh[xyz:SP500-45.0] PASSED [ 75%]
tests/test_thresholds_and_review.py::test_ctx_receipt_age_per_market_and_never_fresh[xyz:XYZ100-45.0] PASSED [ 76%]
tests/test_thresholds_and_review.py::test_clock_skew_warn_threshold[2000-False] PASSED [ 77%]
tests/test_thresholds_and_review.py::test_clock_skew_warn_threshold[2001-True] PASSED [ 77%]
tests/test_thresholds_and_review.py::test_clock_skew_warn_threshold[-2001-True] PASSED [ 78%]
tests/test_thresholds_and_review.py::test_depth_mid_must_be_within_1500ms[1500-True] PASSED [ 78%]
tests/test_thresholds_and_review.py::test_depth_mid_must_be_within_1500ms[1501-False] PASSED [ 79%]
tests/test_thresholds_and_review.py::test_stall_reconnect_threshold[14.99-False] PASSED [ 79%]
tests/test_thresholds_and_review.py::test_stall_reconnect_threshold[15.001-True] PASSED [ 80%]
tests/test_thresholds_and_review.py::test_mirror_side_regimes_enter_and_leave PASSED [ 81%]
tests/test_thresholds_and_review.py::test_partial_and_unverified_values_are_null_with_reason PASSED [ 81%]
tests/test_thresholds_and_review.py::test_wallet_drilldown_with_recorded_info_responses PASSED [ 82%]
tests/test_thresholds_and_review.py::test_one_collector_per_profile_in_one_process PASSED [ 82%]
tests/test_tool_shapes.py::test_market_tools_shape[hl_market] PASSED     [ 83%]
tests/test_tool_shapes.py::test_market_tools_shape[hl_flow] PASSED       [ 83%]
tests/test_tool_shapes.py::test_market_tools_shape[hl_context] PASSED    [ 84%]
tests/test_tool_shapes.py::test_market_tools_shape[hl_wallets] PASSED    [ 84%]
tests/test_tool_shapes.py::test_market_tool_specifics PASSED             [ 85%]
tests/test_tool_shapes.py::test_status_and_alerts_shape PASSED           [ 86%]
tests/test_tool_shapes.py::test_wallet_drilldown_shape_and_totals PASSED [ 86%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_market-args0-market is required] PASSED [ 87%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_market-args1-unsupported] PASSED [ 87%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_flow-args2-market is required] PASSED [ 88%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_wallets-args3-window_min must be 1, 5 or 15] PASSED [ 88%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_wallets-args4-window_min must be 1, 5 or 15] PASSED [ 89%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_context-args5-no longer retained] PASSED [ 89%]
tests/test_tool_shapes.py::test_bad_input_returns_json_error_never_raises[hl_alerts-args6-unsupported] PASSED [ 90%]
tests/test_tool_shapes.py::test_bad_address_and_no_snapshot PASSED       [ 91%]
tests/test_tool_shapes.py::test_tool_schemas_are_openai_style PASSED     [ 91%]
tests/test_trades.py::test_subscription_snapshot_is_not_counted_and_starts_the_segment PASSED [ 92%]
tests/test_trades.py::test_duplicate_trade_counted_once_even_across_a_reconnect PASSED [ 92%]
tests/test_trades.py::test_reconnect_records_a_gap_and_windows_restart PASSED [ 93%]
tests/test_trades.py::test_late_trade_is_flagged PASSED                  [ 93%]
tests/test_trades.py::test_recorded_session_has_a_real_reconnect_gap_and_no_double_counting PASSED [ 94%]
tests/test_trades.py::test_tape_marks_trades_from_before_a_gap PASSED    [ 94%]
tests/test_wallets.py::test_handoff_example_shares_and_net PASSED        [ 95%]
tests/test_wallets.py::test_handoff_example_ranks_and_target PASSED      [ 96%]
tests/test_wallets.py::test_two_hundred_equal_wallets_hit_the_cap PASSED [ 96%]
tests/test_wallets.py::test_ties_at_the_cutoff_break_by_address PASSED   [ 97%]
tests/test_wallets.py::test_empty_side_share_is_unavailable_not_zero PASSED [ 97%]
tests/test_wallets.py::test_duplicated_execution_counts_once_through_the_store PASSED [ 98%]
tests/test_wallets.py::test_wallet_drilldown_trades_add_up_to_totals PASSED [ 98%]
tests/test_wallets.py::test_shares_match_an_independent_calculation_on_recorded_trades PASSED [ 99%]
tests/test_wallets.py::test_window_never_spans_a_gap_and_reports_completeness PASSED [100%]

=============================== warnings summary ===============================
../devenv314/lib/python3.14/site-packages/fastapi/testclient.py:1
  ~/contest/devenv314/lib/python3.14/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 179 passed, 1 warning in 11.29s ========================
[exit code: 0]
```

### Tests, Python 3.12.3: `evidence/logs/pytest-py3.12.log`

The same 179 tests in the same order. First and final lines:

```
$ cd ~/contest/hyperliquid-desk && HL_DECK_LIVE_LOG=~/contest/.tmp/deck-live.log PYTHONDONTWRITEBYTECODE=1 TMPDIR=~/contest/.tmp ~/contest/devenv/bin/python -m pytest -c tests/pytest.ini -v -rs tests
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.2.0, pluggy-1.7.0 -- ~/contest/devenv/bin/python
rootdir: ~/contest/hyperliquid-desk/tests
…
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 179 passed, 1 warning in 11.94s ========================
[exit code: 0]
```

### Tests under a long temp path: `evidence/logs/pytest-py3.14-long-basetemp.log`

```
$ cd ~/contest/hyperliquid-desk && HL_DECK_LIVE_LOG=~/contest/.tmp/deck-live.log PYTHONDONTWRITEBYTECODE=1 TMPDIR=~/contest/.tmp ~/contest/devenv314/bin/python -m pytest -c tests/pytest.ini -q tests --basetemp=~/contest/.tmp/longpath-check-556b09394569a488ae99245e71df5af16cb649ec/testtmp-r6
…
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
179 passed, 1 warning in 11.46s
[exit code: 0]
```

### Tests with Hermes's storage helper importable: `evidence/logs/pytest-py3.14-hermes-importable.log`

```
$ cd ~/contest/hyperliquid-desk && HERMES_HOME=~/contest/.tmp/hh-pytest-r6-official PYTHONPATH=~/contest/hermes HL_DECK_LIVE_LOG=~/contest/.tmp/deck-live.log PYTHONDONTWRITEBYTECODE=1 TMPDIR=~/contest/.tmp ~/contest/devenv314/bin/python -m pytest -c tests/pytest.ini -q tests
…
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
179 passed, 1 warning in 11.49s
[exit code: 0]
```

### Dashboard bundle check: `evidence/logs/build-dashboard-check.log`

```
$ cd ~/contest/hyperliquid-desk && python3 scripts/build_dashboard.py . --check
dashboard/dist/index.js is up to date (935143 bytes, sha256 7d392996b7ab8da49791f7d20bf9b868bc4a042e2819497adaf3d9713dc2b0e7)
[exit code: 0]
```

### `plugins doctor . --ci`: `evidence/logs/plugins-doctor.log`

```
$ env -i bash --noprofile --norc -c 'source ~/contest/hermes-env.sh; cd ~/contest/hyperliquid-desk && ~/contest/hermes/.hermes/bin/hermes --version && ~/contest/hermes/.hermes/bin/hermes plugins doctor . --ci'
Hermes Agent v0.23.0 (2026.9.24) · stable · local 2bd378ed1b8d
Install directory: ~/contest/hermes
Install method: git
Python: 3.14.9
OpenAI SDK: 2.54.0
Up to date
Plugin Doctor: ~/contest/hyperliquid-desk
  manifest: hyperliquid-desk 1.0.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 6 tool(s), 0 hook(s)
[exit code: 0]
```

### `plugins validate . --install-deps`: `evidence/logs/plugins-validate.log`

```
$ env -i bash --noprofile --norc -c 'source ~/contest/hermes-env.sh; cd ~/contest/hyperliquid-desk && ~/contest/hermes/.hermes/bin/hermes plugins validate . --install-deps'
  → Resolving Python dependencies…
  ✓ Resolving Python dependencies
  → Installing Python dependencies…
  ✓ Installing Python dependencies

✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.23.0' parses
✓ config schema — shape valid
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — 1 requirements from pyproject
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no collisions
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)
  · dashboard backend API is served through the plugin host (buffered responses)

Validation passed.
[exit code: 0]
```

The dependency environment was committed: the receipt this run wrote (`logs/update_receipts/pm_20261108T090127Z-sync-….json` under the contest Hermes home) shows `outcome: ok, exit_code: 0`, and the install's `facts.json` was updated within the run.

The first validate attempt of the first build is kept in `evidence/logs/plugins-validate.attempt1-log-written-inside-tree.log`. It printed "Validation passed" and exited 0, but its dependency commit failed ("Dependency inputs changed while preparing publication; retry."). Its own log was being written inside the plugin tree, which Hermes hashes before and after when a `pyproject.toml` is present (`pm/workspace.py:43-63`, `pm/install.py:833`). Every later run writes its output outside the tree.

## Endpoints used

All are listed, each with its doc link and quote, in `docs/ENDPOINTS.md`. All are public and read-only.
- **Info, `POST https://api.hyperliquid.xyz/info`:**
  - `perpDexs` and `meta` (with `dex`): the metadata check, at collector start;
  - `l2Book` with `nSigFigs` 4 and 3: the approximate depth books, every 5 s per market while connected;
  - `clearinghouseState` and `frontendOpenOrders` (with `user` and the market's `dex`): only when a wallet is opened.
- **Websocket, `wss://api.hyperliquid.xyz/ws`:** `l2Book` (`fast: true`, 5 levels), `l2Book` (20 levels), `trades`, `bbo`, `activeAssetCtx`, and `{"method": "ping"}`.
- **Never:** `/exchange`, signing or keys. `hl_desk/info_client.py` refuses any other info type.
- **Local only:** the owner service is a Unix socket in the plugin's data directory; there is no network listener.

## Files (at the commit that adds this report)

103 files:

- `.gitignore`
- `LICENSE`
- `README.md`
- `THIRD_PARTY_NOTICES.md`
- `__init__.py`
- `catalog/hyperliquid-desk.yaml`
- `dashboard/dist/index.js`
- `dashboard/dist/style.css`
- `dashboard/manifest.json`
- `dashboard/plugin_api.py`
- `dashboard/src/app.js`
- `dashboard/vendor/lightweight-charts/LICENSE`
- `dashboard/vendor/lightweight-charts/NOTICE`
- `dashboard/vendor/lightweight-charts/README.md`
- `dashboard/vendor/lightweight-charts/lightweight-charts.standalone.development.js`
- `design/dashboard-mockup.dc.html`
- `docs/DEMO.md`
- `docs/ENDPOINTS.md`
- `docs/SNAPSHOT.md`
- `evidence/BUILD_REPORT.md`
- `evidence/logs/build-dashboard-check.log`
- `evidence/logs/plugins-doctor.log`
- `evidence/logs/plugins-validate.attempt1-github-504.log`
- `evidence/logs/plugins-validate.attempt1-log-written-inside-tree.log`
- `evidence/logs/plugins-validate.attempt2-github-504.log`
- `evidence/logs/plugins-validate.log`
- `evidence/logs/preflight.log`
- `evidence/logs/pytest-py3.12.log`
- `evidence/logs/pytest-py3.14-hermes-importable.log`
- `evidence/logs/pytest-py3.14-long-basetemp.log`
- `evidence/logs/pytest-py3.14.log`
- `evidence/review-response-16cb0e362810a5dde8fdcad514c3d66d7ad997eb.md`
- `evidence/review-response-364fb76b45451b3df5bdccba1ae735722ca3719a.md`
- `evidence/review-response-556b09394569a488ae99245e71df5af16cb649ec.md`
- `evidence/review-response-a4d146c334ae508ee9e23dd03b48dc29b28bfb4f.md`
- `evidence/review-response-b21d205eac6d19390a3b18de2b24b344090cff78.md`
- `evidence/review-response-cb9c9d7b84d4ad822e815609a821fdedac9835df.md`
- `evidence/review-response-cd83e6f555c7895136cf7beb198632a6bff6b8ef.md`
- `evidence/screenshots/review2-wallet-drilldown-live.png`
- `evidence/screenshots/review3-wallet-drilldown-live.png`
- `evidence/screenshots/review4-wallet-account-expired-live.png`
- `evidence/screenshots/review5-wallet-delayed-delivery-live.png`
- `evidence/screenshots/review6-snapshot-delayed-delivery-live.png`
- `evidence/screenshots/step7-tab-after-stop.png`
- `evidence/screenshots/step7-tab-live-BTC-full.png`
- `evidence/screenshots/step7-tab-live-BTC-top.png`
- `hl_desk/__init__.py`
- `hl_desk/book.py`
- `hl_desk/clock.py`
- `hl_desk/collector.py`
- `hl_desk/commands.py`
- `hl_desk/config.py`
- `hl_desk/deck.py`
- `hl_desk/feed.py`
- `hl_desk/flow.py`
- `hl_desk/freshness.py`
- `hl_desk/info_client.py`
- `hl_desk/markets.py`
- `hl_desk/owner.py`
- `hl_desk/replay.py`
- `hl_desk/runtime.py`
- `hl_desk/schema.py`
- `hl_desk/state.py`
- `hl_desk/store.py`
- `hl_desk/tools.py`
- `hl_desk/trades.py`
- `hl_desk/util.py`
- `hl_desk/wallets.py`
- `plugin.yaml`
- `pyproject.toml`
- `schema/metrics.v1.json`
- `scripts/build_dashboard.py`
- `scripts/record_fixtures.py`
- `skills/hyperliquid-commentary/SKILL.md`
- `tests/conftest.py`
- `tests/fixtures/recorded/info/clearinghouseState_default.json`
- `tests/fixtures/recorded/info/clearinghouseState_xyz.json`
- `tests/fixtures/recorded/info/frontendOpenOrders_default.json`
- `tests/fixtures/recorded/info/frontendOpenOrders_xyz.json`
- `tests/fixtures/recorded/info/meta_default.json`
- `tests/fixtures/recorded/info/meta_xyz.json`
- `tests/fixtures/recorded/info/perpDexs.json`
- `tests/fixtures/recorded/ws/session.jsonl`
- `tests/helpers.py`
- `tests/pytest.ini`
- `tests/test_book.py`
- `tests/test_collector_local.py`
- `tests/test_consistency.py`
- `tests/test_deck.py`
- `tests/test_freshness.py`
- `tests/test_lifecycle.py`
- `tests/test_metadata.py`
- `tests/test_review1.py`
- `tests/test_review2.py`
- `tests/test_review3.py`
- `tests/test_review4.py`
- `tests/test_review5.py`
- `tests/test_review6.py`
- `tests/test_schema_and_commands.py`
- `tests/test_thresholds_and_review.py`
- `tests/test_tool_shapes.py`
- `tests/test_trades.py`
- `tests/test_wallets.py`

## Dropped, not built, or different from the handoff wording

- **No feature was dropped for lack of a confirmed endpoint.**
- **Heatmap** ("sampled visible liquidity") is optional and wasn't built.
- **Node-mode panels** show "needs node mode", with `data-node-hook` attributes for Handoff 1c: ladder stops, top, age and Δ10s; waiting stops; wall watch; tape flags; the activity buckets; and the wallet activity split.
- **activeAssetCtx fields** (mark, oracle, funding, open interest, day volume) are always unverified and null, because the feed has no source timestamp (Kevin's decision 7).
- **bbo** is subscribed, but its prices are never shown.
- **hl_deck's other alerts** (SELLING DONE, CONVICTION, whale GONE/BACK/FLIPPED) aren't ported.
- **Aggregated depth books** come from the info endpoint, because two 20-level websocket subscriptions for one coin can't be told apart.
- **REPLAY mode** is used by the tests, not exposed to users. Replay is never an automatic fallback.

## Subagents used

- **hermes-docs** (×3): the Hermes plugin and catalog rules, the dashboard extension, and the Hyperliquid API.
- **deck-porter:** found and read hl_deck.py, read-only.
- **dashboard-ui:** built the tab in a staging folder; I integrated it as the only writer to the checkout.
- **test-runner:** every official run, including this round's.
- **handoff-check:** pre-report review of the first build, whose findings were fixed before the first commit.

I was the only committer. Codex's reviews run through Kevin's review script, following his review-loop instructions.

## Compliance notes

- **Read-only outside `~/contest`.**
  - **Reads outside it:** hl_deck.py, to port its definitions; the deck's output log, for the compatibility test, read through a copy in `~/contest/.tmp`; and the Hermes docs and source.
  - **Never touched:** the node, its services and data, and every other repo. No code was copied from the private repo.
  - **Two slips early in the session:** I wrote temporary files to the harness's scratchpad under `/tmp`. They were moved into `~/contest` within minutes, and that directory is empty.
- **Review-loop exception.** Status lines go to Kevin through his status tool, as the review-loop file asks (confirmed by Kevin). The tool keeps its own status files outside `~/contest`.
- **No remote, no push, nothing public.**
- **No service installed.** The contest dashboard ran only during the live checks.

## Open questions

1. **Context fields.** Decision 7 makes mark, oracle, funding, open interest and day volume permanently unverified on public data, because `activeAssetCtx` carries no source timestamp. Should the data check look for a timestamped public source for them, or is "unverified, null with the receipt age" what Kevin wants shown?
2. **Clock bound on other machines.** On machines whose OS doesn't report a clock error bound (for example macOS), source-timestamped values stay unverified until the user sets `clock_error_bound_ms`. Is that acceptable for the catalog build, or should the README's install steps ask for it?
3. **Freshness limits.** Receipt limits, and source-age limits that add a few seconds for network delay, come from cadences measured on Nov 7. Adjust them if they flap.
4. **Hermes bug report.** Hermes v0.23.0's `plugins validate --install-deps` exits 0 and prints "Validation passed" even when its dependency commit fails. Report it upstream?
5. **Old local history.** Codex raised this as finding 1 in reviews 1–6. The reviewed history has been clean since the parentless publication commit. Kevin also wants the old development history deleted from the local repository. That is a separate phase, after review 8 and his explicit approval; until then it stays untouched.
