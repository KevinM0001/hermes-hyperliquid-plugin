# Commentary: worked examples

Five answers in the target form of `skills/hyperliquid-commentary/SKILL.md`: bottom line first, 3–5 bullets, about 120 words, then one source line. Each one is built from recorded data, not from live data.

**Labels**
- **[record]**: a recording of a live session: the terminal deck's transcript, or a websocket capture of public data.
- **[replay]**: a node recording, a historical replay of Kevin's archived node capture. Its manifest says "not live", and the plugin labels it REPLAY.

**How the numbers were read.** Each recording was streamed, never loaded whole. Node files were reached by a byte-offset search on block time. The plugin's own functions were called on the recorded trades:
- `hl_desk.deck.window_metrics` and `classify` for the Flow Deck numbers and labels;
- `hl_desk.node.calibration` for burst thresholds and ranks.

The plugin's collector, the engine and the plugin's replay were not run. That has three consequences:
- The examples quote no snapshot IDs.
- Confirmed or pending labels (the latch) aren't claimed, only each tick's live classification.
- Where a figure comes from the recording's own message rather than a tool, its provenance says so.

Word counts are whitespace-separated tokens in the bottom line and bullets, not the source line.

| # | Mode | Label | Recording | Span (UTC) | Rules it exercises |
| --- | --- | --- | --- | --- | --- |
| 1 | terminal deck | [record] | `deck-live.log` (a local file outside the repo) | 2026-09-29 17:20–17:23 | reliability line; new risk vs unwind; impact per dollar; cross-asset claim refused; instrument by price |
| 2 | public | [record] | the run4 public websocket capture (outside the repo) | 2026-11-08 00:45:30–00:47:00 | top-1 share; impact per dollar; two facts, no contrast; capture gap; unavailable with reasons; uncovered band |
| 3 | public | [record] | same file | 2026-11-08 00:45:30–00:47:00 | the activity-threshold trap; IMPACT unavailable under $100K; top-1 share with its net |
| 4 | node | [replay] | Engine's node recording (outside the repo) | 2026-11-04 15:03:30–15:05:00 | burst direction, time and rank; opening vs closing per window; an alert fires once; stops on request: minimum sizes, venue check |
| 5 | node | [replay] | Engine's node recording (outside the repo) | 2026-11-05 23:01:00–23:02:30 | one event dominating a window; closing, not new shorts; two alerts with time and direction; directional label not the subject |

---

## 1. HYPE sells on one account while BTC is bought (the review's §6) [record]

**Source.** The review (`commentary-brief-review-and-testset-20261109.md` §6) rebuilt this from the terminal Flow Deck's transcript, 2026-09-29 17:20–17:23Z. The frames are in `deck-live.log` (a local file outside the repo), from line 95503 (17:20:08Z). The study's §9.2 holds the tick table it uses. The answer is the review's text, unchanged. It comes from the terminal deck, not this plugin, so it has no snapshot line. The plugin's version would end with one.

> **HYPE is selling off hard on one seller, and the book under it is refilling.**
>
> - HYPE 86.17 → breaking lower. A single account is **93% of aggressive flow**, peaking at **−$2.44M** and holding the top spot for 7 ticks (17:20:28–17:21:18Z).
> - This is **new short risk**, not people getting out — the flow is tagged `Open Short`, a 1,216× jump off its own baseline.
> - **Impact is falling fast**: 38.0 → 33.3 → 23.4 → 5.2 → 1.5 over about sixty seconds. Same selling, less price movement each tick — bids are refilling underneath.
> - **BTC is doing the opposite**: absorbing **$1.32M of new longs** at 83,114 in the same window. The two are diverging, not one dragging the other.
> - Impact decay is a description, not a signal — across 7.7 hours it fired 16 times and nothing measurable followed.

115 words by the review's count; 142 whitespace-separated tokens.

**What it shows.**
- No label is the subject of a sentence.
- The seller's share is stated, so "selling off" can't be read as broad.
- The open/close tag separates new risk from unwind.
- Impact is given per dollar.
- The cross-asset bullet refuses a causal claim, and each market is named by its own price (86.17 against 83,114).
- The last bullet is the reliability line, for this deck's impact-decay alert (BOOK REFILLING, n=16).

