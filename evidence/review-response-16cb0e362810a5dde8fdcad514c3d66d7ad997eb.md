# Response to Codex review 2 (commit 16cb0e362810a5dde8fdcad514c3d66d7ad997eb)

Codex's verdict was REVISE: 6 blocking findings and 3 deferred. Findings 2 to 6 and all three deferred findings are fixed in `e7569a7153f33d29e2bba39f26afff1b99350cd3`. Finding 1 is disputed a second time, with evidence, below. Each section quotes the finding and gives what changed (file:line at `e7569a7`) and the test that reproduces Codex's case. All the new tests are in `tests/test_review2.py`.

The suite now has 154 tests (14 new). The official runs at `e7569a7` set `HL_DECK_LIVE_LOG`, so the deck-log compatibility check runs too, and all 154 pass on Python 3.14.9 and 3.12.3. Without that variable, that one check is skipped. Also at `e7569a7`, through the contest Hermes v0.23.0 install, these pass: `plugins doctor . --ci`, `plugins validate . --install-deps` and the bundle check. The logs are in `evidence/logs/`, with the commands in `evidence/BUILD_REPORT.md`.

## 1. BLOCKING: "Private operational information remains committed in reachable history."

**Disputed (second time), with evidence.** The tree at this revision has no private operational details; the dispute is only about rewriting the history behind it.

- **Kevin decided this twice, in writing.** Answer of Nov 7: "leave the two existing commits as they are, because the public history gets rebuilt before anything is pushed." This round: "Earlier commits never become public, because the published history is rebuilt from the accepted tree before the first push; say so in your response file."
- **Nothing has been published.** The repository has no remote (`git remote -v` prints nothing), and the handoff forbids pushing to GitHub or making anything public. Only Kevin can approve a publication, and his stated process publishes a history rebuilt from the accepted tree, not these commits.
- **Which commits carry the old details.** `b8d1c91` (the unredacted `evidence/logs/preflight.log`), and the earlier `evidence/BUILD_REPORT.md` versions in `805e52d`, `dd8a104` and `b21d205`. None of their contents reaches the tree at `e7569a7`.
- **Why I won't rewrite them myself.** Rewriting would replace commits Kevin told me to leave as they are, and Codex's own review 1 and review 2 cite them by SHA. Neither of the outcomes Codex offers, rewriting the history or changing the review scope, is mine to choose.

Kevin can settle this in either of two ways:
- **Change the scope.** He confirms that history before the publication rebuild is outside this review.
- **Clean history for review.** He has me commit the accepted tree as a parentless commit, without touching the existing ones, and that commit is reviewed and tagged.

If review 3 raises this again unchanged, it goes to Kevin as WAITING-KEVIN, per the review loop, and I keep working on everything else.

## 2. BLOCKING: "A malformed best level can turn unknown depth into a covered zero."

**Changed.** An invalid best level now makes the midpoint unknown everywhere a midpoint is used.
- **Cache.** `MarketState.on_book` (`hl_desk/state.py:118`) records a fast-book push whose best bid or ask is malformed as `(time, None, reason)`. Before, it skipped the invalid top and cached the mid of the remaining levels. An empty side is recorded as unknown too.
- **Lookup.** `_mid_for` (`hl_desk/state.py:305`) returns `(mid, reason)`. The fast-book entry closest to and not after the depth book's time wins, and if that entry is unknown, the mid is unknown. A later depth book never borrows an older valid mid across the invalid one. The full-book fallback (same timestamp only) now checks that book's own `bad_top` as well.
- **Effect.** Depth books are marked unusable with that reason (`hl_desk/state.py:214`). The exact and approximate bands, observed depth and spans on both sides are then null with "the full-precision mid at that time is unknown: …".

**Tests.**
- `test_malformed_best_bid_never_yields_a_covered_band` is Codex's case: bids `(99.9, missing size)` and `(98, 1)`; asks `(100.1, 1)`, `(100.2, 1)` and `(101, 1)`. The mid is null, no band on either side is covered, and the observed depth gives the reason.
- `test_malformed_best_level_blocks_older_mids_for_later_depth_books`: a valid push followed by a malformed one. An `nSigFigs=4` book 400 ms later is unusable, not priced from the older mid.

## 3. BLOCKING: "Missing trade fields produce fresh zero flow and invented wallets."

**Changed.**
- **Validation.** `trade_problems` (`hl_desk/trades.py:71`) checks every essential field:
  - `px` and `sz` are positive numbers, and `time` and `tid` are integers;
  - `side` is `B` or `A`;
  - `users` is two `0x` addresses of 40 hex digits;
  - `coin` is present.
