# Report: node-mode bursts with the burst calibration, and the node-mode flicker fix (review label h1)

Author: Claude Code · Nov 8, 2026 · for Kevin. Folder paths are written as `~`; private operational details are left out.

## What Kevin asked, and what he decided

The task: load the burst calibration into the plugin. Branch from Part B's accepted plugin commit `2836e80`. Ship Kevin's default:
- a target of 10 bursts a day;
- each market's selected threshold as measured Nov 3 to 7, in a dated, versioned file with its achieved rate;
- for HYPE and xyz:SP500, whose achieved rate isn't within 25% of 10, a label that states the achieved rate.

Also keep a manual override per market, and rank each live burst against the reference ("a top x% minute, Nov 3 to 7"), with the rank unavailable when the file isn't loaded. First, confirm that the plugin's burst rule matches the calibration's rule in the analysis plan (PLAN.md), and report before changing anything if it doesn't.

**The rule check found a mismatch, and nothing was changed before Kevin decided.** The full check is in `~/contest/engine-run/burst-cal/RULE_CHECK.md`:
- the plugin evaluated bursts only on public trades messages, never on node fills per node block;
- it treated a rejected record as a gap that re-arms, where the calibration only makes the windows containing it incomplete.

**Kevin's choice A (Nov 8):**
- In node mode, detect bursts on node fills, evaluated once per node block, with the calibration's rule in PLAN.md. This includes its handling of rejected or unreadable records: they make the windows containing them incomplete, and an incomplete window never fires, re-arms or disarms.
- The calibrated thresholds (10 a day, measured Nov 3 to 7) and the rank apply to these node-mode bursts.
- Public mode keeps its current public-trades burst behavior and doesn't use the node calibration.

## Commits (branch `feat/node-burst-calibration`, from `2836e8081ee8ac830d5abc0d304a070ac424aec6`)

| Commit | What |
| --- | --- |
| `969978b9e72b35ace18453bc8e0ed8fa06cb9cf2` | Node-mode bursts on node fills, the calibration and its reference, the manual override setting, schema 1.2.0, docs, tab, tests |
| `844d9aa9b514f67905b94c2b4b9e4c1dcb34cba3` | The Flow Deck's description says node-mode bursts come from node fills |
| `f6115ccedad0ee04f7166babec89e2f78c74c630` | A load error's reason names the file and the error only, never a file path |
| `3b2764a5da18aa0665cf9c4b001242c92033ef51` | `alerts.burst.config` and `alerts.burst.node_mode.calibration` carry the calibration's version, dates and achieved rates (PLAN.md, "Plugin side") |
| `cc6507a65cf413c53c85ed1b1c65dc50aed4c376` | Validation, after the pre-review (handoff-check) found records that could still be counted or re-arm: an executions message that can't be read; an execution without its own integer tid and time; more than two fill records (`duplicates`); a line that can't be assigned to a market, for every market. The rank label for the very top keeps PLAN's form, and the schema's refresh note is corrected. |
| `138e22aa9deff4114e0eb2a52d0b6f5bb3395d73` | Kevin's node-mode flicker fix: the tab never blanks for a delay; the node feed's delay (5 s average) as a counter at the top and as the basis of the DELAYED and STALE labels (see "Node-mode flicker") |
| `70c25532e194ba62872c9248825e304db7e19c86` | After the flicker pre-review: the delay under clock skew, a gap restarts the averaged delay, DELAYED wording, poll failures in the delay bar, docs |
| this commit | This report and the official logs (`evidence/logs/burst-calibration/`); no code |

- Author and committer of every commit: `KevinM0001 <210251245+KevinM0001@users.noreply.github.com>`. No other email appears in any commit object.
- Nothing was pushed. `v1-public-btc` is still `593211d`. The review never creates or moves it.
- **One commit was replaced before any review.** The first code commit, `08af228`, had a `plugin.yaml` description that isn't valid YAML (`{"BTC": 25000000}` in a plain scalar). `plugins doctor` refused it: "no valid plugin manifest". Nothing had seen it, so it was amended to `969978b`, with a corrected description. A new test, `tests/test_lifecycle.py::test_the_manifest_is_valid_yaml`, now catches this: the test environments have no YAML parser, so it lints every plain value for `": "` and `" #"`, and parses the manifest when a parser is present.

