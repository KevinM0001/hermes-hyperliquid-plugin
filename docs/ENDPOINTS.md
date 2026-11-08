# Hyperliquid endpoints used

Every network call this plugin makes goes to one of the endpoints below, each confirmed in Hyperliquid's official API docs on 2026-11-07. All are public and read-only. The plugin never calls the exchange endpoint (`/exchange`), signs nothing and holds no exchange or wallet keys (the optional node feed key below is for the engine's feed, not Hyperliquid).

Docs root: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api

## Info endpoint: `POST https://api.hyperliquid.xyz/info`

| Request `type` | Used for | When | Doc link | Rate-limit weight |
| --- | --- | --- | --- | --- |
| `perpDexs` | List of perp dexes (HIP-3 dex names) for the metadata check | Once per collector start | [info-endpoint/perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) ("Retrieve all perpetual dexs") | 20 |
| `meta` with `dex` | Market universe per dex (`""` default dex, `"xyz"` for xyz:SP500 and xyz:XYZ100); unknown or delisted markets are unsupported | Once per collector start, per dex needed | [info-endpoint/perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) ("Retrieve perpetuals metadata"; `dex`: "Perp dex name. Defaults to the empty string which represents the first perp dex.") | 20 |
| `l2Book` with `nSigFigs` 4 and 3 | Aggregated books for depth bands | Every 5 s per market and aggregation, only while connected (4 markets: 96 requests, weight 192 per minute) | [info-endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) ("L2 book snapshot"; "Returns at most 20 levels per side"; `nSigFigs`: 2, 3, 4, 5 or null) | 2 |
| `clearinghouseState` with `user`, `dex` | An opened wallet's current positions (wallets drill-down), on the market's dex | Only when the user or agent opens a wallet | [info-endpoint/perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) ("Retrieve user's perpetuals account summary") | 2 |
| `frontendOpenOrders` with `user`, `dex` | An opened wallet's current open orders, on the market's dex | Only when the user or agent opens a wallet | [info-endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) ("Retrieve a user's open orders with additional frontend info"; `dex`: "Defaults to the empty string which represents the first perp dex.") | 20 |

The REST limit is "an aggregated weight limit of 1200 per minute" ([rate-limits-and-user-limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)). Info answers for positions and orders cover one dex per call; the plugin queries the market's dex and says so, and never claims a default-dex answer covers every dex.

## Websocket: `wss://api.hyperliquid.xyz/ws`

One connection ([websocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket): "Mainnet: `wss://api.hyperliquid.xyz/ws`"), subscribed with `{"method": "subscribe", "subscription": {...}}`. Per market, 5 subscriptions (20 for the 4 default markets; the limit is 1000 per IP).

| Subscription | Used for | Doc link |
| --- | --- | --- |
| `{"type": "l2Book", "coin": c, "fast": true}` | Best bid/ask, mid, spread (5 levels; observed cadence about 0.54 s) | [websocket/subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) ("Optional parameters: `nSigFigs: int`, `mantissa: int`, `fast: boolean` (5 levels if fast, 20 levels if slow)") |
| `{"type": "l2Book", "coin": c}` | Ladder levels (20 per side; observed cadence about 5.4 s) | same page (`WsBook`, "Snapshot feed, pushed on each block that is at least 0.5 since last push") |
| `{"type": "trades", "coin": c}` | Tape, flow, Flow Deck, bursts, wallets (`WsTrade` with `side`, `tid`, `users: [buyer, seller]`) | same page |
| `{"type": "bbo", "coin": c}` | Consistency check only ("sent only if the bbo changes on a block"; never shown as fresh) | same page |
| `{"type": "activeAssetCtx", "coin": c}` | Mark, oracle, funding, open interest, day volume (`WsActiveAssetCtx`) | same page |
| `{"method": "ping"}` | Keeps the connection open; never makes data fresh | [websocket/timeouts-and-heartbeats](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats) ("The server will close any connection if it hasn't sent a message to it in the last 60 seconds.") |

## Supporting doc pages

- Notation (side, size and notional conventions): https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/notation ("B = Bid = Buy, A = Ask = Short. Side is aggressing side for trades."; "Sz … In units of coin"; "Ntl … USD amount, Px * Sz")
- HIP-3 market names: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids ("builder-deployed perps always have name in the format `{dex}:{coin}`")
- Funding period: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding ("The funding rate on Hyperliquid is paid every hour.")
- Rate limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

## What the docs leave open

Each gap is labeled wherever it shows:
- The l2Book cadence reads "at least 0.5 since last push" with no unit. Measured on 2026-11-07, 20-level books arrived about every 5.4 s and fast books about every 0.54 s; the freshness limits are set from those observations.
- `WsBook` messages don't say which subscription they answer, so two 20-level websocket subscriptions for one coin can't be told apart. Aggregated books come from the info endpoint instead.
- The level count `n` is called "number of levels" (info l2Book) and "number of orders" (websocket `WsLevel`). It is shown as "API count".
- `WsTrade.time` isn't stated to be the block time in the identity rule "(block_time, coin, tid)"; the plugin uses (coin, time, tid).
- `WsActiveAssetCtx` has no timestamp, and its push cadence isn't stated.
- The units of `openInterest` and `dayNtlVlm` aren't stated; they are labeled coin and USD by the docs' notation conventions.
- The period of the `funding` field isn't stated on the API pages; it is labeled per 1 hour from the funding page.

## Features dropped for lack of a confirmed endpoint

None of the v1 features was dropped. `openOrders` is documented as well, but `frontendOpenOrders` is used because it also lists trigger orders.

## Not a Hyperliquid endpoint: the optional node feed

Node mode (Handoff 1c) reads the engine's own live feed (l4-livefeed protocol v2), from a local Unix socket or a relay WebSocket URL that the user sets in `node_feed`. It isn't a Hyperliquid API endpoint, so it isn't listed above; the README's disclosures describe it.

