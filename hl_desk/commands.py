"""The /hl slash command: status by default; start, stop and restart control the collector.

The handler is synchronous, so Hermes runs it in the gateway executor (docs:
'Sync handlers run in the gateway executor'), never on the event loop; the
start/stop work (a file lock and a thread join) stays off the loop.
"""

from __future__ import annotations

from .runtime import get_runtime

USAGE = ("Usage: /hl [status|start|stop|restart|help]\n"
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
        return USAGE
    except Exception as exc:
        return f"/hl failed: {type(exc).__name__}: {exc}"