## The rule as built (`hl_desk/node/bursts.py`), against PLAN.md

| PLAN.md | Plugin |
| --- | --- |
| Per side, the taker notional in (W − 60 s, W] | `NodeBurstRule.on_block`: the samples are pruned to times above W − 60 s, then summed per side. Notional is px × sz as exact decimals. The side is the taker's. |
| Evaluation unit: one node executions message per block per market | W is the block time of each `execs` message (`flow.on_execs` calls `bursts.on_block` once per message) |
| Re-arm when the side's sum before this evaluation's executions is below the threshold | `before` excludes this block's executions; a side re-arms when `before < threshold` |
| Add the executions, then fire each armed threshold the sum has reached, once | `total = before + added`; fires when armed and `total ≥ threshold`, then disarms |
| A gap re-arms every threshold and starts a new segment; no burst spans a gap | `NodeFlow.boundary` → `NodeBurstRule.boundary` on a sequence gap, a start that isn't the expected one, a reconnect without resume, or an unavailable replay. A new producer session or epoch replaces the market's whole state (a new rule). The segment starts at the first timed watermark after the gap. |
| A window that starts less than 60 s after the range start or a gap is incomplete | `W − 60 s < segment start` → incomplete |
| A window containing a rejected or unreadable record keeps its executions but never fires, re-arms or disarms | A bad record's time is kept 60 s. While it is in the window, the evaluation only adds the valid executions, and the armed state is untouched. |

