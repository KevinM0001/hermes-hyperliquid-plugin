"""Node results aged to the moment a latest snapshot is served (Codex review 7, B1; Kevin, Nov 10).

A snapshot is built once a second, but a tool may read the latest one up to a few seconds later. So every node
result a tool serves from the latest snapshot is re-judged at serving time: each LIVE family status has its delay
and ages advanced by the snapshot's age when served (an upper bound: the average can grow no faster than time
passes), and a family whose delay has passed the delayed or stale limit by then reads BEHIND or STALE with the
reason, "by the time this was served". The market's delay block is advanced the same way.

A late family labels, it doesn't withhold (Kevin, Nov 10: present the information even if delayed, say so and show
the time). The ladder, walls, thinning, stops, tape, order flow map, Flow Deck and burst buckets and wallet splits
built on a family that has lapsed to BEHIND or STALE are served as built, with the family's state, reason and delay
next to them and their "as of" note re-stated at the served delay, so a reader sees "BEHIND, 73.4 s behind" beside
the numbers and the delay keeps counting up. Only no data, INVALID (a gap, a crossed book, a sequence hole, a lost
family) and UNKNOWN (clock skew) withhold, at build time and here alike; a family that was usable when built never
lapses into either by waiting, so nothing is withheld here that the build served.

A historical snapshot (read by ID) is not aged: it is labelled as of its build time. REPLAY families run on the
replay's own clock and are never aged. The tab's snapshot route isn't aged here either: the tab ages what it
shows itself, counting the served age and every second since it asked (app.js snapshotValidity).
"""

from __future__ import annotations

from .client import delay_state
from .panels import CENSUS_BANDS_KEYS, CENSUS_BOOK_KEYS, THIN_NOTE, as_of_note, census_withheld, usable

USABLE = ("LIVE", "REPLAY")
BUCKET_KEYS = ("new_long", "cover", "new_short", "exit")


def aged_status(st: dict | None, age_ms: float | None) -> dict | None:
    """A family status as it stands age_ms after it was judged (None: unknown). Only LIVE statuses change state; a
    BEHIND or STALE status judged while catching up loses that claim after 3 s (the replay it described is over or
    unknown by then) and gets back the plain delay reason of its own state (delayed or stale)."""
    if not isinstance(st, dict):
        return st
    if st.get("state") != "LIVE":
        if st.get("catching_up") and (age_ms is None or age_ms > 3_000):
            plain = delay_state(st.get("delay_ms"), st)[1] if isinstance(st.get("delay_ms"), (int, float)) else st.get("reason")
            return dict(st, catching_up=False, reason=plain)
        return st
    if age_ms is None:
        return dict(st, state="STALE", reason="the snapshot's age when served isn't known, so it can't be shown as current")
    out = dict(st, aged_by_ms=round(age_ms))
    keys = ("watermark_age_ms", "source_age_ms", "delay_ms", "delayed_after_ms", "stale_after_ms")
    if not all(isinstance(st.get(k), (int, float)) for k in keys):
        return out | {"state": "STALE", "reason": "its delay or limit isn't known, so it can't be shown as current"}
    for k in ("watermark_age_ms", "source_age_ms", "delay_ms"):
        out[k] = round(st[k] + age_ms)
    state, reason = delay_state(out["delay_ms"], st)
    if state != "LIVE":
        return out | {"state": state, "reason": f"{reason}, by the time this was served"}
    return out


def _why(fam: str, st: dict) -> str:
    return f"{fam} {st.get('state')}: {st.get('reason')}"


