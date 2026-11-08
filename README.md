# Hyperliquid Desk for Hermes

A live Hyperliquid market dashboard inside Hermes, on Hyperliquid's **public** data:

- **A dashboard tab** with header stats, the order-book ladder, the tape, aggressive flow, observed depth, the **Flow Deck** (per-market regime cards plus an events list with burst alerts), the **wallets** behind the volume with drill-down, and a price chart drawn with TradingView Lightweight Charts.
- **Six agent tools**: `hl_market`, `hl_flow`, `hl_context`, `hl_status`, `hl_alerts`, `hl_wallets`.
- **A commentary skill**, `hyperliquid-desk:hyperliquid-commentary`, that explains only what the tools returned, cites the snapshot and its freshness, and describes rather than recommends.
- **A `/hl` command** for status and for starting, stopping and restarting the collector.

It's read-only. There is no order code, no exchange endpoint, no signing and no key. Markets: `BTC`, `HYPE`, `xyz:SP500`, `xyz:XYZ100`; each must pass a metadata check against Hyperliquid's info endpoint, and unknown markets are unsupported.

Requires **Hermes >= 0.23.0** (tested on v0.23.0), Linux or macOS.

## Install

Copy or clone the plugin directory into your Hermes plugins folder, enable it, then restart Hermes:

```bash
git clone https://github.com/KevinM0001/hermes-hyperliquid-plugin ~/.hermes/plugins/hyperliquid-desk
hermes plugins enable hyperliquid-desk     # asks for consent to install its one dependency: websockets>=13.0,<16
```

Then, in a Hermes chat:

```
/hl start        # starts the live collector (nothing runs until you do this)
/hl              # collector state, provenance and freshness per market
```

Open the dashboard (`hermes dashboard`) and choose the **Hyperliquid Desk** tab. The tab has its own Start, Stop and Restart buttons. Ask the agent things like "What is BTC doing? Use the hyperliquid commentary skill", or paste the tab's snapshot ID so the agent cites the same numbers you see.

## How the numbers are kept honest

- **One state owner per Hermes profile.** One collector builds an immutable snapshot every second. The tools and the tab read the same snapshot by its ID (`hd-<run>-<seq>`), from whichever Hermes process they run in (other processes ask the owner over a local socket), so chat and screen can't disagree. A "latest" snapshot older than 5 s, or one from a stopped collector, is never served as current. A snapshot asked for by ID is labeled with its age, and every tool result built from it is labeled historical, "as of" its build time. The tab also ages the snapshot it shows, counting from when it asked for it: it withholds a snapshot, with the reason, as soon as it is more than 5 s old or any status it shows as fresh would have lapsed, including one that arrives late.
- **Provenance on everything.** Every result carries its mode (`LIVE`, `REPLAY` or `SYNTHETIC`), its source, the market, the snapshot ID and its coverage (since when trades were observed, and the gaps).
- **Freshness is separate from provenance.** A value is **fresh** only when its feed is still advancing (its progress signal advanced within a receipt-age limit, on the monotonic clock) **and** its source timestamp is recent, judged against the local clock with a known error bound. That bound is the kernel's maximum clock error when the system clock is NTP-synchronized (Linux), or the `clock_error_bound_ms` setting. A feed that advances but runs behind is **delayed**. With no known clock bound, or no source timestamp, a value is **unverified**, never fresh. Statuses are re-checked every second even when nothing arrives, and a connected socket or a ping never makes data fresh. The thresholds are in `schema/metrics.v1.json` and are tested.
- **Null with a reason, never zero.** Missing, partial, stale, unverified or warming-up values are `null` with the reason shown next to them.
- **Definitions live in one versioned schema**, `schema/metrics.v1.json` (v1.0.0). For every metric and alert rule it gives the formula, units, inputs, side convention, window, threshold, reset rule and completeness rule.

## What is sampled, unverified or unavailable

