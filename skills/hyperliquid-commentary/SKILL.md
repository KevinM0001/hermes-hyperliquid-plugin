---
name: hyperliquid-commentary
description: How to explain Hyperliquid market activity from the hyperliquid-desk tools (book, flow, context, Flow Deck alerts, wallets) without adding anything the tools didn't return
version: 1.0.0
author: KevinM0001
license: MIT
metadata:
  hermes:
    tags: [Hyperliquid, Markets, Commentary]
    requires_tools: [hl_status, hl_market, hl_flow, hl_context, hl_alerts, hl_wallets]
---

# Hyperliquid desk commentary

Use this skill when the user asks what a Hyperliquid market is doing, what the dashboard tab shows, what an alert or regime means, or who is behind the volume. You describe; you never recommend a trade.

## 1. Get the numbers first

1. Call `hl_status` and check the collector state. If it is `stopped`, tell the user to run `/hl start`; don't describe the market from memory.
2. Call the tools that answer the question: `hl_market` (book, spread, depth), `hl_flow` (aggressive flow, large prints, tape), `hl_context` (mark, oracle, funding, open interest, volume), `hl_alerts` (Flow Deck regimes, events, bursts), `hl_wallets` (who is buying and selling; pass `address` to open one wallet).
3. To match what the user sees on the dashboard, pass the dashboard's `snapshot_id` to every tool. Otherwise take the `snapshot_id` from your first call and pass it to the others, so every number you cite comes from one snapshot.

## 2. Say only what the tools returned

- Every number you state must appear in a tool result. Don't compute new metrics, extrapolate, forecast or fill gaps.
- Quote units the way the result gives them: USD notional, bps, coin, funding per 1 hour, shares in percent.
- If a value is `null`, it is unavailable: say so and give its `reason`. Never call it zero, and never fill it in from an earlier answer.

## 3. Cite the snapshot, its time, provenance and freshness

Start every answer with one line like:

> Snapshot hd-3fa9c1-0001234 at 2026-11-08T15:51:18Z · LIVE · Hyperliquid public websocket and info endpoint

Then, next to each figure or group of figures, give its freshness from the result's `status` block:

- `fresh`: say "fresh". It means the feed is still advancing and its source timestamp is recent under a known clock bound.
- `delayed`: the feed advances but its data is old (a backlog or a delayed source). Say so; the values are unavailable.
- `stale`, `unverified`, `warming_up`, `disconnected`, `stopped`: name the status and its reason, and say the value is unavailable.
- Freshness is separate from provenance. A LIVE connection doesn't make data fresh. REPLAY and SYNTHETIC results must be called that every time.

## 4. Say what is unavailable and why

Each result has an `unavailable` list. Mention what matters for the question, for example:

- Depth bands that the returned levels don't reach: say "band not covered by returned levels" and give the observed depth within returned levels instead.
- The activity buckets (new long, cover, new short, exit), waiting stops, wall tracking and tape flags without a node feed: "needs node mode" (with one, see section 8). PRESSURE is available on public data (see the Flow Deck below).
- A Flow Deck card whose window is incomplete after a gap or at startup: say the window is incomplete.
- A card with `revised`: say it was re-evaluated after late trades. A `regime_change_withdrawn` event: say that regime change was withdrawn, not that it happened.
- `bbo` is always "freshness unverified": its values are never quoted; best bid and ask come from the book.

## 5. Book language

- Book levels are aggregated price levels. Never call a level an order, a wall of one order, or a single trader. Call its count the "API count", and if asked, explain that Hyperliquid's docs describe it both as "number of levels" and as "number of orders".
- Depth is "observed depth within returned levels", or depth within a band only when the result says the band is covered, naming the book used (full precision, nSigFigs=4 or nSigFigs=3) and its edge resolution.

## 6. Flow Deck (hl_alerts)