def aged_view(view: dict | None, age_ms: float) -> dict | None:
    """A node market view re-judged at serving time; the same gating as panels.market_view."""
    if not isinstance(view, dict) or not isinstance(view.get("families"), dict):
        return view
    fams = {k: aged_status(v, age_ms) for k, v in view["families"].items()}
    dl = view.get("delay")
    if isinstance(dl, dict) and dl.get("available") and dl.get("state") == "LIVE" and age_ms is None:
        dl = dict(dl, state="STALE", reason="the snapshot's age when served isn't known, so it can't be shown as current")
    elif isinstance(dl, dict) and dl.get("available") and dl.get("state") != "UNKNOWN" and age_ms is not None:
        dl = dict(dl, delay_ms=round(dl["delay_ms"] + age_ms), current_ms=round(dl["current_ms"] + age_ms),
                  aged_by_ms=round(age_ms))
        state, reason = delay_state(dl["delay_ms"], dl | {"delay_window_ms": dl["window_ms"]})
        dl.update(state=state, reason=reason)
    if isinstance(dl, dict) and dl.get("catching_up") and (age_ms is None or age_ms > 3_000):
        # the market's delay block drops the catching-up claim on the same rule as the families (aged_status)
        dl = dict(dl, catching_up=False)
    if fams == view["families"] and dl is view.get("delay"):
        return view
    out = dict(view, families=fams, delay=dl)
    # (view key, family, the family's name in a reason, the label of its "as of" note)
    blocks = (("ladder", "book", "book", "book"), ("walls", "book", "book", "book"), ("thinning", "book", "book", "book"),
              ("stops", "triggers", "triggers", "waiting stops"), ("tape", "flow", "node fills", "fills"),
              ("flow_map", "flow", "node fills", "fills"))
    for key, fam, name, label in blocks:
        block, st = out.get(key), fams.get(fam) or {}
        if not isinstance(block, dict) or block.get("available") is False or not usable(view["families"].get(fam)):
            continue
        if not usable(st):                              # UNKNOWN by serving time: withheld as the build would
            why = f"{name} {st.get('state')}: {st.get('reason')}"
            out[key] = {"available": False, "reason": why} | (
                {"note": THIN_NOTE} if key == "thinning" else {"coverage": block.get("coverage")} if key == "tape" else
                {"stream": "node fills", "provenance": block.get("provenance"), "freshness": st, "window": block.get("window")} if key == "flow_map" else {})
            continue
        if key == "flow_map":
            out[key] = dict(block, freshness=st, as_of_note=as_of_note(label, st))   # its own freshness is the full status
        else:
            out[key] = dict(block, freshness=st.get("state"), as_of_note=as_of_note(label, st))
    est_block = (out.get("stops") or {}).get("estimate") if isinstance(out.get("stops"), dict) else None
    if isinstance(est_block, dict) and isinstance(fams.get("owner_pos"), dict):
        # The estimate's state follows the aged owner_pos family (its as-of time is already in the block).
        out["stops"] = dict(out["stops"], estimate=dict(est_block, freshness=fams["owner_pos"].get("state")))
    bands = fams.get("bands") or {}
    if "stops" in out and not usable(bands) and usable(view["families"].get("bands")):
        st = out.get("stops")
        if isinstance(st, dict) and isinstance(st.get("census"), dict):     # the census counts read from the bands view
            out["stops"] = dict(st, census=census_withheld(st["census"], _why("bands", bands), CENSUS_BANDS_KEYS))
    if isinstance(view.get("liquidation_events"), list):
        out["liquidation_events"] = [dict(ev, freshness=aged_status(ev.get("freshness"), age_ms))
                                     for ev in view["liquidation_events"]]
    return out


def aged_fill_result(res: dict | None, age_ms: float) -> dict | None:
    """A result built from node fills (Flow Deck or burst buckets, a wallet split), re-judged at serving time: its
    flow freshness is aged and the numbers stay with it (a late family labels, Kevin, Nov 10); only a family no
    longer usable (UNKNOWN) withholds them with the reason."""
    if not isinstance(res, dict) or not res.get("available") or not isinstance(res.get("freshness"), dict):
        return res
    st = aged_status(res["freshness"], age_ms)
    if st is res["freshness"] or usable(st):
        return dict(res, freshness=st)
    why = f"node fills {st.get('state')}: {st.get('reason')}"
    keep = {k: res[k] for k in ("stream", "window_label", "as_of_note", "provenance", "coverage", "window") if k in res}
    out = keep | {"available": False, "reason": why, "freshness": st}
    for k in BUCKET_KEYS:                               # the bucket metrics a card or burst shows, withheld
        if k in res:
            out[k] = {"value": None, "reason": why, "unit": "USD"}
    return out


def aged_taker_flow(res: dict | None, age_ms: float) -> dict | None:
    """chart.taker_flow (the 15 s bars under the snapshot's candles) re-judged at serving time, as the flow map is: a
    late family labels (the bars stay, the freshness ages, the "as of" note is re-stated at the served delay); only a
    family no longer usable withholds the bars with the reason, keeping where they came from."""
    if not isinstance(res, dict) or not res.get("available") or not isinstance(res.get("freshness"), dict):
        return res
    st = aged_status(res["freshness"], age_ms)
    if st is res["freshness"]:
        return res
    if usable(st):
        return dict(res, freshness=st, as_of_note=as_of_note("fills", st))
    why = f"node fills {st.get('state')}: {st.get('reason')}"
    keep = {k: res[k] for k in ("stream", "interval", "bucket_s", "provenance", "coverage", "retention") if k in res}
    return keep | {"available": False, "reason": why, "freshness": st}


def age_snapshot_for_serving(snap: dict) -> dict:
    """The latest snapshot with every node result re-judged at serving time (historical snapshots unchanged)."""
    served = snap.get("served") or {}
    if not served.get("latest"):
        return snap
    age_s = served.get("age_s")
    age_ms = max(0.0, age_s * 1000) if isinstance(age_s, (int, float)) and not isinstance(age_s, bool) else None
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
        chart = ms.get("chart")
        if isinstance(chart, dict) and isinstance(chart.get("taker_flow"), dict):
            m["chart"] = dict(chart, taker_flow=aged_taker_flow(chart["taker_flow"], age_ms))
        out["markets"][name] = m
    return out