- **Book levels are aggregated.** Hyperliquid returns at most 20 price levels per side, never individual orders. The level count is shown as **API count**: Hyperliquid's docs describe it both as "number of levels" (info l2Book) and as "number of orders" (websocket).
- **Depth.** Exact depth within ±0.25%, ±0.5% and ±1% comes only from the full-precision returned levels, and only where they reach that band on that side; otherwise it is unavailable and the panel shows the observed depth within the returned levels with its span. The aggregated `nSigFigs=4` and `nSigFigs=3` books (polled from the info endpoint) give a separate figure, labeled approximate. Each bucket counts at its representative price; the bucket alignment isn't documented, so the figure comes with a range covering the boundary buckets. An approximate imbalance appears only when both sides come from one aggregated book.
- **Flow, the Flow Deck and wallets** cover only what was observed since the collector started or the last gap; nothing is backfilled. Each trade is counted once, by (coin, time, tid). A trade missing any essential field (price, size, time, tid, a B/A side, two 0x addresses, or its coin) is never filled in: it ends the coverage segment as a gap, trades freshness restarts, and counting resumes after it. A trades message that can't be attributed to a market ends every market's trade coverage the same way. The first trades message after each (re)subscription is Hyperliquid's snapshot of recent trades: it is shown on the tape but not counted.
- **bbo** is always "freshness unverified": it's sent only when it changes, so silence proves nothing. Its prices are never shown; best bid and ask come from the fast book.
- **activeAssetCtx** (mark, oracle, funding, open interest, day volume) carries no source timestamp, so its source age is unknown. It is therefore always **unverified**, and those values are null with their receipt age shown. This is a deliberate consequence of the freshness rule; no other public channel supplies these fields with a timestamp.
- **Trades** in a quiet market: after 30 s (BTC), 60 s (HYPE) or 120 s (xyz) with no trade, flow values are unverified, because silence can't be told apart from a stalled feed. A late trade (one at or before the watermark) is counted but never makes the feed fresh again.
- **Funding** is shown per 1 hour (Hyperliquid's funding page: "paid every hour"; the API field's own period isn't stated). Open interest is in coin, and day volume is in USD; the API pages don't state those units, so the plugin follows the docs' notation conventions.
- **Flow Deck.** Regimes, thresholds and card numbers are ported from the author's terminal Flow Deck (`hl_deck.py`, a private tool, not distributed).
  - **PRESSURE** is aggressive buy notional over aggressive sell notional, every execution included (flips too), and is available when selling is at least $25K. It is the same in public and node mode, so **TRENDING-UP/DOWN** can fire on public data.
  - hl_deck's flip-free ratio survives only as a named legacy comparison in the schema, and isn't computed.
  - The regime explanations are plain descriptions of the window's measurements; they never claim a cause, an intent or a forecast.
  - Each card shows the **confirmed** (latched) regime, the **live** classification of the current window and any **pending** change separately.
  - IMPACT is the absolute observed trade-price change per $1M of net aggressive flow: a descriptive ratio.
  - A late trade, one whose time falls in a tick already evaluated, re-evaluates the affected ticks, so the card's numbers, the latch and the regime-change events always match the trades counted. A change the late trade no longer supports is reported as withdrawn.
  - The top wallet's share keeps hl_deck's population, and the card adds its net aggressive notional (aggressive bought minus aggressive sold).
  - The opening and closing activity buckets (new long, cover, new short, exit) need node mode. The schema defines them per position effect, flips split.
  - hl_deck's whale alerts (GONE, BACK, FLIPPED, CONVICTION, SELLING DONE) aren't ported in v1.
- **Burst alerts** (handoff definition): aggressive buying or selling of at least $10M within 60 s for BTC; HYPE $2M, xyz markets $1M by default, all configurable. They name the aggressive wallets behind the burst. No regime, alert or window ever spans a gap.
- **Wallets.** Addresses are public on-chain identifiers; the plugin never guesses who owns one. Bought plus sold is participation, not volume, and net bought is not a position change or PnL. An opened wallet's account state is fetched from the info endpoint for the market's dex only and is shown only when the answer's own time is recent when served (within 10 s under the clock bound, counting the time both requests took); otherwise every value is unavailable with the reason. Open orders have no response timestamp, so they're never shown as current. The tab keeps aging the account state from the moment it asked for it, so delivery time and every second the window stays open count. Once it passes 10 s, including on arrival, it is marked expired and its values are withheld until **Fetch account again** gets a new answer.
- **Node mode** (order-level ladder columns, waiting stops, wall watch, tape flags, activity buckets) isn't in this version; those panels say "needs node mode". The resting-liquidity heatmap isn't built.

## Disclosures (catalog rule 13)

| What | Details |
| --- | --- |
| Network destinations | `https://api.hyperliquid.xyz/info` (POST: `perpDexs`, `meta`, `l2Book`, `clearinghouseState`, `frontendOpenOrders`) and `wss://api.hyperliquid.xyz/ws` (subscriptions `l2Book`, `trades`, `bbo`, `activeAssetCtx`, plus `ping`). Nothing else. Each endpoint and its doc link are in [docs/ENDPOINTS.md](docs/ENDPOINTS.md). No telemetry. |
| Background worker | One worker thread, `hyperliquid-desk-collector`, created with Hermes's documented `spawn_context_thread` so it keeps its profile's context, running its own asyncio loop: one websocket connection, an info poll every 5 s per market for depth books, and a snapshot every second. Blocking HTTP requests run in that loop's short-lived executor threads, never on the gateway's event loop. Snapshots stay in memory; nothing is written per snapshot. It starts only on `/hl start` or the tab's Start button, never on install, import, registration, `plugins doctor` or `plugins validate`. Stop it with `/hl stop`; `/hl restart` restarts it. Reconnects back off from 1 s to 30 s, and the disconnected state is shown. Bounded memory: 16 minutes of trades per market and the last 180 snapshots. |
| Storage | `<HERMES_HOME>/plugin-data/hyperliquid-desk/`: `collector.lock` (marks the profile's single collector owner) and, while the collector runs, `owner.sock`, a Unix socket with mode 0600 (removed on stop). If that path is too long for a Unix socket, the socket goes in a private per-user directory under the system temp folder instead (`hl-desk-<uid>/`, mode 0700). Snapshots stay in the owner's memory: the last 180, each with the exact trades its panels used. Nothing else is written, and nothing is written outside it. Inside Hermes this profile directory is always used; the `HL_DESK_DATA_DIR` environment variable, used by the tests, is honored only where Hermes's plugin storage isn't available (outside Hermes). |
| Local IPC | The process that owns the collector answers the profile's other Hermes processes (for example the dashboard process and a plugin host) on `owner.sock`. It serves snapshots by ID, a wallet's executions from a retained snapshot, status, stop and restart. Only the same OS user can connect, and there is no network listener. |
| Commands | `/hl [status|start|stop|restart|help]` |
| Tools | `hl_market`, `hl_flow`, `hl_context`, `hl_status`, `hl_alerts`, `hl_wallets`: all read-only; `hl_wallets` with an `address` makes two info requests. |
| Dashboard routes | `/api/plugins/hyperliquid-desk/`: `snapshot`, `status`, `wallets`, `tool/<name>` (GET); `collector/start|stop|restart` (POST). |
| Credentials | None. No secrets are read or stored. |
| Shell commands, reads outside its data | None. The clock error bound is read with a read-only `adjtimex` system call through the C library already loaded in the process; no external command is run to find it. |
| Bundled JavaScript | TradingView Lightweight Charts 5.2.1, readable development build, Apache-2.0. License, notice and on-chart attribution are kept; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No remote code or fonts are loaded. |

## Settings

Set these in `config.yaml` under `plugins.entries.hyperliquid-desk.settings`, or in the Desktop plugin settings form: `markets`, `burst_min_usd` (per market), `large_print_usd` (per market), `wallet_target_share_pct`, and `clock_error_bound_ms`. That last one is needed only where the OS can't report its clock error bound (for example macOS, or an unsynchronized clock); without a bound, nothing source-timestamped can be fresh. Formulas never change at runtime.

## Development

```bash
python -m pytest -c tests/pytest.ini tests    # network off; recorded public data in tests/fixtures/recorded
python scripts/build_dashboard.py .           # rebuilds dashboard/dist/index.js from src + the vendored chart library
python scripts/record_fixtures.py --out <dir> # re-records public fixtures (needs network)
```

The tests cover every tool's JSON shape; each data rule (a band the levels don't reach, an empty side, a duplicate trade, stale data, a frozen source behind a live connection, a quiet market, a reconnect gap, clock skew); every Flow Deck regime at its thresholds; bursts below, at and above threshold and across a gap; wallet ranks and shares against independent calculations; the metadata check per market; and the real collector against a local stand-in server, including its reconnect.

See [docs/DEMO.md](docs/DEMO.md) for the 90-second public-data demo, and [docs/SNAPSHOT.md](docs/SNAPSHOT.md) for the snapshot contract.

## License

MIT for this plugin (see `LICENSE`). Lightweight Charts keeps its Apache-2.0 license and NOTICE.