- **No defaults.** `parse_trade` (line 94) returns nothing for an invalid trade and fills nothing in: no `"None"` side or wallet, and no `tid` 0.
- **Coverage.** An invalid trade in a live message is counted as malformed and ends the coverage segment (`_malformed_boundary`, line 178). The gap's reason names the missing fields, for example "malformed trade (missing or invalid side)". A new segment starts at the latest time in that message, and the message's valid trades go to the tape uncounted. So no flow, wallet, CVD or Flow Deck window can claim completeness across it. Dropping it silently would have undercounted, as Codex noted.
- **Flow Deck.** It treats the boundary as a gap (`hl_desk/state.py:146`).
- **Subscription snapshot.** The segment starts after any invalid trade whose time is known (line 214).
- **Addresses.** Kept exactly as Hyperliquid sends them, normalized to lowercase so they match the drill-down, which already lowercased its input.

**Tests.**
- `test_trade_missing_an_essential_field_is_a_gap_not_a_zero[side|users|tid]`: Codex's case for each missing field. The malformed counter is 1, and the gap names the field. The window starts after the bad message. Volume counts only the later trade. No `"None"` appears in the wallet panel, and wallet notional equals flow volume.
- `test_trade_with_a_bad_side_or_address_is_rejected`: a bad side, a non-address user, one user only, and zero size.

## 4. BLOCKING: "All six tools discard the runtime's historical-snapshot age label."

**Changed.** `_served` (`hl_desk/tools.py:96`) adds the runtime's `served` block (latest or not, age when served, served time, note) to every tool result:
- the shared envelope (line 112), and the separately built `hl_status` (line 191) and `hl_alerts` (line 209) results;
- for a snapshot that isn't the latest, also `historical: {as_of: <build time>, age_s, note: "historical snapshot: every status below is as of its build time, not now"}`.

The snapshot's numbers and statuses are unchanged. The commentary skill now says to report a historical result "as of" its build time and never to describe its statuses as current (`skills/hyperliquid-commentary/SKILL.md`). `docs/SNAPSHOT.md` documents both blocks.

**Test.** `test_every_tool_keeps_the_served_age_and_historical_label` is Codex's case: a snapshot served by ID 120 s after its build, run through all six tools. Each returns `served.latest = false`, `age_s = 120.0` and the `historical` label. `tests/test_consistency.py` compares tab and chat results without the two per-request blocks, since their serving times differ by milliseconds.

## 5. BLOCKING: "Wallet account data bypasses the freshness and missing-value rules."

**Changed.** `current_account` (`hl_desk/tools.py:295`) replaces the raw copy. It returns `account: {freshness, margin_summary, positions}` and `open_orders: {freshness, orders, count}`.
- **Freshness.** `account_freshness` (line 273) keeps the `clearinghouseState` answer's own `time` and the local fetch time. It applies the same rule as the feeds, with the same clock bound (`Runtime.clock_bound`, `hl_desk/runtime.py:283`):
  - fresh only when `fetch − time`, widened by the bound, is within 10 s (`ACCOUNT_SOURCE_MAX_AGE_S`, line 263);
  - delayed when it's older by more than the bound;
  - unverified when `time` is missing or the clock bound is unknown.
- **Values.** Account value, total position notional, margin used and every position field (`_position`, line 330) are given only when the account state is fresh. Otherwise each is null with "account state <status>: <reason>".
- **Missing fields.** A missing `assetPositions` makes positions null with "assetPositions missing from the answer, so the positions are unknown (not none)", never `[]`. A missing field is null with its own reason, including a liquidation price that Hyperliquid doesn't give.
- **Open orders.** Always unverified, with orders and count null, because `frontendOpenOrders` has no response timestamp. The reason says an order's placement time doesn't establish that the set is current.
- **Tab.** The drill-down (`dashboard/src/app.js:1400`) shows the account state's status, its own time and its age when received. Every value goes through the null-with-reason renderer. "No positions" appears only for a fresh answer that lists none, and open orders show as unavailable with the reason.

**Tests.**
- `test_hour_old_account_state_is_delayed_and_null`: Codex's case, one hour old with a 7-coin BTC position and $700 account value. It is `delayed`, with the source time kept and every value null.
- `test_fresh_account_state_shows_values_and_missing_positions_are_unknown_not_empty`: covers the fresh path, the missing-`assetPositions` path and a missing `time` (unverified).
- `test_open_orders_are_never_presented_as_current`.
- `tests/test_thresholds_and_review.py::test_wallet_drilldown_with_recorded_info_responses`: real recorded answers, whose `time` is 0.09–0.14 s before the fetch, come out fresh.

