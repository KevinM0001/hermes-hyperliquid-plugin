"""Node results aged to the moment a latest snapshot is served (Codex review 7, B1).

A snapshot is built once a second, but a tool may read the latest one up to a few seconds later. A node family
that was LIVE at build time stops being LIVE once its last watermark is older than its stale limit, or its source
block older than its source limit (PROTOCOL.md section 8). So every node result a tool serves from the latest
snapshot is re-judged at serving time: each LIVE family status has its ages advanced by the snapshot's age when
served, and any family that has lapsed reads STALE or BEHIND with the reason. Every number built on it is then
withheld, exactly as the panel builder withholds it for a family that isn't usable.

A historical snapshot (read by ID) is not aged: it is labelled as of its build time. REPLAY families run on the
replay's own clock and are never aged. The tab's snapshot route isn't aged here either: the tab ages what it
shows itself, counting the served age and every second since it asked (app.js snapshotValidity).
"""

from __future__ import annotations

USABLE = ("LIVE", "REPLAY")
BUCKET_KEYS = ("new_long", "cover", "new_short", "exit")


def aged_status(st: dict | None, age_ms: float) -> dict | None:
    """A family status as it stands age_ms after it was judged. Only LIVE statuses change."""
    if not isinstance(st, dict) or st.get("state") != "LIVE":
        return st
    out = dict(st, aged_by_ms=round(age_ms))
    wm, lim = st.get("watermark_age_ms"), st.get("stale_after_ms")
    src, src_lim = st.get("source_age_ms"), st.get("max_source_age_ms")
    if not all(isinstance(x, (int, float)) for x in (wm, lim, src, src_lim)):
        return out | {"state": "STALE", "reason": "its age or limit isn't known, so it can't be shown as current"}
    out["watermark_age_ms"], out["source_age_ms"] = round(wm + age_ms), round(src + age_ms)
    if wm + age_ms > lim:
        return out | {"state": "STALE",
                      "reason": f"no watermark for {(wm + age_ms) / 1000:.1f} s (limit {lim / 1000:.1f} s) by the time "
                                "this was served; heartbeats don't count"}
    if src + age_ms > src_lim:
        return out | {"state": "BEHIND",
                      "reason": f"source block is {(src + age_ms) / 1000:.1f} s old (limit {src_lim / 1000:.1f} s) by "
                                "the time this was served"}
    return out


def _why(fam: str, st: dict) -> str:
    return f"{fam} {st.get('state')}: {st.get('reason')}"


def aged_view(view: dict | None, age_ms: float) -> dict | None:
    """A node market view re-judged at serving time; the same gating as panels.market_view."""
    if not isinstance(view, dict) or not isinstance(view.get("families"), dict):
        return view
    fams = {k: aged_status(v, age_ms) for k, v in view["families"].items()}
    if fams == view["families"]:
        return view
    out = dict(view, families=fams)
    book, trig, fl = fams.get("book") or {}, fams.get("triggers") or {}, fams.get("flow") or {}
    if "book" in out and book.get("state") not in USABLE and (view["families"].get("book") or {}).get("state") in USABLE:
        why = _why("book", book)
        out["book"] = dict(out["book"], valid=False, reason=why)
        out["ladder"] = {"available": False, "reason": why}
        out["walls"] = {"available": False, "reason": why}
        out["thinning"] = {"available": False, "reason": why, "note": (view.get("thinning") or {}).get("note")}
    if "stops" in out and trig.get("state") not in USABLE and (view["families"].get("triggers") or {}).get("state") in USABLE:
        why = _why("triggers", trig)
        out["stops"] = {"available": False, "reason": why}
        lad = out.get("ladder")
        if isinstance(lad, dict) and "bids" in lad:     # the ladder's stops column follows the trigger family too
            na = {"available": False, "reason": why}
            out["ladder"] = dict(lad, stops_available=False, stops_reason=why, **{
                side: [dict(r, stops_to_next_level=None if r.get("stops_to_next_level") is None else dict(na))
                       for r in lad[side]] for side in ("bids", "asks")})
    if "tape" in out and fl.get("state") not in USABLE and (view["families"].get("flow") or {}).get("state") in USABLE:
        out["tape"] = {"available": False, "reason": f"node fills {fl.get('state')}: {fl.get('reason')}",
                       "coverage": (view.get("tape") or {}).get("coverage")}
    if isinstance(view.get("liquidation_events"), list):
        out["liquidation_events"] = [dict(ev, freshness=aged_status(ev.get("freshness"), age_ms))
                                     for ev in view["liquidation_events"]]
    return out


def aged_fill_result(res: dict | None, age_ms: float) -> dict | None:
    """A result built from node fills (Flow Deck or burst buckets, a wallet split), re-judged at serving time:
    when its flow family has lapsed, its numbers are withheld with the reason."""
    if not isinstance(res, dict) or not res.get("available") or not isinstance(res.get("freshness"), dict):
        return res
    st = aged_status(res["freshness"], age_ms)
    if st is res["freshness"] or st.get("state") in USABLE:
        return dict(res, freshness=st)
    why = f"node fills {st.get('state')}: {st.get('reason')}"
    keep = {k: res[k] for k in ("stream", "window_label", "provenance", "coverage", "window") if k in res}
    out = keep | {"available": False, "reason": why, "freshness": st}
    for k in BUCKET_KEYS:                               # the bucket metrics a card or burst shows, withheld
        if k in res:
            out[k] = {"value": None, "reason": why, "unit": "USD"}
    return out


def age_snapshot_for_serving(snap: dict) -> dict:
    """The latest snapshot with every node result re-judged at serving time (historical snapshots unchanged)."""
    served = snap.get("served") or {}
    age_s = served.get("age_s")
    if not served.get("latest") or not isinstance(age_s, (int, float)):
        return snap
    age_ms = max(0.0, age_s * 1000)
    out = dict(snap, markets={})
    for name, ms in snap["markets"].items():
        m = dict(ms)
        node = ms.get("node")
        if isinstance(node, dict):
            m["node"] = dict(node, live=aged_view(node.get("live"), age_ms))
        deck = ms.get("deck")
        if isinstance(deck, dict):
            card = deck.get("card")
            events = []
            for ev in deck.get("events") or []:
                if ev.get("type") == "burst" and isinstance(ev.get("node_activity"), dict):
                    ev = dict(ev, node_activity=aged_fill_result(ev["node_activity"], age_ms))
                elif ev.get("type") == "liquidation_burst" and isinstance(ev.get("freshness"), dict):
                    ev = dict(ev, freshness=aged_status(ev["freshness"], age_ms))
                events.append(ev)
            m["deck"] = dict(deck, events=events, card=dict(card, activity_buckets=aged_fill_result(
                card.get("activity_buckets"), age_ms)) if isinstance(card, dict) else card)
        wallets = ms.get("wallets")
        if isinstance(wallets, dict):
            m["wallets"] = {k: (dict(p, node_activity_split=aged_fill_result(p.get("node_activity_split"), age_ms))
                                if isinstance(p, dict) and "node_activity_split" in p else p)
                            for k, p in wallets.items()}
        out["markets"][name] = m
    return out

