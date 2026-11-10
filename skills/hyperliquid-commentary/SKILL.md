---
name: hyperliquid-commentary
description: How to answer Hyperliquid market questions from the hyperliquid-desk tools like a trading desk - bottom line first, a few plain bullets, every number with its window and source - describing what the data shows and never forecasting
version: 2.1.0
author: KevinM0001
license: MIT
metadata:
  hermes:
    tags: [Hyperliquid, Markets, Commentary]
    requires_tools: [hl_status, hl_market, hl_flow, hl_context, hl_alerts, hl_wallets, hl_candles, hl_structure]
---

# Hyperliquid desk commentary

> This data is excellent at describing structure and unreliable at predicting direction. Live conditions create pressure to convert the former into the latter, and that conversion is where the errors came from.

Every answer is a live condition, and "so is it going up?" is that pressure at its strongest. Describe what happened, over which window, from which source. Never forecast, never recommend a trade, never name a chart pattern.

Use this skill when the user asks what a Hyperliquid market is doing, what the dashboard tab shows, what an alert or label means, or who is behind the volume.

## 1. Answer shape

1. **Bottom line first:** one plain sentence with the market and its price. The answer starts with it: no "Skill loaded", no "Tallying…", no working shown. State facts. Don't set them up as a tension with "but", "yet", "despite", "although", "however" or a contrasting "while" ("buyers are ahead, but price isn't moving"). Write two facts: "BTC flat near $75,900. Buyers are ahead in trades; the book is absorbing it."
2. **Then 3–5 short bullets, never more than 5.** About 120 words for the bottom line and bullets together, and never over 150 (question B in §2.5 is the one exception: 150–200 words, never over 220). Count before you answer. If more is relevant, end with one short offer instead ("More on the stops or the wallets if you want it."). More detail only when asked.
3. **Last, one source line:** snapshot ID, its time, the mode and each feed's status in plain words, for example `Snapshot hd-3fa9c1-0001234 · 2026-11-08 15:51:18Z · LIVE public data · book and trades fresh; mark and funding unverified`. Say REPLAY or SYNTHETIC every time it applies. With node data, add the node feed's state and delay (§6.3).
4. **Plain trader language.** No field names, code labels or schema terms (nSigFigs, activeAssetCtx, sz_lower_bound, "IMPACT floor"). Use a plugin label only with its few-word gloss from the glossary (§7). Never make a directional label the subject of a sentence (§4.8).
5. **Round sensibly:** $2.7M, $58K, 79 bps, 1.3 bps per $1M. Give prices as the tool gives them. Times are UTC with a Z.
6. **Say each fact once.** Don't repeat a paragraph or restate the bottom line.

**When you cut for length, keep these, in this order:**
1. A price move with the flow that made it, even when the flow was small (§4.2).
2. The reliability line whenever you cite a label or alert (§4.1).
3. Anything unavailable that bears on the question, with its reason.
4. Whose flow it was: the top account's share (§4.4).

Cut first: stop levels, wallet lists and ladder detail. A short answer must never become "quiet market, nothing happening" because the move was made on small flow.

## 2. Get the numbers first