**Live.** In the contest Hermes v0.23.0 dashboard, a wallet opened from the BTC panel showed "account state fresh · its time … · 0.126 s old when received (limit 10 s)", with its positions. Open orders showed as unavailable with the reason (`evidence/screenshots/review2-wallet-drilldown-live.png`).

## 6. BLOCKING: "The versioned schema does not describe several published metrics correctly."

**Changed** in `schema/metrics.v1.json` (51 → 58 metric entries):
- **Depth.**
  - `book.depth_band` (line 237) now defines the two separate outputs, exact and approximate, and when exact is available: the side's deepest full-precision level reaches the band edge, no level on that side is malformed, and the mid is known.
  - The new `book.depth_band_approximate` (line 834) defines the representative-price sum and `range_usd = [approx − the outermost bucket counted, approx + the nearest bucket left out]`, with its completeness conditions. Codex's case (`[0, 497]` and `[0, 905]`) follows from it.
  - `book.depth_imbalance` (line 256) covers both outputs.
- **CVD.** `flow.cvd_bucket` (line 355) now defines `cumulative_usd` from `cumulative_from`, and a `slice` rule for `hl_flow`: `cumulative_base_usd` plus the shown deltas.
- **Wallet account metrics.** New entries `wallet.current_account_value`, `wallet.current_total_position_notional`, `wallet.current_margin_used`, `wallet.current_position` (size, entry, position value, unrealized PnL, margin used, liquidation price, leverage) and `wallet.current_open_orders` (lines 847–895). Two freshness fields, `freshness.fields.clearinghouseState` (line 139) and `freshness.fields.frontendOpenOrders` (line 145, never fresh), define their freshness and completeness rules.
- **Trade validity.** `trades.validity` (line 907) states the rule from finding 3.

**Test.** `test_schema_describes_exact_approximate_cvd_slice_and_account_metrics`.

## DEFERRED 1: "Status and catalog still claim public PRESSURE requires node mode."

**Changed.** `NODE_PANELS` (`hl_desk/state.py:26`) now lists "Flow Deck activity buckets (new long, cover, new short, exit)". The catalog known issue (`catalog/hyperliquid-desk.yaml:19`) lists "the opening and closing activity buckets". The activity-bucket limitation stays. **Test:** `test_pressure_is_not_declared_node_only`.

## DEFERRED 2: "Long valid home paths silently disable the owner service."

**Changed.**
- **Fallback path.** `sock_path` (`hl_desk/owner.py:38`) keeps `owner.sock` in the profile's data directory when the path fits (`MAX_SOCK_PATH = 100`). Otherwise it uses `<system temp>/hl-desk-<uid>/<sha256 of the data directory, 20 hex>.sock`. That path is the same for every process of the profile, and unique per profile.
- **Private directory.** `_secure_fallback_dir` (line 48) creates that directory with mode 0700 and refuses it unless it belongs to this user and is private. The socket itself is still 0600.
- **Visible errors.** If even that path is too long, or the directory isn't private, the error is no longer silent. The collector state carries `owner_service_error` (`hl_desk/collector.py:97`), and `/hl status` prints it (`hl_desk/commands.py:26`).
- **README.** It documents the fallback.

**Test.** `test_long_data_dir_gets_a_short_private_socket`: a 120-character directory name gets a short socket in a private directory. A request is answered over it, and the socket is removed on close.

## DEFERRED 3: "The README still discloses a snapshot file writer that was removed."

**Changed.** `README.md:67` now says blocking HTTP requests run in the collector loop's executor threads, and that snapshots stay in memory with nothing written per snapshot. The runtime docstring (`hl_desk/runtime.py:12–14`) says the data directory holds the owner lock and, while the collector runs, the owner socket. **Test:** `test_readme_describes_in_memory_snapshots_only`.

## Also changed

- **Drill-down position.** Starting the overlay at the tab's left edge (`dashboard/src/app.js:1323`, `1436`) stops the Hermes sidebar from covering the drill-down. It was visible at 1440 px in the live check.
- **Docs.** README, `docs/SNAPSHOT.md` and the commentary skill describe the trade-validity gap, the `served` and `historical` blocks, and the wallet account freshness.
