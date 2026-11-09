# Response to Codex h1 review 1 of `56c65e39edf8660a198f3deccb74682eaa21b5d8` (REVISE, 2 blocking)

Fixed in `c0be2a068fdd7c17c237a7a446958ecfe0fa6046`, with two follow-ups: `5dda388285da449f1aad3d8100b0b0bebc9c0122` (statuses still fresh show their ages as of now) and `edc5a6c6e425931b2ac04dce775f488369a15d43` (every status shows its ages as of now). That is the final code; this file, the report update and the official logs are in the evidence commit that follows it. The review loop asks for every BLOCKING finding to be fixed and the DEFERRED ones to be left until after the release; that is what was done.

## B1 (BLOCKING): latest public tool results do not age their source freshness before serving

> `run_tool` re-evaluates the node families, but leaves public fields' build-time statuses and numbers intact. The runtime permits a latest snapshot up to five seconds old. A public feed can therefore pass its own freshness deadline while that snapshot remains eligible for a latest read. … The reproduction publishes a snapshot whose fast book has a progress age of 2.9 seconds, then advances the serving clock by one second and calls the actual `run_tool('hl_market', ...)` path. The result is `ok: true`, `served.latest: true`, … fast-book status `fresh`, … best bid **99.0**.

**Agreed; fixed.**

**What changed.**
- **`hl_desk/serving_public.py` (new).**
  - `lapse` re-judges a public status that was fresh at build time at the snapshot's served age. Progress age plus that age over the max age reads `stale`. Source age plus that age plus the clock bound over the source max age reads `delayed`. Either way the reason says "by the time this was served".
  - `age_market` then withholds every value the build gates on that field, in the build's own withheld shape:
    - `book_fast`: the top of book and its best-bid and best-ask price lines;
    - `book_full`: the ladder, its largest-level price lines, and the exact and observed depth built on it;
    - `book_sf4` and `book_sf3`: the approximate and observed depth built on them;
    - `trades`: the flow values, the CVD buckets' values and cumulative origin, the large-print count, the Flow Deck card (as `DeckEngine.card` withholds it), and the wallet panels;
    - `ctx`: the context values and their price lines.
  - Every status still fresh shows its ages as of the served moment (`aged_by_s`), and the lapsed fields are added to `unavailable`.
  - Records the build doesn't gate are kept: the tape, the candles, the large prints themselves and the deck events.
- **`hl_desk/tools.py` `run_tool`.** The latest snapshot is aged after the node aging: `snap = age_snapshot(age_snapshot_for_serving(snap))`. A snapshot read by ID is historical and stays as of its build time, with its `historical` label.

