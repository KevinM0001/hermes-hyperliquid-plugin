# Commentary: tool proposals

**Proposals only. Nothing here is built.** Each would be the Plugin session's code (and, where noted, the UI session's tab). Labels: [record] cited, [measured] read off this machine, [inferred].

## Answer time won't improve until a tool change like proposal 1 lands

The demo's chat step took **1 min 13 s against 22 s in the script** (Codex h1, D2) [record]. A typical answer makes **5–7 tool calls** [record, the brief]. Each call is a full model round trip: the model reads the result, decides on the next call and waits again.

The revised skill makes answers shorter and plainer. It can't remove the round trips, so **the 1 min 13 s is a tool-shape problem, not a wording one** (review §4). The skill now asks for only the tools a question needs, which may save a call or two on narrow questions [inferred]. A "what is BTC doing?" answer still needs the deck, the book, the context and usually the wallets. The commentary work shouldn't be judged on latency until a change like proposal 1 is in.

---

## 1. A combined `hl_brief` tool (lead proposal)

**What:** one read-only tool, `hl_brief(market | markets, snapshot_id?)`. It returns, from one snapshot, everything a "what is X doing?" answer needs, already in the answer's order. It replaces `hl_status` + `hl_alerts` + `hl_market` + `hl_context` + `hl_wallets` for that question: **1–2 calls instead of 5–7**. A second call is left for follow-ups ("open that wallet", "any stops?").

**Fields** (each section carries its own window label):

| Section | Contents | Why (rule it serves) |
| --- | --- | --- |
| `source` | snapshot ID, time, mode (LIVE / REPLAY / SYNTHETIC), each feed's status in plain words, collector state; node: feed state and delay | the source line; freshness and "unavailable" reasons |
| `price` | market name *and* its last trade price and mid, side by side | name each instrument from its price |
| `window` | the Flow Deck's 90 s numbers: move, net, volume, IMPACT (or its reason), PRESSURE (or its reason), top account with share, net and observation time | impact per dollar; top-1 share; a named wallet's age |
| `path` | the last 6 ticks' price and net flow (15 s), or the last 6 × 30 s flow buckets with each bucket's last price | one event can't stand for a window (example 5: the price low isn't in any tool result today) |
| `labels` | confirmed / live / pending, each with its few-word gloss and its `status` (see proposal 2) | glossary; reliability line |
| `events` | the last few alerts, each with side, fired time and `age_s` against the snapshot | alert direction and age |
| `book` | top, spread, `levels.largest`, observed depth within the returned levels; bands only where covered, each with its own resolution | largest level from `levels.largest`; band resolution |
| `unavailable` | the list, in plain words | unknowns stated briefly |
| `node` (when present) | activity buckets for the window; the stop set as a one-line count with "sizes are minimums" and the venue-check note, not the rows | opening vs closing; stops kept out of the lead |

**Not in it:** wallet lists, tape, the ladder, stop rows. They stay in the existing tools for follow-ups.

**Cost:** about one tool's worth of code. It reads the snapshot that already exists and computes nothing new [inferred]. Every field already exists somewhere in the snapshot, except `path`'s per-bucket last price and `events[].age_s`.

**Test:** Codex's chat rehearsal timed before and after, on the same question and snapshot. Success means the chat step lands near the script's 22 s [inferred target; not measured].

## 2. A validation `status` on every label and alert

**What:** a short `status` string next to every Flow Deck label, burst, liquidation alert and node panel label in tool output (and on the tab). It comes from the schema. Examples:
- "descriptive; no predictive test on record";
- "calibrated for frequency (about 10 a day), not for what follows".

**Why:** the reliability line is the review's highest-priority rule (§1.1). Today it lives only in the skill, so the model has to remember it. A field in the result is quoted, not recalled.

## 3. `age_s` and a normalised `direction` on every event

**What:** each event gets `age_s` (snapshot time minus fired time) and `direction` with values `buying` / `selling` / `up` / `down` / `longs liquidated` / `shorts liquidated`.

**Why:** two contradictory alerts with no direction or age misled a reader once (study §9.3a). The model currently derives both from `at`, `side` and `type`, and can get either wrong.

## 4. Flag a window whose total is one event

**What:** when one 30 s bucket holds more than a set share of a window's net flow, the card says so, for example `dominated_by: {bucket_start, net_usd, share_of_window_net}`. The share is computed by the tool, so the model never computes it.

**Why:** example 5. The 90 s window shows −$51M net and price +9 bps (PULLED-OFFERS), and one 30 s bucket holds −$54.5M. A model reading the card alone has every reason to invent a cause. This is review §2.7 and test item 9.

## 5. Liquidation alerts: say what happened after they fired

**What:** a liquidation burst fires once per episode. Add the episode's running count and notional since it fired (or the current 60 s count) to the event, or to the card.

**Why:** in example 4 the alert fired at 15:04:09Z with 2 liquidations ($101K), and 336 more ($11.9M) followed within the 90 s window without a new alert [measured from the recording]. Quoting the event as given is correct but understates the episode. The tool, not the model, should add the rest.

## 6. A plain gloss next to each label, instead of renaming

**What:** keep the labels as they are in data, logs and tool output (review §3: renaming breaks continuity with `deck-live.log`, `hl_replay.py` and every recorded episode). Add a `plain` field with the glossary's few words, for example `VACUUM-UP` → "price moved a long way for the flow". The tab can show it under the label (UI session).

**Why:** the skill's glossary covers chat. A field covers the tab and any other consumer, and the two stay worded the same.

## 7. Keep stops out of the default results

**What:** `hl_brief` gives the stop set as a one-line count. Rows come only from `node_stops`.

**Why:** in one measurement resting stops were about 0.15% of the resting bids they would hit (scaled for the short window, about 3%) [record, review §4]. In example 4, the nearest stop band held at least 0.56 BTC against 150.65 BTC of resting asks in the 50 returned levels, 80,141–80,198 [measured from the recording]. Leading with stops leads with a rounding error.

---

## Not proposed

- **Renaming labels in the data** (review §3).
- **A forecasting or signal field of any kind.** Nothing in the record supports one (study §10.1).
- **A longer chart or pattern detection.** The skill now says never to name chart patterns (review §4).
