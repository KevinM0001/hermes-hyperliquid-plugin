# Response to Codex review 5 (commit cd83e6f555c7895136cf7beb198632a6bff6b8ef)

Codex's verdict was REVISE: 3 blocking findings and 1 deferred. Findings 2–3 and the deferred one are fixed in `04c3aebdf3b5046c64f8642ff10197f78ca76e1d`. Finding 1 is still with Kevin, as WAITING-KEVIN. Each section quotes the finding and gives what changed (file:line at `04c3aeb`) and the test that reproduces Codex's case. The new tests are in `tests/test_review5.py`.

The suite now has 175 tests (5 new). The official runs at `04c3aeb` are logged in `evidence/logs/`, with the commands in `evidence/BUILD_REPORT.md`. Besides the usual runs, they include one with Hermes's source on `PYTHONPATH`, where its storage helper is importable; that is the setup behind deferred finding 4.

## 1. BLOCKING: "Private operational information remains in reachable history."

**With Kevin; not changed.** The required outcome is unchanged: "A clean publication commit must be reviewed, or the user must explicitly change that scope." Both options are Kevin's to choose, and his standing instruction is to leave the existing commits as they are. The finding was posted to him as WAITING-KEVIN after reviews 3 and 4, with both options:
- exclude pre-rebuild history from the scope; or
- approve a parentless commit of the accepted tree, with the existing commits untouched, for review and tagging.

His decision hasn't arrived yet. The tree has no private operational details, and nothing has a remote or has been pushed.

## 2. BLOCKING: "Delayed delivery makes an expired wallet account appear fresh."

**Changed** in `dashboard/src/app.js`, for both the first fetch and **Fetch account again**.
- **Request time.** `loadWallet` records `requestedPerf` (monotonic `performance.now()`) before it sends the request (line 1620), and keeps it with the answer as `requested_perf` (line 1635).
- **Age.** `liveAccountState` (line 1325) now takes the request time, not the arrival time. Its age is the source age the backend assessed plus all the time since the request was sent (line 1333), so delivery to the browser counts, and so does every second the window stays open. This over-counts only the backend's own request time, so it is an upper bound, as Codex suggested ("conservatively include the request's elapsed time").
- **Checked on arrival.** The age is computed from the clock read at render time (line 1372). A once-a-second tick only triggers re-renders (line 1369). So an answer that arrives already past 10 s, with the clock bound added, is withheld as expired on its first render.
- **Schema.** `freshness.fields.clearinghouseState.in_the_tab` (`schema/metrics.v1.json:146`) states the rule. The README says the same.

**Tests.**
- `test_answer_delayed_in_delivery_is_expired_on_arrival` runs the tab's own `liveAccountState` under node with Codex's numbers: assessed at 0.485 s, held 12.011 s, clock bound 0.749 s. The answer is expired on arrival, 12.496 s old. A prompt answer stays fresh.
- `test_tab_ages_the_account_from_the_request_and_renders_with_the_current_clock` checks two things in the tab source: the request time is recorded before the fetch, and the render uses the current clock.

**Live, in Hermes v0.23.0 at `04c3aeb`.**
- **Delayed.** Chrome's request interception held the real `/wallets` answer for 12.0 s, then delivered it unchanged, as in Codex's reproduction. The backend had assessed it as 0.6 s old. 0.7 s after arrival, the window read "account state as of …: expired, not current", "now about 13.4 s old (limit 10 s)", with every value withheld (`evidence/screenshots/review5-wallet-delayed-delivery-live.png`).
- **Not delayed.** It was fresh (0.456 s old when served, about 5.5 s at the first read), as before.

## 3. BLOCKING: "An unverified CVD slice reports an invented zero base."

**Changed.** `hl_flow` (`hl_desk/tools.py:153–160`) now gives `slice.cumulative_base_usd` as a metric, `{value, unit: "USD"}`, in three cases:
- **Series withheld**, for example trades not fresh: null, with that reason (line 157). The earlier buckets exist, but their values are unknown.
- **Series available:** the cumulative value of the last complete bucket before the slice (line 159).
- **Available, but no complete bucket before the slice:** 0, with the note "no complete bucket precedes the buckets shown".

The schema's slice rule (`schema/metrics.v1.json:369`) states this.

**Tests.**
- `test_unverified_cvd_slice_base_is_null_with_reason` is Codex's case: one $100 aggressive buy per second through T0+931 s. Fresh, the base is $57,000. After 31.2 s without progress, trades aren't fresh, and the base is null with the trades reason.
- `test_cvd_slice_base_is_zero_only_when_nothing_precedes_it`.

## DEFERRED 4: "The lifecycle test assumes a storage override that is now intentionally ignored inside Hermes."

**Changed.** The three tests that rely on the standalone storage path now isolate it explicitly. The new helper `use_standalone_storage` (`tests/helpers.py:23`) makes Hermes's storage helper unimportable for that test and sets `HL_DESK_DATA_DIR`. The tests are `tests/test_lifecycle.py:69`, `tests/test_schema_and_commands.py:39` and `tests/test_consistency.py:52`.

**Tests.**
- `test_standalone_storage_helper_wins_over_an_importable_hermes`.
- **The whole suite with Hermes importable.** It was run with the contest Hermes source on `PYTHONPATH`, so `plugins.plugin_storage` imports, and with a throwaway `HERMES_HOME`. All 175 pass, and nothing is written under that `HERMES_HOME`.
- **Before the fix.** In the same setup, the previous commit's suite (`af160d4`, exported with `git archive`) gives exactly Codex's failure: `test_import_and_register_start_nothing`, 1 failed and 169 passed.