**Validation per execution.** The feed carries an execution, not its two raw fills. The producer pairs the fills by identity and marks the pair (PROTOCOL.md 4.6): `complete` is false when a side's record is missing or the two disagree on px, sz or time; `conflict` is true when repeated records disagree. The plugin rejects, and counts by reason (`counters.rejected`):
- an executions message it can't read (no list of executions; the client reads a body that isn't an object as empty, and that is unreadable here, never an empty block);
- an execution without its own integer tid and time (the time is never taken from the block's), or without a readable price or size;
- more than two fill records: the producer merges identical repeats and counts them in `duplicates`, and PLAN wants exactly two;
- `complete` false, or `conflict`;
- not exactly one taker and one maker, or not one buyer and one seller;
- an aggressor that isn't the taker's side, or a participant without an address;
- a fill time other than the block time;
- a non-positive price or size;
- an identity already seen in an earlier block, or conflicting copies within one block (an identical copy within its own block counts once).

Three of PLAN's checks map onto the feed differently:
- **Hash.** The feed carries no hash, so PLAN's same-hash check can't be made in the plugin.
- **Coin.** The producer pairs the two records within a market by tid, and each `execs` message is one market's.
- **`crossed`.** It reaches the plugin as each participant's role, and the plugin checks there is exactly one taker.

**Records with no counterpart in a file replay.** These are disclosed as plugin choices:
- **A block whose fills weren't read** (`fills_known: false`) is an unreadable record: its windows are incomplete. For the other node-fill metrics it is still a flow-window boundary, as PROTOCOL.md 6.7 says; for bursts it isn't a gap.
- **A block time running backwards.** PLAN stops the run. A live plugin can't stop, so it rejects that block, and its windows are incomplete for 60 s from the last block time.
- **A block with executions but no block time** (`null`, which the protocol allows) is an unreadable record at the last block time and again at the next block's. That covers every window that could contain it.
- **A line that can't be read or assigned to a market** (not JSON, not an object, or a market line without a market, sequence or known kind) is, as PLAN says for an event that can't be assigned, an unreadable record in every market's windows, at the last block time and the next. The market whose sequence it consumed then also sees a sequence gap at its next line. A lost message in a market's sequence is a gap, as a missing block (a height jump) is in the calibration.
- **Identity memory.** Identities are remembered for 16 minutes, the same retention as the node fills. The calibration remembers them for its whole range.

**Public mode is unchanged.** Without a node feed, `deck.check_burst` runs as before, on public trades with `burst_min_usd`, and the calibration is never loaded. **In node mode** (a `node_feed` is set), public-trades bursts aren't evaluated or shown. Every burst in the Flow Deck comes from node fills.

## Thresholds, labels and the rank (`hl_desk/node/calibration.py`, `hl_desk/node/calibration/`)

**`burst-calibration-2026-11-08.json`**, version `2026-11-08.1`, dated 2026-11-08, measured `2026-11-03T00:00:00.000Z` to `2026-11-08T00:00:00.000Z` ("Nov 3 to 7"), target 10 a day, tolerance 25%. Each market's entry is copied from `history-20261108T100453Z.json`, `bursts[m].selection`, at target 10:

| Market | Threshold | Achieved | Within 25% | Label |
| --- | --- | --- | --- | --- |
| BTC | $30M | 10.6 a day | yes | automatic: about 10 a day, measured Nov 3 to 7 |
| HYPE | $5M | 14.6 a day | no | automatic: 14.6 a day, measured Nov 3 to 7 (the target of 10 a day has no threshold within 25%) |
| xyz:SP500 | $5M | 7.2 a day | no | automatic: 7.2 a day, measured Nov 3 to 7 (the target of 10 a day has no threshold within 25%) |
| xyz:XYZ100 | $5M | 11.0 a day | yes | automatic: about 10 a day, measured Nov 3 to 7 |

- **What the file carries.** Covered hours, the grid, the selection rule, and both source files' names, sha256 and generation times. It has no path and no capture field: `capture` and `files_read` were not copied.
- **`relative_error` is unsigned**, as in the source (`|achieved / target − 1|`). xyz:SP500's 0.28 is below the target.
- **The reference** is `burst-reference-20261108T100453Z.json.gz`: the source file gzipped, byte for byte (4,406,949 → 1,693,330 bytes). Its sha256 is checked against the calibration's on load.
- **The rank** is count(reference sums ≥ burst sum) / reference count, with ties counted as at or above, as "a top x% minute, Nov 3 to 7". A sum above every reference minute reads "a top 0% minute, Nov 3 to 7 (larger than every minute in the reference)". If either file can't be read, the rank is unavailable with the reason (the file name and the error), never estimated; without the calibration file, the automatic threshold is unavailable too.
- **Where the metadata goes.** Each threshold (in `deck.bursts`, `node_status` and every burst event) carries source, label, version, dated, measured_utc, target and achieved rate. `schema/metrics.v1.json` `alerts.burst.config` and `alerts.burst.node_mode.calibration` carry the version, dates and each market's threshold and achieved rate, as PLAN.md asks; a test checks them against the shipped file.
- **When it loads.** The calibration is loaded once, when a node source first starts. Thresholds are fixed per stream: a change (a new version, or a manual threshold) takes effect at the next collector start. That restart is a gap: a new segment with its 60 s warm-up and no window from before it. As after any gap, a window whose sum is at or above the new threshold fires once at its first complete evaluation. Whether that meets PLAN's "changing a threshold alone never creates a burst" is Kevin's call; the plugin has no way to change a threshold without a restart.

## How a user sets a market's threshold manually and switches back to automatic

Setting: `node_burst_min_usd`, a map of market to USD per 60 s, under `plugins.entries.hyperliquid-desk.settings` (also in `plugin.yaml` for the Desktop settings form). The README has a section on it, "Burst thresholds in node mode".

1. **Set one market by hand:**
   ```bash
   hermes config set plugins.entries.hyperliquid-desk.settings.node_burst_min_usd.BTC 25000000
   ```
   xyz markets use their full name, for example `…node_burst_min_usd.xyz:SP500 3000000`. Then run `/hl restart` (or Stop and Start in the tab): settings are read when the collector starts. The Flow Deck's burst-rule line, `node_status` and each burst then say "manual: $25,000,000 (setting node_burst_min_usd)".
2. **Switch that market back to automatic:**
   ```bash
   hermes config unset plugins.entries.hyperliquid-desk.settings.node_burst_min_usd.BTC
   ```
   then `/hl restart`. Setting the value to `auto` instead does the same. Other markets are unaffected. A value that isn't a finite positive number (0, `inf`, NaN, `true`, text) also leaves the market on automatic (tested).

The rank of a manual-threshold burst is still against the Nov 3 to 7 reference. Public mode ignores this setting and uses `burst_min_usd`.

**Verified with Hermes v0.23.0's own CLI** in an isolated Hermes home (`~/contest/engine-run/burst-cal/hermes-home`):
- `config set …node_burst_min_usd.BTC 300000` (and HYPE, xyz:SP500) wrote the nested map;
- `config unset` removed one market, and unsetting the last one removed the empty map;
- the tab then showed the manual labels, and after unsetting all three, the automatic labels.

## Official checks at `70c2553` (the final code; logs in `evidence/logs/burst-calibration/`)

| Check | Result | Log |
| --- | --- | --- |
| Tests, Python 3.14.9 | 289 passed, 0 skipped, 0 failed | `pytest-py3.14.log` |
| Tests, Python 3.12.3 | 289 passed, 0 skipped, 0 failed | `pytest-py3.12.log` |
| Dashboard bundle check | `dashboard/dist/index.js is up to date (1024630 bytes, sha256 5bdccb30…)` | `build-dashboard-check.log` |
| `plugins doctor . --ci` (Hermes v0.23.0) | runtime discovery, manifest parsing, import and registration passed; 9 tools | `plugins-doctor.log` |
| `plugins validate . --install-deps` | Validation passed | `plugins-validate.log` |

- **Nothing skipped.** `HL_DECK_SOURCE`, `HL_DECK_LIVE_LOG` and `BURST_CALIBRATION_SOURCE` were set, so the hl_deck checks and the check of the shipped files against the calibration run ran.
- **Where they ran.** Hermes ran from its own install, with an isolated home under `~/contest/engine-run/burst-cal/hermes-home`. That home got its own `hermes pm install` dependency environment, so no other Hermes home was written. The tests ran from the clean checkout at `70c2553`; the run checked HEAD and an empty `git status` before and after.
- **An earlier full run** (at `cc6507a`) had 287 passed and 1 failed on both Pythons, with the same failure each time: `tests/test_review2.py::test_long_data_dir_gets_a_short_private_socket` (`assert (105 <= 100)`).
  - That test stands in for the system temp folder with `tempfile.mkdtemp`, which follows `TMPDIR`. The in-scope `TMPDIR` used then made the socket path 105 characters long, against the 100-character limit.
  - With a shorter `TMPDIR`, the same commit passed all 288 on Python 3.14. The runs above used the shorter one.
  - Logs: `pytest-py3.14-cc6507a-long-tmpdir.log` and `pytest-py3.12-cc6507a-long-tmpdir.log`.
  - The failed test left two empty temp folders under `~/contest/engine-run/burst-cal/tmp`; they were left in place.
- **Log substitutions.** The committed logs are copies with the home path written as `~`, the user name in pytest's temp folder as `<user>`, and the paths given for `hl_deck.py` and the deck's live log as `<hl_deck.py>` and `<a copy of the deck live log>`. The originals are in `~/contest/engine-run/burst-cal/official/`.
- **Tab-check logs.** The independent burst count (`tab-independent-check.log`), the tab text from the burst cases (`tab-*.txt`), and the live and recorded observer samples (`live-run-*.{log,jsonl}`, `recorded-observer-*`) are in the same folder. The screenshots stay in `~/contest/engine-run/burst-cal/tab/`, because they show local paths.

## Tab check (offline, recorded node data only)

- **Setup.**
  - The plugin at `70c2553` (the final code), installed by `git archive` into the isolated Hermes home, with the dashboard started with `--isolated`. Case (c) was last run at `cc6507a`; the label code hasn't changed since.
  - The node feed was a local stand-in serving a recorded tap at recorded speed: Part A's follow test, Nov 7, 20:47:56 to 20:53:13 UTC, all four markets, verbatim feed lines. Its blocks are from Nov 7, so node freshness reads BEHIND.
  - No live node data was read. No runner, forwarder or service was started.
  - Screenshots and logs are in `~/contest/engine-run/burst-cal/tab/`. They aren't committed, because they show local paths.
- **(a) Manual BTC $300K, HYPE $150K, xyz:SP500 $100K; xyz:XYZ100 automatic** (`h1-70c2553-a-manual.png`):
  - The burst-rule line names each market's threshold and label.
  - 10 bursts were shown when the screenshot was taken, each with the NODE badge, its manual label, "a top x% minute, Nov 3 to 7", the aggressive takers ranked and their share, and node buckets withheld ("node fills BEHIND").
  - The same rule, written out independently (`tests/test_node_bursts.py::_independent`) and run on the same tap and thresholds, gives 13 bursts over the whole recording. The 10 shown are its first 10, with the same sums. Their ranks, recounted from Kevin's original reference file, are the same: 30.3, 40.8, 37.6, 21, 31.6, 23.4, 40.5, 3.4, 21 and 28.5%. The last 3 fall after the point the tab had reached.
- **(b) The reference file held aside in the installed copy** (`h1-70c2553-b-no-reference.png`):
  - Every burst still fires at its threshold.
  - Each shows "rank unavailable: the burst reference file isn't loaded (burst-reference-20261108T100453Z.json.gz: No such file or directory)": no path, and no estimate.
  - The file was put back afterwards.
- **(c) All markets back on automatic, via `hermes config unset`** (`h1-cc6507a-c-automatic.png`). The rule line reads: "BTC node fills, $30.00M (automatic: about 10 a day, measured Nov 3 to 7) · HYPE node fills, $5.00M (automatic: 14.6 a day, measured Nov 3 to 7 (the target of 10 a day has no threshold within 25%)) · xyz:SP500 node fills, $5.00M (automatic: 7.2 a day, …) · xyz:XYZ100 node fills, $5.00M (automatic: about 10 a day, measured Nov 3 to 7)". No burst reaches these thresholds in the 5-minute tap.

## Tests added or changed

- **`tests/test_node_bursts.py`** (new):
  - PLAN.md's rule cases: the warm-up ($100,100 at 59 s, then $100 at 61 s, gives one $100,200 burst); a second block at the same time doesn't re-fire, and a later crossing does; a gap re-arms, with warm-up after it; separate sides.
  - An incomplete window keeps its executions and never fires or disarms, and never re-arms: a control without the bad record fires twice, with it once.
  - Each rejection (unreadable, complete false, conflict, two takers, two buyers, a fill time other than the block time, a missing address) against a control that fires. Also: a repeated identity in a later block, conflicting copies within one block, unread fills, a backwards block time, a block without a time, and no evaluation before the segment starts.
  - After the pre-review: an executions message that isn't a list (or is missing), an execution without its own time, a boolean tid, `duplicates` above 0 or unreadable; through the feed client, a line that can't be assigned to a market and a line that isn't JSON (both markets' windows incomplete, never a gap), and an `execs` line whose body isn't an object.
  - The rank: ties at or above (0.75); values outside the reference give 1.0 and 0.0 ("a top 0% minute"); unavailable when the reference is missing or its sha256 differs; reasons without a path.
  - Every label, including HYPE's and xyz:SP500's achieved rate.
  - The setting, including "auto", 0, `inf`, NaN and `true`.
  - The shipped files agree with each other and carry no path.
  - With `BURST_CALIBRATION_SOURCE` set, they match the calibration run byte for byte (the reference) and value for value (the selection).
  - The schema carries the calibration.
  - A recorded window of node fills (120 s, four markets) through the feed client: 9 bursts at low manual thresholds, the same as the rule written out independently, each ranked as counted from the reference.
