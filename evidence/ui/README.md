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

What the data says in these shots is the data: the public runs are live Hyperliquid public data at the time stamped in
each header; the recorded feed's blocks are from Nov 7, so its families read STALE (delay about 110,000 s) and the node
panels withhold their values with that reason, which is the plugin's rule, not a display choice.

The capture driver (a local capture script (outside the repo)) is a 150-line CDP script: open the URL, click Start, wait,
screenshot, read the clipboard, change the route, click Stop. It is not part of the plugin.
