# Demo script: public data only, under 90 seconds

Mode: **LIVE, public data** (no node, no relay key, no replay file). Before recording, run `/hl start` and wait about 2 minutes, so the Flow Deck has complete 90-second windows. Keep the dashboard tab open on BTC.

Prerequisite for the chat step (0:58): the Hermes profile needs a configured inference provider. Without one, Hermes refuses to start a chat turn ("no API keys or providers found"), and every other step still works. The chat answer depends on the model, so rehearse it once before recording.

| Time | Screen | Say / type |
| --- | --- | --- |
| 0:00–0:08 | Hermes dashboard, **Hyperliquid Desk** tab, BTC | "Hyperliquid Desk: a live Hyperliquid dashboard inside Hermes, on public data only. Read-only: no trading keys, no orders." |
| 0:08–0:20 | Header: LIVE badge, snapshot ID, freshness statuses | "Every number carries its source, snapshot ID and freshness. Fresh means the feed is advancing and its timestamps are recent; anything stale or delayed shows as unavailable with a reason, never as zero. Mark, funding and open interest come from a feed with no source timestamp, so they're shown marked unverified, with how long ago the last changed value arrived." |
| 0:20–0:32 | Ladder and observed depth | "The ladder shows Hyperliquid's aggregated levels with their API count. Exact depth bands appear only where full-precision levels reach them; the coarse figures are labeled approximate." |
| 0:32–0:47 | Flow Deck cards and events | "The Flow Deck, ported from my terminal deck: each market's confirmed regime, what the current window classifies as, and what that means in plain words, plus burst alerts naming the wallets behind them." |
| 0:47–0:58 | Wallets panel; click the top buyer | "Who's driving the volume: buyers and sellers until they make up half their side, with that wallet's own trades and its current positions." |
| 0:58–1:20 | Chat: copy the snapshot ID from the header, then type: *"Using snapshot <ID>, what is BTC doing? Use the hyperliquid commentary skill."* | Let the agent answer. It cites the same snapshot, its time and freshness, gives each number with its own window and resolution (the flow window, the wallets' window, each depth band's book), quotes the regime meaning and says what's unavailable (for example, mark and funding have no source timestamp, so it quotes them as unverified, received N s ago). |
| 1:20–1:28 | Back to the tab; point at the same numbers | "Same snapshot, same numbers, each with its own window, in chat and on screen. Hyperliquid Desk for Hermes." (If a value has just lapsed, chat says stale first: the tab re-judges a held snapshot only after 30 s on screen.) |

Notes:
- If a market is quiet, its trades status reads "unverified" and its flow values are unavailable. That is expected behavior, so show it rather than cutting it.
- Panels marked "needs node mode" are out of the public-only build.
- The Flow Deck also carries each market's watch block (Kevin's swing-context page: the 24h and 7d range position with the hatched middle, 15m/1h/4h flow, the 4h wave, taker per hour, the hour's top wallet and a verdict by his own rule); `hl_watch` answers it in chat. It is a description, not a forecast, and says when a window is incomplete or a range's candles are still loading.
- In node mode (outside this script), wait at least 10 s after pressing Start before opening the node ladder: until then its "last 10 s" columns show 0 instead of "not measured yet" (a known issue, deferred).
- The chart's interval buttons (1m to 1d) show the venue's own candle history; its last candle is in progress and reads "unverified, received N s ago" (its updates carry no source time), so say that rather than calling it live.
