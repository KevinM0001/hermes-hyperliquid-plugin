"""Routes public websocket messages and info polls into the desk state.

Shared by the live collector and the replayer so both build snapshots the
same way. Only the channels this plugin subscribes to are handled; pong and
subscriptionResponse messages are counted but never touch freshness.
"""

from __future__ import annotations

from .state import DeskState


def subscriptions(markets: list[str]) -> list[dict]:
    subs = []
    for coin in markets:
        subs.append({"type": "l2Book", "coin": coin, "fast": True})
        subs.append({"type": "l2Book", "coin": coin})
        subs.append({"type": "trades", "coin": coin})
        subs.append({"type": "bbo", "coin": coin})
        subs.append({"type": "activeAssetCtx", "coin": coin})
    return subs


def dispatch(desk: DeskState, msg: dict, mono_ns: int, wall_ms: int) -> str:
    ch = msg.get("channel")
    data = msg.get("data")
    if ch == "l2Book":
        ms = desk.markets.get(data.get("coin"))
        if ms:
            ms.on_book(data, mono_ns, wall_ms)
    elif ch == "trades":
        dispatch_trades(desk, data, mono_ns, wall_ms)
    elif ch == "bbo":
        ms = desk.markets.get(data.get("coin"))
        if ms:
            ms.on_bbo(data, mono_ns, wall_ms)
    elif ch == "activeAssetCtx":
        ms = desk.markets.get(data.get("coin"))
        if ms:
            ms.on_ctx(data, mono_ns, wall_ms)
    elif ch == "candle":
        store = getattr(desk, "candles", None)     # the collector's candle store (hl_desk.candles); a replay has none
        if store is not None:
            store.on_ws_candle(data, mono_ns, wall_ms)
    return ch or "unknown"


def dispatch_trades(desk: DeskState, data, mono_ns: int, wall_ms: int) -> None:
    """Route a trades message by its records' coins, never by the first record alone.

    Trades are subscribed per coin, so a message normally names one subscribed coin; its records that name
    no coin or another coin are invalid records of that message, and the market's own validator turns
    them into a coverage gap. When no record names a subscribed coin but some name no coin at all, the
    message can't be attributed, so every subscribed market's trade coverage ends at it: no window may
    claim uninterrupted observation across a message that might have been its own."""
    records = data if isinstance(data, list) else [data]
    if not records:
        return
    coins = [r.get("coin") if isinstance(r, dict) else None for r in records]
    known = [c for c in dict.fromkeys(coins) if isinstance(c, str) and c in desk.markets]
    if known:
        unnamed = [r for r, c in zip(records, coins) if not (isinstance(c, str) and c in desk.markets)]
        for coin in known:
            mine = [r for r, c in zip(records, coins) if c == coin]
            # Records naming no subscribed coin are passed to each subscribed coin of this message as invalid.
            desk.markets[coin].on_trades(mine + unnamed if len(known) > 1 else records, mono_ns, wall_ms)
        return
    if any(not isinstance(c, str) or not c for c in coins):
        reason = "a trades message with no attributable coin (a record without a coin)"
        for ms in desk.markets.values():
            ms.on_unattributed_trades(records, reason, mono_ns, wall_ms)


def dispatch_depth(desk: DeskState, request: dict, response: dict, mono_ns: int, wall_ms: int) -> None:
    ms = desk.markets.get(request.get("coin"))
    if ms and isinstance(response, dict):
        ms.on_depth_poll(int(request["nSigFigs"]), response, mono_ns, wall_ms)
