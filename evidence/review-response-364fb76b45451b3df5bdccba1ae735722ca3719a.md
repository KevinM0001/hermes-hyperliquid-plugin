# Response to Codex review 4 (commit 364fb76b45451b3df5bdccba1ae735722ca3719a)

Codex's verdict was REVISE: 3 blocking findings and 2 deferred. Findings 2–3 and both deferred ones are fixed in `af160d45ff5c46d2b19052a39655cf0e489be90d`. Finding 1 is still with Kevin; it was posted to him as WAITING-KEVIN after review 3 and again after this review. Each section quotes the finding and gives what changed (file:line at `af160d4`) and the test that reproduces Codex's case. The new tests are in `tests/test_review4.py`.

The suite now has 170 tests (6 new). The official runs at `af160d4` are logged in `evidence/logs/`, with the commands in `evidence/BUILD_REPORT.md`; they include a run under a long `--basetemp`.

## 1. BLOCKING: "Private operational information remains in the reviewed commit's reachable history."

**With Kevin; not changed.** Codex's required outcome is the same as in reviews 2 and 3: "A clean publication commit must be reviewed, or the user must explicitly change the review scope." Both options are Kevin's to choose, and his standing instruction is to leave the existing commits as they are. Per rule 6 of the review loop, the finding went to him as WAITING-KEVIN, with both options:
- exclude history before the publication rebuild from the review scope; or
- approve committing the accepted tree as a parentless commit, with the existing commits untouched, for review and tagging.

His decision hasn't arrived yet. The tree has no private operational details, and nothing has a remote or has been pushed.

## 2. BLOCKING: "An open wallet panel never expires its 'current' account values or fresh status."

**Changed** in `dashboard/src/app.js`.
- **Arrival time.** `loadWallet` (line 1615) records when the answer reached the tab: `received_perf`, from `performance.now()`, a monotonic clock (line 1629).
- **Ticking.** While the wallet window is open, it re-renders every second (line 1366).
- **Aging.** The pure function `liveAccountState` (line 1323) takes the answer's freshness block and adds the time since arrival to the age it had when served. Once that age, widened by the clock bound, passes the 10 s limit, it returns `expired` with the reason, for example "this account state is now about 17.1 s old (limit 10 s), so its values are no longer shown as current; fetch it again".
- **When expired:**
  - the heading changes from "current account state, not part of the snapshot" to "account state as of <its time>: expired, not current" (line 1423);
  - the status reads `expired`;
  - account value, position notional and margin used become unavailable with that reason (line 1443), and so do the positions;
  - an answer whose arrival time wasn't recorded is treated as expired.
- **Fetch again.** "Fetch account again" (line 1427, `refreshWallet` line 1646) asks for a new answer for the same snapshot. If it fails, the expired answer stays, marked expired, and the error is shown.
- **Unchanged.** The snapshot's trade rows, which are immutable, and the open orders, which are always unverified.

**Tests.**
- `test_open_wallet_account_expires_after_its_limit` runs `liveAccountState`, taken verbatim from `app.js`, under node. The test skips when node isn't installed.
  - Codex's timing: an answer that was 0.465 s old when served and has been open 15.019 s is expired, "about 15.5 s old".
  - At 9 s it is still fresh; a missing arrival time counts as expired; a delayed answer stays delayed.
- `test_wallet_window_withholds_expired_values_and_can_fetch_again` checks the wiring in the tab source.

**Live, in Hermes v0.23.0 at `af160d4`.**
- 5 s after a wallet was opened: "account state fresh · … 0.526 s old when served … · now about 5.1 s old", with values.
- At 17 s: "account state as of 07:52:12.137 UTC: expired, not current", with every value unavailable and the reason (`evidence/screenshots/review4-wallet-account-expired-live.png`).
- After **Fetch account again**: fresh, with a new source time and new values.

## 3. BLOCKING: "The rolling chart cutoff corrupts a candle that is still marked complete."

**Changed.**
- **Aligned start.** `MarketState._chart` (`hl_desk/state.py:375`) starts at the 15 s candle boundary at or before (watermark − 15 min), not at (watermark − 15 min) itself. It includes a trade exactly at that boundary (line 377).
- **Nothing pruned.** Trades are retained for 16 minutes (`RETENTION_MS`), and the earliest boundary is at most 15 min 15 s before the watermark, so every trade of the oldest candle is still there.
- **Completeness.** The rule is unchanged: a candle that starts at or before the segment start, or ends after the watermark, is incomplete.
- **Schema.** `chart.candle` (`schema/metrics.v1.json:797`) now gives this window and says the rolling cutoff never truncates a candle.

**Test.** `test_oldest_candle_holds_every_trade_of_its_interval` is Codex's case: trades at +30 s (90 × 10), +31 s (100 × 1), +44 s (110 × 1) and +931 s (100 × 1). The candle `[T0+30 s, T0+45 s)` is O=90, H=110, L=90, C=110, volume $1,110, complete.

## DEFERRED 4: "The 'no shell commands' disclosure omits an actual local subprocess."

**Changed.** `_libc` (`hl_desk/clock.py:37`) gets `adjtimex` from the C library already loaded in the process, with `ctypes.CDLL(None)` (line 41), falling back to `libc.so.6`. `ctypes.util.find_library`, which can run `ldconfig`, is no longer used or imported. The README's row (`README.md:74`) states how the bound is read. **Test:** `test_clock_bound_lookup_launches_no_subprocess` installs an audit hook for `subprocess.Popen`, `os.system`, `os.exec`, `os.posix_spawn` and `os.fork`, clears the cache, and reads the bound; nothing is launched.

## DEFERRED 5: "The storage disclosure omits an override that also works inside Hermes."

**Changed.** `default_data_dir` (`hl_desk/runtime.py:60`) uses Hermes's `plugin_data_dir` whenever it can be imported, so inside Hermes the profile directory always wins. `HL_DESK_DATA_DIR` is honored only where that helper can't be imported, which means outside Hermes, as in the tests. Outside Hermes without the variable, it raises a clear error. The README's storage row (`README.md:68`) documents this. **Tests:**
- `test_data_dir_override_is_ignored_inside_hermes`: with a stand-in `plugins.plugin_storage`, the profile directory is returned and the override isn't created.
- `test_data_dir_override_outside_hermes_only`.