- **`tests/test_node_flow.py`:**
  - `test_burst_node_buckets_through_the_plugin` became `test_node_burst_through_the_plugin`. In node mode the burst comes from node fills and is the same in chat and the tab, with its label, rank, takers and own-side buckets (withheld while not fresh). A public burst that would fire isn't shown, and a producer restart drops the old stream's bursts.
  - `test_public_mode_keeps_its_public_trades_bursts` is new.
  - The incomplete-execution test now expects no node burst and the rejection counted.
- **`tests/test_node_plugin.py`.** The replay-beside-live test's reference client gets the same burst attachment as the plugin's live client.
- **`tests/test_tool_shapes.py`.** Schema version 1.2.0.
- **`tests/test_lifecycle.py`.** The manifest YAML check.

## Node-mode flicker (Kevin, Nov 8)

Kevin's report: with `node_feed` set to the second runner's live feed, every 1 to 2 s, on every market, the whole tab flashed to an empty "No snapshot" view under a red banner ("…last watermark is about 2.0 s old (limit 2 s; heartbeats don't count)…"), then came back. He asked:
1. A delay never withholds the snapshot or swaps the page, and values and labels update in place, without shifting the layout. The empty view is only for having no data at all.
2. A counter at the top of the tab with the node feed's delay: the age of the latest watermark for the market on screen, averaged over the last 5 s, updated about once a second. The delayed and stale labels use the same averaged value, with thresholds comfortably above the normal delay, so they don't toggle. The age stays in the snapshot data.
3. Gap and epoch handling stay as accepted.

