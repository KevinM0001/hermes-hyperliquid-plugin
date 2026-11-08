# Snapshot contract (v1)

The state owner builds one immutable snapshot per second. Agent tools and the dashboard tab read the same snapshot by its `snapshot_id`, so chat and screen can't disagree. A process that doesn't own the collector asks the owner over `owner.sock` (see `hl_desk/owner.py`). The tab computes nothing; it only formats what the snapshot says. Every metric is defined in `schema/metrics.v1.json`.

## Routes (mounted at `/api/plugins/hyperliquid-desk/`)

| Route | Returns |
| --- | --- |
| (every tool) | `served: {latest, age_s, served_at, note}` (`age_s` is monotonic elapsed time since the build, on the owner's clock; a latest snapshot whose age can't be established isn't served) as below, and `historical: {as_of, age_s, note}` when the snapshot was asked for by ID and isn't the latest: its statuses are as of `as_of`, not now. |
| `GET snapshot?market=BTC[&snapshot_id=…]` | `{ok, snapshot_id, built_at, built_at_ms, mode, plugin_version, schema, collector, markets: [names], unsupported_markets, market: <MarketSnapshot for the chosen market>, deck: {<market>: {provenance, trades_freshness, card, events}}}`; `{ok: false, error, runtime}` when there is no current snapshot: the collector is stopped or not running, or the latest snapshot is more than 5 s old. `served: {latest, age_s, served_at, note}` says how old the snapshot was when served; one asked for by ID is labeled historical. |
| `GET status` | `{ok, runtime: {owner, collector, pid}}` |
| `POST collector/start`, `collector/stop`, `collector/restart` | `{ok, result: {owner, collector, action: {message, ok, at}}}` |
| `GET wallets?market=BTC&window_min=5[&address=0x…][&snapshot_id=…]` | exactly the `hl_wallets` tool result; with `address`, adds `wallet: {address, trades: {executions[], totals, window}, current: {label, fetched_at (both answers in hand), times: {requested, state_received, orders_received, returned}, dex, coverage, source, account: {freshness, margin_summary: {account_value_usd, total_position_notional_usd, total_margin_used_usd}, positions: value [ {coin, size_coin, entry_px, position_value_usd, unrealized_pnl_usd, margin_used_usd, liquidation_px, leverage} ] or null+reason}, open_orders: {freshness: unverified, orders: null+reason, count: null+reason}}}`. `account.freshness` is `{status, reason, source_time_ms, source_time, assessed_at, source_age_s, source_max_age_s: 10, clock_bound_s, clock_bound_basis}` from the answer's own `time`, judged when both answers are in hand, so request time counts as age: values are given only when it is `fresh` (schema `freshness.fields.clearinghouseState`). A missing `assetPositions` makes the positions null with a reason, never an empty list. |
| `GET tool/<name>?market=…&snapshot_id=…[&source=live\|replay]` | exactly that agent tool's JSON (`hl_market`, `hl_flow`, `hl_context`, `hl_status`, `hl_alerts`, and the node tools `node_status`, `node_book`, `node_stops`, whose `source` picks the live feed or the replay) |

## Value convention

A metric is `{"value": <number|string>, "unit": "USD"}` or, when unavailable, `{"value": null, "unit": "USD", "reason": "…"}`. Show the reason; never show 0 for null.

## MarketSnapshot

| Path | Meaning |
| --- | --- |
| `market`, `meta` | name; `{dex, dex_label, sz_decimals, max_leverage}` from info meta |
| `provenance` | `{mode: LIVE/REPLAY/SYNTHETIC, source, market, snapshot_id, snapshot_built_at, dex, coverage: {trades: {segment_start, watermark, observed_s, gaps[]}, book_levels_per_side}}` |
| `freshness.<field>` | for `book_fast`, `book_full`, `book_sf4`, `book_sf3`, `trades`, `bbo`, `ctx`: `{field, status, reason, source_time, progress_age_s, max_age_s, source_age_s, source_max_age_s, clock_bound_s, clock_bound_basis, progress_signal, …}`. Status is one of `fresh`, `delayed`, `stale`, `unverified`, `warming_up`, `disconnected`, `stopped`. Fresh needs both an advancing feed and a recent source time under a known clock bound; `ctx` (no source time) is never fresh. |
| `unavailable[]` | `{field, status, reason}` for every non-fresh field: show next to the provenance badge |
| `clock` | `{offset_ms, clock_skew_suspected}` |
| `book.top` | `best_bid, best_ask, mid, spread, spread_bps` (values) + `book_time_ms`; from the fast 5-level book |
| `book.ladder` | `{bids: [{px, sz, api_count, notional_usd}], asks: […], api_count_note, aggregated: true, book_time_ms}` (20-level full-precision book), or `{bids: [], asks: [], reason}` |
| `book.depth.bands[]` | `{band_pct, exact: {bid: {notional_usd, size_coin, levels_counted, edge_px, book: "full precision", status: covered} or {notional_usd: null+reason, status: unavailable}, ask: …, imbalance, label}, approximate: {bid: {approx_usd, range_usd: [low, high], aggregation, bucket_width, status: approximate} or {approx_usd: null+reason, status: unavailable, tried}, ask: …, imbalance, label, convention}}`. Exact comes only from full-precision levels; approximate comes from aggregated buckets and is never shown as exact. |
| `book.depth.observed` | per book kind (`full precision`, `nSigFigs=4`, `nSigFigs=3`): `{bid: {label: "observed depth within returned levels", levels, notional_usd, deepest_px, span_pct}, ask: …, edge_resolution}` or `{reason}` |
| `book.bbo` | `{value: null, status: unverified, reason, last_event_source_time, matches_fast_book_top}`: never show bbo prices |
| `flow.window` | `{start, end, length_s, since}` or `{reason}` |
| `flow.values` | `buy_aggressive_notional, sell_aggressive_notional, net, volume, trade_count` |
| `flow.cvd` | `{bucket_s: 30, buckets: [{start_ms, start, buy_usd, sell_usd, delta_usd, cumulative_usd, complete, reason?}], cumulative_from_ms, cumulative_from, cumulative_rule, resets_at_gap, reason}`. Incomplete buckets, and every bucket while trades aren't fresh, have null values with a reason. `hl_flow` returns the last 12 buckets and a `slice` with the cumulative base before them. |
| `flow.large_prints` | `{threshold_usd, count, prints: [trade]}` |
| `flow.tape[]` | trade `{time, px, sz, notional_usd, side (B/A), aggressor, buyer, seller, tid, hash, counted}`; `counted: false` = from the subscription snapshot, before coverage; `segment`: `current` or `before a gap (an earlier coverage segment)` |
| `context` | `mark, oracle, mid, funding (fraction per 1h), funding_pct_per_hour, open_interest (coin), open_interest_usd, day_notional_volume, prev_day_px` + `units_note` |
| `deck.card` | see below |
| `deck.events[]` | `{type: regime_change | regime_change_withdrawn | regime_ended_by_gap | burst, market, at, at_ms, text, …}` (`regime_change_withdrawn`: a late trade changed the ticks a change was confirmed on; schema `deck.late_trades`); regime change has `from, to, held_s`; burst has `side, aggressive_notional_usd, threshold_usd, executions, wallets: <ranked list>` |
| `wallets["1m" | "5m" | "15m"]` | `{window: {requested_min, start, end, length_s, complete, completeness}, unique_execution_notional_usd, executions, buyers: <ranked list>, sellers: <ranked list>, wallets: {address: row}, notes[]}` or `{window_min, reason}` |
| `chart` | `{candle_s: 15, candles: [{time (unix s), open, high, low, close, volume_usd}], price_lines: [{price, label, kind}], gaps[]}` |
| `node` | `{panels, live, replay, separation}`: see "Node mode" below; without a node feed `live` is `{available: false, reason: "needs node mode: ..."}` |

Ranked list: `{total_usd, listed: [{rank, address, amount_usd, share_pct}], share_reached_pct, target_pct, cap, stopped_by: target|cap|empty|exhausted, wallets_on_side, summary}`. Show `summary` verbatim, e.g. "Top 50 make up 25% of buying; the 50% target isn't reached."

Wallet row: `{address, bought_usd, sold_usd, aggressive_bought_usd, aggressive_sold_usd, participation_usd, net_bought_usd, buy_share_pct, sell_share_pct, executions}`.

### Flow Deck card (`deck.card`)

When the 90 s window is complete: `{market, window_s, tick_s, tick, window_start_ms, window_end_ms, regime: {value: the confirmed regime} or null+reason, confirmed_regime: {state, tag, since, held_s, as_of_tick, meaning} | null, live_classification: {state, tag, tick, describes, sparse, sparse_note, meaning}, pending: {state, ticks, of} | null, numbers: {price, move_bps, net_flow_usd, volume_usd, impact (descriptive ratio), pressure (aggressive buy / sell notional), top_wallet: {value: address, share_pct, share_label, net_aggressive_usd, aggressive_bought_usd, aggressive_sold_usd, net_label} | null+reason, accounts, trades}, activity_buckets: {new_long, cover, new_short, exit, *_pct, stream: "node fills", provenance, freshness, coverage, ...} from node fills over the card's window when a node feed is configured, else each null with "needs node mode", hook}`. The confirmed regime, the live classification and the pending change are separate; the confirmed label never describes the current numbers by itself. `meaning: {state, key_number, text, wording}` comes from schema `regimes.meanings`.

When it isn't: `regime` and every number are null with the reason (e.g. "window incomplete: needs 90 s after the segment start …"), and `last_confirmed` may hold the last confirmed regime, labeled historical.

## Node mode (`market.node`)

`market.node = {panels, live, replay, separation}`. `live` is the configured node feed's view, or `{available: false, reason}`
(no feed configured, or the feed turned out to serve a REPLAY); `replay` is an explicitly started replay's view, or null. Live
and replay are separate partitions and are never mixed. A view is:

| Key | Shape |
| --- | --- |
| `provenance` | `{source, mode: LIVE/REPLAY, epoch, session, replay_session, stream, market, protocol}` |
| `families` | per family `book`, `triggers`, `depth`, `bands`, `flow`: `{state: LIVE/STALE/BEHIND/UNKNOWN/INVALID/REPLAY, reason, watermark_block, watermark_block_time_ms, applied_through, watermark_age_ms, source_age_ms, stale_after_ms, max_source_age_ms}`. The two limits are the ones the state was judged by: a reader holding the status keeps aging it (the tab withholds a snapshot once a family it shows as LIVE would be past either limit, counting the time since it asked). UNKNOWN means the worst clock offset among the session's accepted lines over the last 10 s puts this machine's clock behind the producer's. A watermark without a source time (`block_time_ms` null) advances no family's freshness. A tool reading the latest snapshot re-judges every node status that was LIVE at build time at the moment it serves it (`aged_by_ms`): one past either limit by then reads STALE or BEHIND, and every node number built on it (ladder, walls, thinning, stops, tape, Flow Deck and burst node buckets, wallet splits) is withheld with the reason. A snapshot read by `snapshot_id` is historical and stays as of its build time |
| `connection`, `replay` | the source's connection state (with a relay's refusal reason), and a replay's status (state, speed, position, recording span, partial history) |
| `book` | `{valid, reason, crossed: null or {since_seq, best_bid, best_ask}, book_time_ms}`: panels built on the book are `{available: false, reason}` unless valid |
| `ladder` | `bids`/`asks` rows `{px, total_sz, orders, notional_usd, largest_order_sz, largest_order_time_ms, oldest_order_time_ms, change_10s: {added, absorbed, pulled, unknown}, stops_to_next_level}`, `labels`, `book_time_ms`, best bid/ask, mid. `stops_to_next_level` is `{orders, sz, unknown_size_orders}`; `null` on the last row (no next level); `{available: false, reason}` on every row while the trigger family isn't LIVE (or REPLAY), with `stops_available`/`stops_reason` on the ladder, never 0 |
| `stops` | conditional interest: `rows` `{lo, hi, kind, side, orders, sized_orders, sz, unknown_size_orders, largest_order, dominated, size_check}`, `outside` (below, above, no reference price), `accounted`, `excluded_by_engine` |
| `walls`, `history`, `thinning`, `tape` | wall watch; the 15-minute heatmap samples (partial until covered); the liquidity-thinning bands with the sharpest drop and its note; the node tape with flags and its coverage |
| `liquidation_events` | liquidation bursts (also merged into `deck.events`) |

Flow Deck cards carry `activity_buckets` from node fills (`stream: "node fills"`), burst events `node_activity` (their side only), and
wallet panels `node_activity_split`; each is unavailable with a reason when the node fills don't cover its window. Top level:
`node_status` (live connection, configured settings, replay status).

