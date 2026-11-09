"""Per-market state and the snapshot every tool and the tab read.

One DeskState per Hermes profile is owned by the runtime (hl_desk.runtime).
Only the collector thread mutates it; it builds an immutable snapshot every
second (and on demand in replay), and tools and the dashboard read those
snapshots by ID, so chat and screen show the same numbers.
"""

from __future__ import annotations

import json
import secrets
from collections import deque

from . import PLUGIN_VERSION, book as bk, flow, schema, wallets
from .clock import ClockBound
from .config import DeskConfig
from .deck import DeckEngine
from .freshness import NS, FieldTracker
from .trades import TradeStore
from .util import dex_of, fnum, iso_ms, median, na, rnd, val

CTX_KEYS = ("markPx", "oraclePx", "midPx", "funding", "openInterest", "dayNtlVlm", "prevDayPx")
WALLET_WINDOWS_MIN = (1, 5, 15)
CANDLE_MS = 15_000
CHART_SPAN_MS = 15 * 60 * 1000
NODE_PANELS = ("ladder: stops, top order, age, change in 10 s", "waiting stops", "wall watch",
               "tape flags (stop, TWAP, liquidation)", "Flow Deck activity buckets (new long, cover, new short, exit)",
               "wallet activity split (new long, cover, new short, exit)")
SOURCES = {
    "LIVE": "Hyperliquid public websocket wss://api.hyperliquid.xyz/ws and info endpoint https://api.hyperliquid.xyz/info",
    "REPLAY": "recorded Hyperliquid public data replayed from a file",
    "SYNTHETIC": "constructed test data, not market data",
}


