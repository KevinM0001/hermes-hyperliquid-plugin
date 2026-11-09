# Author report: the commentary specialist, 2026-11-09

Branch `feat/commentary-20261109`, from `f5db32e`. Commits:

| Commit | What |
| --- | --- |
| `4eccf30` | the four deliverables |
| `e027109` | the taker-side rule (review §1.3), missed in the first pass |
| `d9039c5` | depth resolution in plain words; "closing flow is not passive" kept from v1 |
| (this file) | the report |

Labels: [measured] read off this machine, [record] cited from a document, [inferred].

## Scope

`git diff --stat f5db32e..d9039c5` touches exactly the four deliverables [measured]:
- `skills/hyperliquid-commentary/SKILL.md`
- `docs/commentary/EXAMPLES.md`
- `docs/commentary/TESTSET.md`
- `docs/commentary/TOOL_PROPOSALS.md`

No code, tool, test, dashboard, data label or log changed. Nothing was pushed.

**Tests.** Every test that reads `SKILL.md` was found by grep and run with Hermes's pinned interpreter. **10 passed in 0.99 s** [measured]:
- `test_h1_review2.py::test_b3_largest_level_is_a_computed_field_and_the_skill_uses_it`
- `test_review3.py::test_skill_does_not_call_pressure_node_only`
- `test_deck.py::test_unresolved_is_gone_everywhere`
- `test_node_review_20261109.py::test_b2_b3_the_skill_and_the_stops_say_so`
- `test_h1_review4.py::test_b3_each_tool_window_and_depth_band_carries_its_own_label`
- `test_lifecycle.py`, all 5 tests (it checks the skill registers)

The command:

```
python -m pytest -c tests/pytest.ini -q <those tests>
```

The full suite wasn't run: only text files changed, and no other test reads them [measured by grep]. The Plugin session's merge run will cover it.

**Runs.** Nothing heavy. The example numbers came from streamed reads of the recordings, each under 3 s [measured]. Node files were reached by a byte-offset search on block time. The plugin's own pure functions were called on the recorded trades (`deck.window_metrics`, `deck.classify`, `node.calibration`). No engine, collector, plugin replay or load test ran. Nothing touched the node, production or the contest runner.

**Rehearsal: not done.** It needs a provider key and about 10–15 minutes, and the handoff says to ask Kevin first. That request is in a local path (outside the repo).

## What changed and why

The brief documents three problems [record, brief lines 14–16]:
1. answers too long and technical (about 600 words for "what is BTC doing?");
2. slow (1 min 13 s against 22 s);
3. narration mistakes Codex caught.

The skill now fixes the first and guards the third. The second is a tool-shape problem (review §4, line 203), so it goes to `TOOL_PROPOSALS.md`, which says plainly that answer time won't improve until a tool change like `hl_brief` lands.

Where the brief and the review disagree, the review wins (handoff):
- **The example sentence:** two facts, not a contrast (review line 95, against brief line 23).
- **Chart patterns:** "do not name chart patterns" (review line 164, against brief line 29).

## Each rule, traced

Line numbers are in `skills/hyperliquid-commentary/SKILL.md` at `d9039c5`. "Brief" is a local path (outside the repo); "review" is a local path (outside the repo).

### Opening and voice

