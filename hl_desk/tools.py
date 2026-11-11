"""Agent tools. Each returns a JSON string and never raises.

Every result carries provenance (mode, source, market, snapshot ID,
coverage) with the freshness status of the fields it used right next to
it, and lists what is unavailable and why. Tools read the same published
snapshot as the dashboard tab; pass snapshot_id to read a specific one.
"""

from __future__ import annotations

import json
import re
import time

from . import schema
from .candles import INTERVALS as CANDLE_INTERVALS
from .clock import ClockBound
from .structure import CANDLES_USED, DEFAULT_INTERVALS, structure as market_structure
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
                        "and day notional volume, with units and freshness. These values are always marked unverified "
                        "(status, reason, received_age_s): Hyperliquid's activeAssetCtx carries no source timestamp, so only "
                        "the receipt age of the last changed value is known; they are null with a reason when the collector "
                        "is stopped, disconnected or warming up, or when no changed value arrived within the max age."),
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
                        "the 90 s window's move, net flow, volume, IMPACT, PRESSURE and top wallet, its path (six 15 s points: time, "
                        "price, move, net flow) and dominated_by (whether one tick, print or address carries more than half of the "
                        "net flow or volume, with the share), each label's validation status, plus the events list of regime "
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
    "hl_candles": {
        "name": "hl_candles",
        "description": ("Candle history for one Hyperliquid market at one interval (1m, 5m, 15m, 30m, 1h, 4h, 1d) from the venue's "
                        "own candle data: the most recent closed candles (open, high, low, close, base volume, trade count; the "
                        "venue keeps at most the last 5000 per interval) and the in-progress candle, which is unverified (its "
                        "updates carry no source time) and comes with its receipt age. The first call for a market and interval "
                        "starts the fetch and answers state 'loading': call again a second later."),
        "parameters": {"type": "object", "properties": {
            "market": _MARKET_PROP,
            "interval": {"type": "string", "enum": list(CANDLE_INTERVALS), "description": "Candle interval (default 1h)."},
            "limit": {"type": "integer", "description": "How many of the most recent candles to return (default 100, max 500)."}},
            "required": ["market"]},
    },
    "hl_structure": {
        "name": "hl_structure",
        "description": ("Multi-timeframe read of one Hyperliquid market from the venue's own candles, computed here (numbers only, "
                        "no labels) so the answer is facts: for each interval (default 1d, 4h, 1h, 30m, 5m, 1m) over the most "
                        "recent closed candles (30 days, 42 four-hour, 48 one-hour, 48 thirty-minute, 60 five-minute, 60 "
                        "one-minute): the high and low with their times and where the last close sits between them "
                        "(position_pct: 0 at the low, 100 at the high), the change over 1, 3 and all bars, the streak of closes, "
                        "the last bar's range, body, direction and volume against the median, the largest bar, the volumes and "
                        "the mean bar range; plus the prior day's candle and whether the last price is above its high or below "
                        "its low. The in-progress candle is reported separately, unverified. The first call for a market starts "
                        "the candle fetches and waits up to 3 s; an interval still 'loading' is answered by calling again a "
                        "second later. Descriptive, not a forecast: use it to state what the data shows for and against each "
                        "direction."),
        "parameters": {"type": "object", "properties": {
            "market": _MARKET_PROP,
            "intervals": {"type": "array", "items": {"type": "string", "enum": list(CANDLE_INTERVALS)},
                          "description": "Intervals to read, any subset of 1m, 5m, 15m, 30m, 1h, 4h, 1d "
                                         "(default [\"1d\", \"4h\", \"1h\", \"30m\", \"5m\", \"1m\"])."}},
            "required": ["market"]},
    },
    "hl_watch": {
        "name": "hl_watch",
        "description": ("The swing-context block for Hyperliquid markets, computed by the plugin from its own data, for one "
                        "market or every followed market: the price and where it sits in the 24h range (the last 24 closed 1h "
                        "candles; the middle 35 to 65% is the band he identified as his loss-maker) and in the 7d range, taker "
                        "flow imbalance over 15m, 1h and 4h from minute bars of public trades, the 4h wave (the second half of "
                        "the four hours against the first: accelerating, decelerating or flat), taker notional per hour, the "
                        "hour's top wallet share, and a verdict badge by his own rule (MIDDLE, LOW or HIGH, + FADING or + "
                        "BUILDING) with a plain-English gloss. Every window says how many minutes it covers and whether it spans "
                        "a gap; the parts built on trades or on the fast book are null with the reason when those aren't fresh; "
                        "a range says when its candles are still loading. Descriptions by that rule, not a forecast."),
        "parameters": {"type": "object", "properties": {"market": {"type": "string", "description": "Optional: one market only."},
                                                        "snapshot_id": _SNAP_PROP}},
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
                  "each depth band's approximate value names its own book and bucket width (approximate.<side>.resolution); "
                  "bands and sides can use different books",
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
        "notes": ["every number here covers window.label's window only; hl_wallets and the Flow Deck use their own "
                  "windows, so never combine their counts or volumes with these",
                  "side follows the docs: B = buyer aggressed, A = seller aggressed",
                  "each trade counted once, identified by (coin, time, tid)",
                  "the window covers only what was observed since the collector started or the last gap"]})