**Cause.** There were two:
- **The tab.** Its own gate (`snapshotValidity`, from Codex reviews 5 to 8) replaced the whole page with the empty view as soon as any LIVE node family's last watermark, aged by the snapshot's age plus the time since the tab asked, passed the 2 s stale limit.
- **The client.** Family states used the instantaneous watermark receipt age (2 s) and source age (5 s), so the labels toggled at normal delays too.

**Change** (commit `138e22aa9deff4114e0eb2a52d0b6f5bb3395d73`):
- **The delay** (`hl_desk/node/client.py`, `FeedClient.delay`). Each market keeps its timed watermarks' receipt times and offsets. The delay at time t is the latest watermark's age: receipt wall time minus block time, plus the monotonic time since receipt. Its exact average over the last 5 s (or since the first watermark) is `delay_ms`; `current_ms` is the value now.
- **The states** (`delay_state`). A valid family reads LIVE up to 10 s of averaged delay, BEHIND (shown as DELAYED) above 10 s, and STALE above 30 s. Heartbeats never count, so a silent feed's delay keeps growing until it reads DELAYED, then STALE. INVALID (gaps, a new epoch or session, an incomplete snapshot, a crossed book), UNKNOWN (clock skew), REPLAY and "no watermark since valid" are unchanged.
- **Serving time.** A tool reading the latest snapshot adds the snapshot's served age to `delay_ms`, an upper bound (`hl_desk/node/serving.py`). The tab's gate does the same with the time since it asked.
- **The tab.**
  - It never blanks for a delay. A snapshot that has aged past a limit stays on screen, and the delay bar says why until the next snapshot replaces it in place.
  - The empty view now comes only from the snapshot route having no current snapshot (collector stopped, not publishing, first start).
  - A fixed-height delay bar at the top shows "Node feed delay", the averaged value in a fixed-width tabular number, the state chip (DELAYED for BEHIND), the window, the current value and the thresholds. The family chips show DELAYED for BEHIND.
