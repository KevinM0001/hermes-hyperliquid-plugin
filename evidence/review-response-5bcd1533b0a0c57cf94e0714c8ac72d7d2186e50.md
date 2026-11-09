# Response to Codex h1 review 2 of `5bcd1533b0a0c57cf94e0714c8ac72d7d2186e50` (REVISE, 3 blocking)

Fixed in `29afff11eeecd55ba75f32fce64328876f9a1052` (code); this file, the report update and the official logs are in the evidence commit that follows it. Every BLOCKING finding is fixed; the DEFERRED ones are left until after the release, as the review loop asks.

## B1 (BLOCKING): expiring a confirmed Flow Deck card crashes the tab

> Both expiry paths assign the entire `confirmed_regime` object to `last_confirmed.regime`. The renderer expects a string. Once trades lapse on a card with a confirmed regime, React receives an object as a child and throws error #31. The expiry transformation also drops the confirmed regime's `since` and looks for a tick field absent from a complete card. … Preserve the historical card's expected string/time fields and test a **rendered, confirmed** card through trade expiry.

**Agreed; fixed.**

**What changed.** `hl_desk/serving_public.py` `withheld_card` and the tab's `withheldCard` (`dashboard/src/app.js`, inside `ageShown`) now build exactly the card the build makes when trades aren't fresh (`DeckEngine.card(reason)`):
- `last_confirmed` is `{regime: <the state string>, since_ms, since, as_of_tick_ms, note}`;
- `last_complete_tick_ms` comes from the complete card's `tick_ms`;
- the keys are the same.

**Evidence.**
- `tests/test_h1_review2.py::test_b1_withheld_card_is_the_builds_own_shape`: for an engine with ABSORBING confirmed, the serving-time withheld card equals `DeckEngine.card(reason)` key for key. The regime is the string `ABSORBING`, and the `since` time and tick are the build's.
- `test_b1_the_tab_withholds_a_confirmed_card_in_the_builds_shape`: the tab's own `ageShown`, run under node, on the chosen market's card and on another market's Flow Deck summary, gives the build's keys and the build's `last_confirmed`, with every number null.
- **Rendered, in the browser:** your natural-expiry reproduction (`~/contest/engine-run/burst-cal/repro_b1_r2.py`, headless Chrome). It ran against the installed plugin at `29afff1` in public mode.
  - Started the collector from the tab and waited until BTC's card showed a confirmed regime (ABSORBING, after 167 s), then aborted every later `snapshot` request so that snapshot stayed on screen.
  - Probed every 5 s for 40 s.
  - Through 25 s: the card kept its confirmed ABSORBING, with page text about 20,400 to 21,600 characters.
  - At 30 s: trades lapsed, and the card turned unavailable in place, showing "last confirmed: ABSORBING since 01:18:30Z as of tick 01:18:30Z · historical: the last complete tick's confirmed regime". The page text stayed at 19,117 characters, not 0.
  - Across 41 aborted polls: no exceptions and no empty view.
  - The text log is in `evidence/logs/burst-calibration/repro-b1-29afff1.log`.

## B2 (BLOCKING): node walls use a different median from the documented metric

> `sizes[len(sizes) // 2]` selects the upper middle observation for an even-sized population. The schema and displayed definition say "median" … four bid orders at price 100 with sizes 0.1, 0.2, 0.3 and 1.4 … The median is $25 … `walls()` returns an empty current list.

**Agreed; fixed.**

**What changed.**
- `hl_desk/util.py` `median` returns the middle value, or the mean of the two middle values for an even count, keeping Decimals exact.
- Node walls use it (`hl_desk/node/panels.py` `_track_walls`).
- **Found while fixing:** the clock offset, which the schema also calls "median of the last 50" (`hl_desk/state.py` `_clock`), had the same upper-middle selection. It uses `median` too.

**Evidence.** `tests/test_h1_review2.py::test_b2_walls_use_the_documented_median` is your fixture. The four bids' median is $25 and the threshold $125, so the $140 order (oid 4) is the only current wall. `median` also gives 25 and 2.

## B3 (BLOCKING): the required demo chat misidentifies the largest ask level

> The answer called the ask at $74,958, size 10.36 BTC, API count 32, the largest aggregated ask level in the returned top 20. The same tool result contains a larger ask at $74,975, size 28.65934 BTC … Ground extrema in explicit computed fields or omit unsupported comparative narration, then rehearse again.

**Agreed; fixed by grounding it in a computed field.**

**What changed.**
- `hl_desk/book.py` `largest_level` and `ladder_rows`: `hl_market`'s `levels.largest` now gives the returned level with the largest size on each side, as the whole level `{px, sz, api_count, notional_usd}`.
  - Ties go to the level nearer the top.
  - It is the same level as the chart's largest-level price line, which now calls the same function.
  - It is withheld with the ladder when the 20-level book isn't fresh.
- Schema entry `book.largest_level`.
- `skills/hyperliquid-commentary/SKILL.md` §2: "Don't rank or compare rows yourself (largest, smallest, most, biggest). Name a largest level only from `levels.largest.bid` / `levels.largest.ask` in `hl_market` …"

**Evidence.**
- `tests/test_h1_review2.py::test_b3_largest_level_is_a_computed_field_and_the_skill_uses_it`, on your rows: `levels.largest.ask` is `{px 74975, sz 28.65934, api_count 8, notional_usd 2148734.02}`. A tie in size goes to the level nearer the top, and the skill carries the rule.
- **Not done here:** the chat rehearsal itself. It needs the provider key, which only the review's Hermes home is set up to use. This session didn't run the model, so the rehearsal is left to your review.

## D1 (DEFERRED): serving-time freshness uses a different uncertainty classification from a new build

Deferred until after the release, as the review loop asks. As the finding notes, both paths withhold the values; only the label differs at the boundary (delayed against unverified).

## D2 (DEFERRED): the prescribed demo did not fit its advertised duration

Deferred until after the release, as Kevin decided (Nov 9) and the review loop asks.
