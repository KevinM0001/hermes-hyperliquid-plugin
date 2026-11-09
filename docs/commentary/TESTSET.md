# Commentary test set

Ten items, from the review (`commentary-brief-review-and-testset-20261109.md` §5). Each guards against a failure that actually happened, and each cites where it happened, so none can be dropped later as hypothetical. Items 1, 2, 6 and 7 are the strongest: each has known ground truth *and* a documented wrong answer. A model's output can be scored against both.

**Status: rehearsed 2026-11-09, two rounds, on Hermes with `claude-sonnet-4-6`.** Each answer, its score and its time are in a local path (outside the repo).
- **Setup:** the recorded numbers were given to the model in the tools' result shape, since no collector or replay ran. The times therefore cover narration only.
- **Round 1, on `d804310`:** items 3, 5, 7 (node), 8, 9 and 10 were run. 5 passed and item 9 failed.
- **Round 2, on `f75f887`:** the same six, plus items 1, 2, 4 and 7 (transcript) with the recorded screens as the data, were run. All 10 passed on "must contain" and "must not claim". No answer met the length target, and several broke smaller rules.
- **Not yet run:** item 6 (the book side is missing).
- **Scoring:** done by the skill's author; a second reader should re-score.

## How each item is scored

An answer **passes** when it:
1. contains every "must contain" fact; any wording is fine if the fact is unambiguous;
2. makes none of the "must not claim" claims, in any wording;
3. follows the skill's form: bottom line first, 3–5 bullets, about 120 words, a source line, and the reliability line when a label or alert is cited.

Points 1 and 2 decide pass or fail. Point 3 is reported, not failed, so content misses aren't hidden behind form misses.

Scoring is by a reader (Kevin, Codex or a Claude reviewer) with the item's ground truth in hand, not by keyword match. "Isn't new shorts" and "new shorts" share the words.

## Sources referred to below

| Name | What it is | On this machine |
| --- | --- | --- |
| study | `live-flow-study-20260929.md`: the original order-flow study | `a local path (outside the repo)` (28,538 bytes) |
| deck transcript | `deck-live.log`: the terminal Flow Deck's screen, every 10 s, 2026-09-29 04:24:23Z onward | `deck-live.log` (a local file outside the repo) (57,709,485 bytes); the same size at a local path (outside the repo) |
| flow log | `flow-live.log`: the 60 s flow monitor, 02:46:56Z onward | `flow-live.log` (a local file outside the repo) |
| pareto | `pareto-0310.txt`: concentration run at 03:09:34Z, with 5-minute buckets | `pareto-0310.txt` (a local file outside the repo) |
| run4 | public websocket capture, 2026-11-08 00:42:05–00:47:12Z | the run4 public websocket capture (outside the repo) |
| Nov 4 / Nov 5 node | node recordings (REPLAY) | Engine's node recording (outside the repo), `w20261105T2300Z-2400Z.jsonl` |

The node's raw fill files for that day weren't readable from this session [measured, 2026-11-09].

## The items

### 1. Which instrument was which (BTC vs HYPE)

- **Snapshot:** [transcript] deck transcript 17:15–17:24Z, 2026-09-29, both coins (frames from line 95503, 17:20:08Z).
- **Question:** "What happened in BTC and HYPE between 17:15 and 17:24?"
- **Must contain:** the $1.32M new-long buyer was in **BTC**; the $2.44M seller was in **HYPE**; each is identified by its price (BTC ~83,100, HYPE ~86.1).
- **Must not claim:** that BTC led HYPE; any cross-asset causation ("BTC dragged HYPE down").
- **Documented wrong answer:** the study's first version had the coins transposed and concluded that a BTC liquidation dragged HYPE down, with BTC leading by about 90 s (study §9.2).
- **Guards:** study §9.2 (the transposition) and §9.5 defect 5 (re-derive the instrument from price). Skill §3, "Name each instrument from its price".
- **Exists?** **Yes**, as a transcript. **Needs building:** the plugin's tools can't read `deck-live.log`. A rehearsal has to hand the model the frames, for example as a tool-result fixture with the panel headers' prices kept.

### 2. Two alerts pointing opposite ways

- **Snapshot:** [transcript] the frame at 17:20:38Z (deck transcript line 95636). Its alert panel shows HYPE `CONVICTION` ("new shorts 1K -> 1.22M … Fresh risk, not unwind"), fired 17:20:38Z, and HYPE `LIQUIDATION DONE` ("exits collapsed 108K -> 4K … The seller has finished"), fired 17:18:18Z. Both were inside the 300 s display life.
- **Question:** "What are the HYPE alerts saying right now?"
- **Must contain:** both alerts, each with the time it fired and its direction (fresh selling; selling finished); that they point opposite ways; which is newer (CONVICTION, 17:20:38Z).
- **Must not claim:** one state chosen from either alert ("the selling is done" or "sellers are in control" alone).
- **Documented wrong answer:** the 17:18:18 firing was read as a reversal entry. HYPE then fell to 85.817 by 17:23:18Z, a 36.7 bps drawdown (study §9.1).
- **Guards:** study §9.3a. Skill §3, "Every alert gets its direction and its time".
- **Exists?** **Yes**, with the fired times from the study's §9.1 table; the frame itself doesn't show them, which was the defect. **Needs building:** the same fixture as item 1, with the fired times attached. Neither alert is in this plugin, so the answer should also say they're the terminal deck's (skill §7, "Not in this plugin").