| Skill line | Rule | Source |
| --- | --- | --- |
| 15 | the study's conclusion as the skill's first line | review §7, lines 260–265 (the handoff asks for it first) |
| 17 | describe, never forecast; no trade advice; no chart patterns | brief line 28; review §4 line 164 |
| 23 | bottom line first; two facts, not a contrast; the review's sentence as the model | brief line 23; review §2.2 lines 84–96 |
| 24 | 3–5 bullets, about 120 words, more only when asked | brief line 24 |
| 25 | one source line (snapshot, time, mode, each feed's status); REPLAY/SYNTHETIC always said | today's skill §3 (moved from first to last line, see below); brief line 36 |
| 26 | plain language, no field names; a label only with its gloss; a directional label never the subject | brief line 25; review §3 lines 143–148 |
| 27 | round sensibly; UTC times | brief line 26 |
| 28 | say each fact once | brief line 14 (the repeated paragraph) |
| 30–36 | what to keep when cutting: the move with its flow first | review §1.2 line 46 and §4 line 173 ("a brevity target makes it worse") |

### Accuracy rules (§3)

| Skill line | Rule | Source |
| --- | --- | --- |
| 41 | call only the tools the question needs; one snapshot ID throughout | today's skill §1; brief line 15 |
| 48 | only what the tools returned | today's skill §2 |
| 49 | every number with its own tool's window | brief line 35; Codex h1 review 4 B3 (kept verbatim; a test checks it) |
| 50 | each depth band with its own resolution, in plain words; uncovered bands | brief line 35; h1 review 4 B3 (test); brief line 25 for plain words |
| 51 | largest level only from `levels.largest` | brief line 32; h1 review 2 B3 (kept verbatim; a test checks it) |
| 52 | partial lists as "N of M" | brief line 34 |
| 53 | taker side only; exactly 50.0% means both sides were counted | review §1.3 lines 51–54 |
| 54 | unavailable is not zero, with its reason | brief line 36; today's skill §2 |
| 55 | name each instrument from its price | review §1.4 lines 56–61; §4 line 179 |
| 56 | every alert with its direction and time; opposite alerts both given | review §1.5 lines 63–68; §4 line 180 |
| 57 | whole-position TP/SL: unknown or a lower bound; venue check only at restart | brief line 33; Node Project review B2/B3 (kept verbatim; a test checks it) |
| 58 | corrected or raw, for Parquet original sizes | review §4 lines 187–192 |

### What the data can and can't say (§4)

| Skill line | Rule | Source |
| --- | --- | --- |
| 65 | the reliability line on every cited label or alert | review §1.1 lines 21–37; §4 line 172 |
| 66 | small flow doesn't mean nothing happened | review §1.2 lines 39–49; §4 line 173 |
| 67 | impact per dollar | review §4 lines 174–177 |
| 68 | whose flow it was: top-1 share | review §2.3 lines 98–104; §4 line 178 |
| 69 | a named wallet is already old | review §2.4 lines 106–110 |
| 70 | new risk or closing, from the data | review §2.5 lines 112–115 |
| 71 | pulled is not consumed | review §2.6 lines 117–121 |
| 72 | a directional label is never the subject | review §3 lines 143–148 |
| 73 | one event can't stand for the window | review §2.7 lines 123–128 |
| 74 | stops rarely deserve the lead | review §4 lines 181–186 |
| 75 | capture gaps in plain words | review §4 lines 193–195 |
| 76 | name the subject of "stopped", "stalled", "down" | review §2.7 lines 129–131 |
| 78 | IMPACT unavailable under $100K | review §1 line 70 |
| 79 | ignore `clipCV` | review §1 line 71 |
| 80 | `0s` means one block | review §1 line 72 |
| 81 | no chart patterns | review §4 lines 164–168 |

### Kept from today's skill, shortened (§5–§6, §8)

| Skill lines | What | Source |
| --- | --- | --- |
| 83–89 | freshness and unavailable reasons | brief line 36 |
| 93–101 | Flow Deck: confirmed / live / pending | GPT review R11, in the schema `regimes.decisions` |
| 93–101 | `meaning.text` quoted when asked | today's skill §6 |
| 93–101 | events, bursts and rank labels | the burst calibration, Nov 8 |
| 103–107 | wallets | Codex h1 reviews, the `served` and `historical` blocks |
| 109–118 | node data | today's skill §8: Node Project review, delay counter (`138e22a`) |
| 163–166 | describe, don't recommend | brief line 28 |

### Glossary (§7, lines 119–161)

Every label has its plain meaning and its validation status (brief line 39; review §3 lines 150–156):
- The meanings follow `schema/metrics.v1.json` (`regimes.rules`, `alerts.burst`) and `docs/SNAPSHOT.md`.
- No predictive test of any plugin label is on record: a grep of the README, schema and evidence for forward returns, back-tests and "predictive" found nothing [measured].
- The terminal deck's unported alerts get their recorded status (study §10.1–10.2).

## Changes to today's rules, for Codex to check

1. **The snapshot line moved from the first line to the last.** The brief's "bottom line first" (line 23) and the old "start every answer with one line like Snapshot…" can't both hold. The line itself is kept, with its snapshot ID, time, mode and feed statuses.
2. **Freshness per figure became per feed in the source line.** Any non-fresh value is still named in place, with its status and reason. A fresh value no longer repeats "fresh" beside each number.
3. **`meaning.text` is quoted only when the user asks what a label means.** Otherwise the glossary's few-word gloss is used. The brief (line 25) asks for "what it means in a few words". The meaning texts run to 20–30 words each.
4. **The depth resolution is said in plain words.** "Approximate, in $10 buckets" replaces the tool's "nSigFigs=4 book…" string, and the per-band distinction Codex required is kept. A test checks the rule's wording; it still passes.
5. **Producer epoch** is no longer asked for in node answers. Provenance, mode and family states still are. It's an internal identifier (brief line 25).

## The other deliverables

- **`EXAMPLES.md`**, five examples:
  - 1 transcript [record]: the review's §6, unchanged;
  - 2 public-mode [record] from the run4 public websocket capture (outside the repo);
  - 2 node-mode [replay] from the Nov 4 and Nov 5 recordings.

  Each states its file, time span and how every number was read. Four findings while building them bear on the skill:
  - In public mode, ABSORBING can sit next to a 79 bps move, and a 6 bps HYPE move came on $58K. The trap is live.
  - A liquidation alert fires once per episode: Nov 4 shows 2 liquidations ($101K), then 336 more ($11.9M) with no new alert [measured].
  - On Nov 5, the window's −$51M net selling is one 30 s bucket of −$54.5M, under a PULLED-OFFERS label [measured].
  - The price path (the chart) isn't returned by any tool.

  The last three became TOOL_PROPOSALS 4, 5 and 1 (`path`).
- **`TESTSET.md`**: the review's 10 items, each with its question, must contain, must not claim, documented wrong answer, provenance and what exists on this machine.
  - Six items (1, 2, 4, 5, 7 and 8) have all their data here; 1, 2, 4 and 7 still need a fixture to serve it to a model. Four (3, 6, 9 and 10) have only part of it, or a substitute.
  - The node's raw fill files weren't readable from this session [measured, 2026-11-09].
  - The transcript items need a fixture to be served to a model; none was built, since that would add files.
- **`TOOL_PROPOSALS.md`**: led by `hl_brief` (1–2 calls instead of 5–7), then six smaller proposals. None built.

## Known limits

- **The examples weren't produced by the plugin's tools.** They hold the numbers the tools' own functions give on the recorded trades, so they quote no snapshot ID. They don't claim the latch (confirmed or pending labels), only each tick's live classification. The node examples' Flow Deck numbers use node executions; the plugin's card uses public trades, the same venue trades [inferred].
- **Rehearsed since; see the next section.** The earlier note here, "no answer has been rehearsed yet", is superseded.
- **The word target is "about 120"; the examples run 125–136 tokens** (whitespace count). The review's own example is 115 by its count and 142 by the same whitespace count.

## Round 2: the rehearsal and schema 1.3.0 (commit `f75f887`)

Kevin approved the rehearsal: the provider file copied into a Hermes home inside this clone, Anthropic `claude-sonnet-4-6`, as in Codex's chat step. The UI session then relayed Kevin's go-ahead for the fixes and a second round, and its schema 1.3.0 changes. Each answer, score and time is in a local path (outside the repo).

### The new rules, traced

Line numbers are in `SKILL.md` at `f75f887`.

| Skill line | Rule | Source |
| --- | --- | --- |
| 23 | the answer starts with the bottom line, with no working shown; no contrast words | rehearsal round 1: item 10 opened "Skill loaded. Now to the question."; items 3 and 9 used "while" and "despite" as contrasts |
| 24 | 3–5 bullets, never over 150 words | rehearsal round 1: all six answers ran 220–354 words |
| 55 | no totals across rows; never pick out the heaviest row | rehearsal round 1, item 10: it summed 17 bands and ranked "the heaviest" |
| 76 | addresses in full, or not named | rehearsal round 1, items 7 and 8: shortened addresses |
| 80 | the one-event rule holds when the question asks for the total | rehearsal round 1, item 9 (FAIL): it opened "Yes, heavily" on the 5-minute total |
| 111 | a regime change's held time belongs to the previous label; no chains | rehearsal round 1, item 7: TRENDING-DOWN "held 60s"; the event says 45 s |
| 115 | a liquidation alert fires once per episode, so its numbers aren't the total | rehearsal round 1, items 7 and 8: "small … relative to the burst". EXAMPLES.md example 4 shows why |
| 43 | the two Ask Hermes questions; <STATE> is the card's confirmed label when it has one | UI session, Nov 9: the button's clipboard text and its `askQuestion` code |
| 57 | grouped books: bucket width in plain words, reason when withheld; their largest level is a coarse bucket | UI session, schema 1.3.0 (`book.ladders`) |
| 93, 98, 171 | context values and the forming candle are served unverified with their received age: quote them as such, never fresh; candle volume is in the coin | UI session, schema 1.3.0. Their agents' two SKILL.md sentences are dropped from the UI branch in favour of this text, so one skill text merges |

The 10 tests that read `SKILL.md` still pass: 10 passed in 0.91 s [measured].

### What the rehearsal shows [measured on 2 rounds; inferred beyond them]

- **Content holds.** In round 2 every test item passed its must-contain and must-not checks, including item 9, which failed in round 1. That covers ten items: 3, 5, 7 (node), 8, 9 and 10, plus 1, 2, 4 and 7 (transcript).
- **Length doesn't.** Round 2 ran 193–513 words. The six repeats got shorter (item 3: 220 → 193; item 9: 244 → 222), but none reached 150.
- **Some rules weren't followed even after they were made explicit:**
  - Item 7 still calls the liquidation alert "small relative to the burst", and still gives TRENDING-DOWN "held 60s".
  - Item 10 still sums the bands (now with a wrong order count, 483 for 480), still ranks them, and calls buy stops "sell stops".
  - The card-form answer hangs the current window's numbers on the confirmed label and gives the dollar total without the path.
- **Takeaway.** Prompt wording reaches the content rules but not the arithmetic, length and ranking ones. TOOL_PROPOSALS 1, 4 and 5 move those into the tool, and the rehearsal makes the case for them. Tuning the skill further against these same ten items would fit it to them rather than test it (study §10.3).
