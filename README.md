# Hyperliquid Desk for Hermes

A live Hyperliquid market dashboard inside Hermes, on Hyperliquid's **public** data:

- **A dashboard tab** with header stats, the order-book ladder, the tape, aggressive flow, observed depth, the **Flow Deck** (per-market regime cards plus an events list with burst alerts), the **wallets** behind the volume with drill-down, and a price chart drawn with TradingView Lightweight Charts.
- **Six agent tools**: `hl_market`, `hl_flow`, `hl_context`, `hl_status`, `hl_alerts`, `hl_wallets`.
- **A commentary skill**, `hyperliquid-desk:hyperliquid-commentary`, that explains only what the tools returned, cites the snapshot and its freshness, and describes rather than recommends.
- **A `/hl` command** for status and for starting, stopping and restarting the collector.

It's read-only. There is no order code, no exchange endpoint, no signing and no trading or wallet key; the only credential is the optional node feed key described under Node mode. Markets: `BTC`, `HYPE`, `xyz:SP500`, `xyz:XYZ100`; each must pass a metadata check against Hyperliquid's info endpoint, and unknown markets are unsupported.

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

- **One state owner per Hermes profile.** One collector builds an immutable snapshot every second. The tools and the tab read the same snapshot by its ID (`hd-<run>-<seq>`), from whichever Hermes process they run in (other processes ask the owner over a local socket), so chat and screen can't disagree. A "latest" snapshot older than 5 s, or one from a stopped collector, is never served as current. A snapshot's age is measured on the owner's monotonic clock, so a wall-clock step can't make it look younger; a latest snapshot whose age can't be established that way isn't served. A snapshot asked for by ID is labeled with its age, and every tool result built from it is labeled historical, "as of" its build time. The tab also ages the snapshot it shows, counting from when it asked for it: it withholds a snapshot, with the reason, as soon as it is more than 5 s old or any status it shows as fresh would have lapsed, including one that arrives late.
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
- **Burst alerts.** Aggressive buying or selling over a threshold within 60 s. They name the aggressive wallets behind the burst. No regime, alert or window ever spans a gap.
  - **Public mode** (handoff definition): from public trades; at least $10M for BTC, HYPE $2M, xyz markets $1M by default, set per market in `burst_min_usd`.
  - **Node mode** (a `node_feed` is set): from node fills, evaluated once per node block, with the rule the thresholds were measured with. A window containing a rejected or unreadable record is incomplete: it never fires, re-arms or disarms. Public-trades bursts aren't shown in node mode. Each market's threshold is automatic by default: a dated, versioned calibration measured on node fills from Nov 3 to 7, targeting about 10 bursts a day. BTC is $30M and xyz:XYZ100 $5M ("automatic: about 10 a day, measured Nov 3 to 7"). HYPE and xyz:SP500 are $5M, and their labels state the achieved rate, 14.6 and 7.2 a day, because no threshold came within 25% of 10. Each burst is ranked against that week's minutes: "a top x% minute, Nov 3 to 7", or unavailable when the reference file isn't loaded. See "Burst thresholds in node mode" below to set a market's threshold by hand.
- **Wallets.** Addresses are public on-chain identifiers; the plugin never guesses who owns one. Bought plus sold is participation, not volume, and net bought is not a position change or PnL. An opened wallet's account state is fetched from the info endpoint for the market's dex only and is shown only when the answer's own time is recent when served (within 10 s under the clock bound, counting the time both requests took); otherwise every value is unavailable with the reason. Open orders have no response timestamp, so they're never shown as current. The tab keeps aging the account state from the moment it asked for it, so delivery time and every second the window stays open count. Once it passes 10 s, including on arrival, it is marked expired and its values are withheld until **Fetch account again** gets a new answer.
- **Node mode** (optional, setting `node_feed`): the node ladder, waiting stops, wall watch, heatmap history, node tape with flags, burst alerts with their rank, Flow Deck activity buckets, wallet activity splits, liquidation bursts and the liquidity-thinning panel come from the engine's own feed; see "Node mode (optional)" below. Without a node feed those panels say "needs node mode".

## Node mode (optional)