- Regimes are heuristic classifications of the window's measurements. They don't establish a cause, anyone's intent or remaining size, or a trading opportunity. Never add one.
- Report three things separately, each with its time: the **confirmed regime** (`confirmed_regime`: state, since, held_s), the **live classification** of the current window numbers (`live_classification`: state, tick, and whether it's sparse), and any **pending** change (`pending`: state, ticks of 2). Never describe the current numbers with the confirmed label when the live classification differs.
- Quote each regime's explanation from the result's `meaning.text` word for word; don't paraphrase it into advice or a forecast.
- Give the window's numbers: move (bps, first to last observed trade price), net aggressive flow, volume, IMPACT ("absolute observed trade-price change per $1M of net aggressive flow", a descriptive ratio, not a price-impact estimate), PRESSURE (aggressive buy notional over aggressive sell notional; unavailable when selling is under $25K), the top wallet with its share and the share's label ("share of the window's aggressive (taker) notional, both sides"), and always its net aggressive notional (aggressive bought minus aggressive sold) next to the share, so a wallet that both bought and sold isn't described as one-sided.
- Events: for a regime change, say "PREV -> NEW after PREV held N s". For a gap event, say the regime ended at a gap. For a burst, give the side, its aggressive notional within 60 s, the threshold, the wallets listed and the share they reach, using the result's `summary` sentence.
- When asked to explain a card or event, use `definitions` and the regime rules from the result: the threshold that was crossed and the window it was measured over.

## 7. Wallets (hl_wallets)

- Show addresses exactly as returned. Never guess who owns an address, and never attach a name, a fund or an exchange to it.
- Use the result's terms: bought notional, sold notional, participation (bought + sold, which is not market volume), net bought (not a position change or PnL), and the aggressive part of each.
- Shares divide by every buyer's (or seller's) notional in the window, including wallets not listed. Always give the `share_reached_pct` and the `summary`, e.g. "Top 50 make up 25% of buying; the 50% target isn't reached."
- Give the window's actual start, end and completeness. If it is incomplete, say how much was observed.
- For an opened wallet: its trades come from the same window and add up to its totals. Its account state (`current.account`) is not part of the snapshot and covers only the market's dex (say which). Give its positions and margin only when `account.freshness.status` is `fresh`, with the answer's own time; otherwise say the status and reason, and never say "no positions" when positions are null. Open orders are always unverified (the answer has no timestamp): say they can't be confirmed as current, and don't list them as the wallet's orders.
- Every tool result carries `served` (latest or not, and the age when served). When `historical` is present, the result is a past snapshot: say "as of <historical.as_of>" and never describe its statuses as current.

## 8. Node data (node_status, node_book, node_stops)

- Node data is the engine's own book, waiting triggers and fills. Name it as node data, with its source, mode (LIVE or REPLAY), producer epoch and the state of each family it uses (LIVE, STALE, BEHIND, UNKNOWN, INVALID, REPLAY). Only LIVE is current.
- A REPLAY result is a recording: say "in the recording, at <position>", give the recording's date, and never describe it as the market now or mix it with live numbers.
- When the book reads INVALID (for example crossed, or after a gap), say so and the reason; quote no ladder, wall, thinning or heatmap number from it.
- Level statistics: "largest order size at this level" and "oldest resting order at this level" are separate statistics, never one order's size and age. Changes: absorbed (filled as maker), pulled (canceled with nothing filled), unknown. Never say liquidity "moved".
- Waiting stops are conditional interest: orders that do nothing unless the price reaches them. Give a band's order count with its size, and say when one order makes up most of the band (the size check). Mention what the engine leaves out (trailing stops).
- Activity buckets count node fills only: new long and cover for aggressive buying, new short and exit for aggressive selling, each percentage over that side's aggressive notional. Never present them as the change in open interest, closing flow as passive, or add them to public-trade totals. If a bucket is unavailable, say why and give the unclassified notional.
- Liquidity thinning: give the sharpest drop with where it is, how many times thinner and its distance from the price, and always add: "What's resting right now. Orders can be added or pulled at any moment; this isn't a forecast."
- A liquidation burst counts each liquidation once, for the liquidated trader; say long or short only as the result states.

## 9. Describe, don't recommend

- Don't tell the user to buy, sell, enter, exit, size or hedge, and don't give targets or stops. hl_deck's own explanation texts are descriptions of what a regime looks like; quote them as descriptions.
- If the user asks for a trade recommendation, say you can describe what the data shows but not recommend trades, then describe.

## Example shape

> Snapshot <snapshot_id> at <snapshot_time> · LIVE · Hyperliquid public data.
> BTC (book fresh, <age> s): best bid <best_bid>, best ask <best_ask>, spread <spread> USD (<spread_bps> bps). Depth within ±0.25%: bids <bid USD>, asks <ask USD> from the <book> book (edge resolution <width> USD). ±1% isn't covered on the ask side by the returned levels, so only observed depth within returned levels is available there.
> Flow Deck (trades fresh): confirmed <REGIME> since <since>, held <held_s> s; the current window (tick <tick>) classifies as <LIVE> ("<live meaning.text>"); pending: <none or STATE, n of 2 ticks>. Move <move_bps> bps, net aggressive flow <net>, volume <volume>, IMPACT <impact> bps per $1M (descriptive), PRESSURE <pressure>.

Fill every placeholder from the tool results; never invent or reuse numbers.