**Evidence.**
- `tests/test_h1_review1.py::test_b1_latest_public_reads_are_judged_at_serving_time` is your reproduction, through `run_tool` and the tab's tool route. The fast book has a 2.9 s progress age at build (max 3 s) and is read 1 s later as the latest snapshot. It reads `stale` with "by the time this was served", with a progress age of 3.9 s. `best_bid`, `best_ask`, `mid`, `spread` and `spread_bps` are null with the reason. The 20-level book (max 15 s) stays fresh with its levels. A historical read of the same snapshot still shows 100.0 and `fresh`, labelled historical.
- `test_b1_lapsed_trades_withhold_flow_deck_and_wallets`: trades fresh at build time with a 29.5 s progress age (BTC's max is 30 s), read 1 s later:
  - `hl_flow`'s values, the CVD values and the large-print count are withheld;
  - `hl_alerts`' card regime and numbers are withheld;
  - `hl_wallets`' panel is withheld with the reason;
  - `hl_status` reports trades `stale`;
  - the tape is kept.
- The official run at the fixed commit is in `evidence/logs/burst-calibration/` (see the report).

## B2 (BLOCKING): the tab retains expired numbers and fresh panel badges after detecting expiry

> On arrival, `snapshotValidity` can reject a snapshot, but the code still calls `setResp(data)`. The timer subsequently changes only `aged`. … the ladder, flow, depth, chart, wallets and deck continue consuming the unchanged snapshot and its unchanged freshness statuses. … Keep the layout if desired, but invalidate the affected numbers and statuses together.

**Agreed; fixed, keeping the layout.**

Kevin's Nov 8 instruction for node mode stands: a delay never blanks the page or swaps it for the empty view, and values and labels update in place. Your finding is that values whose freshness has lapsed must not stay on screen as fresh. Both now hold: the page keeps its layout, and lapsed values and statuses turn unavailable together, in place, with the reason.

**What changed.**
- **`dashboard/src/app.js` `ageShown` (new, pure).** It ages the snapshot on screen with the same rules as the backend, counting the served age plus all the time since the tab asked for it:
  - public fields, as `serving_public.py`, with the reason "while on screen (the snapshot is about N s old)";
  - node families and the node values built on them, as `hl_desk/node/serving.py`, on the averaged delay;
  - the chosen market and every market's Flow Deck summary.
  - Fresh statuses that haven't lapsed show their ages as of now.
- **`App`.** It renders `ageShown`'s view on arrival and re-ages it every second while the snapshot stays on screen. The delay bar still says why the snapshot isn't current. The empty view is used only when the snapshot route has no current snapshot at all.

**Evidence.**
- **`tests/test_h1_review1.py::test_b2_the_tab_withholds_lapsed_values_in_place`.** It takes a real tab payload from the snapshot route and runs the tab's own `ageShown`, extracted from `app.js`, under node:
  - at 0.5 s nothing is withheld, and the fresh ages advance by 0.5 s;
  - at 14 s, your held-delivery case, the fast book is `stale` "while on screen", the top of book is null with the reason, and its price lines are removed, while the 20-level book (15 s) and trades (30 s) are still fresh;
  - at 15.5 s the ladder and the full-precision observed depth are withheld;
  - at 31 s the flow values, the Flow Deck card and every wallet panel are withheld;
  - the Python rule gives the same statuses and the same withheld values at the same age.
- **`test_b2_the_app_shows_the_aged_view_on_arrival_and_while_shown`.** The app renders that view on arrival and every second, and never blanks the page.
- **A browser reproduction of yours** (`~/contest/engine-run/burst-cal/repro_b2.py`, headless Chrome). It ran against the installed plugin at `edc5a6c` with the public collector started from the tab and no node feed. It held one real `snapshot?market=BTC` answer for 12 s, delivered it unchanged, and aborted the later polls. Results:
- Before the hold: BID/ASK 74,853 / 74,854, with "l2Book fast · fresh · 0.9 s".
- 6 s after the hold began, with the snapshot on screen about 6.4 s old: BID/ASK, spread and their badges read "unavailable", with "l2Book fast (5 levels) stale: no progress for 6.9 s (max 3 s), while on screen (the snapshot is about 6.4 s old)". The fast-book chip read "stale · 6.9 s", and trades read "fresh · 6.5 s / max 30 s".
- After the held 12 s answer was delivered, and through t+20 s: the same, with ages counting up (trades fresh at 19 s, under its 30 s limit). At the end, the screenshot shows the 20-level ladder unavailable with "l2Book full (20 levels) stale: no progress for 23.5 s (max 15 s), while on screen (the snapshot is about 19.4 s old)". The context badge shows its age as of then ("unverified · 20 s"), and the chart's book price lines are withheld.
- The empty "No snapshot" view never appeared. The aborted polls were said in the delay bar ("Last poll failed …").
- The log and screenshot are in `~/contest/engine-run/burst-cal/tab/`. The browser-check text is copied in `evidence/logs/burst-calibration/repro-b2-edc5a6c.log`.

## D1 (DEFERRED): the README names the wrong metric-schema version

Fixed in this evidence commit at Kevin's request (Nov 9), so this review covers it. `README.md:38` now says `schema/metrics.v1.json` (v1.2.0), matching `schema/metrics.v1.json`'s `version`.

## D2 (DEFERRED): this rehearsal did not meet the demo's advertised timing

Deferred until after the release, as the review loop asks. (The chat step took 1 min 13 s against the 22 s the demo script allows.)
