"""Node mode: the l4-livefeed protocol v2 client and the node panels built on it (Handoff 1c Part B).

The engine (or a relay) publishes newline-delimited JSON lines; PROTOCOL.md in the engine repository
(l4-livefeed, protocol version 2, document revision 2.2) is the normative contract. This package is an
independent client implementation of it:

- proto.py      constants, exact-decimal parsing, the per-kind field lists (used to check fixtures)
- book.py       the resting executable book: levels, level statistics, lifecycle-labelled changes
- triggers.py   waiting stop / take-profit orders, stop bands with the size check
- flow.py       node executions: activity buckets, wallet splits, liquidation bursts, the tape
- client.py     the protocol state machine (sessions, sequences, snapshots, swaps, watermarks)
- replay.py     recordings: a reader and a player on a replay clock
- transport.py  local Unix socket and relay WebSocket connections
- panels.py     the per-market node block the dashboard and the node_* tools share
- manager.py    owns the live and replay clients for one plugin runtime
"""
