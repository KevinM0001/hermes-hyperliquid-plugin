# Dashboard look: proposal screenshots (UI batches 1 to 3, 2026-11-09)

Headless Chrome 156 (`--headless=new`, 1440 px wide, device scale 1) on an isolated Hermes dashboard
(`hermes dashboard --isolated --port <N> --skip-build`, its own `HERMES_HOME` under a scratch directory outside the repo, the plugin
copied from this clone). The machine's own Hermes and settings were not touched; the isolated dashboard's theme was
switched to Mono for two shots and set back. No run touched the node, production or the contest runner.

| File | What it shows | How |
| --- | --- | --- |
| `00-before-public-top.png` | The tab before this change (Hermes Teal), public mode, BTC, live | Start pressed in the tab, 150 s later |
| `01-public-top.png` | Proposal, public mode, BTC, live, Hermes Teal | same |
| `02-public-full.png` | The whole tab, same run | viewport grown to the tab's height |
| `03-public-top-mono.png` | Same run, Hermes's Mono theme | `PUT /api/dashboard/theme {name: mono}` on the isolated dashboard, reload |
| `04-public-full-mono.png` | The whole tab, Mono | same |
| `05-public-hype-top.png` | HYPE, a market without a confirmed regime | market tab clicked |
| `06-chat-strip.png` | The Chat page after Ask Hermes: the `chat:top` strip, the question in the clipboard | Ask Hermes clicked; clipboard read back: `Using snapshot <ID>, what is BTC doing? Use the hyperliquid commentary skill.` |
| `07-node-top.png` | Node mode on the recorded feed: node strip and node panels first | `node_feed` = a local socket served by `serve_recorded_feed.py` from the Part A follow-test tap (outside the repo) (the Part A follow-test tap, 4 markets, 318 s), Start pressed, 110 s later |
| `08-node-full.png` | The whole tab in node mode | same run |
| `09-node-replay.png` | The Node replay area playing `tests/fixtures/node/xyz-2markets-66s.jsonl` (REPLAY) | replay Start pressed in the tab, 40 s later, xyz:XYZ100 selected |
| `10-node-replay-full.png` | The whole tab with the replay area | same |
| `11-public-nostart.png` | The empty state before Start | page opened, nothing pressed |
| `12-before-public-full.png` | Before: the whole tab as it was (`088babf` dashboard files), public live | the proposal's public full-page view, for comparison with `02` |
| `13-before-public-hype-top.png` | Before: HYPE | compare with `05` |
| `14-before-node-top.png`, `15-before-node-full.png` | Before: node mode on the recorded feed | compare with `07`, `08` |
| `20-live-node-top.png`, `21-live-node-full.png` | Live node mode (UI batch 3): the isolated dashboard subscribed read-only to the contest runner's feed socket (`the contest runner's feed socket`), Start pressed in the tab, 240 s later | the runner was still catching up (feed delay about 11,900 s), so every node family reads STALE and the node panels withhold their values with that reason |
| `22-live-node-stops.png` | The Waiting stops panel in that run | scrolled to the panel |
| `23-live-node-ladder.png` to `27-live-node-tape.png` | Node ladder, heatmap, wall watch, thinning, node tape in that run | scrolled to each panel |
| `28-live-node-top-mono.png`, `29-live-node-stops-mono.png` | The same run in the Mono frame | theme switched on the isolated dashboard only, reset after |
| `30-live-node-top.png`, `31-live-node-full.png` | Live node mode with current data (UI batch 3, second capture, 04:52 to 04:56Z): feed delay 1.5 s, every family LIVE | same read-only subscription to the runner's socket, Start pressed in the tab, 240 s later; the heatmap has about 6 of its 15 minutes (history since 04:49:56Z, the moment the book became valid) |
| `32-live-node-stops.png` | The Waiting stops panel with live data (19,657 triggers accounted) | scrolled to the panel |
| `33` to `37-live-node-*.png` | Node ladder, heatmap, wall watch, thinning, node tape with live data | scrolled to each panel |
| `38-live-node-top-mono.png`, `39-live-node-full-mono.png`, `39b-live-node-stops-mono.png` | The same run in the Mono frame | theme switched on the isolated dashboard only, reset after |
| `43-node-layout-proposed-ladder.png`, `44-node-layout-proposed-stops.png`, `45-node-layout-proposed-tapes.png` | Proposed node-mode layout (UI batch 3, not built): the order-level tables take the full row (node ladder, then heatmap; waiting stops, then wall watch; node tape above the public tape) | mock-up in the file-based harness on the live 04:56Z snapshots, a temporary CSS override, reverted; compare with `33`, `32`, `37` |
| `50-labels-tape-latest-of.png` | Batch 5, option (a) built: the Tape panel's "latest 40 of 450 trades in the window" | file-based harness on the recorded BTC snapshot (the committed code, not a mock) |
| `51-node-layout-built-ladder.png`, `52-node-layout-built-tapes.png` | Batch 5, full-width node tables built: node ladder and heatmap; node tape above the public tape with "latest 30 executions" | file-based harness on the live 04:56Z snapshots (the runner's socket refused connections from about 05:25Z, so no new live capture with values was possible) |
| `53-live-top-feed-refused.png`, `54-live-full-feed-refused.png`, `55-live-top-mono-feed-refused.png` | The built layout on the live dashboard, 05:26 to 05:31Z: public data live, the node feed "disconnected: Connection refused", every node family INVALID with "no data for this market from this source yet" | 240 s run as before; what the tab shows when the runner is down |
| `60-review5-replay-start-2s.png`, `61-review5-replay-start-5s.png`, `62-review5-replay-start-12s.png` | The Plugin session's review-5 issue shown (UI batch 6, nothing changed): the node ladder's "last 10 s" columns (added, absorbed, pulled, unknown) read 0 before 10 s of block time have passed instead of "not measured yet". Replay Restart pressed on BTC on the review dashboard at 05:41:26Z (the runner was down); the Ladder summary box scrolled to its right-hand columns; shots about 2 s (the new session's first snapshot, book time 20:47:56.147: 0 in all four columns at every level), 4 s and 12 s after the click (real sizes). The snapshot route agreed: zeros at 2.1 s of replay time, sizes at 4.5 s and 11.8 s | `tools/shoot.py` on the review instance |
| `40-nofm-a-label.png` | "N of M" option (a), UI batch 4: a plain label in the Tape panel's status line, "latest 40 of 450 trades in the window" | mock-up in the file-based harness on the recorded BTC snapshot (40 tape rows, 450 counted trades); temporary edit, not committed |
| `41-nofm-b-expander.png` | Option (b): the label plus a "Show all 450" button; disabled in the mock because the snapshot carries only the newest 40 rows | same |
| `42-nofm-c-scroll.png` | Option (c): the count in the panel title ("Tape 40 of 450") and a fixed-height scrolling list | same |

What the data says in these shots is the data: the public runs are live Hyperliquid public data at the time stamped in
each header; the recorded feed's blocks are from Nov 7, so its families read STALE (delay about 110,000 s) and the node
panels withhold their values with that reason, which is the plugin's rule, not a display choice.

The capture driver (a local capture script (outside the repo)) is a 150-line CDP script: open the URL, click Start, wait,
screenshot, read the clipboard, change the route, click Stop. It is not part of the plugin.

## The interactive review on Engine's Nov 5 recording (2026-11-09, from 13:44Z)

The isolated dashboard (a spare port, the isolated dashboard's config (outside the repo): public live, `node_feed` = the contest runner's
socket, `node_replay_file` = Engine's recording (outside the repo), Engine's Thursday Nov 5
14:00 to 16:00Z recording, 4.2 GB, four markets, block time 13:28:51Z to 16:13:37Z). The runner was down (its socket
refused connections since 05:05Z), so the live node panels say so and the Replay area carries the node tables.

| File | What it shows | How |
| --- | --- | --- |
| `70-review-nov5-top.png` | The top of the tab after the switch: public side live, the node feed banner (connection refused, retry), "a replay is loaded (PLAYING, 1x)" | 13:49Z, two minutes after the replay started; tab code at `7197cb7` |
| `71-review-nov5-loading.png` | What the tab shows while the plugin loads the recording: "No snapshot. the latest snapshot is 89.9 s old: the collector isn't publishing" (the whole load took 1 min 50 s; the Start call itself timed out at 10 s and the replay appeared when the load ended) | 82 s after pressing Start in the Replay area |
| `72-review-nov5-full.png` | The whole tab with the Replay area playing: Families and the L4 ladder summary side by side in half a row each (before the width change), waiting stops, walls, thinning, node tape, the heatmap filling (24 samples) | 13:49Z; the ladder table scrolled to its right-hand columns |
| `73-review-nov5-full-after.png` | The same view at `c616fbf`: the Families strip and the L4 ladder summary take the full row (two `wide` props); replay position 14:12Z of the recording, heatmap with a full 15-minute window | 14:30Z, page reloaded after the bundle sync; no restart, the recording kept playing |
