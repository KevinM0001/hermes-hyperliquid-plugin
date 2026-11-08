"""The /hl slash command: status by default; start, stop and restart control the collector.

The handler is synchronous, so Hermes runs it in the gateway executor (docs:
'Sync handlers run in the gateway executor'), never on the event loop; the
start/stop work (a file lock and a thread join) stays off the loop.
"""

from __future__ import annotations

from .runtime import get_runtime

USAGE = ("Usage: /hl [status|start|stop|restart|help] or /hl replay start [file]|pause|resume|seek <ISO time or ms>|"
         "speed <x>|restart|stop\n"
         "  /hl or /hl status  collector state, then provenance and freshness per market\n"
         "  /hl start          start the live collector (public Hyperliquid websocket + info endpoint)\n"
         "  /hl stop           stop it and close the connection\n"
         "  /hl restart        stop, then start (the restart is recorded as a gap)")


def _fmt_status(rt) -> str:
    st = rt.status()
    c = st["collector"]
    lines = [f"hyperliquid-desk collector: {c.get('state', 'stopped')}"
             + (f" (conn {c.get('conn')}, reconnects {c.get('reconnects')})" if c.get("conn") else "")]
    if c.get("last_error"):
        lines.append(f"  last error: {c['last_error']}; next retry in {c.get('next_retry_s')} s")
    if c.get("owner_service_error"):
        lines.append(f"  owner service: {c['owner_service_error']}")
    snap, err = rt.snapshot(None)
    if snap is None:
        lines.append(f"  {err}")
        return "\n".join(lines)
    lines.append(f"snapshot {snap['snapshot_id']} at {snap['built_at']} · mode {snap['mode']}")
    for name, ms in sorted(snap["markets"].items()):
        fr = ms["freshness"]
        cov = ms["provenance"]["coverage"]["trades"]
        parts = [f"{k}={fr[k]['status']}" for k in ("book_fast", "book_full", "trades", "ctx", "bbo")]
        lines.append(f"  {name:<11} " + " ".join(parts))
        lines.append(f"  {'':<11} trades since {cov.get('segment_start') or '-'} to {cov.get('watermark') or '-'}"
                     f" · gaps {len(cov.get('gaps') or [])}")
    for u in snap.get("unsupported_markets", []):
        lines.append(f"  unsupported: {u['market']} ({u['reason']})")
    lines.append("Node panels: needs node mode. Freshness is separate from provenance; null values carry their reason.")
    return "\n".join(lines)


def _replay(rt, parts: list[str]) -> str:
    if not parts:
        return USAGE
    cmd, arg = parts[0].lower(), (" ".join(parts[1:]) or None)
    if cmd == "seek" and arg:
        arg = _parse_time_ms(arg)
        if arg is None:
            return "seek needs a time: an ISO UTC time (2026-11-07T22:48:30Z) or milliseconds since the epoch"
    res = rt.replay(cmd, arg)
    if not res.get("ok"):
        return f"replay {cmd}: {res.get('error')}"
    st = res.get("replay")
    if st is None:
        return "replay stopped; live data is unaffected"
    from .util import iso_ms
    return (f"REPLAY {st['state']} · {st.get('file')} · position {iso_ms(st.get('position_ms'))} of "
            f"{iso_ms(st.get('recording_start_ms'))} to {iso_ms(st.get('recording_end_ms'))} · speed {st.get('speed')}x · "
            f"replay session {st.get('replay_session')} · partial history: {st.get('partial_history')}. "
            "Replay data is shown apart from live data and is never current.")


def _parse_time_ms(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def handle_hl(raw_args: str = "", **_kwargs) -> str:
    try:
        arg = (raw_args or "").strip().lower()
        rt = get_runtime()
        if arg in ("", "status"):
            return _fmt_status(rt)
        if arg == "start":
            res = rt.start()
            return res["action"]["message"] + "\n" + _fmt_status(rt)
        if arg == "stop":
            res = rt.stop()
            return res["action"]["message"]
        if arg == "restart":
            res = rt.restart()
            return res["action"]["message"] + "\n" + _fmt_status(rt)
        if arg.startswith("replay"):
            return _replay(rt, (raw_args or "").strip().split()[1:])
        return USAGE
    except Exception as exc:
        return f"/hl failed: {type(exc).__name__}: {exc}"