- **The data.** The node view carries `delay` for the market on screen, and `node_status` returns it. Every family status keeps `watermark_age_ms` and `source_age_ms` and adds `delay_ms` and its limits, so Hermes's answers report it. The schema (`node.family_state`), README, SNAPSHOT.md and the commentary skill say so.

**Thresholds chosen:** DELAYED above 10 s and STALE above 30 s of 5 s average delay (`delayed_after_ms`, `stale_after_ms`; window `delay_window_ms` 5,000).
- No live delay measurement existed before this change. The recorded feeds in the repo are catch-up recordings, offset by hours.
- The node itself ran 0.3 to 1.6 s behind the chain in Part A.
- The limits were chosen several times above a normal delay of a second or two, and confirmed against the live run below.

**The live run (Kevin's one 15 s run, approved through the user on Nov 8)** at `138e22a`, 22:05:19 to 22:05:48 UTC.
- **Setup.** The plugin was installed by `git archive` in the isolated Hermes home, with `node_feed` set to the second runner's live feed socket and the dashboard started with `--isolated`. `~/contest/engine-run/burst-cal/observe_tab.py` (headless Chrome) pressed Start; node data showed 2.5 s later. It sampled the tab every 250 ms for 15 s, then took a screenshot and pressed Stop, 20.8 s after Start. The dashboard was stopped by its PID, and `node_feed` was set back.
- **Results.**
  - 60 samples: 0 empty "No snapshot" views and 0 red banners.
  - The delay bar stayed 30 px high throughout.
  - The counter changed about once a second and fell steadily: 222.0, 216.7, 212.7, 208.7, 205.0, 198.0, 191.6, 185.1, 179.6, 174.2, 168.2, 163.1, 157.7 s.
  - The state read STALE throughout, with no toggling.
- **The delay seen was not the steady one.**
  - A new subscriber starts from the publisher's latest book snapshot and the messages after it, so its first watermarks are minutes old. This client was still working through that backlog at about 5 s per second, a Part B behaviour this change doesn't touch; the current (not averaged) delay fell from 220.2 s to 146.4 s.
  - The runner itself was live, about 2.5 s behind the chain. It wrote its checkpoint for height 1,214,207,000 at 22:06:13.8, and the node's block time for that height is 22:06:11.3 (read-only from the node's block-time file).
  - So the steady delay after catch-up should be a few seconds, well under the 10 s limit. It wasn't observed within these 15 s.
  - A longer live run (for example 90 s, to see the counter settle) needs Kevin's approval and wasn't done.
