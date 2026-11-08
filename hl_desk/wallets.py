"""Wallets behind the volume (Handoff 1 step 5b), on the flow metric's execution set.

Each execution's notional counts once to its buyer's bought notional and
once to its seller's sold notional, even when both are the same address.
Bought plus sold is participation, never market volume. Shares divide by
the total over every wallet on that side in the window, shown or not.
"""

from __future__ import annotations

from collections import defaultdict

from .util import iso_ms, na, rnd, val


def tally(trades) -> dict[str, dict]:
    w: dict[str, dict] = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "aggressive_bought": 0.0,
                                              "aggressive_sold": 0.0, "executions": 0})
    for t in trades:
        n = t.notional
        b, s = w[t.buyer], w[t.seller]
        b["bought"] += n
        s["sold"] += n
        if t.side == "B":
            b["aggressive_bought"] += n
        elif t.side == "A":
            s["aggressive_sold"] += n
        b["executions"] += 1
        if t.seller != t.buyer:
            s["executions"] += 1
    return w


def ranked_list(amounts: dict[str, float], total: float, target_pct: float, cap: int, side_word: str) -> dict:
    """Shortest ranked prefix reaching target_pct of total, or cap wallets, whichever first."""
    ranked = sorted(((a, x) for a, x in amounts.items() if x > 0), key=lambda r: (-r[1], r[0]))
    if total <= 0:
        return {"total_usd": val(0.0, "USD"), "listed": [], "share_reached_pct": na(f"no {side_word} in window", "%"),
                "target_pct": target_pct, "cap": cap, "stopped_by": "empty",
                "summary": f"No {side_word} in this window; shares are unavailable, not 0%."}
    listed, cum = [], 0.0
    stopped_by = "exhausted"
    for addr, x in ranked:
        if len(listed) >= cap:
            stopped_by = "cap"
            break
        listed.append((addr, x))
        cum += x
        if cum * 100.0 >= target_pct * total - 1e-9:
            stopped_by = "target"
            break
    if stopped_by == "exhausted" and len(listed) >= cap and cum * 100.0 < target_pct * total - 1e-9:
        stopped_by = "cap"
    reached = cum / total * 100.0
    n = len(listed)
    if stopped_by == "target":
        summary = f"Top {n} make up {reached:.0f}% of {side_word}, reaching the {target_pct:.0f}% target."
    elif stopped_by == "cap":
        summary = f"Top {n} make up {reached:.0f}% of {side_word}; the {target_pct:.0f}% target isn't reached."
    else:
        summary = f"All {n} make up {reached:.0f}% of {side_word}."
    return {"total_usd": val(rnd(total, 2), "USD"),
            "listed": [{"rank": i + 1, "address": a, "amount_usd": rnd(x, 2),
                        "share_pct": rnd(x / total * 100.0, 4)} for i, (a, x) in enumerate(listed)],
            "share_reached_pct": val(rnd(reached, 4), "%"), "target_pct": target_pct, "cap": cap,
            "stopped_by": stopped_by, "wallets_on_side": len(ranked), "summary": summary}


def wallet_row(addr: str, w: dict, total_bought: float, total_sold: float) -> dict:
    return {
        "address": addr,
        "bought_usd": rnd(w["bought"], 2),
        "sold_usd": rnd(w["sold"], 2),
        "aggressive_bought_usd": rnd(w["aggressive_bought"], 2),
        "aggressive_sold_usd": rnd(w["aggressive_sold"], 2),
        "participation_usd": rnd(w["bought"] + w["sold"], 2),
        "net_bought_usd": rnd(w["bought"] - w["sold"], 2),
        "buy_share_pct": (val(rnd(w["bought"] / total_bought * 100, 4), "%") if total_bought > 0
                          else na("no buying in window", "%")),
        "sell_share_pct": (val(rnd(w["sold"] / total_sold * 100, 4), "%") if total_sold > 0
                           else na("no selling in window", "%")),
        "executions": w["executions"],
    }


def panel(trades, window: dict, target_pct: float = 50.0, cap: int = 50) -> dict:
    w = tally(trades)
    total_bought = sum(x["bought"] for x in w.values())
    total_sold = sum(x["sold"] for x in w.values())
    buyers = ranked_list({a: x["bought"] for a, x in w.items()}, total_bought, target_pct, cap, "buying")
    sellers = ranked_list({a: x["sold"] for a, x in w.items()}, total_sold, target_pct, cap, "selling")
    rows = {}
    for item in buyers["listed"] + sellers["listed"]:
        a = item["address"]
        rows[a] = wallet_row(a, w[a], total_bought, total_sold)
    return {
        "window": window,
        "unique_execution_notional_usd": rnd(sum(t.notional for t in trades), 2),
        "executions": len(trades),
        "buyers": buyers,
        "sellers": sellers,
        "wallets": rows,
        "notes": ["bought + sold is participation, not market volume: each execution counts to two wallets",
                  "net bought is notional, not a position change or PnL",
                  "addresses are public on-chain identifiers; owners are not identified"],
    }


def wallet_trades(trades, address: str) -> dict:
    """A wallet's executions in the panel's own execution set; they sum to its totals."""
    mine = []
    bought = sold = ab = asold = 0.0
    for t in trades:
        if address not in (t.buyer, t.seller):
            continue
        role = "both" if t.buyer == t.seller == address else ("buyer" if t.buyer == address else "seller")
        n = t.notional
        if t.buyer == address:
            bought += n
            if t.side == "B":
                ab += n
        if t.seller == address:
            sold += n
            if t.side == "A":
                asold += n
        mine.append({"time_ms": t.time, "time": iso_ms(t.time), "px": t.px, "sz": t.sz,
                     "notional_usd": rnd(n, 2), "role": role, "aggressor_side": t.side,
                     "aggressive": (t.side == "B" and t.buyer == address) or (t.side == "A" and t.seller == address),
                     "counterparty": t.seller if role == "buyer" else t.buyer if role == "seller" else address,
                     "tid": t.tid, "hash": t.hash})
    return {"address": address, "executions": mine,
            "totals": {"bought_usd": rnd(bought, 2), "sold_usd": rnd(sold, 2),
                       "aggressive_bought_usd": rnd(ab, 2), "aggressive_sold_usd": rnd(asold, 2),
                       "net_bought_usd": rnd(bought - sold, 2)}}