class MarketState:
    def __init__(self, market: str, cfg: DeskConfig, meta: dict | None) -> None:
        self.market = market
        self.cfg = cfg
        self.meta = meta
        f = schema.freshness_field
        self.trackers = {
            "book_fast": FieldTracker("l2Book fast (5 levels)", "source_time_progress", cfg.book_max_age_s("fast"),
                                      "stale", "WsBook.time advances", source_max_age_s=cfg.book_source_max_age_s("fast")),
            "book_full": FieldTracker("l2Book full (20 levels)", "source_time_progress", cfg.book_max_age_s("full"),
                                      "stale", "WsBook.time advances", source_max_age_s=cfg.book_source_max_age_s("full")),
            "book_sf4": FieldTracker("l2Book nSigFigs=4 (info)", "source_time_progress", cfg.book_max_age_s("sf4"),
                                     "stale", "info l2Book time advances", source_max_age_s=cfg.book_source_max_age_s("sf4")),
            "book_sf3": FieldTracker("l2Book nSigFigs=3 (info)", "source_time_progress", cfg.book_max_age_s("sf3"),
                                     "stale", "info l2Book time advances", source_max_age_s=cfg.book_source_max_age_s("sf3")),
            "trades": FieldTracker("trades", "source_time_progress", cfg.trades_max_age_s(market), "unverified",
                                   "a trade later than the trades watermark",
                                   "no trade for longer than the max age: a quiet market can't be told apart from a stalled feed",
                                   source_max_age_s=cfg.trades_source_max_age_s(market)),
            "bbo": FieldTracker("bbo", "none", None, "unverified", "none (sent only when the bbo changes)",
                                f("bbo")["reason"]),
            "ctx": FieldTracker("activeAssetCtx", "change_on_receipt", cfg.ctx_max_age_s(market), "unverified",
                                "a context that differs from the previous one",
                                "no changed context for longer than the max age (no source time; cadence undocumented)"),
        }
        self.books: dict[str, dict] = {}
        self.fast_mids: deque[tuple[int, float | None, str | None]] = deque(maxlen=60)
        self.ctx: dict | None = None
        self.ctx_key: str | None = None
        self.bbo: dict | None = None
        self.offsets: deque[int] = deque(maxlen=50)
        self.store = TradeStore(market)
        self.deck = DeckEngine(market, cfg.burst_threshold(market), cfg.wallet_target_share_pct, cfg.wallet_cap)
        self.conn: int | None = None

    # --- connection lifecycle -------------------------------------------
    def on_connect(self, conn: int) -> None:
        self.conn = conn
        for tr in self.trackers.values():
            tr.reset()
        self.books.clear()
        self.fast_mids.clear()
        self.ctx = self.ctx_key = None
        self.bbo = None
        self.store.begin(conn)

    def on_disconnect(self, reason: str, wall_ms: int | None) -> None:
        last_wm = self.store.watermark_ms
        self.store.end(reason, wall_ms)
        self.deck.on_gap(last_wm, reason)

    # --- messages ----------------------------------------------------------
    def on_book(self, data: dict, mono_ns: int, wall_ms: int) -> None:
        raw = data.get("levels") or [[], []]
        bids, pb = bk.parse_levels(raw[0])
        asks, pa = bk.parse_levels(raw[1])
        t = data.get("time")
        if t is None:
            return
        t = int(t)
        nb, na_ = len(raw[0] or []), len(raw[1] or [])
        if nb > 5 or na_ > 5:
            kinds = ["book_full"]
        elif nb < 5 and na_ < 5:
            kinds = ["book_fast", "book_full"]   # thin book: both subscriptions carry the same levels
        else:
            kinds = ["book_fast"]
        for kind in kinds:
            self._apply_book(kind, bids, asks, t, mono_ns, wall_ms, {"bid": pb, "ask": pa})

    def _apply_book(self, kind: str, bids, asks, t: int, mono_ns: int, wall_ms: int, problems=None) -> None:
        tr = self.trackers[kind]
        prev = self.books.get(kind)
        if prev is not None and t <= prev["time_ms"]:
            tr.on_no_progress(mono_ns)      # frozen or repeated: never refreshes freshness
            return
        problems = problems or {"bid": [], "ask": []}
        self.books[kind] = {"bids": bids, "asks": asks, "time_ms": t, "recv_wall_ms": wall_ms, "problems": problems,
                            "bad": {s: bk.problem_reason(s, problems[s]) for s in ("bid", "ask")},
                            "bad_top": {s: (bk.problem_reason(s, problems[s]) if problems[s] and problems[s][0]["index"] == 0
                                            else None) for s in ("bid", "ask")}}
        tr.on_progress(mono_ns, source_ms=t)
        if kind == "book_fast":
            bad = self.books[kind]["bad_top"]
            if bad.get("bid") or bad.get("ask"):
                # The best level was malformed, so the mid at this time is unknown: record that, so a later
                # depth book never borrows an older mid across it.
                self.fast_mids.append((t, None, bad.get("bid") or bad.get("ask")))
            else:
                mid = bk.mid_of(bids, asks)
                self.fast_mids.append((t, mid, None if mid is not None else "empty side"))
            self.offsets.append(wall_ms - t)

    def on_depth_poll(self, nsf: int, resp: dict, mono_ns: int, wall_ms: int) -> None:
        kind = {4: "book_sf4", 3: "book_sf3"}[nsf]
        levels = resp.get("levels") or [[], []]
        t = resp.get("time")
        if t is None:
            return
        bids, pb = bk.parse_levels(levels[0])
        asks, pa = bk.parse_levels(levels[1])
        self._apply_book(kind, bids, asks, int(t), mono_ns, wall_ms, {"bid": pb, "ask": pa})

    def on_trades(self, data: list, mono_ns: int, wall_ms: int) -> None:
        fast = self.books.get("book_fast")
        was_open = self.store.segment_start_ms is not None
        seg_before = self.store.segment_start_ms
        wm_before = self.store.watermark_ms
        new = self.store.on_message(data, fallback_start_ms=fast["time_ms"] if fast else None)
        if was_open and self.store.segment_start_ms != seg_before:
            # A malformed trade closed the segment.
            self._after_boundary(wm_before)
        if not was_open and self.store.segment_start_ms is not None:
            self.deck.reset_segment(self.store.segment_start_ms)
        advanced = (new and self.store.watermark_ms is not None
                    and (wm_before is None or self.store.watermark_ms > wm_before))
        if advanced:
            # Progress is the trades watermark strictly advancing; a late or same-time trade is counted
            # in the execution set but never makes the feed fresh again.
            self.trackers["trades"].on_progress(mono_ns, source_ms=self.store.watermark_ms)
        else:
            self.trackers["trades"].on_no_progress(mono_ns)
        if new:
            self.deck.advance(self.store, new_trades=new)
            if not getattr(self, "node_bursts", False):     # node mode: bursts come from node fills instead
                self.deck.check_burst(self.store, new)

    def on_unattributed_trades(self, data: list, reason: str, mono_ns: int, wall_ms: int) -> None:
        """A trades message that may belong to this market but can't be attributed to it: coverage can't be
        claimed across it, so the segment ends as a gap and freshness restarts (feed.dispatch)."""
        fast = self.books.get("book_fast")
        wm_before = self.store.watermark_ms
        if self.store.force_boundary(data, reason, fast["time_ms"] if fast else None):
            self._after_boundary(wm_before)
        self.trackers["trades"].on_no_progress(mono_ns)

    def _after_boundary(self, wm_before: int | None) -> None:
        """A gap inside a connection: the Flow Deck ends any regime at it, and trade freshness restarts, so
        nothing from the new segment is fresh until a valid trade advances it."""
        reason = self.store.gaps[-1]["reason"] if self.store.gaps else "a coverage gap"
        self.deck.on_gap(wm_before, reason)
        self.deck.reset_segment(self.store.segment_start_ms)
        self.trackers["trades"].restart(f"the coverage gap at {iso_ms(self.store.segment_start_ms)} ({reason})")

    def on_bbo(self, data: dict, mono_ns: int, wall_ms: int) -> None:
        pair = data.get("bbo") or [None, None]
        bid = fnum(pair[0].get("px")) if pair[0] else None
        ask = fnum(pair[1].get("px")) if pair[1] else None
        self.bbo = {"time_ms": data.get("time"), "bid": bid, "ask": ask}
        self.trackers["bbo"].on_no_progress(mono_ns)
        self.trackers["bbo"].source_ms = data.get("time")

    def on_ctx(self, data: dict, mono_ns: int, wall_ms: int) -> None:
        raw = data.get("ctx") or {}
        key = json.dumps({k: raw.get(k) for k in CTX_KEYS}, sort_keys=True)
        fast = self.books.get("book_fast")
        if key == self.ctx_key:
            self.trackers["ctx"].on_no_progress(mono_ns)
            return
        self.ctx_key = key
        self.ctx = {k: fnum(raw.get(k)) for k in CTX_KEYS}
        self.trackers["ctx"].on_progress(mono_ns, watermark_ms=fast["time_ms"] if fast else None)

    # --- snapshot ------------------------------------------------------------
    def build(self, now_ns: int, link: str, snap_id: str, mode: str, built_ms: int, clock=None) -> dict:
        fr = {k: tr.evaluate(now_ns, link, built_ms, clock) for k, tr in self.trackers.items()}
        cov = self.store.coverage()
        prov = {"mode": mode, "source": SOURCES[mode], "market": self.market, "snapshot_id": snap_id,
                "snapshot_built_at": iso_ms(built_ms), "dex": dex_of(self.market),
                "coverage": {"trades": {k: cov[k] for k in ("segment_start", "watermark", "observed_s", "gaps")},
                             "book_levels_per_side": "at most 20 returned by Hyperliquid (fast book: 5)"}}

        def reason_for(key: str) -> str | None:
            f = fr[key]
            return None if f["status"] == "fresh" else f"{f['field']} {f['status']}: {f['reason']}"

        # --- book
        fast, full = self.books.get("book_fast"), self.books.get("book_full")
        r_fast = reason_for("book_fast")
        if r_fast or fast is None:
            top = {k: na(r_fast or "no fast book yet", u) for k, u in
                   (("best_bid", "USD"), ("best_ask", "USD"), ("mid", "USD"), ("spread", "USD"), ("spread_bps", "bps"))}
        else:
            top = bk.top_of_book(fast["bids"], fast["asks"], fast.get("bad_top"))
            top["book_time_ms"] = fast["time_ms"]
        r_full = reason_for("book_full")
        ladder = ({"bids": [], "asks": [], "reason": r_full or "no full book yet", "api_count_note": bk.API_COUNT_NOTE,
                   "largest": {"bid": None, "ask": None, "reason": r_full or "no full book yet"}}
                  if r_full or full is None else bk.ladder_rows(full["bids"], full["asks"], full.get("problems")) | {"book_time_ms": full["time_ms"]})
        depth_books = []
        for kind, label in (("book_full", "full precision"), ("book_sf4", "nSigFigs=4"), ("book_sf3", "nSigFigs=3")):
            b = self.books.get(kind)
            r = reason_for(kind)
            if b is None or r:
                depth_books.append({"kind": label, "usable": False, "reason": r or "not received yet"})
                continue
            mid, why = self._mid_for(b["time_ms"])
            if mid is None:
                depth_books.append({"kind": label, "usable": False, "reason": why})
                continue
            depth_books.append({"kind": label, "usable": True, "bids": b["bids"], "asks": b["asks"], "mid": mid,
                                "time_ms": b["time_ms"], "bucket_width": bk.bucket_width(b["bids"], b["asks"]),
                                "bad": b.get("bad") or {}})
        observed = {}
        for b in depth_books:
            if b["usable"]:
                observed[b["kind"]] = {s: ({"label": "observed depth within returned levels",
                                            "notional_usd": na(b["bad"][s], "USD")} if b["bad"].get(s)
                                           else bk.observed_depth(b[s + "s"], b["mid"], s)) for s in ("bid", "ask")}
                observed[b["kind"]].update({"book_time_ms": b["time_ms"], "edge_resolution": b["bucket_width"]})
            else:
                observed[b["kind"]] = {"reason": b["reason"]}
        bbo = {"value": None, "status": fr["bbo"]["status"], "reason": fr["bbo"]["reason"],
               "last_event_source_time": iso_ms(self.bbo["time_ms"]) if self.bbo else None,
               "matches_fast_book_top": (None if not self.bbo or not fast or not fast["bids"] or not fast["asks"]
                                         else self.bbo["bid"] == fast["bids"][0][0] and self.bbo["ask"] == fast["asks"][0][0])}

        # --- flow
        r_tr = reason_for("trades")
        wm, seg = self.store.watermark_ms, self.store.segment_start_ms
        if wm is not None and seg is not None:
            fstart = max(seg, wm - 15 * 60 * 1000)
            ftrades = self.store.window(fstart, wm, start_inclusive=False)
            fwin = {"start_exclusive_ms": fstart, "start": iso_ms(fstart), "end_ms": wm, "end": iso_ms(wm),
                    "since": "segment start" if fstart == seg else "15-minute retention limit",
                    "length_s": round((wm - fstart) / 1000, 3)}
        else:
            ftrades, fwin = [], {"reason": r_tr or "no coverage segment yet"}
        flow_block = {
            "window": fwin,
            "values": flow.flow_values(ftrades, r_tr),
            "cvd": flow.cvd_buckets(self.store, r_tr),
            "large_prints": flow.large_prints(ftrades, self.cfg.large_print_threshold(self.market), r_tr),
            "tape": [t.as_dict(counted, "current" if seg == self.store.segment_no and self.store.segment_start_ms is not None
                               else "before a gap (an earlier coverage segment)")
                     for t, counted, seg in reversed(self.store.tape)][:40],
        }

        # --- context
        r_ctx = reason_for("ctx")
        c = self.ctx or {}
        if r_ctx or not self.ctx:
            r = r_ctx or "no context yet"
            ctx_block = {k: na(r, u) for k, u in (("mark", "USD"), ("oracle", "USD"), ("mid", "USD"),
                                                   ("funding", "fraction per 1h"), ("funding_pct_per_hour", "% per 1h"),
                                                   ("open_interest", "coin"),
                                                   ("open_interest_usd", "USD"), ("day_notional_volume", "USD"),
                                                   ("prev_day_px", "USD"))}
        else:
            oi = c.get("openInterest")
            ctx_block = {
                "mark": val(c.get("markPx"), "USD"), "oracle": val(c.get("oraclePx"), "USD"),
                "mid": val(c.get("midPx"), "USD") if c.get("midPx") is not None else na("midPx absent", "USD"),
                "funding": val(c.get("funding"), "fraction per 1h"),
                "funding_pct_per_hour": val(rnd(c["funding"] * 100, 6) if c.get("funding") is not None else None, "% per 1h"),
                "open_interest": val(oi, "coin"),
                "open_interest_usd": val(rnd(oi * c["markPx"], 2) if oi is not None and c.get("markPx") else None, "USD",
                                         "needs open interest and mark"),
                "day_notional_volume": val(c.get("dayNtlVlm"), "USD"),
                "prev_day_px": val(c.get("prevDayPx"), "USD"),
            }
        ctx_block["units_note"] = ("funding is per 1 hour (funding page: 'paid every hour'; the API field's period isn't stated); "
                                   "open interest is in coin (the docs' size convention; the field's unit isn't stated); "
                                   "day notional volume is USD over Hyperliquid's own day window")

        # --- deck, wallets, chart (node buckets come from the live node feed's fills only, never public trades)
        node_client = getattr(self, "node_client", None)
        node_view = getattr(self, "node_view", None) or {}
        deck = {"card": self.deck.card(r_tr), "events": self.deck.events_list()}
        now = (built_ms, now_ns)          # node fills are judged at this snapshot's build time, as the node views are
        deck["card"]["activity_buckets"] = self._node_card_buckets(deck["card"], node_client, now)
        deck["events"] = self._node_events(deck["events"], node_client, node_view.get("live"), now)
        deck["bursts"] = self._burst_info(node_client)
        wallet_panels = {}
        for n in WALLET_WINDOWS_MIN:
            wallet_panels[f"{n}m"] = self._wallet_panel(n, r_tr)
            self._node_wallet_splits(wallet_panels[f"{n}m"], node_client, now)
        chart = self._chart(top, ctx_block, ladder)
        unavailable = [{"field": v["field"], "status": v["status"], "reason": v["reason"]}
                       for v in fr.values() if v["status"] != "fresh"]
        return {"market": self.market, "meta": self.meta, "provenance": prov, "freshness": fr,
                "unavailable": unavailable,
                "clock": self._clock(),
                "book": {"top": top, "ladder": ladder,
                         "depth": {"bands": bk.depth_bands(depth_books[0], depth_books[1:]), "observed": observed,
                                   "note": ("exact band depth comes only from full-precision returned levels, and only where they "
                                            "reach the band edge on that side; aggregated nSigFigs books give a separately labeled "
                                            "approximation")},
                         "bbo": bbo},
                "flow": flow_block, "context": ctx_block, "deck": deck, "wallets": wallet_panels,
                "chart": chart,
                "node": self._node_block(node_view, node_client)}

    # --- node mode (Handoff 1c Part B) ------------------------------------------------
    def _node_block(self, view: dict, client) -> dict:
        live = view.get("live")
        if live is None:
            live = {"available": False, "reason": "needs node mode: no node feed configured (setting node_feed)"}
        return {"panels": list(NODE_PANELS), "live": live, "replay": view.get("replay"),
                "separation": "live and replay are separate partitions; replay is never drawn over live prices or mixed "
                              "into current metrics"}

    def _node_card_buckets(self, card: dict, client, now=(None, None)) -> dict:
        from .node.panels import deck_buckets
        base = self.deck._buckets()
        if client is None:
            return base | ({"reason": "needs node mode: no node feed configured (setting node_feed)"}
                           if card.get("complete") else {})
        if not card.get("complete"):
            from .node.panels import flow_meta
            why = (card.get("regime") or {}).get("reason") or "the card's 90 s window isn't complete yet"
            return {k: na(why, "USD") for k in ("new_long", "cover", "new_short", "exit")} | {
                "stream": "node fills", "available": False, "reason": why} | flow_meta(client, self.market, *now)
        # The card's window is [start, end]; node fills are counted over the same block-time span.
        b = deck_buckets(client, self.market, card["window_start_ms"] - 1, card["window_end_ms"], *now)
        return _bucket_metrics(b, "the card's 90 s window") | {"hook": base.get("hook")}

    def _node_events(self, events: list, client, live_view: dict | None, now=(None, None)) -> list:
        """The Flow Deck's events. In node mode, bursts come only from node fills (the burst calibration's rule,
        once per node block; hl_desk/node/bursts.py) and public-trades bursts aren't shown; in public mode, bursts
        are the public-trades rule's, with node buckets marked as needing node mode. Published events are shared with
        earlier snapshots, so they're never edited: each is copied."""
        from .node.panels import _jsonable, flow_meta
        node_mode = getattr(self, "node_bursts", False)
        out = []
        for ev in events:
            if ev.get("type") == "burst":
                if node_mode:
                    continue
                ev = dict(ev) | {"node_activity": {"available": False, "reason": "needs node mode: no node feed configured"}}
            out.append(ev)
        mf = client.markets.get(self.market) if (node_mode and client is not None) else None
        rule = mf.flow.bursts if mf is not None else None
        if rule is not None:
            # Each burst's own-side buckets, from exactly its window's node fills, with the node fills' freshness as
            # of this build; withheld with the reason while the node fills aren't usable.
            meta = flow_meta(client, self.market, *now)
            fresh = meta["freshness"]
            usable = fresh.get("state") in ("LIVE", "REPLAY")
            for ev in rule.events:
                if usable:
                    # A node burst fires only on a complete window of valid executions, so its buckets are exact.
                    b = {"available": True, "stream": "node fills",
                         "window": {"start_ms": ev["window_start_ms_exclusive"], "end_ms": ev["window_end_ms"]},
                         "coverage": {"covers": True, "executions": ev["executions"],
                                      "note": "the burst's own window: complete, every execution valid"},
                         **_jsonable(ev["activity"])}
                    m = _bucket_metrics(b, "the burst's 60 s window", only=ev["side"]) | meta
                else:
                    m = {"stream": "node fills", "available": False,
                         "reason": f"node fills {fresh.get('state')}: {fresh.get('reason')}"} | meta
                out.append(_jsonable({k: v for k, v in ev.items() if k != "activity"}) | {"node_activity": m})
        for le in (live_view or {}).get("liquidation_events") or []:
            out.append(dict(le) | {"at": iso_ms(le["at_ms"]), "source": "node fills"})
        out.sort(key=lambda e: e.get("at_ms") or 0, reverse=True)
        return out

    def _burst_info(self, client) -> dict:
        """Which burst rule this market uses, and its threshold (shown with the Flow Deck)."""
        if not getattr(self, "node_bursts", False):
            return {"mode": "public", "stream": "public trades",
                    "rule": schema.load()["alerts"]["burst"]["formula"],
                    "threshold_usd": self.deck.burst_threshold, "setting": "burst_min_usd"}
        mf = client.markets.get(self.market) if client is not None else None
        rule = mf.flow.bursts if mf is not None else None
        if rule is None:
            why = ("no node fills for this market yet" if client is not None else
                   "the configured node feed isn't a live source (a replay is never live)")
            return {"mode": "node", "stream": "node fills", "available": False, "reason": why}
        from .node.panels import _jsonable
        return {"mode": "node", "stream": "node fills", "available": True, "setting": "node_burst_min_usd"} | _jsonable(
            rule.status())

    def _node_wallet_splits(self, panel: dict, client, now=(None, None)) -> None:
        if not panel.get("window"):
            return
        users = [x["address"] for side in ("buyers", "sellers") for x in ((panel.get(side) or {}).get("listed") or [])]
        if client is None:
            panel["node_activity_split"] = {"available": False, "reason": "needs node mode: no node feed configured"}
            return
        from .node.panels import wallet_buckets
        w = panel["window"]
        panel["node_activity_split"] = wallet_buckets(client, self.market, users, w["start_exclusive_ms"], w["end_ms"], *now)

    def _mid_for(self, t: int) -> tuple[float | None, str | None]:
        """(mid, reason): the fast book's mid closest to and not after t, within 1.5 s. A malformed best level
        makes that mid unknown; the full book's own mid is used only at the same time and only if valid."""
        best = None
        for ft, mid, why in self.fast_mids:
            if ft <= t and t - ft <= 1500 and (best is None or ft > best[0]):
                best = (ft, mid, why)
        if best is not None:
            if best[1] is None:
                return None, f"the full-precision mid at that time is unknown: {best[2]}"
            return best[1], None
        full = self.books.get("book_full")
        if full and full["time_ms"] == t:
            bad = full.get("bad_top") or {}
            if bad.get("bid") or bad.get("ask"):
                return None, f"the full-precision mid at that time is unknown: {bad.get('bid') or bad.get('ask')}"
            mid = bk.mid_of(full["bids"], full["asks"])
            return mid, None if mid is not None else "empty side"
        return None, "no full-precision mid within 1.5 s of this book"

    def _clock(self) -> dict:
        if not self.offsets:
            return {"offset_ms": None, "clock_skew_suspected": None}
        med = median(self.offsets)                  # the mean of the two middle offsets when their count is even
        warn = schema.freshness_field("clock_skew")["warn_abs_ms"]
        return {"offset_ms": med, "warn_abs_ms": warn, "clock_skew_suspected": abs(med) > warn,
                "note": ("local wall clock minus source time at receipt. Progress ages use the monotonic clock; source recency "
                         "compares source time with the local wall clock under the clock bound, so a source time ahead "
                         "of local time beyond that bound withholds freshness (see clock_relationship)")}

    def _wallet_panel(self, minutes: int, r_tr: str | None) -> dict:
        wm, seg = self.store.watermark_ms, self.store.segment_start_ms
        if r_tr or wm is None or seg is None:
            return {"window_min": minutes, "reason": r_tr or "no coverage segment yet", "buyers": None, "sellers": None}
        req = wm - minutes * 60_000
        start = max(req, seg)
        trades = self.store.window(start, wm, start_inclusive=False)
        window = {"requested_min": minutes, "start_exclusive_ms": start, "start": iso_ms(start), "end_ms": wm,
                  "end": iso_ms(wm), "length_s": round((wm - start) / 1000, 3), "complete": start == req,
                  "completeness": ("complete" if start == req else
                                   f"incomplete: only {(wm - start) / 1000:.0f} s observed since the segment start (start or last gap)")}
        return wallets.panel(trades, window, self.cfg.wallet_target_share_pct, self.cfg.wallet_cap)

    def wallet_execution_set(self, minutes: int, end_ms: int, start_ms: int):
        return self.store.window(start_ms, end_ms, start_inclusive=False)

    def _chart(self, top: dict, ctx_block: dict, ladder: dict) -> dict:
        wm, seg = self.store.watermark_ms, self.store.segment_start_ms
        candles = []
        if wm is not None and seg is not None:
            # Start at the candle boundary at or before (watermark - 15 min), so the oldest candle holds every trade
            # of its own interval; trades are retained for 16 minutes, so none of them has been pruned. The store
            # holds only the segment's counted trades, so nothing before the segment start is included.
            first = (wm - CHART_SPAN_MS) // CANDLE_MS * CANDLE_MS
            cur = None
            for t in self.store.window(first, wm, start_inclusive=True):
                b = t.time // CANDLE_MS * CANDLE_MS
                if cur is None or cur["t"] != b:
                    cur = {"t": b, "time": b // 1000, "open": t.px, "high": t.px, "low": t.px, "close": t.px, "_vol": 0.0}
                    candles.append(cur)
                cur["high"] = max(cur["high"], t.px)
                cur["low"] = min(cur["low"], t.px)
                cur["close"] = t.px
                cur["_vol"] += t.notional          # summed exactly, rounded once below
        lines = []
        for key, label in (("best_bid", "best bid"), ("best_ask", "best ask")):
            if top.get(key, {}).get("value") is not None:
                lines.append({"price": top[key]["value"], "label": label, "kind": key})
        for key, label in (("mark", "mark"), ("oracle", "oracle")):
            if ctx_block.get(key, {}).get("value") is not None:
                lines.append({"price": ctx_block[key]["value"], "label": label, "kind": key})
        for side in ("bids", "asks"):
            rows = ladder.get(side) or []
            if rows:
                big = bk.largest_level(rows, side)          # the same level as hl_market's levels.largest
                lines.append({"price": big["px"], "kind": f"largest_{side[:-1]}_level",
                              "label": f"largest returned {side[:-1]} level: {big['sz']} (API count "
                                       f"{big['api_count'] if big['api_count'] is not None else 'unavailable'}, aggregated)"})
        for c in candles:
            c["volume_usd"] = rnd(c.pop("_vol"), 2)
            c["complete"] = c["t"] > (seg or 0) and c["t"] + CANDLE_MS - 1 <= (wm or 0)
        return {"candle_s": CANDLE_MS // 1000, "candles": candles, "price_lines": lines,
                "candles_note": ("each candle summarizes every public trade in its own aligned 15 s interval, from the boundary at or "
                                 "before (watermark - 15 min); 'complete' is false for a candle that starts at or before the segment "
                                 "start or ends after the trades watermark"),
                "gaps": list(self.store.gaps)[-10:],
                "note": "candles from public trades in the current coverage segment; price lines from the fast book, context and the 20-level book"}


def _bucket_metrics(b: dict, window_label: str, only: str | None = None) -> dict:
    """Activity buckets as metrics: each value null with a reason when unavailable; each percentage over its
    own side's aggressive notional from the same node fills (one population)."""
    keys = {"buying": ("new_long", "cover"), "selling": ("new_short", "exit")}
    out = {"stream": "node fills", "window_label": window_label, "provenance": b.get("provenance"),
           "freshness": b.get("freshness"), "coverage": b.get("coverage")}
    if not b.get("available"):
        reason = b.get("reason") or "node fills unavailable"
        for side, ks in keys.items():
            if only and side != only:
                continue
            for k in ks:
                out[k] = na(reason, "USD")
        if b.get("unclassified_usd") is not None:
            out["unclassified_usd"] = val(b["unclassified_usd"], "USD")
        return out | {"available": False, "reason": reason}
    out["available"] = True
    out["window"] = b.get("window")
    for side, ks in keys.items():
        if only and side != only:
            continue
        sd = b[side]
        out[f"{side}_aggressive_usd"] = val(sd["aggressive_usd"], "USD")
        for k in ks:
            if sd["available"]:
                out[k] = val(sd["buckets"][k], "USD")
                p = (sd.get("pct") or {}).get(k)
                out[f"{k}_pct"] = (val(round(p, 4), f"% of aggressive {side} notional") if p is not None else
                                   na(sd.get("reason") or f"no aggressive {side} notional", f"% of aggressive {side} notional"))
            else:
                out[k] = na(sd["reason"], "USD")
                out[f"{k}_pct"] = na(sd["reason"], f"% of aggressive {side} notional")
        out[f"{side}_unclassified_usd"] = val(sd["unclassified_usd"], "USD")
    out["no_single_taker_usd"] = val(b.get("no_single_taker_usd"), "USD")
    out["uncertain_usd"] = val(b.get("uncertain_usd"), "USD")
    out["population"] = ("each bucket counts the aggressive (taker) side's own position effects; percentages divide by "
                         "that side's aggressive notional over the same node fills; not a change in open interest")
    return out


class DeskState:
    def __init__(self, cfg: DeskConfig, mode: str = "LIVE") -> None:
        self.cfg = cfg
        self.node = None          # hl_desk.node.manager.NodeManager when running under a collector
        self.mode = mode
        self.run_id = secrets.token_hex(3)
        self.seq = 0
        self.markets: dict[str, MarketState] = {}
        self.unsupported: list[dict] = []
        self.metadata: dict | None = None
        self.link = "stopped"
        self.collector: dict = {"state": "stopped"}
        self.frozen_last: dict = {}
        self.clock = ClockBound(cfg.clock_bound_s)

    def set_markets(self, checks: list[dict]) -> None:
        for c in checks:
            if c["supported"]:
                self.markets[c["market"]] = MarketState(c["market"], self.cfg, c["meta"])
            else:
                self.unsupported.append(c)

    def build_snapshot(self, now_ns: int, wall_ms: int) -> dict:
        self.seq += 1
        sid = f"hd-{self.run_id}-{self.seq:07d}"
        link = self.link
        # The exact executions each market's panels were built from, kept beside the snapshot (not serialized).
        self.frozen_last = {m: (s.store.segment_start_ms, tuple(s.store.trades)) for m, s in self.markets.items()}
        node_views = self.node.views(wall_ms, now_ns) if self.node is not None else {}
        for m, s in self.markets.items():
            s.node_view = node_views.get(m)
            s.node_client = self.node.live_client() if self.node is not None else None
            # Node mode (a node feed is configured): bursts come from node fills, never public trades.
            s.node_bursts = self.node is not None and getattr(self.node, "live", None) is not None
        return {
            "snapshot_id": sid,
            "built_at_ms": wall_ms,
            "built_at": iso_ms(wall_ms),
            "built_mono_ns": now_ns,
            "mode": self.mode,
            "plugin_version": PLUGIN_VERSION,
            "schema": schema.schema_ref(),
            "collector": dict(self.collector),
            "markets": {m: s.build(now_ns, link, sid, self.mode, wall_ms, self.clock) for m, s in self.markets.items()},
            "clock_relationship": dict(zip(("bound_s", "basis"), self.clock.get())),
            "unsupported_markets": list(self.unsupported),
            "metadata": self.metadata,
            "node_status": self.node.status() if self.node is not None else {"live": None, "replay": None},
        }


__all__ = ["DeskState", "MarketState", "NS"]