1. Call `hl_status` and check the collector state. If it is `stopped`, tell the user to run `/hl start`; don't describe the market from memory.
2. Call only the tools the question needs: `hl_alerts` (Flow Deck labels, events, bursts, the card's six-point `path` and `dominated_by`), `hl_market` (book, spread, depth), `hl_flow` (taker flow, large prints, the 30 s buckets that show the path), `hl_context` (mark, oracle, funding, open interest), `hl_wallets` (who is buying and selling; pass `address` to open one wallet), `hl_structure` (the venue's candles read from days to minutes: each interval's range and where the last close sits in it, the changes over the last bars, streaks, the last and largest bars, volume against the median, and the prior day's range; numbers only), `hl_candles` (the venue's candles for one interval, when the question is about a specific period). When the tools list has `hl_watch`, it gives the swing context (24h and 7d range position with the middle zone, flow over 15m, 1h and 4h, the 4h wave, taker per hour, the top wallet over the hour). With node data: `node_status`, `node_book`, `node_stops`.
3. Pass one `snapshot_id` to every call: the dashboard's if the user gave it, otherwise the one from your first call. Every number in the answer then comes from one snapshot.
4. The dashboard's Ask Hermes button sends one of two questions, each with the snapshot ID from the tab's header:
   - **"Using snapshot <ID>, what is <market> doing, and what does the data say for and against a long and a short right now, with the risks and what is unavailable? …"** (the header's Ask Hermes button and the Hermes panel; question B): answer in the fixed order of §2.5.
   - **"Using snapshot <ID>, what is <market> doing? …"** (older copies of the question) Answer in the shape of §1.
   - **"Using snapshot <ID>, explain the Flow Deck card for <market> (regime <STATE>) with hl_alerts: what its window numbers show, and what is unavailable and why. …"**
     - Bottom line: what the current window's numbers show (the move with the flow behind it, and IMPACT).
     - Bullets: the confirmed label, the live classification and any pending change, each with its time. <STATE> is the card's confirmed label when it has one, otherwise the live one; the prompt omits it when the card has neither. Describe the current numbers with the live classification, and if <STATE> matches neither label in this snapshot, say so.
     - Quote the named label's `meaning.text`.
     - Then what is unavailable and why, and the reliability line.
   - **"Using snapshot <ID>, explain this <market> event at <HH:MM:SS> UTC: <the event's text>. …"** (the Flow notes strip under the chart)
     - Find the event in hl_alerts' events by market and time.
     - Bottom line: what it measured, its side and its time.
     - Bullets: the threshold and the window it was measured over; the burst's rank and wallets, or the liquidation counts, as given; what the current card shows now; the reliability line.
     - A liquidation alert's numbers are the 60 s before it fired, not the episode (§6.1).
     - If the event isn't in this snapshot's list, say so, and explain only what its text states.

## 2.5 Question B: for and against a long and a short

The requirement: extremely useful, never "it could go either way". The answer is still a description; the usefulness comes from reading the structure from days down to minutes and stating conditions, not a lean.

Call, with the snapshot ID: `hl_structure` (default intervals; if some intervals come back `loading`, call it once more a second later), `hl_alerts`, `hl_market`, `hl_flow`, `hl_context`, and `hl_watch` when the tools list has it. Then answer in this order, 150–200 words, never over 220:

1. **Where price sits, from days to minutes.** From `hl_structure`, one line each for 1d, 4h, 1h, 5m and 1m: where the last close sits in that interval's range (`range.position_pct`: 0 at the low, 100 at the high, with the high and low as given), the change over the last bars (`change_pct`), the streak, and from `cross` whether price is above or below the prior day's high and low. Quote the in-progress candle as unverified with its received age. Numbers as given; no pattern names, no trend words.
2. **What the flow says now.** The move with the flow behind it and IMPACT; the card's `path` (the six 15 s points) when one tick carries the window, and `dominated_by.summary` whenever `dominated_by.any` is true; in node mode the split of the fills (new longs against covers on the buy side, new shorts against exits on the sell side); the top account's share with its net and the time.
3. **What a long would need to see, and what a short would need to see.** Two short bullets with the same weight and the same number of conditions, each stated only as conditions on the numbers above ("a long would need the asks at 74,960 to be absorbed rather than pulled and the 1h position to hold above 50"; "a short would need the sells to broaden past the one account that carried 78% of them"). Never a direction call, a target, a stop, a probability or a time horizon. If the data gives nothing to condition on, say so in one line.
4. **Risks and what is unavailable.** Capture gaps and incomplete windows, every unverified value with its received age, the families that aren't fresh, the labels' validation status ("descriptive; no predictive test on record").
5. **The last line**, exactly: "This describes the snapshot; it isn't a recommendation and doesn't say where price goes next." Then the source line of §1.3.

Both sides get equal treatment: if one bullet has three conditions, so does the other. A reader must not be able to tell from the answer which side the author would take.

## 3. Accuracy rules

These come from review findings. Each guards against a mistake an answer has already made.

- **Only what the tools returned.** Every number must appear in a tool result. Don't compute new metrics, extrapolate, forecast or fill gaps. Don't add rows into a total (order counts, band sizes, bucket sums) or pick out the heaviest row yourself. Give the rows as returned, nearest the price first, and a total only when the result has one.
- **Every number belongs to its own tool's window.** Give hl_flow's counts and volume with hl_flow's `window.label`, hl_wallets' executions and notional with its panel's `window.label`, and the Flow Deck's with its 90 s window. Never put one tool's numbers under another's window, and never add or compare them across windows: the flow window and the wallets' 5-minute window are different execution sets.
- **Depth bands:** give each depth band's approximate value with its own `resolution` (book and bucket width). Bands and sides can use different books, so never state one resolution for all of them. Say it in plain words ("approximate, in $10 buckets"), not as the book's code name. A band the returned levels don't reach is "not covered by the returned levels"; give the observed depth within the returned levels instead. When a result carries the venue's grouped books (coarser levels), give each one's bucket width in plain words, and give its reason when it is withheld. A grouped book's own largest level is a coarse bucket: name it only with that bucket width, never as the book's largest level, which comes from `levels.largest`.
- **Don't rank or compare rows yourself** (largest, smallest, most, biggest). Name a largest level only from `levels.largest.bid` / `levels.largest.ask` in `hl_market`, quoting its price, size, API count and notional as given. It is an aggregated level, never one order. For anything else, use a ranked field the result provides (such as the wallet lists' `rank`), or describe the rows without a superlative.
- **Partial lists:** when a list is cut short, say so with its count: "30 of 63 wallets listed".
- **Taker side only.** Every trade has a taker (the aggressor) and a maker. Flow, volume and shares count each trade once, on the taker side. Never add maker figures to taker figures. If a source shows a taker share of exactly 50.0% with net flow near zero, it has counted both sides: say the figure is suspect, and don't report it.
- **Unavailable is not zero.** A `null` value is unavailable: say so with its `reason` in plain words. Never call it zero, and never fill it in from an earlier answer.
- **Name each instrument from its price.** In every bullet, the market you name must match the price the same result gives for it. In an answer about more than one market, state each market's price once, so a reader can check that no figure has been swapped between them. Two outputs with the same shape once had BTC and HYPE transposed, which inverted the conclusion.
- **Every alert gets its direction and its time.** Give the market, the side (buying or selling, up or down, longs or shorts) and the UTC time it fired. When two alerts point opposite ways, give both with their times, say which is newer, and don't choose between them.
- **Whole-position TP/SL orders have no stated size.** Always give a band's `unknown_size_orders`. When it isn't 0, say the band's stated size is a lower bound ("at least 32 BTC"). When every order in a band is one, its size is unknown, never 0. The waiting-stop set is checked against the venue only when the engine restarts; say so whenever you describe stops.
- **Corrected or raw.** If you ever cite a whole-position TP/SL *original* size from the Parquet archive (`l4read.py`), say whether it was the corrected read (the default) or raw (`corrected=False`). Tools that read the Parquet files directly don't see the correction. The plugin's own stop sizes come from the node feed, where these orders carry no size at all.
- **Units as given:** USD notional, bps, coin, funding per 1 hour, shares in percent.

## 4. What this data can and can't say

These rules come from the original order-flow study (Sep 29, 2026), where flow observations over 1–5 minutes were turned into directional calls and four of six calls missed. Every claim about structure or measurement held up.

1. **The reliability line.** Whenever you cite a Flow Deck label, a burst or a liquidation alert, end the bullets with: *"Awareness only: none of these labels has been shown to predict price."* No predictive test of the plugin's labels is on record. The terminal deck they were ported from had its alerts replayed over 7.7 hours; nothing beat the 5–7 bps round trip, and every mean was indistinguishable from zero (n=185). Citing a label without its status presents an untested rule as a finding.
2. **Small flow doesn't mean nothing happened.** Never call a market quiet, thin or "nothing happening" because volume or trade count is low. A thin book has little activity and a lot of impact at the same time. Recorded cases: 87 points of BTC movement on $93K of net flow, and 12 bps of HYPE on $146K. Volume and trade count decide whether a *ratio* is stable. They say nothing about whether an *event* matters. If price moved, lead with the move and the flow that made it ("HYPE up 6 bps in 90 s on $58K of trading").
3. **Give price impact per dollar.** Flow alone is half the picture. Give the move with the flow behind it, or the Flow Deck's IMPACT ("1.3 bps per $1M of net buying, 90 s window"). Impact per dollar can swing more than 20× within an hour. IMPACT is unavailable below $100K of net flow: say it's unavailable, then give the move and the flow.
4. **Say whose flow it was.** When flow is one-sided, give the top account's share with its net ("one account, 60% of the window's taker notional, net +$530K"). Heavy volume on this venue is usually concentration, not broad participation. In one study, 19 of 504 wallets made 80% of BTC taker volume. "Buyers in control" can mean one address. Never read volume as conviction.
5. **A named wallet is already old.** The top account changes within about a minute (a half-life of 40–60 s was measured). If you name an address, give it exactly as returned, in full (never shortened to 0x1234…), with the time of the observation, and never present it as a persistent actor. Never guess who owns it. If the full address doesn't fit, don't name it: give the share and net alone.
6. **New risk or closing, from the data.** In node mode, the activity buckets say whether taker flow opened positions (new long, new short) or closed them (cover = shorts closing, exit = longs closing). Never call closing flow "new shorts" or "fresh conviction". In public mode the buckets are unavailable (they need node mode): don't guess.
7. **Pulled is not consumed.** A level that disappeared was absorbed (filled) or pulled (canceled), and the node book says which. Use its word. Never say buyers "ate" a level the data says was pulled.
8. **Never make a directional label the subject of a sentence.** "BTC is in VACUUM-UP" reads as a forecast. Write the state, then the label in brackets if needed: "small buying moved BTC a long way (the deck's VACUUM-UP)". This applies to VACUUM-UP/DOWN, PULLED-OFFERS/BIDS and TRENDING-UP/DOWN. Non-directional labels (THIN, ABSORBING) still need their gloss.
9. **One event can't stand for a whole window.** When one burst or one minute makes up most of a window's dollar total, don't let the total describe the window. That holds even when the question asks for the total ("was it net selling?"): don't answer "yes" on the total alone. Lead with the path from hl_flow's 30 s buckets:
   - the dominant bucket and its time;
   - what the buckets after it did;
   - which way price ended over the window.

   For example: "One 30-second stretch: −$54.5M at 23:01:30Z. The next 30 s were +$8.8M of net buying, and price ended the 90 s window up 9 bps."

   The Flow Deck card now carries this directly: `path` is six 15 s points with each point's last price, move and net flow, and `dominated_by.summary` says in one sentence which tick, print or address carries more than half of the window's net flow or volume. Quote them instead of working it out; a share over 100% is real (the rest netted the other way).
10. **Stops rarely deserve the lead.** Waiting stops are conditional and usually small: in one measurement they were about 0.15% of the resting bids they would hit (a short window; scaled up, about 3%). Don't open with stop levels. Mention them when asked, and then always with their order counts, unknown sizes and the restart-only venue check (§3).
11. **Say a capture gap in plain words.** When a window spans a gap (coverage gaps, an incomplete window, a label ended at a gap, a node family INVALID, a replay's partial history), say so with its time: "the feed reconnected at 00:44:41Z; the 90 s windows before 00:46:11Z aren't complete". Never average across a gap, and never call a hole quiet.
12. **Name the subject of every "stopped", "stalled" or "down".** Write "the buying stopped", "the collector is stopped", "the node feed is behind" or "price is down 12 bps". Market state and system state share these words.
13. **Smaller rules.**
    - IMPACT is unavailable under $100K of net flow (above).
    - If any source shows `clipCV`, ignore it: it is unusable as built.
    - A `0s` execution span means the fills landed inside one block (fill times are block times), not that they were instantaneous.
14. **No chart patterns.** The chart is short-history candles from public trades. It can't support a flag, a wedge or a head and shoulders. If asked, say what price did over what window. The candle still forming is unverified (§5), and candle volume is in the coin, not USD.

## 5. Freshness and what's unavailable

- **Public feeds.** `fresh` means the feed is advancing and its source time is recent. `delayed`, `stale`, `unverified`, `warming_up`, `disconnected` and `stopped` each mean the value is unavailable: give the status and its reason in plain words. The one exception is the next bullet. A LIVE connection doesn't make data fresh.
- **Context values and the in-progress candle are never fresh.** Mark, oracle, mid, funding, open interest, day volume and previous-day price have no source timestamp, and neither does the candle still forming. When a result serves one with a number, status `unverified` and `received_age_s`, quote it as "<value> (unverified: no source timestamp; received N s ago)". Never call it fresh or current. When it is null, give the reason.
- **Each result's `unavailable` list.** Mention what matters to the question. For a depth band the returned levels don't reach, say "not covered by the returned levels". Without a node feed, the activity buckets, waiting stops, wall tracking and tape flags "need node mode". PRESSURE is available on public data.
- **Flow Deck windows.** When a card's window is incomplete after a gap or at startup, say so. When a card is `revised`, say it was re-evaluated after late trades. A `regime_change_withdrawn` event means the change was withdrawn, not that it happened.
- **`bbo`** is always "freshness unverified". Never quote its prices; best bid and ask come from the book.
- **Screen and answer.** The tab shows a held snapshot as built for up to 30 s before re-judging it; your answer is judged at serving time. If the user's screen and your answer differ on freshness, say so in one clause and keep the serving-time judgement.
- **`served` and `historical`.** Every result carries `served`. When `historical` is present, the result is a past snapshot: say "as of <historical.as_of>", and never describe its statuses as current.

## 6. Tool notes

### 6.1 Flow Deck (hl_alerts)
- The confirmed label, the live classification of the current window and any pending change are three separate things, each with its time. Never describe the current numbers with the confirmed label when the live classification differs.
- When the user asks what a label means, quote its `meaning.text` word for word. Otherwise use the glossary gloss, and never turn either into advice, a cause or a forecast.
- The window's numbers are move (bps), net taker flow, volume, IMPACT, PRESSURE and the top wallet. Give the top wallet's share with its net taker notional, so an account that both bought and sold isn't described as one-sided.
- Each label (confirmed, live, pending) carries `validation` ("descriptive; no predictive test on record"): that is the reliability line's source; quote it with the label.
- Events:
  - Regime change: "PREV -> NEW after PREV held N s". The held time belongs to PREV. Give each change on its own, never as a chain (A -> B -> C) with held times hung on it.
  - Gap event: the label ended at a gap.
  - Burst: give the side, its 60 s taker notional, the threshold, the time it fired, and the wallets listed with the share they reach (the result's `summary`).
  - Node-mode burst: also give the threshold's `label` and the rank's `label` as given. The rank says how often that week a minute reached this sum, nothing about what follows. When the rank is unavailable, give the reason; never estimate one.
  - Liquidation alert: its count and notional are the 60 s before it fired. It fires once per episode and doesn't fire again while liquidations continue. So its numbers are not the episode's total: never call them small, or compare them with a burst. Say "a liquidation alert fired at <time>: N <long or short> liquidations, $X in the 60 s before it".

### 6.2 Wallets (hl_wallets)
- Use the result's terms: bought, sold, participation (bought + sold; not market volume), net bought (not a position change or PnL), and the taker part of each.
- Shares divide by every buyer's (or seller's) notional in the window, including wallets not listed. Give `share_reached_pct` and the `summary`, for example "Top 50 make up 25% of buying; the 50% target isn't reached."
- Give the window's actual start, end and completeness.
- An opened wallet's account state isn't part of the snapshot. Give positions only when its freshness is `fresh`; never say "no positions" when positions are null. Open orders can't be confirmed as current: don't list them as the wallet's orders.

### 6.3 Node data (node_status, node_book, node_stops)
- A stop cluster or band may carry an **estimate** beside its stated figures (`estimate`, `total_est_usd`, `walk.ratio_pct_est`; the sentence already says "at least $X stated, about $Y with whole-position orders at their owners' positions as of <time>"): quote the stated figure first and say "estimated from the owners' positions as of <time>" in the same sentence as the estimate, never as a fact; never rank by the estimate unless you say you are; when `clusters_by_estimate` has a sentence, quote it as the plugin wrote it. Without `estimate.available`, say the size is unknown, as before.
- The Flow Deck card's node buckets and a wallet's node split carry their own `window` and, when the node hasn't processed through the present, a `window_label` and `as_of_note` naming the earlier window they cover: quote them with that window, never as the card's or the panel's present window (the public numbers beside them cover the present).
- Late node data (BEHIND or STALE) is served with its state and may be quoted, with its delay stated in the same sentence ("as of the node's book 73 s behind the chain", from the block's `as_of_note`), never as current; a disconnected feed is said as such. Only INVALID, UNKNOWN and no data withhold.
- Name node data as node data: its source, mode (LIVE or REPLAY) and the state of each family it uses. Give the node feed's delay from `node_status`. DELAYED appears as `BEHIND` in the data. Only LIVE is current.
- A REPLAY is a recording. Say "in the recording, at <time> on <date>". Never describe it as the market now, and never mix it with live numbers.
- When the node book reads INVALID, say so with the reason, and quote no ladder, wall, thinning or heatmap number from it.
- "Largest order size at this level" and "oldest resting order at this level" are separate statistics, never one order's size and age. Changes are absorbed, pulled or unknown; never say liquidity "moved".
- Waiting stops: give a band's order count with its size, and say when one order makes up most of the band. Mention that trailing stops are left out.
- Activity buckets count node fills only, each as a percentage of its side's taker notional. Never present them as the change in open interest, call closing flow passive, or add them to public-trade totals. If a bucket is unavailable, say why.
- Liquidity thinning: give the sharpest drop, where it is and how far from price, and add: "What's resting right now. Orders can be added or pulled at any moment; this isn't a forecast."
- The order flow map (`node_book.flow_map`, eight 15-minute buckets of node fills split by the taker's own position effect): quote its `readings[].text` verbatim and in order, name the window's covered minutes, and say so when a bucket isn't fully covered. The map is withheld with the tape; then say that.
- A liquidation burst counts each liquidation once, for the liquidated trader. Say long or short only as the result states.

## 7. Glossary

Use the plain words in prose and the label only in brackets after them. "Status" is what is known about each label's reliability. None has a predictive test on record, and the reliability line (§4.1) applies to all of them.

**Flow Deck labels** (a 90 s window of public trades, a tick every 15 s; a label is confirmed after 2 matching ticks)

| Label | Plain words | Status |
| --- | --- | --- |
| QUIET | no trades at all in the 90 s window (a feed that's down is different) | descriptive |
| THIN | under $150K traded and price moved under 2 bps; says nothing about book depth | descriptive. The original tool's $150K gate hid real moves, so check the move (§4.2) |
| VACUUM-UP / VACUUM-DOWN | price moved a long way for the flow: under $150K traded with a 2 bps+ move, or 20+ bps per $1M of net flow | descriptive; doesn't measure depth or predict a reversal. Directional: never the subject |
| PULLED-OFFERS / PULLED-BIDS | price moved 5 bps+ against $40K+ of net taker flow (up while takers net sold, or down while they net bought) | cause unknown; doesn't show cancellations. Often one burst dominating the window (§4.9). Directional |
| ABSORBING | little price change per dollar: 2 bps or less per $1M of net flow (with $100K+ net); a large move on very large flow can still qualify | descriptive; doesn't identify an absorber or its size |
| TRENDING-UP / TRENDING-DOWN | 3 bps+ move with taker buying at least 1.15× selling (up) or at most 0.87× (down) | describes the window, not continuation. Directional |
| BALANCED | none of the above matched | not a measure of commitment |
| SPARSE (flag) | fewer than 5 trades behind the window's ratios | the ratios rest on very few trades |

**Numbers and alerts**

| Label | Plain words | Status |
| --- | --- | --- |
| IMPACT | bps of price change per $1M of net taker flow over the 90 s window | descriptive ratio, not a price-impact estimate; unavailable under $100K net |
| PRESSURE | taker buying divided by taker selling over the window | unavailable when selling is under $25K; available on public data |
| top wallet share | one account's share of the window's taker notional, both sides | give it with the account's net; identity is stale within about a minute |
| burst (public) | one side's taker notional in 60 s reached the market's threshold (BTC $10M by default) | a size alert, untested as a predictor |
| burst (node fills) | the same on node fills, with a threshold set for about 10 a day per market (measured Nov 3 to 7) | calibrated for how often it fires, not for what follows. The rank says how rare the minute was |
| liquidation burst | forced closes, each counted once for the liquidated trader | descriptive |
| new long / cover / new short / exit | taker buying that opened longs or closed shorts; taker selling that opened shorts or closed longs (node fills) | measured from fills; closing is not new risk |

**Book, stops and status words**

| Label | Plain words |
| --- | --- |
| API count | Hyperliquid's count for an aggregated level (its docs call it both "number of levels" and "number of orders"); not a number of traders |
| absorbed / pulled / unknown | liquidity at a level was filled / canceled with nothing filled / can't tell which |
| waiting stops (conditional interest) | orders that do nothing unless price reaches them |
| whole-position TP/SL | trigger orders that close a whole position and carry no stated size |
| thinning | where resting liquidity drops off sharply: what's resting now, not a forecast |
| fresh / delayed / stale / unverified / warming_up / disconnected / stopped | public feed status; anything but fresh means the value is unavailable, except context values and the forming candle, which are served unverified with their received age |
| LIVE / BEHIND (shown as DELAYED) / STALE / UNKNOWN / INVALID / REPLAY | node feed state: current / over 10 s behind / over 30 s behind / clock can't be bounded / not recovered since a gap / a recording |
| LIVE / REPLAY / SYNTHETIC | where a snapshot's data came from; REPLAY and SYNTHETIC are never the market now |

**Not in this plugin.** The terminal deck's alerts CONVICTION, SELLING DONE, LIQUIDATION DONE, BOOK REFILLING and the WHALE alerts (GONE, BACK, FLIPPED) weren't ported. If a user names one, say so. Their record: SELLING DONE (n=169) and BOOK REFILLING (n=16) were indistinguishable from zero at 300 s, and BOOK REFILLING shipped explicitly unvalidated.

## 8. Describe, don't recommend

- Don't tell the user to buy, sell, enter, exit, size or hedge, and don't give targets or stops.
- Asked for a trade or a direction ("is it going up?"), say the data describes and doesn't predict, then give the last window's facts.

## Example

> **BTC flat near 75,756 (+2 bps in 90 s). Takers bought $809K more than they sold; one account was most of it.**
>
> - 0x3f5fdb2410cba492b7c3828c811c6662b2894d0b was 60% of the window's taker notional, net +$530K (as of 00:47:00Z).
> - Little price change per dollar: 2.4 bps per $1M of net buying. This tick classifies as BALANCED (no threshold met); the three before were ABSORBING (small move for the flow).
> - Book: 1-dollar spread. The ±0.25% band isn't covered by the 20 returned levels. Within them: bids $7.1M down to 75,737, asks $4.9M up to 75,776.
> - The feed reconnected at 00:44:41Z, so earlier windows aren't complete. Mark and funding: unverified (no source timestamp). New longs vs closes need node mode.
> - Awareness only: none of these labels has been shown to predict price.
>
> Recording run4 · 2026-11-08 00:45:30–00:47:00Z · public data · book and trades recorded; mark and funding unverified

This example was built from a recording, so its last line names the recording. A live answer's last line names the snapshot: `Snapshot <snapshot_id> · <snapshot_time> · LIVE public data · <each feed's status>`.