def hl_context(snap: dict, args: dict, rt=None) -> dict:
    ms, err = _market(snap, args.get("market"))
    if err:
        raise LookupError(err)
    return _envelope("hl_context", snap, ms, ["ctx"], {
        "context": ms["context"], "meta": ms["meta"],
        "notes": ["context values are served marked unverified (status, reason, received_age_s): activeAssetCtx carries "
                  "no source timestamp, so only the receipt age of the last changed context is known; quote them as "
                  "unverified, received that many seconds ago",
                  "a context value is null with its reason when the collector is stopped, disconnected or warming up, "
                  "or when no changed context arrived within the max age (a dead stream never shows old numbers)"]})


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
                       "card": ms["deck"]["card"], "bursts": ms["deck"].get("bursts")}
        events.extend(ms["deck"]["events"])
    events.sort(key=lambda e: e["at_ms"], reverse=True)
    reg = schema.load()["regimes"]
    return {"tool": "hl_alerts", "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            "mode": snap["mode"], **_served(snap), "cards": cards, "events": events[:30],
            "definitions": {"regime_order": [r["state"] + ": " + r["when"] for r in reg["rules"]],
                            "latch": reg["latch"], "gap_rule": reg["gap_rule"],
                            "burst": schema.load()["alerts"]["burst"]["formula"],
                            "node_burst": {k: schema.load()["alerts"]["burst"]["node_mode"][k]
                                           for k in ("applies", "rule", "incomplete_windows", "rejected_records",
                                                     "threshold", "rank")},
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


def hl_candles(snap: dict, args: dict, rt=None) -> dict:
    """The same answer as the tab's GET candles (hl_desk.candles): the venue's closed candles and the in-progress candle,
    read from the collector's candle store at serving time, not from the snapshot. Chat gets at most 500 candles."""
    market, interval = args.get("market"), args.get("interval") or "1h"
    if not market:
        raise ValueError("market is required")
    if interval not in CANDLE_INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(CANDLE_INTERVALS)}")
    limit = args.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be a whole number from 1 to 500")
    if rt is None or not hasattr(rt, "candles"):
        res = {"ok": False, "state": "error", "market": market, "interval": interval,
               "reason": "candles aren't available from this runtime (no collector with a candle store)"}
    else:
        res = rt.candles(market, interval, limit)
        if not res.get("ok") and "error" in res and "state" not in res:
            raise ValueError(res["error"])
    # The snapshot's own served age and historical label stay on the answer, as on every tool's; the candles themselves
    # aren't part of the snapshot.
    return {"tool": "hl_candles", **res, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            **_served(snap), "schema": snap["schema"], "disclaimer": DISCLAIMER,
            "note": ("candles come from the venue's candleSnapshot and candle channel when this is served, not from the "
                     "snapshot (snapshot_id is the desk's state at the call); the in-progress candle is unverified: say "
                     "'received N s ago' from in_progress.received_age_s")}


STRUCTURE_WAIT_S, STRUCTURE_POLL_S = 3.0, 0.25      # how long hl_structure waits for first fetches, and how often it asks
_sleep, _monotonic = time.sleep, time.monotonic     # replaced by the tests


def _candle_views(rt, market: str, intervals: list[str]) -> tuple[dict | None, dict | None]:
    """One store view per interval (the most recent N+1 candles), asked through the runtime as hl_candles does (local,
    or over the owner socket from another process), polled every STRUCTURE_POLL_S for up to STRUCTURE_WAIT_S while any
    first fetch is running. Returns (views, None), or (None, error) for a runtime-level error (the collector isn't
    running); raises ValueError for an unknown market."""
    deadline = _monotonic() + STRUCTURE_WAIT_S
    while True:
        views = {iv: rt.candles(market, iv, CANDLES_USED[iv] + 1) for iv in intervals}
        for v in views.values():
            if not v.get("ok") and "error" in v and "state" not in v:
                raise ValueError(v["error"])
            if v.get("state") == "error" and "failed_at" not in v:
                return None, {"ok": False, "state": "error", "market": market, "reason": v.get("reason")}
        left = deadline - _monotonic()
        if left <= 0 or not any(v.get("state") == "loading" for v in views.values()):
            return views, None
        _sleep(min(STRUCTURE_POLL_S, left))


def hl_structure(snap: dict, args: dict, rt=None) -> dict:
    """A deterministic multi-timeframe read of one market's candles (hl_desk.structure), from the same candle store as
    hl_candles and the tab, at serving time: per interval the statistics over the most recent closed candles with the
    in-progress candle apart, and the prior day against the last price. The first call starts the fetches and waits up
    to STRUCTURE_WAIT_S for them; an interval still loading says so and is answered on the next call."""
    market = args.get("market")
    if not market:
        raise ValueError("market is required")
    intervals = args.get("intervals")
    if intervals is None:
        intervals = list(DEFAULT_INTERVALS)
    elif isinstance(intervals, str):
        intervals = [s.strip() for s in intervals.split(",") if s.strip()]
    if (not isinstance(intervals, list) or not intervals or len(set(intervals)) != len(intervals)
            or any(iv not in CANDLE_INTERVALS for iv in intervals)):
        raise ValueError(f"intervals must be a list of distinct intervals from {', '.join(CANDLE_INTERVALS)}")
    if rt is None or not hasattr(rt, "candles"):
        res = {"ok": False, "state": "error", "market": market,
               "reason": "candles aren't available from this runtime (no collector with a candle store)"}
    else:
        views, err = _candle_views(rt, market, intervals)
        res = err if err is not None else market_structure(market, intervals, views)
    return {"tool": "hl_structure", **res, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            **_served(snap), "schema": snap["schema"], "disclaimer": DISCLAIMER,
            "note": ("computed from the venue's candles in the collector's candle store when this is served, not from the "
                     "snapshot (snapshot_id is the desk's state at the call); numbers only: say what they are, for and "
                     "against each direction, and never turn them into a forecast; an interval in state 'loading' is "
                     "answered by calling again in a second")}


def hl_watch(snap: dict, args: dict, rt=None) -> dict:
    """the swing-context block per market (hl_desk.watch), read from the snapshot's deck.<market>.watch: the same block
    the tab shows, with each market's provenance and the status of the fields it is built on (trades, the fast book)."""
    names = [args["market"]] if args.get("market") else sorted(snap["markets"])
    markets = {}
    for name in names:
        ms, err = _market(snap, name)
        if err:
            raise LookupError(err)
        markets[name] = {"provenance": ms["provenance"], "status": _status_line(ms, ["trades", "book_fast"]),
                         "watch": (ms.get("deck") or {}).get("watch")}
    m = schema.load()["metrics"]["deck.watch"]
    return {"tool": "hl_watch", "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            "mode": snap["mode"], **_served(snap), "markets": markets,
            "definitions": {k: m[k] for k in ("formula", "thresholds", "window", "completeness_rule")}
            | {"source": "schema/metrics.v1.json deck.watch (ported from a private swing-context page)"},
            "schema": snap["schema"], "disclaimer": DISCLAIMER,
            "note": ("descriptions by a fixed rule, not a forecast or a signal: say where the price sits, what the flow "
                     "and the wave measured and over how many covered minutes, which wallet led the hour, and what is "
                     "unavailable and why (a null part has its reason beside it as <part>_reason)")}


HANDLERS = {"hl_market": hl_market, "hl_flow": hl_flow, "hl_context": hl_context, "hl_status": hl_status,
            "hl_alerts": hl_alerts, "hl_wallets": hl_wallets, "hl_candles": hl_candles, "hl_structure": hl_structure,
            "hl_watch": hl_watch}


_NODE_SOURCE_PROP = {"type": "string", "enum": ["live", "replay"],
                     "description": "live (default) or replay: a replay is never mixed with live data and is labelled REPLAY."}

SCHEMAS.update({
    "node_status": {
        "name": "node_status",
        "description": ("Node mode status: the node feed connection (local socket or relay), its producer session, and for "
                        "each market the state of every family (book, triggers, depth, bands, node fills): LIVE, STALE, "
                        "BEHIND, UNKNOWN (clock skew), INVALID or REPLAY, with the source watermark, plus any replay's "
                        "recording time, position, speed and status."),
        "parameters": {"type": "object", "properties": {"market": {"type": "string", "description": "Optional: one market only."},
                                                        "source": _NODE_SOURCE_PROP, "snapshot_id": _SNAP_PROP}},
    },
    "node_book": {
        "name": "node_book",
        "description": ("Node-mode order book for one market from every resting order: ladder levels with total size, order "
                        "count, 'largest order size at this level' and 'oldest resting order at this level' (separate "
                        "statistics), changes in the last 10 s labelled only from lifecycle records (absorbed, pulled, unknown), "
                        "walls, the liquidity-thinning bands out to 10%, the order flow map (eight 15-minute buckets of node fills "
                        "split by the taker's own position effect, with its written readings) and whether the book is crossed "
                        "(then invalid). With provenance and freshness."),
        "parameters": {"type": "object", "properties": {"market": _MARKET_PROP, "source": _NODE_SOURCE_PROP,
                                                        "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
    "node_stops": {
        "name": "node_stops",
        "description": ("Node-mode waiting stop and take-profit orders (conditional interest) for one market, in price bands "
                        "around the mid: each band's order count beside its size, a flag when one order makes up most of a "
                        "band, whole-position orders of unknown size counted apart, and everything outside the range or "
                        "excluded (trailing stops) reported, never dropped. With provenance and freshness."),
        "parameters": {"type": "object", "properties": {"market": _MARKET_PROP, "source": _NODE_SOURCE_PROP,
                                                        "snapshot_id": _SNAP_PROP}, "required": ["market"]},
    },
})


def _node_view(snap: dict, args: dict) -> tuple[str, dict, str]:
    market = args.get("market")
    ms, err = _market(snap, market)
    if err:
        raise LookupError(err)
    source = (args.get("source") or "live").lower()
    if source not in ("live", "replay"):
        raise ValueError("source must be live or replay")
    node = ms.get("node") or {}
    view = node.get(source)
    if view is None:
        reason = ("no replay is running: start one with /hl replay start" if source == "replay"
                  else "needs node mode: no node feed configured (setting node_feed)")
        view = {"available": False, "reason": reason}
    return ms["market"], view, source


def _node_envelope(tool: str, snap: dict, market: str, view: dict, source: str, body: dict) -> dict:
    return {"tool": tool, "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            **_served(snap), "market": market, "source": source,
            "mode": (view.get("provenance") or {}).get("mode") or ("REPLAY" if source == "replay" else None),
            "provenance": view.get("provenance"), "families": view.get("families"),
            "replay": view.get("replay"), **body,
            "notes": ["node data is the engine's own book, triggers and fills, separate from the public-API panels",
                      "a replay is labelled REPLAY and is never current market data"]}


def node_status(snap: dict, args: dict, rt=None) -> dict:
    wanted = args.get("market")
    source = (args.get("source") or "live").lower()
    markets = {}
    for name, ms in snap["markets"].items():
        if wanted and name != wanted:
            continue
        view = (ms.get("node") or {}).get(source)
        if view is None:
            markets[name] = {"available": False,
                             "reason": "no replay is running" if source == "replay" else "no node feed configured"}
            continue
        markets[name] = {"families": view.get("families"), "provenance": view.get("provenance"),
                         "delay": view.get("delay"),
                         "book": view.get("book"), "replay": view.get("replay"), "counters": view.get("counters"),
                         "recent_events": view.get("recent_events"),
                         "bursts": view.get("bursts")}
    if wanted and not markets:
        raise LookupError(f"unknown market {wanted!r}")
    return {"tool": "node_status", "ok": True, "snapshot_id": snap["snapshot_id"], "snapshot_time": snap["built_at"],
            **_served(snap), "source": source, "node": snap.get("node_status"), "markets": markets,
            "states": {"LIVE": "the node feed's delay (5 s average of the latest watermark's age) is within its limit",
                       "BEHIND": "delayed: the node feed's 5 s average delay is above the delayed limit; the data is "
                                 "served with this label and its delay, as of its block time, never as current",
                       "STALE": "the node feed's 5 s average delay is above the stale limit (heartbeats don't count); "
                                "the data is served with this label and its delay, as of its block time, never as current",
                       "UNKNOWN": "clock skew: ages can't be bounded; the data is withheld",
                       "INVALID": "not recovered since a gap, new session or incomplete snapshot; the data is withheld",
                       "REPLAY": "a recording, never current"},
            "rule": ("a late family labels, it doesn't withhold: BEHIND and STALE data is served with "
                     "its state, reason and delay_ms; INVALID, UNKNOWN and no data withhold"),
            "owner_pos": ("the market's owner position stream (PROTOCOL.md 4.8, opt-in): the signed position of every "
                          "owner of a waiting whole-position TP/SL, feeding the stop map's size estimate beside the "
                          "stated figures; listed only when the handshake named the stream; INVALID with the reason when "
                          "the feed has none; no per-account position is ever served")}


def node_book(snap: dict, args: dict, rt=None) -> dict:
    market, view, source = _node_view(snap, args)
    body = {"book": view.get("book"), "ladder": view.get("ladder"), "walls": view.get("walls"),
            "thinning": view.get("thinning"), "tape": view.get("tape"),
            "flow_map": view.get("flow_map")}        # the order flow map with its readings (Nov 10); withheld with the tape
    if view.get("available") is False:
        body = {"available": False, "reason": view.get("reason")}
    return _node_envelope("node_book", snap, market, view, source, body)


def node_stops(snap: dict, args: dict, rt=None) -> dict:
    market, view, source = _node_view(snap, args)
    body = {"stops": view.get("stops")}
    if view.get("available") is False:
        body = {"available": False, "reason": view.get("reason")}
    return _node_envelope("node_stops", snap, market, view, source, body)


HANDLERS.update({"node_status": node_status, "node_book": node_book, "node_stops": node_stops})


# Node tools answered from a relay subscribe on demand (handoff 1d step 7): the families each one needs, the time it
# waits for them (a market's full data starts from the relay's latest snapshot plus the lines since) and the poll.
NODE_ON_DEMAND = {"node_book": ("book",), "node_stops": ("triggers",), "node_status": ("flow",)}
NODE_WAIT_S = 20.0
NODE_WAIT_POLL_S = 0.5


def _node_ready(snap: dict | None, market: str, fams: tuple) -> bool:
    try:
        view = snap["markets"][market]["node"]["live"]
        return all(view["families"][f]["state"] not in ("INVALID",) for f in fams)
    except (KeyError, TypeError):
        return False


def _with_node_lease(name: str, args: dict, rt):
    """For a node tool reading live relay data: subscribe to the market for this call, wait (bounded) until its
    families are valid in a snapshot, and return (lease, snapshot or None). (None, None) when not applicable."""
    market = args.get("market")
    if (name not in NODE_ON_DEMAND or args.get("snapshot_id") or (args.get("source") or "live").lower() != "live"
            or not isinstance(market, str) or not hasattr(rt, "node_lease")):
        return None, None
    lease = rt.node_lease(market)
    if not lease:
        return None, None
    deadline = time.monotonic() + NODE_WAIT_S
    snap = None
    try:
        while True:
            snap, _ = rt.snapshot(None)
            if _node_ready(snap, market, NODE_ON_DEMAND[name]) or time.monotonic() >= deadline:
                return lease, snap
            time.sleep(NODE_WAIT_POLL_S)
    except Exception:
        return lease, None              # the caller still releases it; the snapshot is read again there


def run_tool(name: str, args: dict | None, rt) -> str:
    """Resolve the snapshot and run one tool. Always returns a JSON string; never raises."""
    lease = None
    try:
        args = dict(args or {})
        lease, _ = _with_node_lease(name, args, rt)
        snap, err = rt.snapshot(args.get("snapshot_id"))
        if snap is None:
            return _error(name, err or "no snapshot available", collector=rt.status().get("collector"))
        # Results in the latest snapshot are re-judged at serving time: a node family past its delay limit by now
        # isn't served as LIVE, a public field past its freshness limit isn't served as fresh, and nothing built on
        # either is served. Historical reads (by ID) are as of build time.
        from .node.serving import age_snapshot_for_serving
        from .serving_public import age_snapshot
        snap = age_snapshot(age_snapshot_for_serving(snap))
        return _dumps(HANDLERS[name](snap, args, rt))
    except (LookupError, ValueError) as exc:
        return _error(name, str(exc).strip("'\""))
    except Exception as exc:  # never let a tool call fail with an exception
        return _error(name, f"internal error: {type(exc).__name__}: {exc}")
    finally:
        if lease:
            rt.node_release(lease)        # released once the call has answered
