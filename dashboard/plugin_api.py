"""Dashboard backend routes for the hyperliquid-desk tab, mounted at /api/plugins/hyperliquid-desk/.

The tab computes nothing itself: every route returns the same published
snapshot the agent tools read, so chat and screen show the same numbers
for the same snapshot ID. Handlers are async; anything that blocks (start,
stop, a file read, an info request) runs in asyncio.to_thread so the
gateway's event loop is never held.

Hermes imports this file as its own module, so it finds the plugin's
already-loaded engine (hermes_plugins.<slug>.hl_desk) by file path, or
imports it once if this process never loaded the plugin. Either way it
talks to the profile's single state owner: in memory when this process owns
the collector, otherwise through the owner service (hl_desk.owner).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()

_ENGINE_DIR = (Path(__file__).resolve().parent.parent / "hl_desk").resolve()


def _loaded(name: str):
    for mod_name, mod in list(sys.modules.items()):
        if not (mod_name == f"hl_desk.{name}" or mod_name.endswith(f".hl_desk.{name}")):
            continue
        f = getattr(mod, "__file__", None)
        if f and Path(f).resolve().parent == _ENGINE_DIR:
            return mod
    return None


def _engine(name: str):
    """The engine submodule (runtime or tools) for this plugin directory.

    Prefer the copy Hermes loaded with the plugin (same process as the tools). A
    dashboard process that never ran the plugin's register() imports the engine
    itself; the engine's owner lock and thread adoption still keep one collector
    per profile, and a second copy reads the owner's snapshots.
    """
    mod = _loaded(name)
    if mod is not None:
        return mod
    try:
        import importlib
        root = str(_ENGINE_DIR.parent)
        if root not in sys.path:
            sys.path.append(root)
        mod = importlib.import_module(f"hl_desk.{name}")
        if name == "runtime":
            _apply_settings(mod)
        return mod
    except Exception:
        return None


_ENGINE_CACHE: dict = {}


async def _engine_async(name: str):
    """_engine() off the event loop: the lookup resolves paths and may import the engine and read
    config, so it runs in a worker thread once; later requests use the cached module."""
    mod = _ENGINE_CACHE.get(name)
    if mod is None:
        mod = await asyncio.to_thread(_engine, name)
        if mod is not None:
            _ENGINE_CACHE[name] = mod
    return mod


def _apply_settings(runtime_mod) -> None:
    """Read this plugin's settings the documented way for plugin_api.py (load_config_readonly)."""
    try:
        from hermes_cli.config import load_config_readonly
        settings = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(
            "hyperliquid-desk", {}).get("settings") or {}
        runtime_mod.set_settings_getter(lambda key, default=None: settings.get(key, default))
    except Exception:
        pass


def _not_loaded() -> dict:
    return {"ok": False, "error": "the hyperliquid-desk plugin isn't loaded in this process; enable it with "
                                  "'hermes plugins enable hyperliquid-desk' and restart, or use /hl in chat"}


async def _snapshot(snapshot_id: str | None):
    rt_mod = await _engine_async("runtime")
    if rt_mod is None:
        return None, "the hyperliquid-desk engine couldn't be loaded in this process"
    # In-memory for the owner; otherwise a request to the owner process over its socket: off the loop either way.
    return await asyncio.to_thread(rt_mod.get_runtime().snapshot, snapshot_id)


def _deck_summary(snap: dict) -> dict:
    out = {}
    for name, ms in snap["markets"].items():
        out[name] = {"provenance": ms["provenance"], "trades_freshness": ms["freshness"]["trades"],
                     "book_fast_freshness": ms["freshness"].get("book_fast"),     # the watch row's price source
                     "card": ms["deck"]["card"], "events": ms["deck"]["events"], "bursts": ms["deck"].get("bursts"),
                     "watch": ms["deck"].get("watch")}
    return out


@router.get("/snapshot")
async def get_snapshot(market: str | None = Query(default=None), snapshot_id: str | None = Query(default=None),
                       node: str | None = Query(default=None)):
    snap, err = await _snapshot(snapshot_id)
    if node == "visible" and snap is not None:
        # The tab polls only while it is visible: node data from a relay flows while a node panel is on screen and
        # for 60 s after (handoff 1d step 7). Off the loop, like every runtime call.
        names_ = snap.get("markets") or {}
        open_market = market if market in names_ else (sorted(names_)[0] if names_ else None)
        rt_mod = await _engine_async("runtime")
        if rt_mod is not None:
            await asyncio.to_thread(rt_mod.get_runtime().node_panel, open_market)
    if snap is None:
        rt_mod = await _engine_async("runtime")
        status = await asyncio.to_thread(rt_mod.get_runtime().status) if rt_mod else None
        return {"ok": False, "error": err, "runtime": status}
    names = sorted(snap["markets"])
    chosen = market if market in snap["markets"] else (names[0] if names else None)
    return {
        "ok": True,
        "snapshot_id": snap["snapshot_id"], "built_at": snap["built_at"], "built_at_ms": snap["built_at_ms"],
        "served": snap.get("served"),
        "mode": snap["mode"], "plugin_version": snap["plugin_version"], "schema": snap["schema"],
        "collector": snap["collector"], "markets": names, "unsupported_markets": snap.get("unsupported_markets", []),
        "market": snap["markets"][chosen] if chosen else None,
        "deck": _deck_summary(snap),
        "node_status": snap.get("node_status"),
    }


