"""Public results aged to the moment they are served or shown (Codex h1 review 1, B1 and B2).

A snapshot is built once a second, but it may be read up to 5 s later as the latest one (and the tab keeps showing
it until the next one arrives). A public field that was fresh at build time stops being fresh once its last progress
is older than its max age, or its source time older than its source max age (with the clock bound), counting the
snapshot's age. So when a latest snapshot is served, each fresh status is re-judged at that age: a field that has
lapsed reads stale (no progress) or delayed (old source time) with the reason, and every value the build gates on
that field is withheld exactly as the build withholds it (null with the reason), in the same shape:

- book_fast: the top of book (best bid and ask, mid, spread) and its price lines;
- book_full: the ladder, its largest-level price lines, and the exact band depth and observed depth built on it;
- book_sf4, book_sf3: the approximate band depth and observed depth built on them;
- trades: the flow values, the CVD buckets' values, the large-print count, the Flow Deck card's numbers and regime,
  and the wallet panels;
- ctx: the context values and their price lines (never fresh in practice: activeAssetCtx has no source time).

Records that the build doesn't gate (the tape, candles, large prints themselves, deck events) are kept. A snapshot read
by ID is historical and isn't aged: it is labelled as of its build time. The tab applies the same rules to the
snapshot it shows (app.js ageShown), counting the time since it asked for it.
"""

from __future__ import annotations

from .util import na

BOOK_LABEL = {"book_full": "full precision", "book_sf4": "nSigFigs=4", "book_sf3": "nSigFigs=3"}
CARD_NUMBERS = ("move_bps", "net_flow_usd", "volume_usd", "impact", "pressure", "trades", "accounts", "top_wallet")


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def lapse(f: dict | None, age_s: float) -> tuple[str, str] | None:
    """(status, reason) when a status fresh at build time is no longer fresh age_s later; None otherwise."""
    if not isinstance(f, dict) or f.get("status") != "fresh":
        return None
    pa, mx = f.get("progress_age_s"), f.get("max_age_s")
    if _num(pa) and _num(mx) and pa + age_s > mx:
        return "stale", f"no progress for {pa + age_s:.1f} s (max {mx:g} s)"
    sa, smx = f.get("source_age_s"), f.get("source_max_age_s")
    bound = f.get("clock_bound_s") if _num(f.get("clock_bound_s")) else 0
    if _num(sa) and _num(smx) and sa + age_s + bound > smx:
        return "delayed", f"source time {sa + age_s:.1f} s old (max {smx:g} s with a {bound:g} s clock bound)"
    return None


def _withhold_metrics(block: dict, reason: str) -> dict:
    return {k: (na(reason, v.get("unit")) if isinstance(v, dict) and "value" in v else v) for k, v in block.items()}


def _age_band(band: dict, lapsed: dict) -> dict:
    out = dict(band)
    for part, value_key in (("exact", "notional_usd"), ("approximate", "approx_usd")):
        sides = band.get(part)
        if not isinstance(sides, dict):
            continue
        new, why = dict(sides), None
        for side in ("bid", "ask"):
            s = sides.get(side) or {}
            if s.get("book") in lapsed and s.get("status") in ("covered", "approximate"):
                why = lapsed[s["book"]]
                new[side] = {value_key: na(why, "USD"), "status": "unavailable", "reason": why}
        if why:
            new["imbalance"] = na(why, "ratio")
            out[part] = new
    return out


def withheld_card(card: dict, reason: str) -> dict:
    """The Flow Deck card exactly as the build makes it when trades aren't fresh (deck.DeckEngine.card): the same
    keys, and the last confirmed regime as {regime: its state, since_ms, since, as_of_tick_ms, note}."""
    out = {k: card[k] for k in ("market", "window_s", "tick_s") if k in card}
    tick = card.get("last_complete_tick_ms", card.get("tick_ms"))   # a complete card names its tick tick_ms
    out.update({"regime": na(reason), "confirmed_regime": None, "live_classification": None, "pending": None,
                "last_complete_tick_ms": tick,
                "numbers": {k: na(reason) for k in CARD_NUMBERS}})
    if "activity_buckets" in card:
        out["activity_buckets"] = card["activity_buckets"]
    cr = card.get("confirmed_regime")
    if isinstance(cr, dict) and cr.get("state") is not None:
        out["last_confirmed"] = {"regime": cr["state"], "since_ms": cr.get("since_ms"), "since": cr.get("since"),
                                 "as_of_tick_ms": tick, "note": "historical: the last complete tick's confirmed regime"}
    elif card.get("last_confirmed"):
        out["last_confirmed"] = card["last_confirmed"]
    return out


def _aged_ages(f: dict, age_s: float) -> dict:
    out = dict(f, aged_by_s=round(age_s, 3))
    for a in ("progress_age_s", "source_age_s", "last_message_age_s"):
        if _num(f.get(a)):
            out[a] = round(f[a] + age_s, 3)
    return out