### 3. A big move on small flow (thin book)

- **Snapshot:** the review names "~87 pts of BTC on $93K net flow" (study §8.4). The study gives no time for it.
- **Question:** "What's BTC doing?" asked on that frame.
- **Must contain:** the move; that impact per dollar was extreme (or, under $100K net, that the move came on very little flow).
- **Must not claim:** "quiet", "nothing happening", "thin and inactive"; dropping the move because volume or trades were under a floor.
- **Documented wrong answer:** the original tooling's `THIN` gate ($150K) and `QUIET` gate (4 fills) hid this move and the others (study §8.4).
- **Guards:** study §8.4. Skill §4.2.
- **Exists?** **Not the original frame.** Two substitutes are on this machine and can stand in now:
  - [record] run4, HYPE, window 00:45:30–00:47:00Z: +6.19 bps on $58K volume, net −$24.6K, IMPACT unavailable (under $100K), VACUUM-UP `thin` (EXAMPLES.md example 3);
  - [replay] Nov 5 node recording, BTC, the minute 23:40Z: 75,864 → 75,961 (+97 pts) on $232K of taker notional, the hour's second-quietest minute. Measured from the recording's executions per minute; not yet cut to a 90 s window.
- **Needs building:** finding the original 87-point frame in the deck transcript, if it's there. The transcript's IMPACT and net lines can be searched for it.

### 4. High volume is one account

- **Snapshot:** [transcript] pareto, BTC taker, 5-minute bucket 02:59Z: volume 3.11M, top-1 77.3%, top-5 90.9%, 2 wallets to 80%, Gini 0.968 (`pareto-0310.txt` line 107).
- **Question:** "Is there real conviction behind this BTC volume?"
- **Must contain:** the top-1 share (77.3%), and that the volume is concentrated.
- **Must not claim:** broad participation; conviction inferred from volume.
- **Documented wrong reading:** "volume spikes are concentration spikes, not participation spikes" (study §2) inverts the usual reading of volume, which an answer would otherwise default to.
- **Guards:** study §2. Skill §4.4.
- **Exists?** **Yes**, as text. **Needs building:** a fixture in the plugin's shape (an `hl_wallets` ranked list or a Flow Deck top wallet). A plugin-shaped substitute exists now: [record] run4 BTC 00:47:00Z, top wallet 60.2% of the window's taker notional (EXAMPLES.md example 2).

### 5. A named wallet is already old

- **Snapshot:** any frame with a dominant address: run4 BTC 00:47:00Z (0x3f5fdb2410…, 60.2%) or HYPE (0xb3657c6e16…, 66.5%) [record]; any deck transcript frame [transcript].
- **Question:** "Who is driving BTC?"
- **Must contain:** if a wallet is named, the address exactly as returned and the time of the observation.
- **Must not claim:** that the wallet is a persistent actor ("a whale has been accumulating"); any owner, fund or exchange.
- **Documented wrong reading:** fixed-wallet tracking was removed as the wrong abstraction; dominance has a 40–60 s half-life (study §2, §5 defect 4).
- **Guards:** study §2, §5 defect 4. Skill §4.5.
- **Exists?** **Yes** (run4, both coins).

### 6. A wall that vanished was pulled, not consumed

- **Snapshot:** 02:46Z, 2026-09-29, BTC breaks 83,000: about $420K of buy flow over the window against a 452 BTC (~$37.5M) ask that disappeared (study §2, §3 call 4).
- **Question:** "BTC just broke 83,000. Did buyers take out the offers?"
- **Must contain:** the wall was **pulled, not consumed**, with the flow against the wall's size.
- **Must not claim:** that buyers absorbed or ate through the supply.
- **Documented right answer:** call 4, "The 83,000 wall was pulled, not consumed": right and verifiable. Call 5, two minutes later, turned it into "83,000 holding as support", which was wrong within 3 minutes. That is the error to fail.
- **Guards:** study §2, §3 calls 4–5. Skill §4.7.
- **Exists?** **Partly.** The flow side is in the flow log (frame 02:46:56Z: BTC 83,057, 1 m net +$84K, 5 m net −$196K). The book side (the 452 BTC ask and its disappearance) isn't in any file found on this machine, and the raw data is unreadable (above).
- **Needs building:** a book snapshot for 02:44–02:48Z. A node-mode substitute can be cut from the Nov 4–6 recordings: find a level whose `book_delta` changes are cancels (`status: canceled`, `maker_filled: "0"`) while the price trades through. Not done yet.

### 7. Closing longs, not new shorts

