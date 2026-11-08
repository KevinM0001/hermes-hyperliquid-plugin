"""Agent tools. Each returns a JSON string and never raises.

Every result carries provenance (mode, source, market, snapshot ID,
coverage) with the freshness status of the fields it used right next to
it, and lists what is unavailable and why. Tools read the same published
snapshot as the dashboard tab; pass snapshot_id to read a specific one.
"""

from __future__ import annotations

import json
import re

from . import schema
from .clock import ClockBound
from .util import dex_of, fnum, iso_ms, na, val

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DISCLAIMER = "Public Hyperliquid data. Descriptive only, not a trade recommendation."

_MARKET_PROP = {"type": "string", "description": "Market name exactly as Hyperliquid lists it, e.g. 'BTC', 'HYPE', 'xyz:SP500', 'xyz:XYZ100'."}
_SNAP_PROP = {"type": "string", "description": "Optional snapshot ID to read a specific retained snapshot (so chat and the dashboard tab can cite the same numbers). Omit for the latest."}

SCHEMAS = {
    "hl_market": {
        "name": "hl_market",
        "description": ("Hyperliquid order book for one market from public data: best bid/ask, spread, the returned aggregated "
                        "levels with their API count, and depth within ±0.25/0.5/1% only where the returned levels reach the band "
                        "(otherwise 'observed depth within returned levels'). Includes provenance, snapshot ID and freshness. "
                        "Levels are aggregated, never individual orders."),
        "parameters": {"type": "object", "properties": {"market": _MARKET_PROP, "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
    "hl_flow": {
        "name": "hl_flow",
        "description": ("Aggressive trade flow for one Hyperliquid market over the observed window (since the collector started or "
                        "the last gap, at most 15 minutes): buy and sell notional by aggressor side, net, volume, 30-second "
                        "cumulative delta, large prints and the recent tape, with the window's actual start and end."),
        "parameters": {"type": "object", "properties": {"market": _MARKET_PROP, "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
    "hl_context": {
        "name": "hl_context",
        "description": ("Market context for one Hyperliquid perp: mark, oracle, funding (per 1 hour), open interest (coin and USD) "
                        "and day notional volume, with units and freshness."),
        "parameters": {"type": "object", "properties": {"market": _MARKET_PROP, "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
    "hl_status": {
        "name": "hl_status",
        "description": ("Status of the hyperliquid-desk collector and every followed market: provenance (LIVE/REPLAY/SYNTHETIC), "
                        "freshness of each feed, coverage and gaps, clock skew, unsupported markets, and which panels need node mode."),
        "parameters": {"type": "object", "properties": {"market": {"type": "string", "description": "Optional: one market only."},
                                                        "snapshot_id": _SNAP_PROP}},
    },
    "hl_alerts": {
        "name": "hl_alerts",
        "description": ("The Flow Deck for Hyperliquid markets (ported from the author's terminal Flow Deck, hl_deck.py): each market's current regime "
                        "(e.g. ABSORBING, VACUUM-DOWN) with its plain-language meaning, when it started and how long it has held, "
                        "the 90 s window's move, net flow, volume, IMPACT, PRESSURE and top wallet, plus the events list of regime "
                        "changes and burst alerts (aggressive buying or selling over a per-market threshold within 60 s) with the "
                        "wallets behind each burst."),
        "parameters": {"type": "object", "properties": {"market": {"type": "string", "description": "Optional: one market only."},
                                                        "snapshot_id": _SNAP_PROP}},
    },
    "hl_wallets": {
        "name": "hl_wallets",
        "description": ("Which public addresses drive a Hyperliquid market's volume over the last 1, 5 or 15 minutes: buyers ranked "
                        "by bought notional and sellers by sold notional, listed until they reach 50% of their side or 50 wallets, "
                        "with shares, net bought and the aggressive part. Pass address to open one wallet: its trades in the same "
                        "window (which add up to its totals) and its current positions and open orders on the market's dex."),
        "parameters": {"type": "object", "properties": {
            "market": _MARKET_PROP,
            "window_min": {"type": "integer", "enum": [1, 5, 15], "description": "Window length in minutes (default 5)."},
            "address": {"type": "string", "description": "Optional 0x address to open (drill-down)."},
            "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
}


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _error(tool: str, message: str, **extra) -> str:
    return _dumps({"tool": tool, "ok": False, "error": message, "provenance": None,
                   "disclaimer": DISCLAIMER, **extra})


def _status_line(ms: dict, keys: list[str]) -> dict:
    fr = ms["freshness"]
    fields = {k: {"status": fr[k]["status"], "reason": fr[k]["reason"], "source_time": fr[k]["source_time"],
                  "progress_age_s": fr[k]["progress_age_s"], "max_age_s": fr[k]["max_age_s"]} for k in keys}
    statuses = {v["status"] for v in fields.values()}
    overall = "fresh" if statuses == {"fresh"} else ("partial" if "fresh" in statuses else sorted(statuses)[0])
    return {"overall": overall, "fields": fields}


def _served(snap: dict) -> dict:
    """How old the snapshot was when served. A snapshot read by ID is historical: its freshness statuses are
    as of its build time, not now."""
    served = snap.get("served")
    if not served:
        return {"served": None}
    out = {"served": served}
    if not served.get("latest"):
        out["historical"] = {"as_of": snap["built_at"], "age_s": served.get("age_s"),
                             "note": "historical snapshot: every status below is as of its build time, not now"}
    return out


def _envelope(tool: str, snap: dict, ms: dict, keys: list[str], body: dict) -> dict:
    out = {"tool": tool, "ok": True,
           "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"], "mode": snap["mode"],
           **_served(snap),
           "provenance": ms["provenance"], "status": _status_line(ms, keys)}
    out.update(body)
    out["unavailable"] = [u for u in ms["unavailable"] if any(u["field"] == ms["freshness"][k]["field"] for k in keys)]
    out["schema"] = snap["schema"]
    out["disclaimer"] = DISCLAIMER
    return out


def _market(snap: dict, market: str | None) -> tuple[dict | None, str | None]:
    if not market:
        return None, "market is required"
    ms = snap["markets"].get(market)
    if ms is None:
        supported = sorted(snap["markets"])
        unsupported = [u for u in snap.get("unsupported_markets", []) if u["market"] == market]
        why = unsupported[0]["reason"] if unsupported else "not followed or unknown to Hyperliquid's metadata"
        return None, f"market '{market}' is unsupported ({why}); supported: {supported}"
    return ms, None


def hl_market(snap: dict, args: dict, rt=None) -> dict:
    ms, err = _market(snap, args.get("market"))
    if err:
        raise LookupError(err)
    b = ms["book"]
    return _envelope("hl_market", snap, ms, ["book_fast", "book_full", "book_sf4", "book_sf3", "bbo"], {
        "top_of_book": b["top"], "levels": b["ladder"], "depth": b["depth"], "bbo": b["bbo"],
        "notes": ["levels are aggregated price levels, never individual orders",
                  "'API count' is Hyperliquid's n; the docs call it 'number of levels' (info l2Book) and 'number of orders' (websocket)"]})


def hl_flow(snap: dict, args: dict, rt=None) -> dict:
    ms, err = _market(snap, args.get("market"))
    if err:
        raise LookupError(err)
    f = ms["flow"]
    cvd = dict(f["cvd"])
    full = cvd["buckets"]
    shown = full[-12:]
    prior = full[:len(full) - len(shown)]
    complete_prior = [b for b in prior if b.get("complete")]
    if cvd.get("reason"):
        # The series is withheld (for example trades unverified): the earlier buckets exist, but their values,
        # and so the base, are unknown; never 0.
        base = na(cvd["reason"], "USD")
    elif complete_prior:
        base = val(complete_prior[-1]["cumulative_usd"], "USD")
    else:
        base = val(0.0, "USD") | {"note": "no complete bucket precedes the buckets shown"}
    cvd["buckets"] = shown
    cvd["slice"] = {
        "buckets_in_snapshot": len(full), "buckets_shown": len(shown),
        "cumulative_base_usd": base,
        "note": ("cumulative_usd counts from cumulative_from (the snapshot's first complete bucket), not from the "
                 "first bucket shown here; cumulative_base_usd is the cumulative value just before the buckets shown"),
    }
    return _envelope("hl_flow", snap, ms, ["trades"], {
        "window": f["window"], "values": f["values"], "cvd_last_6_minutes": cvd,
        "large_prints": f["large_prints"], "tape": f["tape"][:20],
        "notes": ["side follows the docs: B = buyer aggressed, A = seller aggressed",
                  "each trade counted once, identified by (coin, time, tid)",
                  "the window covers only what was observed since the collector started or the last gap"]})


def hl_context(snap: dict, args: dict, rt=None) -> dict:
    ms, err = _market(snap, args.get("market"))
    if err:
        raise LookupError(err)
    return _envelope("hl_context", snap, ms, ["ctx"], {"context": ms["context"], "meta": ms["meta"]})


def hl_status(snap: dict, args: dict, rt=None) -> dict:
    names = [args["market"]] if args.get("market") else sorted(snap["markets"])
    markets = {}
    for name in names:
        ms, err = _market(snap, name)
        if err:
            raise LookupError(err)
        markets[name] = {
            "provenance": ms["provenance"],
            "status": _status_line(ms, list(ms["freshness"])),
            "freshness": ms["freshness"],
            "coverage": {"trades": ms["provenance"]["coverage"]["trades"]},
            "clock": ms["clock"],
            "panels_needing_node_mode": ms["node"]["panels"],
        }
    return {"tool": "hl_status", "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            "mode": snap["mode"], **_served(snap), "collector": snap["collector"], "markets": markets,
            "unsupported_markets": snap.get("unsupported_markets", []), "metadata": snap.get("metadata"),
            "schema": snap["schema"], "disclaimer": DISCLAIMER}


def hl_alerts(snap: dict, args: dict, rt=None) -> dict:
    names = [args["market"]] if args.get("market") else sorted(snap["markets"])
    cards, events = {}, []
    for name in names:
        ms, err = _market(snap, name)
        if err:
            raise LookupError(err)
        cards[name] = {"provenance": ms["provenance"], "status": _status_line(ms, ["trades"]),
                       "card": ms["deck"]["card"]}
        events.extend(ms["deck"]["events"])
    events.sort(key=lambda e: e["at_ms"], reverse=True)
    reg = schema.load()["regimes"]
    return {"tool": "hl_alerts", "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            "mode": snap["mode"], **_served(snap), "cards": cards, "events": events[:30],
            "definitions": {"regime_order": [r["state"] + ": " + r["when"] for r in reg["rules"]],
                            "latch": reg["latch"], "gap_rule": reg["gap_rule"],
                            "burst": schema.load()["alerts"]["burst"]["formula"],
                            "source": "schema/metrics.v1.json regimes and alerts (ported from hl_deck.py)"},
            "schema": snap["schema"], "disclaimer": DISCLAIMER}


def hl_wallets(snap: dict, args: dict, rt=None) -> dict:
    ms, err = _market(snap, args.get("market"))
    if err:
        raise LookupError(err)
    n = args.get("window_min", 5)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n not in (1, 5, 15):
        raise ValueError("window_min must be 1, 5 or 15")
    panel = ms["wallets"][f"{n}m"]
    body = {"window_min": n, "panel": panel}
    addr = args.get("address")
    if addr:
        body["wallet"] = open_wallet(snap, ms, n, addr, rt)
    return _envelope("hl_wallets", snap, ms, ["trades"], body)


def open_wallet(snap: dict, ms: dict, n: int, addr: str, rt) -> dict:
    """Drill-down: the wallet's trades from the panel's own execution set, plus its current state on this dex."""
    if not ADDRESS_RE.match(addr or ""):
        return {"error": "address must be 0x followed by 40 hex characters"}
    addr = addr.lower()
    panel = ms["wallets"][f"{n}m"]
    out: dict = {"address": addr, "note": "an address, not an identified owner"}
    if not panel.get("window"):
        out["trades"] = {"error": panel.get("reason", "window unavailable")}
    elif rt is None or not hasattr(rt, "wallet_trades"):
        out["trades"] = {"error": "no state owner to read this snapshot's execution set from"}
    else:
        out["trades"] = rt.wallet_trades(snap["snapshot_id"], ms["market"], n, addr)
    dex = dex_of(ms["market"])
    fetch = getattr(rt, "fetch_wallet_current", None)
    if fetch is None:
        out["current"] = {"error": "current positions and orders aren't available in this context"}
    else:
        try:
            cur = fetch(addr, dex)
            bound = rt.clock_bound() if hasattr(rt, "clock_bound") else ClockBound().get()
            out["current"] = current_account(cur, dex, bound)
        except Exception as exc:
            out["current"] = {"error": f"info request failed: {exc!r}"}
    return out


ACCOUNT_SOURCE_MAX_AGE_S = 10.0


def _num(d: dict, key: str, unit: str, reason: str | None):
    if reason:
        return na(reason, unit)
    v = fnum(d.get(key)) if isinstance(d, dict) else None
    return val(v, unit) if v is not None else na(f"{key} missing from the answer", unit)


def account_freshness(state: dict | None, assessed_ms: int | None, bound: tuple) -> dict:
    """Schema freshness.fields.clearinghouseState: the answer's own time must be recent under the clock bound
    when it is served (assessed_ms: both answers in hand), so request latency counts as age."""
    b, basis = bound
    src = state.get("time") if isinstance(state, dict) else None
    src = src if isinstance(src, int) and not isinstance(src, bool) else None
    out = {"source_time_ms": src, "source_time": iso_ms(src) if src is not None else None,
           "assessed_at": iso_ms(assessed_ms) if assessed_ms is not None else None,
           "source_max_age_s": ACCOUNT_SOURCE_MAX_AGE_S, "clock_bound_s": b, "clock_bound_basis": basis}
    if assessed_ms is None:
        return out | {"status": "unverified", "reason": "the time both answers were in hand wasn't recorded"}
    if src is None:
        return out | {"status": "unverified", "reason": "the account answer has no time field, so its source age is unknown"}
    age = (assessed_ms - src) / 1000
    out["source_age_s"] = round(age, 3)
    if b is None:
        return out | {"status": "unverified", "reason": f"source recency can't be established: {basis}"}
    if age < -(b + 1.0):
        return out | {"status": "unverified", "reason": "the answer's time is ahead of local time beyond the clock bound"}
    if age - b > ACCOUNT_SOURCE_MAX_AGE_S:
        return out | {"status": "delayed", "reason": f"the answer's time was {age:.1f} s old when served (limit {ACCOUNT_SOURCE_MAX_AGE_S} s)"}
    if age + b > ACCOUNT_SOURCE_MAX_AGE_S:
        return out | {"status": "unverified", "reason": f"source age {age:.1f} s is within the clock bound of its limit"}
    return out | {"status": "fresh", "reason": None}


def current_account(cur: dict, dex: str, bound: tuple) -> dict:
    """A wallet's current positions and open orders, under the same freshness and null rules as everything else."""
    state = cur.get("clearinghouseState")
    assessed = cur.get("returned_ms")
    fr = account_freshness(state, assessed, bound)
    why = None if fr["status"] == "fresh" else f"account state {fr['status']}: {fr['reason']}"
    ms = (state or {}).get("marginSummary") if isinstance(state, dict) else None
    if why is None and not isinstance(ms, dict):
        msr = "marginSummary missing from the answer"
    else:
        msr = why
    margin = {"account_value_usd": _num(ms or {}, "accountValue", "USD", msr),
              "total_position_notional_usd": _num(ms or {}, "totalNtlPos", "USD", msr),
              "total_margin_used_usd": _num(ms or {}, "totalMarginUsed", "USD", msr)}
    if why:
        positions = na(why)
    elif not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
        positions = na("assetPositions missing from the answer, so the positions are unknown (not none)")
    else:
        positions = val([_position(p.get("position", p) if isinstance(p, dict) else {})
                         for p in state["assetPositions"]])
    orders_at = cur.get("orders_received_ms")
    orders_reason = ("the open-orders answer carries no response timestamp, so whether this set is current can't "
                     f"be verified (received at {iso_ms(orders_at) if orders_at is not None else 'an unrecorded time'}); "
                     "an order's placement time doesn't establish it")
    times = {k: iso_ms(cur[k + "_ms"]) if isinstance(cur.get(k + "_ms"), int) else None
             for k in ("requested", "state_received", "orders_received", "returned")}
    return {
        "label": "current account state, not part of the snapshot",
        "fetched_at": times["returned"],
        "times": times,
        "dex": dex or "(default perp dex)",
        "coverage": (f"this answer covers only the '{dex}' dex" if dex else
                     "this answer covers only the default perp dex, not HIP-3 dexes"),
        "source": "info clearinghouseState (positions, margin) and frontendOpenOrders (orders)",
        "account": {"freshness": fr, "margin_summary": margin, "positions": positions},
        "open_orders": {"freshness": {"status": "unverified", "reason": orders_reason},
                        "orders": na(orders_reason), "count": na(orders_reason)},
    }


def _position(p: dict) -> dict:
    lev = p.get("leverage")
    lev_out = (val(fnum(lev.get("value")), "x") | {"type": lev.get("type")} if isinstance(lev, dict) and fnum(lev.get("value")) is not None
               else na("leverage missing from the answer", "x"))
    liq = fnum(p.get("liquidationPx"))
    return {"coin": p.get("coin"),
            "size_coin": _num(p, "szi", "coin (signed: positive long, negative short)", None),
            "entry_px": _num(p, "entryPx", "USD", None),
            "position_value_usd": _num(p, "positionValue", "USD", None),
            "unrealized_pnl_usd": _num(p, "unrealizedPnl", "USD", None),
            "margin_used_usd": _num(p, "marginUsed", "USD", None),
            "liquidation_px": (val(liq, "USD") if liq is not None else
                               na("no liquidation price in the answer (Hyperliquid returns none for some positions)", "USD")),
            "leverage": lev_out}


def drilldown(snap: dict, frozen: dict | None, market: str, window_min, address: str) -> dict:
    """A wallet's executions from exactly the execution set the snapshot's panel was built from.

    `frozen` is the snapshot's retained {market: (segment_start_ms, trades)}; executions inside
    the panel's (start, end] window add up to that wallet's panel totals.
    """
    from .wallets import wallet_trades
    ms = snap["markets"].get(market)
    if ms is None:
        return {"error": f"market {market} isn't in snapshot {snap['snapshot_id']}"}
    panel = ms["wallets"].get(f"{window_min}m") or {}
    win = panel.get("window")
    if not win:
        return {"error": panel.get("reason", "window unavailable")}
    if not frozen or market not in frozen:
        return {"error": f"snapshot {snap['snapshot_id']}'s execution set is no longer retained"}
    _seg, trades = frozen[market]
    execs = [t for t in trades if win["start_exclusive_ms"] < t.time <= win["end_ms"]]
    return wallet_trades(execs, address) | {"window": win, "execution_set": f"frozen with snapshot {snap['snapshot_id']}"}


HANDLERS = {"hl_market": hl_market, "hl_flow": hl_flow, "hl_context": hl_context, "hl_status": hl_status,
            "hl_alerts": hl_alerts, "hl_wallets": hl_wallets}


def run_tool(name: str, args: dict | None, rt) -> str:
    """Resolve the snapshot and run one tool. Always returns a JSON string; never raises."""
    try:
        args = dict(args or {})
        snap, err = rt.snapshot(args.get("snapshot_id"))
        if snap is None:
            return _error(name, err or "no snapshot available", collector=rt.status().get("collector"))
        return _dumps(HANDLERS[name](snap, args, rt))
    except (LookupError, ValueError) as exc:
        return _error(name, str(exc).strip("'\""))
    except Exception as exc:  # never let a tool call fail with an exception
        return _error(name, f"internal error: {type(exc).__name__}: {exc}")