- **Recorded-data checks** at `138e22a`, with the same observer on the recorded feed (blocks from Nov 7, so the delay is about 88,700 s and STALE): 60 samples, 0 empty views, 0 red banners, and a constant bar height. The burst tab check (a) gave the same 10 bursts and ranks as before.

**Pre-review of the flicker change (handoff-check, at `138e22a`) and what changed after it** (commit `70c25532e194ba62872c9248825e304db7e19c86`):
- **The delay bar's state** came from the delay alone, so it read LIVE while clock skew made the families UNKNOWN. It now reads UNKNOWN too.
- **"BEHIND" labels.** The family notes and the invalid-book tag printed "BEHIND" where the chips say DELAYED; both now say DELAYED.
- **Gaps.** A gap (`_invalidate_market`) didn't clear the delay history, so a stall before a reconnect without resume could keep a recovered market DELAYED for up to 5 s. The averaged delay now restarts at the first timed watermark after a gap, like every other node window, which keeps gap handling as accepted.
- **Unknown serving age.** With an unknown served age, the families read STALE but the market's delay wasn't changed; it now reads STALE too.
- **Docs.** README and the schema's `freshness.snapshot.in_the_tab` still said the tab withholds an aged snapshot; both now describe the in-place note.
- **Poll failures.** A failed poll while a snapshot is on screen used to add a banner above the header, pushing everything down; it is now said in the delay bar's fixed note. The banner remains only when there is no snapshot at all.
- **Disclosed, not changed:**
  - Panels still change height when their content changes (an events list that grows, tables filling in as data arrives), and a state note appears under the node strip when a family isn't LIVE. With the averaged limits these no longer toggle at normal delays.
  - The empty view also appears when the snapshot route has no current snapshot because the collector itself hasn't published for more than 5 s (v1's accepted rule: a stalled collector is "no data"), not for a feed delay.