---

## 2. "What is BTC doing?" on public data [record]

**Source.** the run4 public websocket capture (outside the repo), a public websocket capture, 2026-11-08 00:42:05–00:47:12Z. Connection 1 dropped at 00:44:35Z and connection 2 started at 00:44:41Z. The answer reads the Flow Deck window ending at the tick 00:47:00Z, 00:45:30–00:47:00Z, the last complete window. This is the demo's chat question.

> **BTC flat near 75,756 (+2 bps in 90 s). Takers bought $809K more than they sold; one account was most of it.**
>
> - 0x3f5fdb2410cba492b7c3828c811c6662b2894d0b was 60% of the window's taker notional, net +$530K (as of 00:47:00Z).
> - Little price change per dollar: 2.4 bps per $1M of net buying. This tick classifies as BALANCED (no threshold met); the three before were ABSORBING (small move for the flow).
> - Book: 1-dollar spread. The ±0.25% band isn't covered by the 20 returned levels. Within them: bids $7.1M down to 75,737, asks $4.9M up to 75,776.
> - The feed reconnected at 00:44:41Z, so earlier windows aren't complete. Mark and funding: unverified (no source timestamp). New longs vs closes need node mode.
> - Awareness only: none of these labels has been shown to predict price.
>
> Recording run4 · 2026-11-08 00:45:30–00:47:00Z · public data · book and trades recorded; mark and funding unverified

131 words. A live answer's last line names the snapshot instead: `Snapshot <id> · <time> · LIVE public data · …`.

**Where the numbers come from** (`window_metrics` on the 154 recorded trades in the window):

| Figure | Value |
| --- | --- |
| move | 75,741 → 75,756, +1.98 bps |
| net flow | +$808,729 |
| volume | $881,031 |
| IMPACT | 2.45 bps per $1M |
| PRESSURE | 23.37 |
| top taker | 0x3f5fdb2410…, 60.2%, net +$530,187 |
| live classification | BALANCED; the ticks at 00:46:15, 00:46:30 and 00:46:45Z were ABSORBING |

The book figures come from the recording's own messages:
- The fast book at 00:46:59.624Z: 75,756 / 75,757.
- The 20-level full-precision book at 00:46:55.851Z: bids $7,122,931 to 75,737, asks $4,899,575 to 75,776, about ±0.025%.

Mark, funding and open interest are in the recording, but the plugin rates that context feed `unverified` (no source time), so they aren't quoted. The ±0.25% approximate band would need the nSigFigs books, which come from info polls that aren't in this capture.

**What it shows.**
- The review's two-facts form replaces "but nothing is moving price".
- "Buyers are ahead" is shown to be mostly one account.
- Impact is given per dollar.
- The reconnect is given in plain words.
- Each unavailable value is given with its reason.

---

## 3. "What's HYPE doing?": a big move on small flow [record]

**Source.** The same capture, the same window: 00:45:30–00:47:00Z, ending at the tick 00:47:00Z.