def age_market(ms: dict, age_s: float, when: str) -> dict:
    """A public market block re-judged age_s after it was built: every status shows its ages as of now, and a fresh
    one that has lapsed reads stale or delayed with every value built on it withheld. Unchanged at age 0."""
    fr0 = ms.get("freshness") or {}
    if not _num(age_s) or age_s <= 0:
        return ms
    lapses = {k: v for k in fr0 if (v := lapse(fr0[k], age_s))}
    out = dict(ms)
    fr, why = dict(fr0), {}
    for k, f in fr0.items():                     # every status shows its ages as of now
        if isinstance(f, dict) and k not in lapses:
            fr[k] = _aged_ages(f, age_s)
    if not lapses:
        out["freshness"] = fr
        return out
    for k, (status, reason) in lapses.items():
        f = dict(_aged_ages(fr0[k], age_s), status=status, reason=f"{reason}, {when}")
        fr[k] = f
        why[k] = f"{f['field']} {status}: {f['reason']}"
    out["freshness"] = fr
    out["unavailable"] = list(ms.get("unavailable") or []) + [
        {"field": fr[k]["field"], "status": fr[k]["status"], "reason": fr[k]["reason"]} for k in lapses]
    drop_lines: dict[str, str] = {}
    book = dict(ms.get("book") or {})
    if "book_fast" in why:
        top = {k: v for k, v in (book.get("top") or {}).items() if k != "book_time_ms"}
        book["top"] = _withhold_metrics(top, why["book_fast"])
        drop_lines |= {"best_bid": why["book_fast"], "best_ask": why["book_fast"]}
    if "book_full" in why:
        book["ladder"] = {"bids": [], "asks": [], "reason": why["book_full"],
                          "largest": {"bid": None, "ask": None, "reason": why["book_full"]},
                          "api_count_note": (book.get("ladder") or {}).get("api_count_note")}
        drop_lines |= {"largest_bid_level": why["book_full"], "largest_ask_level": why["book_full"]}
    lapsed_books = {BOOK_LABEL[k]: why[k] for k in BOOK_LABEL if k in why}
    if lapsed_books and isinstance(book.get("depth"), dict):
        depth = dict(book["depth"])
        observed = dict(depth.get("observed") or {})
        for label, r in lapsed_books.items():
            if label in observed:
                observed[label] = {"reason": r}
        depth["observed"] = observed
        depth["bands"] = [_age_band(b, lapsed_books) for b in depth.get("bands") or []]
        book["depth"] = depth
    out["book"] = book
    if "ctx" in why:
        out["context"] = _withhold_metrics(ms.get("context") or {}, why["ctx"])
        drop_lines |= {"mark": why["ctx"], "oracle": why["ctx"]}
    if drop_lines and isinstance(ms.get("chart"), dict):
        chart = dict(ms["chart"])
        lines = chart.get("price_lines") or []
        chart["price_lines"] = [ln for ln in lines if ln.get("kind") not in drop_lines]
        gone = sorted({ln.get("kind") for ln in lines if ln.get("kind") in drop_lines})
        if gone:
            chart["price_lines_withheld"] = [{"kind": k, "reason": drop_lines[k]} for k in gone]
        out["chart"] = chart
    if "trades" in why:
        r = why["trades"]
        fl = dict(ms.get("flow") or {})
        fl["values"] = _withhold_metrics(fl.get("values") or {}, r)
        cvd = dict(fl.get("cvd") or {})
        cvd.update(reason=r, cumulative_from_ms=None, cumulative_from=None,
                   buckets=[dict(b, buy_usd=None, sell_usd=None, delta_usd=None, cumulative_usd=None, reason=r)
                            for b in cvd.get("buckets") or []])
        fl["cvd"] = cvd
        fl["large_prints"] = dict(fl.get("large_prints") or {}, count=None, reason=r)
        out["flow"] = fl
        deck = dict(ms.get("deck") or {})
        if isinstance(deck.get("card"), dict):
            deck["card"] = withheld_card(deck["card"], r)
        out["deck"] = deck
        out["wallets"] = {k: {"window_min": int(k.rstrip("m")) if k.rstrip("m").isdigit() else None, "reason": r,
                              "buyers": None, "sellers": None} for k in (ms.get("wallets") or {})}
    return out


def age_snapshot(snap: dict) -> dict:
    """Every market of a latest snapshot aged to its served age; a historical (by ID) one is returned as is."""
    served = snap.get("served") or {}
    if not served.get("latest") or not _num(served.get("age_s")):
        return snap
    age = served["age_s"]
    markets = {k: age_market(v, age, "by the time this was served") for k, v in (snap.get("markets") or {}).items()}
    if all(markets[k] is snap["markets"][k] for k in markets):
        return snap
    return dict(snap, markets=markets)