**Tests** (Kevin: "the existing test suite", nothing more). The existing suite passes; the tests that encoded the old 2 s and 5 s instantaneous rule now state the averaged rule:
- **`tests/test_node_protocol.py`:**
  - an old backlog reads BEHIND;
  - a frozen producer with live heartbeats: on a steady feed, the current delay grows by exactly the time passed, and the average matches an exact-fraction integral counted in the test; STALE exactly past the limit;
  - a quiet market stays LIVE on watermarks alone;
  - null-time watermarks never refresh the delay.
- **`tests/test_node_plugin.py`:**
  - the tab's gate on the averaged delay (it never blanks the page; the gate only says the snapshot isn't current);
  - serving-time judgement at 1.5, 3 and 4.5 s, reading BEHIND or STALE, with the bound never below what the client reads;
  - the monotonic-clock read.
- **`tests/test_node_flow.py`:** node-fill results lapse by their delay bound.
- **Elsewhere:** the client settings key `max_source_age_ms` is now `delayed_after_ms`.

## Pre-review (handoff-check) and what changed

Before the evidence commit, the handoff-check agent read `3b2764a` against Kevin's task, his choice A and PLAN.md. Its findings and what was done:
- **Fixed in `cc6507a`:**
  - an `execs` body that isn't an object, or a missing or non-list `execs`, counted as a complete empty block;
  - a missing fill time defaulted to the block time;
  - an execution with `duplicates` above 0 was accepted;
  - a line that can't be assigned to a market only counted as malformed.
- **Changed wording:**
  - the very top rank now reads "a top 0% minute, …";
  - "changing a threshold never creates a burst" was an overstatement and is replaced by what happens (see "When it loads").
- **Kept, and why:** a lost message in a market's sequence stays a gap for that market. It is the feed's form of the calibration's missing block (height jump), which the calibration treats as a gap.
- **Verified:**
  - author, committer and messages of every commit (`git log --format='%an <%ae> / %cn <%ce>'`, no other email);
  - the shipped reference against the source (the `BURST_CALIBRATION_SOURCE` test runs in the official logs).

## Not done, and notes

- **No live run.** No live node data was read by the plugin, and nothing ran against a live feed; that needs Kevin's approval.
- **The same-hash check isn't made live.** The feed has no hash. The calibration's run checked it on every execution of its 5 days. Its history file reports no rejected executions, no identity repeated across blocks, no unreadable block and one segment.
- **Burst events don't outlive their stream.** A node burst event belongs to its input stream. After a producer restart (a new epoch or session), the earlier stream's bursts and liquidation events are no longer shown, as for every node-fill result in Part B. A reconnect that resumes the same session keeps them.
- **Displayed times.** Node bursts are timed by their block time. The tab shows UTC times without a date, so in the tab check the Nov 7 bursts appear as 20:48 to 20:51 next to Nov 8 regime changes.
- **Versions.** The plugin version stays 1.0.0, as Part B left it; versioning the release is Kevin's call. The schema is 1.2.0 (additive: `alerts.burst.node_mode`, and the node-mode part of `alerts.burst.config`).
- **Superseded runs.** Official runs were started at `969978b`, `844d9aa`, `f6115cc`, `3b2764a`, `cc6507a` (finished: 287 passed and 1 failed on both Pythons, the failure from an over-long temp path, see below) and `138e22a`, and stopped when the code changed. Their partial logs are under `~/contest/engine-run/burst-cal/official/`. Doctor, validate and the tab check were also repeated at `3b2764a`, `cc6507a` and `138e22a`. Only the run at `70c2553` is the official one.