> **HYPE up 6 bps in 90 s, to 77.597, on only $58K of trading.**
>
> - Small flow moved it a long way (the deck's VACUUM-UP: a 2 bps+ move on under $150K traded). Impact per $1M is unavailable: net flow was under $100K.
> - One account was 66% of the window's taker notional, net selling $39K (0xb3657c6e16569eb1b956570e900a4d0f1d174920, as of 00:47:00Z). Takers overall sold $25K more than they bought.
> - Little resting near price: $62K of bids down to 77.573 and $60K of asks up to 77.621 (the 20 returned levels; they don't reach ±0.25%).
> - The feed reconnected at 00:44:41Z; earlier windows aren't complete. Funding and open interest: unverified (no source timestamp).
> - Awareness only: none of these labels has been shown to predict price.
>
> Recording run4 · 2026-11-08 00:45:30–00:47:00Z · public data · book and trades recorded; funding and open interest unverified

125 words.

**Where the numbers come from.** `window_metrics` on 147 recorded trades:

| Figure | Value |
| --- | --- |
| move | 77.549 → 77.597, +6.19 bps |
| volume | $58,342 |
| net flow | −$24,589 |
| IMPACT | null (net under $100K) |
| PRESSURE | 0.41 |
| top taker | 0xb3657c6e16…, 66.5%, net −$38,769 |
| live classification | VACUUM-UP, tag `thin` |

The book is the 20-level full-precision book at 00:46:55.851Z: bids $61,900 to 77.573, asks $60,101 to 77.621.

**What it shows.** This is the trap the review ranks highest under a brevity target. A 120-word answer is pulled toward "HYPE is quiet: $58K traded". The answer leads with the move and the flow that made it, and says that IMPACT is unavailable rather than dropping the point. The rising price and the net selling are two facts with no "but". The account behind most of the flow is named with its net and the time of the observation.

---

## 4. "What just happened in BTC?" in a node recording [replay]

**Source.** Engine's node recording (outside the repo), Nov 4 node recording, labelled "BTC $50M buys 15:04". It's a historical replay with all checks passing and partial history (it starts at each market's first full snapshot). Read at block times 15:03:30–15:05:00Z.

> **In the recording (Nov 4, 15:05:00Z), BTC rose 79 bps in 90 s, 79,513 → 80,141, on $74M of taker buying.**
>
> - Buying burst at 15:04:28Z: $34.5M in 60 s, over the $30M threshold (set for about 10 a day; a top 0.083% minute, Nov 3 to 7). Five wallets made up half of it.
> - Opening and closing: the burst's buying was 62% new longs. Over the 90 s to 15:05:00Z, 55% was shorts covering.
> - Not one account: the top taker was 15% of the window's taker notional (0xcf3f419d08a5bdc2c6e5fbd9ad70904c5420f95f, net +$10.6M).
> - Small move per dollar: 1.3 bps per $1M of net buying (the deck's ABSORBING). A liquidation alert fired at 15:04:09Z (2 short liquidations, $101K); it fires once per episode.
> - Awareness only: none of these labels has been shown to predict price.
>
> Recording w20261104T1400Z-1600Z · REPLAY, node data, not live · 2026-11-04 15:03:30–15:05:00Z · no live delay (a replay runs on its own clock)

136 words.

**Follow-up: "Any stops above price?"**

> **Buy stops just above price: about a BTC or less of stated size per band, and every size is a minimum.**
>
> - 80,100–80,200: 26 orders, at least 0.56 BTC; 7 are whole-position TP/SL with no stated size.
> - 80,200–80,300: 40 orders, at least 1.22 BTC; 23 have no stated size.
> - Resting asks in the 50 levels the node book returned, 80,141 to 80,198: 150.65 BTC.
> - Trailing stops aren't counted. The stop set is checked against the venue only when the engine restarts.
>
> Recording w20261104T1400Z-1600Z · REPLAY · stops as of 15:04:58.676Z, book as of 15:04:59.941Z

84 words.

**Where the numbers come from.** The Flow Deck numbers are `window_metrics` on the recording's node executions, 15:03:30–15:05:00Z, with the taker from each execution:

| Figure | Value |
| --- | --- |
| buying | $73.96M |
| selling | $11.51M |
| net flow | +$62.45M |
| IMPACT | 1.26 bps per $1M |
| PRESSURE | 6.43 |
| top taker | 0xcf3f…, 15.0%, net +$10.55M |
| live classification | ABSORBING |

The open/close split uses each taker's position effect:
- 90 s window: buying 55% cover, 45% new long.
- The burst's own 60 s: 62% new long.

The burst follows the node rule: per block, 60 s taker sum. It fired at block time 15:04:27.646Z with $34.51M. Its rank is `calibration.rank`: "a top 0.083% minute, Nov 3 to 7". 5 of 413 takers reach 50%.

The liquidation alert follows the plugin's rule (2 liquidations or $1M in 60 s, fires once, re-arms below both). It fired at 15:04:08.870Z: 2 liquidations, $101,402, all shorts. Liquidations kept coming, so it didn't re-arm. 336 liquidations ($11.9M) fell in the 90 s window, and no alert counts them. The answer quotes the event as fired and says it fires once, rather than adding up a total no tool returns.

Stops are the recording's `bands` message at 15:04:58.676Z: rows `[lo, hi, kind, side, orders, sz, position_tpsl_orders]`. Asks are its `depth` message at 15:04:59.941Z.

**What it shows.**
- Each open/close figure carries its own window: 60 s burst against 90 s card.
- The burst is given with its direction, time, threshold label and rank label.
- The top-1 share shows the buying was broad.
- A label that sounds calm (ABSORBING) sits next to a 79 bps move, and neither is the subject.
- The liquidation alert is quoted as it fired, with its fire-once rule.
- Stops come only when asked: band by band, as minimums, with the venue-check caveat, and with no total computed across bands.

---

## 5. "What's BTC doing?" just after a sell burst [replay]

**Source.** Engine's node recording (outside the repo), Nov 5 node recording, labelled "BTC $56M sells". Its manifest flags 53 maker/taker leg-sum mismatches at height 1210632898, marked a known false positive, near the end of the file and outside this window. Read at block times 23:01:00–23:02:30Z; the Flow Deck tick is 23:02:30Z.

> **In the recording (Nov 5, 23:02:30Z), BTC is at 75,700, up 9 bps over 90 s. The window's $51M of net selling is one 30-second stretch.**
>
> - 23:01:30–23:02:00Z: net selling $54.5M. The 30 s before it: −$5.4M. The 30 s after: net buying $8.8M.
> - Selling burst at 23:01:39Z: $55.7M in 60 s (a top 0.042% minute, Nov 3 to 7). Three wallets made up half; the top one was 41%.
> - Mostly longs closing, not new shorts: 77% of the burst's selling closed longs. A liquidation alert fired at 23:01:39Z: 36 long liquidations, $213K.
> - The 90 s window classifies as PULLED-OFFERS (price up against net selling). Here that total is one burst, not the whole window.
> - Awareness only: none of these labels has been shown to predict price.
>
> Recording w20261105T2300Z-2400Z · REPLAY, node data, not live · 2026-11-05 23:01:00–23:02:30Z · no live delay (a replay runs on its own clock)

131 words.

**Where the numbers come from.** The Flow Deck numbers are `window_metrics` on node executions, 23:01:00–23:02:30Z:

| Figure | Value |
| --- | --- |
| move | 75,630 → 75,700, +9.26 bps |
| net flow | −$51.06M |
| volume | $95.04M |
| IMPACT | 0.18 bps per $1M |
| PRESSURE | 0.30 |
| top taker | 0x874226b22939904f436ff0434e1fd0ab257c8427, 23.8%, net −$22.6M |
| live classification | PULLED-OFFERS |

The path is in 30 s buckets, the same shape as `hl_flow`'s:

| Bucket (UTC) | Buy | Sell | Net |
| --- | --- | --- | --- |
| 23:01:00 | $0.59M | $5.95M | −$5.36M |
| 23:01:30 | $11.04M | $65.51M | −$54.47M |
| 23:02:00 | $10.36M | $1.59M | +$8.77M |

The sell burst fired at block time 23:01:39.121Z with $55.68M. Its rank is `calibration.rank`: "a top 0.042% minute, Nov 3 to 7". 3 of 712 takers reach 50%, and the top one has 40.6%. Its open/close split: 77% exit, 23% new short.

The liquidation alert, by the plugin's rule: 23:01:38.717Z, 36 liquidations, $212,516, all longs.

The top seller sold 300 BTC ($22.6M), all closing a long, in two blocks: 23:01:32.728Z and 23:01:39.121Z. That fits the "0 s = one block" rule; the answer leaves it out for length.

The price low inside the window, 75,304, isn't in any tool result: the chart isn't returned by a tool. So the answer gives the flow path, not a price path. TOOL_PROPOSALS.md proposes adding a short price path.

**What it shows.**
- This is the review's §2.7 failure, the dollar aggregate standing in for the path, and test item 9.
- A model reading the card alone sees "$51M of net selling, price up 9 bps, PULLED-OFFERS". From that it could invent offers being pulled, or a squeeze.
- The answer gives the path instead, and says the total is one burst.
- It calls the selling closing, not new shorts (test item 7 in node form).
- It gives both alerts with their direction and time.
- It keeps the directional label out of the subject position.