- **Snapshot:** [transcript] flow log, 03:05:57Z: BTC 82,861, 1 m net −$2.79M; sell side "new-short 513K, long-exit 2.99M, exit-driven"; top account 0xcb58b8f5ec −$2.90M (94%).
- **Question:** "Big BTC selling just now — new shorts?"
- **Must contain:** that the selling was mostly longs closing (unwind), with the split.
- **Must not claim:** "new shorts", "fresh bearish conviction", "shorts piling in".
- **Documented wrong answer:** call 9 (03:06), "BTC new-short broke out ⇒ conviction arrived": wrong, and misattributed, since the whales were `Close Long` (study §3).
- **Guards:** study §3 call 9. Skill §4.6.
- **Exists?** **Yes** [transcript]. A node-mode version exists too: [replay] Nov 5, sell burst 23:01:39Z, 77% of it longs closing (EXAMPLES.md example 5).

### 8. Every cited alert carries its validation status

- **Snapshot:** any answer that cites a Flow Deck label, a burst or a liquidation alert: EXAMPLES.md examples 2–5, or any live snapshot.
- **Question:** "What does the TRENDING-UP label on BTC mean — should I follow it?"
- **Must contain:** the label's status: no predictive test on record; the source deck's alerts didn't beat the 5–7 bps round trip. Also the skill's reliability line, or its equivalent.
- **Must not claim:** that the label or alert has predictive value; any trade advice.
- **Guards:** study §10.1 (n=185, nothing distinguishable from zero). Skill §4.1, §7, §8.
- **Exists?** **Yes**, wherever a label is on screen.

### 9. One event can't stand for the window

- **Snapshot:** the review names a window with one dollar-dominant minute: $2.7M that moved price 36 pts while six smaller minutes moved it more (measured 2026-10-17).
- **Question:** "Was that last stretch net selling?"
- **Must contain:** the path (per-bucket flow), or the window's label with the caveat that one event dominates its total.
- **Must not claim:** the dollar aggregate as the window's character ("the window was net selling, so sellers were in charge").
- **Documented wrong answer:** the review's author reported a leg as "net selling" from its dollar total while the per-minute sign matched price direction 8 of 8 times (review §2.7).
- **Guards:** review §2.7. Skill §4.9.
- **Exists?** **Not the original** (the 2026-10-17 data isn't on this machine). A stronger substitute is: [replay] Nov 5, Flow Deck tick 23:02:30Z. The window's net is −$51.06M and price is +9.26 bps (PULLED-OFFERS). One 30 s bucket holds −$54.47M, and the next is +$8.77M (EXAMPLES.md example 5).

### 10. Whole-position TP/SL sizes are a minimum

- **Snapshot:** a stops band containing whole-position TP/SL orders.
- **Question:** "How much is in the stops above 80,100?"
- **Must contain:** the size as unknown or a minimum, with the count of orders that have no stated size; whether the read was corrected or raw, if an original size from the Parquet archive is cited; that stops are checked against the venue only at engine restart.
- **Must not claim:** a complete size; "0" for a band of size-less orders.
- **Guards:** `tpsl-original-size-fix-and-correction-20261109.md` (cited by the review; not found on this machine); Node Project review B2/B3. Skill §3.
- **Exists?** **Yes for the size part** [replay]. Nov 4 bands at 15:04:58.676Z:
  - 80,100–80,200: 26 buy stops, 0.56 BTC stated, 7 whole-position;
  - 80,000–80,100: one sell stop with size "0" and 1 whole-position, so its size is unknown, not 0.

  **Needs building for the corrected-vs-raw part:** the plugin reads the node feed, not the Parquet archive, so it never sees an original size. That half needs a fixture from `l4read.py` with `corrected=True` and `corrected=False`.

## Summary: what exists and what needs building

| # | Exists on this machine | Needs building before a rehearsal |
| --- | --- | --- |
| 1 | transcript frames | a fixture that serves the frames to the model |
| 2 | transcript frame plus fired times from the study | the same fixture, with fired times |
| 3 | substitutes only (run4 HYPE; Nov 5 23:40Z) | the original frame, if it's in the transcript; a 90 s cut of 23:40Z |
| 4 | pareto text; run4 substitute | a plugin-shaped fixture of the 02:59 bucket |
| 5 | yes (run4) | nothing |
| 6 | flow side only | the book side (02:46Z), or a node-recording substitute |
| 7 | yes (flow log; Nov 5 node substitute) | a fixture for the flow-log frame |
| 8 | yes | nothing |
| 9 | substitute (Nov 5 23:02:30Z) | nothing for the substitute |
| 10 | size part (Nov 4 bands) | an `l4read` corrected/raw fixture |

Ready to rehearse now, with the recorded numbers given to the model as tool results: 5, 8, 9 (substitute), 10 (size part), 3 (substitute) and 7 (node substitute). The transcript items (1, 2, 4, 7 as recorded) each need one fixture file in the plugin's tool-result shape.

For the rehearsal, fixtures were built outside the repository, in the clone's git-excluded `.rehearsal/` folder, from the sources above with the plugin's own functions. None is committed.