With a node feed, the tab's node panels and the `node_status`, `node_book` and `node_stops` tools show the engine's own book, waiting triggers and fills (the l4-runner live feed, protocol v2). Set `node_feed` to the engine's local Unix socket, or to a relay's `wss://` URL, and start the collector. Without it, everything above works as before and the node panels say "needs node mode".

- **The protocol is followed as written.**
  - State is kept per source, mode, producer session, replay session and market, and each family (book, triggers, depth, bands, node fills) has its own validity.
  - A new producer session, a missing sequence number, a dropped connection or an incomplete snapshot invalidates the affected families at once. They recover only from their own snapshot, assembled off to the side and swapped in whole, never rolling state back.
  - A retired session's late lines are ignored.
- **Freshness comes only from the engine's watermarks**, once the client has applied everything before them. Heartbeats and connection activity never count.
  - **The node feed's delay** is the age of the market's latest watermark (now minus its block time), averaged over the last 5 s. It is shown as a counter at the top of the tab, about once a second, and carried in the snapshot (`node_status` and every family's `delay_ms`), so answers report it.
  - Each family reads LIVE, DELAYED (`BEHIND` in the data: the averaged delay is above 10 s), STALE (above 30 s; heartbeats don't count, so a silent feed's delay keeps growing), UNKNOWN (this machine's clock is behind the producer's, so ages can't be bounded) or INVALID, and every node result shows it. The limits sit well above the normal delay, so the labels don't toggle.
  - A delay never blanks the tab: values and labels update in place. The empty "No snapshot" view is only for having no data at all (the collector stopped, the feed down, the first start). A snapshot that has aged on screen past a limit stays on screen, and the delay bar says why until the next one replaces it.
- **One stream per metric.** Public panels count public trades; node metrics (activity buckets, wallet splits, liquidations, the node tape) count node fills. The two are never added together.
  - Whether a public trade and a node fill with the same `tid` are the same execution hasn't been proven on recorded data, so neither stream adds flags or quantity to the other.
  - A change of input stream starts a new, labelled window.
- **Activity buckets** (new long and cover for aggressive buying, new short and exit for aggressive selling) come from each participant's own position effect as the engine states it.
  - A flip fills both buckets, and each converts to notional at the execution's price.
  - Flow Deck cards and burst alerts use the aggressive side's effects over the same window, each percentage over that side's aggressive notional.
  - Wallet splits show all fills and the aggressive (taker) fills separately, each over the wallet's own bought or sold notional.
  - A missing effect shows the bucket as unavailable, with the unclassified notional; nothing is scaled to 100%.
  - None of this is a change in open interest.
- **Book panels:**
  - **Level statistics.** "largest order size at this level" and "oldest resting order at this level" are separate statistics.
  - **Changes.** Changes in the last 10 s are labelled only from lifecycle records: absorbed (filled as maker), pulled (canceled with nothing filled) or unknown. Nothing is called "moved".
  - **Crossed book.** If the best bid is at or above the best ask, the book and every panel built on it read INVALID until a fresh snapshot clears it.
  - **Liquidity thinning.** Depth per 0.5% band out to 10% from the price, in dollars and dollars per $1 of price, with the sharpest drop called out. What's resting right now. Orders can be added or pulled at any moment; this isn't a forecast.
  - **Heatmap.** The 15-minute history starts when the book became valid and is marked partial until it covers 15 minutes.
- **Waiting stops** are conditional interest: orders that do nothing unless the price reaches them.
  - Each band shows its order count beside its size, and says so when one order makes up most of it.
  - Whole-position TP/SL orders have no stated size and are counted apart.
  - Every waiting trigger is accounted for, in a band or outside the range. The engine leaves out trailing stops (their current trigger price isn't established) and reports how many.
- **Liquidations** count once each, for the liquidated trader, whose own position effect says long or short. A burst of them (by default 2 within 60 s, or $1M) raises its own alert.
- **Replay** runs only when you start it: `/hl replay start [file]`, then `pause`, `resume`, `seek <time>`, `speed <x>`, `restart` and `stop` (or the tab's replay controls).
  - Each start, seek and restart is a new replay session.
  - Its view shows the recording's date and time, the position, the speed and whether it is playing, paused or ended.
  - It is labelled REPLAY throughout, its end stays REPLAY, and it is never drawn over live prices or mixed into current metrics or commentary.

## Disclosures (catalog rule 13)

| What | Details |
| --- | --- |
| Network destinations | `https://api.hyperliquid.xyz/info` (POST: `perpDexs`, `meta`, `l2Book`, `clearinghouseState`, `frontendOpenOrders`) and `wss://api.hyperliquid.xyz/ws` (subscriptions `l2Book`, `trades`, `bbo`, `activeAssetCtx`, plus `ping`). Nothing else. Each endpoint and its doc link are in [docs/ENDPOINTS.md](docs/ENDPOINTS.md). No telemetry. Optional, only when you set `node_feed`: that local Unix socket or relay WebSocket URL, nothing else; the plugin writes one handshake line there and only reads. |
| Background worker | One worker thread, `hyperliquid-desk-collector`, created with Hermes's documented `spawn_context_thread` so it keeps its profile's context, running its own asyncio loop: one Hyperliquid websocket connection (plus, only when `node_feed` is set, one node feed connection to that socket or URL), an info poll every 5 s per market for depth books, and a snapshot every second. Blocking HTTP requests run in that loop's short-lived executor threads, never on the gateway's event loop. Snapshots stay in memory; nothing is written per snapshot. It starts only on `/hl start` or the tab's Start button, never on install, import, registration, `plugins doctor` or `plugins validate`. Stop it with `/hl stop`; `/hl restart` restarts it. Reconnects back off from 1 s to 30 s, and the disconnected state is shown. Bounded memory: 16 minutes of trades per market and the last 180 snapshots. With a node feed (or a replay), per market and source: the current order book and waiting trigger orders (sized by the market itself), 15 minutes of book changes and heatmap samples, 16 minutes of node fills (and of execution identities for the burst rule), the burst reference (about 230,000 sorted 60 s sums across the four markets, loaded once), and a bounded buffer of events during a snapshot (`buffer_messages`, 50,000); node views are kept in the newest 60 snapshots only. |
| Storage | `<HERMES_HOME>/plugin-data/hyperliquid-desk/`: `collector.lock` (marks the profile's single collector owner) and, while the collector runs, `owner.sock`, a Unix socket with mode 0600 (removed on stop). If that path is too long for a Unix socket, the socket goes in a private per-user directory under the system temp folder instead (`hl-desk-<uid>/`, mode 0700). Snapshots stay in the owner's memory: the last 180, each with the exact trades its panels used. Nothing else is written, and nothing is written outside it. Inside Hermes this profile directory is always used; the `HL_DESK_DATA_DIR` environment variable, used by the tests, is honored only where Hermes's plugin storage isn't available (outside Hermes). |
| Local IPC | The process that owns the collector answers the profile's other Hermes processes (for example the dashboard process and a plugin host) on `owner.sock`. It serves snapshots by ID, a wallet's executions from a retained snapshot, status, stop and restart. Only the same OS user can connect, and there is no network listener. |
| Commands | `/hl [status|start|stop|restart|help]`; `/hl replay start [file]|pause|resume|seek <time>|speed <x>|restart|stop` |
| Tools | `hl_market`, `hl_flow`, `hl_context`, `hl_status`, `hl_alerts`, `hl_wallets`: all read-only; `hl_wallets` with an `address` makes two info requests. `node_status`, `node_book`, `node_stops`: read-only, from the same snapshot as the tab's node panels. |
| Dashboard routes | `/api/plugins/hyperliquid-desk/`: `snapshot`, `status`, `wallets`, `tool/<name>` (GET); `collector/start|stop|restart` (POST); `node/replay/<start|pause|resume|seek|speed|restart|stop>` (POST). |
| Credentials | One optional setting: `node_key`, a Hermes secret (`HL_DESK_NODE_KEY`), read only through Hermes's secret scope and sent only to a relay WebSocket URL as an `Authorization: Bearer` header. Not needed for a local socket or a relay without keys; never in `requires_env`, so the plugin loads and public mode works without it. No other secret is read or stored. |
| Shell commands, reads outside its data | None. The clock error bound is read with a read-only `adjtimex` system call through the C library already loaded in the process; no external command is run to find it. The one file it reads outside its data is a recording you name in `node_replay_file` (or pass to `/hl replay start`), and only when you start a replay. Inside the plugin it also reads its packaged burst calibration (`hl_desk/node/calibration`, about 1.7 MB compressed) once, when a node source first starts; public mode never reads it. |
| Bundled JavaScript | TradingView Lightweight Charts 5.2.1, readable development build, Apache-2.0. License, notice and on-chart attribution are kept; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No remote code or fonts are loaded. |

## Settings

Set these in `config.yaml` under `plugins.entries.hyperliquid-desk.settings`, or in the Desktop plugin settings form: `markets`, `burst_min_usd` (per market, public mode), `node_burst_min_usd` (per market, node mode, manual override), `large_print_usd` (per market), `wallet_target_share_pct`, and `clock_error_bound_ms`. That last one is needed only where the OS can't report its clock error bound (for example macOS, or an unsynchronized clock); without a bound, nothing source-timestamped can be fresh. Formulas never change at runtime.

### Burst thresholds in node mode

Each market starts on automatic: the threshold from the shipped calibration (`hl_desk/node/calibration/burst-calibration-2026-11-08.json`, version 2026-11-08.1, measured Nov 3 to 7). To set one market's threshold by hand, in USD per 60 s:

```bash
hermes config set plugins.entries.hyperliquid-desk.settings.node_burst_min_usd.BTC 25000000
```

Then run `/hl restart`, since settings are read when the collector starts. The Flow Deck's burst rule line and `node_status` show "manual: $25,000,000 (setting node_burst_min_usd)". To switch that market back to automatic, remove it and restart:

```bash
hermes config unset plugins.entries.hyperliquid-desk.settings.node_burst_min_usd.BTC
```

Setting it to `auto` instead (`hermes config set ….node_burst_min_usd.BTC auto`) does the same. Other markets are unaffected; xyz markets use their full name, for example `node_burst_min_usd.xyz:SP500`. In `config.yaml` it reads:

```yaml
plugins:
  entries:
    hyperliquid-desk:
      settings:
        node_burst_min_usd:
          BTC: 25000000
```

A manual threshold has no calibrated rate, so its label says manual. The rank is still against the Nov 3 to 7 reference. In public mode `node_burst_min_usd` isn't used; `burst_min_usd` is.

## Development

```bash
python -m pytest -c tests/pytest.ini tests    # network off; recorded public data in tests/fixtures/recorded
python scripts/build_dashboard.py .           # rebuilds dashboard/dist/index.js from src + the vendored chart library
python scripts/record_fixtures.py --out <dir> # re-records public fixtures (needs network)
```

The tests cover every tool's JSON shape; each data rule (a band the levels don't reach, an empty side, a duplicate trade, stale data, a frozen source behind a live connection, a quiet market, a reconnect gap, clock skew); every Flow Deck regime at its thresholds; bursts below, at and above threshold and across a gap; node-mode bursts on PLAN.md's rule cases (warm-up, same-timestamp blocks, gaps, incomplete windows and every rejection) and on a recorded window of node fills against an independent computation, with ranks counted from the reference; wallet ranks and shares against independent calculations; the metadata check per market; and the real collector against a local stand-in server, including its reconnect.

The node-mode tests (`tests/test_node_*.py`) feed recorded node feed lines (`tests/fixtures/node`, provenance in its `MANIFEST.json`) through the plugin and read every intermediate tool and tab result. Two optional checks need local reference files and are skipped without them: `HL_DECK_SOURCE` (the terminal Flow Deck's `hl_deck.py`, for the bucket definitions) and `HL_DECK_LIVE_LOG` (its frames, a vocabulary check only).

See [docs/DEMO.md](docs/DEMO.md) for the 90-second public-data demo, and [docs/SNAPSHOT.md](docs/SNAPSHOT.md) for the snapshot contract.

## License

MIT for this plugin (see `LICENSE`). Lightweight Charts keeps its Apache-2.0 license and NOTICE.