@router.get("/status")
async def get_status():
    rt_mod = await _engine_async("runtime")
    if rt_mod is None:
        return _not_loaded()
    status = await asyncio.to_thread(rt_mod.get_runtime().status)
    return {"ok": True, "runtime": status}


async def _control(action: str):
    rt_mod = await _engine_async("runtime")
    if rt_mod is None:
        return _not_loaded()
    rt = rt_mod.get_runtime()
    res = await asyncio.to_thread(getattr(rt, action))
    return {"ok": res["action"]["ok"], "result": res}


@router.post("/collector/start")
async def collector_start():
    return await _control("start")


@router.post("/collector/stop")
async def collector_stop():
    return await _control("stop")


@router.post("/collector/restart")
async def collector_restart():
    return await _control("restart")


REPLAY_COMMANDS = ("start", "pause", "resume", "seek", "speed", "restart", "stop")


@router.post("/node/replay/{cmd}")
async def node_replay(cmd: str, arg: str | None = Query(default=None)):
    """Explicit replay control (never automatic). seek takes milliseconds since the epoch; speed a positive number;
    start an optional recording path (default: the node_replay_file setting)."""
    if cmd not in REPLAY_COMMANDS:
        return {"ok": False, "error": f"unknown replay command {cmd!r}; one of {', '.join(REPLAY_COMMANDS)}"}
    value = arg
    try:
        if cmd == "seek":
            value = int(arg) if arg is not None else None
            if value is None:
                return {"ok": False, "error": "seek needs arg: milliseconds since the epoch"}
        elif cmd == "speed":
            value = float(arg) if arg is not None else None
            if value is None or value <= 0:
                return {"ok": False, "error": "speed needs a positive arg"}
    except ValueError:
        return {"ok": False, "error": f"invalid arg {arg!r} for {cmd}"}
    rt_mod = await _engine_async("runtime")
    if rt_mod is None:
        return _not_loaded()
    return await asyncio.to_thread(rt_mod.get_runtime().replay, cmd, value)


@router.get("/wallets")
async def get_wallets(market: str, window_min: int = 5, address: str | None = None,
                      snapshot_id: str | None = None):
    """The same function as the hl_wallets tool; opening a wallet makes info requests, so it runs in a thread."""
    tools_mod, rt_mod = await _engine_async("tools"), await _engine_async("runtime")
    if tools_mod is None or rt_mod is None:
        return _not_loaded()
    import json
    args = {"market": market, "window_min": window_min, "snapshot_id": snapshot_id}
    if address:
        args["address"] = address
    raw = await asyncio.to_thread(tools_mod.run_tool, "hl_wallets", args, rt_mod.get_runtime())
    return json.loads(raw)


@router.get("/candles")
async def get_candles(market: str, interval: str, limit: int = 500):
    """Candle history for one market and interval (1m 5m 15m 30m 1h 4h 1d; limit default 500, max 5000): closed candles
    from candleSnapshot, cached per (market, interval) and refetched only when the newest closed candle is older than
    one interval (at most once per 30 s), plus the in-progress candle from the websocket, served unverified with its
    receipt age. {ok: false, state: "loading"} while the first fetch runs (ask again), {ok: false, state: "error",
    reason} after a failed one; an unknown market or interval is the usual error shape with HTTP 400. The fetch runs on
    the owner's worker threads, never on this loop or the collector's."""
    rt_mod = await _engine_async("runtime")
    if rt_mod is None:
        return _not_loaded()
    res = await asyncio.to_thread(rt_mod.get_runtime().candles, market, interval, limit)
    if not res.get("ok") and "error" in res and "state" not in res:
        return JSONResponse(status_code=400, content=res)
    return res


@router.get("/tool/{name}")
async def run_tool(name: str, market: str | None = None, snapshot_id: str | None = None,
                   source: str | None = None):
    """Any read-only tool by name, for the tab's 'same as chat' check (source: the node tools' live or replay).
    Never starts the collector."""
    tools_mod, rt_mod = await _engine_async("tools"), await _engine_async("runtime")
    if tools_mod is None or rt_mod is None:
        return _not_loaded()
    if name not in tools_mod.HANDLERS:
        return {"ok": False, "error": f"unknown tool {name}"}
    import json
    args = {k: v for k, v in (("market", market), ("snapshot_id", snapshot_id), ("source", source)) if v}
    raw = await asyncio.to_thread(tools_mod.run_tool, name, args, rt_mod.get_runtime())
    return json.loads(raw)
