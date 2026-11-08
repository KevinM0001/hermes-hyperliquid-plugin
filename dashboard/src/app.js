/*
 * hyperliquid-desk: the Hermes dashboard tab (readable source, ES2019, no JSX, no build step
 * beyond concatenation; see scripts/build_dashboard.py).
 *
 * The tab computes nothing. Once a second it reads GET /api/plugins/hyperliquid-desk/snapshot
 * and formats what that snapshot says: thousands separators, $ with K/M/B, bps, %, UTC times.
 * Every metric is defined once in schema/metrics.v1.json; the data contract is docs/SNAPSHOT.md.
 *
 * Rules this file keeps:
 * - a null value shows as "unavailable" with the snapshot's reason, never as 0;
 * - every panel shows where its numbers come from (provenance) and how fresh they are;
 * - public book levels are aggregated: a level is never shown as an individual order and its
 *   count is labelled "API count";
 * - panels and columns that need the node say "needs node mode" and carry a data-node-hook
 *   attribute for Handoff 1c;
 * - no order code: the only actions are the collector's start, stop and restart (POST), and the
 *   tab never starts the collector by itself;
 * - only the documented plugin SDK: SDK.React, SDK.hooks, SDK.fetchJSON and
 *   window.__HERMES_PLUGINS__.register. Nothing is loaded from the network except this
 *   plugin's own API routes.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REGISTRY = window.__HERMES_PLUGINS__;
  if (!SDK || !REGISTRY) {
    console.warn("[hyperliquid-desk] window.__HERMES_PLUGIN_SDK__ is missing; the tab was not registered");
    return;
  }

  const React = SDK.React;
  const h = React.createElement;
  const Fragment = React.Fragment;
  const useState = SDK.hooks.useState;
  const useEffect = SDK.hooks.useEffect;
  const useRef = SDK.hooks.useRef;
  const useCallback = SDK.hooks.useCallback;

  // Defined by the TradingView Lightweight Charts standalone build that dist/index.js carries
  // above this file. Captured once so a later script can't swap it under the tab.
  const LWC = window.LightweightCharts;

  const PLUGIN = "hyperliquid-desk";
  const API = "/api/plugins/" + PLUGIN;
  const POLL_MS = 1000;
  const LATEST_MAX_S = 5;  // a latest snapshot older than this is no longer current (the backend's LATEST_MAX_AGE_S)
  const WALLET_WINDOWS = ["1m", "5m", "15m"];
  const MINUS = "\u2212";
  const ELLIPSIS = "\u2026";
  const ATTRIBUTION = "Charts by TradingView Lightweight Charts\u2122 \u00b7 Copyright (\u0441) 2025 TradingView, Inc. \u00b7 ";
  const TV_URL = "https://www.tradingview.com/";

  // Colours used inside the chart canvas (CSS can't reach it). They match dist/style.css.
  const C = {
    bid: "#4C9AFF", ask: "#FF9A4D", text: "#93A1AE", grid: "#161E27", border: "#1E2731",
    pane: "#0D131A", mark: "#E7EDF3", oracle: "#B39DFF", gap: "#F2C14E", dim: "#6B7A88"
  };
  const MONO = "'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace";

  // Regime badge colours (Flow Deck). Anything not listed (QUIET, THIN, SPARSE, BALANCED,
  // or a state added later) is dim.
  const REGIME_TONE = {
    "ABSORBING": "cyan",
    "VACUUM-UP": "yellow", "VACUUM-DOWN": "yellow",
    "PULLED-OFFERS": "green", "TRENDING-UP": "green",
    "PULLED-BIDS": "red", "TRENDING-DOWN": "red"
  };
  // meaning.key_number names one of the card's numbers; this maps the name to its field.
  const KEY_NUMBER_FIELD = {
    "IMPACT": "impact", "PRESSURE": "pressure", "NET FLOW": "net_flow_usd",
    "MOVE": "move_bps", "VOLUME": "volume_usd"
  };
  const STATUS_TONE = {
    fresh: "ok", stale: "warn", delayed: "warn", unverified: "info", warming_up: "info",
    disconnected: "bad", stopped: "bad", missing: "dim", expired: "warn"
  };
  const FRESHNESS_LEGEND = [
    ["fresh", "the feed is still advancing and its source time is recent under a known clock bound"],
    ["delayed", "the feed advances but its source time is old: a backlog or a delayed source"],
    ["stale", "a field that should keep advancing hasn't, within its max age"],
    ["unverified", "no verified progress signal (bbo always), so silence can't be told from a stall"],
    ["warming_up", "nothing received yet on this connection, or a window lacks coverage"],
    ["disconnected", "the collector has no connection"],
    ["stopped", "the collector isn't running"],
    ["missing", "nothing has ever been received for this field"]
  ];

  // ------------------------------------------------------------------ small helpers

  function g(obj) {
    let o = obj;
    for (let i = 1; i < arguments.length; i++) {
      if (o === null || o === undefined) return undefined;
      o = o[arguments[i]];
    }
    return o;
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function cx() {
    const out = [];
    for (let i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(" ");
  }
  function errText(e) {
    if (!e) return "unknown error";
    return e.message || String(e);
  }

  // ------------------------------------------------------------------ formatting only

  function num(v, maxFrac, minFrac) {
    return v.toLocaleString("en-US", { maximumFractionDigits: maxFrac, minimumFractionDigits: minFrac || 0 });
  }
  function signOf(v, plus) { return v < 0 ? MINUS : (plus && v > 0 ? "+" : ""); }
  function usd(v, plus) {
    const a = Math.abs(v);
    let body;
    if (a >= 1e9) body = num(a / 1e9, 2, 2) + "B";
    else if (a >= 1e6) body = num(a / 1e6, 2, 2) + "M";
    else if (a >= 1e3) body = num(a / 1e3, 1, 1) + "K";
    else body = num(a, 2, 2);
    return signOf(v, plus) + "$" + body;
  }
  function usdSigned(v) { return usd(v, true); }
  function usdFull(v) { return signOf(v) + "$" + num(Math.abs(v), 2, 2); }
  function px(v) { return signOf(v) + num(Math.abs(v), 8); }
  function sz(v) { return num(v, 8); }
  // Prices in one list share the decimals the list's own prices use (formatting only).
  function decimalsOf(values) {
    let d = 0;
    values.forEach(function (v) {
      if (!isNum(v)) return;
      const t = String(v);
      const i = t.indexOf(".");
      if (i >= 0 && t.indexOf("e") < 0) d = Math.max(d, t.length - i - 1);
    });
    return Math.min(d, 8);
  }
  function pxWith(d) { return function (v) { return signOf(v) + num(Math.abs(v), d, d); }; }
  function bps(v) { return signOf(v) + num(Math.abs(v), 2, 2) + " bps"; }
  function bpsSigned(v) { return signOf(v, true) + num(Math.abs(v), 2, 2) + " bps"; }
  function pct(v, digits) {
    const d = digits === undefined ? 1 : digits;
    return num(v, d, d) + "%";
  }
  function pctSigned6(v) { return signOf(v, true) + num(Math.abs(v), 6) + "%"; }
  function intf(v) { return num(v, 0); }
  function ratioSigned(v) { return signOf(v, true) + num(Math.abs(v), 3, 3); }
  function secs(v) { return Math.abs(v) < 10 ? num(v, 1, 1) + " s" : num(v, 0) + " s"; }
  function heldS(v) { return num(v, 0) + "s"; }

  function toMs(t) {
    if (isNum(t)) return t;
    if (typeof t === "string" && t) {
      const ms = Date.parse(t);
      return isNaN(ms) ? null : ms;
    }
    return null;
  }
  function isoOf(t) {
    const ms = toMs(t);
    return ms === null ? null : new Date(ms).toISOString();
  }
  // UTC clock times. Callers pass a time the snapshot gave; a missing time says so.
  function hms(t) { const s = isoOf(t); return s ? s.slice(11, 19) : "(no time)"; }
  function hmsms(t) { const s = isoOf(t); return s ? s.slice(11, 23) : "(no time)"; }
  function dateTime(t) { const s = isoOf(t); return s ? s.slice(0, 10) + " " + s.slice(11, 23) + " UTC" : "(no time)"; }
  function localClock(ms) { return new Date(ms).toLocaleTimeString("en-GB", { hour12: false }); }
  function shortAddr(a) {
    return typeof a === "string" && a.length > 12 ? a.slice(0, 6) + ELLIPSIS + a.slice(-4) : String(a);
  }
  // Hyperliquid info answers carry numbers as strings; format them when they parse.
  function infoNum(x, fmt) {
    if (x === null || x === undefined || x === "") return "not in the answer";
    const n = typeof x === "number" ? x : Number(x);
    return isFinite(n) ? fmt(n) : String(x);
  }

  // ------------------------------------------------------------------ value + status primitives

  function valueOf(m) { return m && m.value !== null && m.value !== undefined ? m.value : null; }
  function reasonOf(m) {
    if (m === null || m === undefined) return "not in this snapshot";
    return m.reason || "no value in this snapshot and no reason given";
  }

  // "unavailable" plus the reason. compact puts the reason in the tooltip only, for places where
  // the same reason is already written once nearby.
  function Unavailable(props) {
    const reason = String(props.reason);
    if (reason.indexOf("needs node mode") === 0) {
      const rest = reason.slice("needs node mode".length).replace(/^[:\s]+/, "");
      return h("span", { className: cx("hd-node-inline", props.block && "hd-na-block"), title: reason },
        "needs node mode",
        props.compact || !rest ? null : h("span", { className: "hd-na-why" }, ": " + rest));
    }
    return h("span", { className: cx("hd-na", props.block && "hd-na-block"), title: reason },
      "unavailable",
      props.compact ? null : h("span", { className: "hd-na-why" }, ": " + reason));
  }

  // A field missing from a row the snapshot did send (should not happen; said, not blanked).
  function missing() { return h(Unavailable, { reason: "not in this snapshot", compact: true }); }

  function V(props) {
    const v = valueOf(props.m);
    if (v === null) {
      return h(Unavailable, { reason: reasonOf(props.m), compact: props.compact, block: props.block });
    }
    return h("span", { className: cx("hd-num", props.className), title: props.title },
      props.fmt ? props.fmt(v) : String(v));
  }

  function toneOfStatus(s) { return STATUS_TONE[s] || "dim"; }

  function freshTitle(f) {
    return [f.field, "status: " + f.status,
      f.progress_signal ? "progress signal: " + f.progress_signal : null,
      f.source_time ? "source time: " + f.source_time : null,
      isNum(f.progress_age_s) ? "progress age: " + f.progress_age_s + " s" : null,
      isNum(f.max_age_s) ? "max age: " + f.max_age_s + " s" : null,
      f.reason ? "reason: " + f.reason : null].filter(Boolean).join("\n");
  }

  function Fresh(props) {
    const f = props.f;
    if (!f) {
      return h("span", { className: "hd-chip hd-tone-dim" }, (props.label || "field") + ": freshness not in snapshot");
    }
    const parts = [String(f.status).replace("_", " ")];
    if (isNum(f.progress_age_s)) {
      parts.push(secs(f.progress_age_s) + (isNum(f.max_age_s) ? " / max " + secs(f.max_age_s) : ""));
    }
    return h("span", { className: "hd-chip hd-tone-" + toneOfStatus(f.status), title: freshTitle(f) },
      h("span", { className: "hd-dot", "aria-hidden": "true" }),
      (props.label || f.field) + " \u00b7 " + parts.join(" \u00b7 "));
  }

  function StatusDot(props) {
    const f = props.f;
    if (!f) return null;
    return h("span", {
      className: "hd-sdot hd-tone-" + toneOfStatus(f.status),
      title: freshTitle(f), role: "img", "aria-label": f.field + " " + f.status
    });
  }

  // The reasons of non-fresh fields, written out (not only on hover).
  function FreshNotes(props) {
    const list = (props.fields || []).filter(function (f) { return f && f.status !== "fresh"; });
    if (!list.length) return null;
    return h("div", { className: "hd-fresh-notes" }, list.map(function (f) {
      return h("div", { key: f.field, className: "hd-tone-text-" + toneOfStatus(f.status) },
        h("b", null, f.field + " " + String(f.status).replace("_", " ") + ": "), f.reason || "no reason given");
    }));
  }

  function Badge(props) {
    return h("span", { className: "hd-badge hd-badge-" + props.kind, title: props.title }, props.children);
  }

  function ModeBadge(props) {
    const mode = props.mode || "UNKNOWN";
    return h(Badge, { kind: "mode-" + String(mode).toLowerCase(), title: props.title || ("mode: " + mode) }, mode);
  }

  // Panel provenance: the snapshot mode (unless LIVE) and the public source the panel reads.
  function Prov(props) {
    return h(Fragment, null,
      props.mode && props.mode !== "LIVE" ? h(ModeBadge, { mode: props.mode, title: props.source }) : null,
      h(Badge, { kind: "api", title: props.source }, "PUBLIC API \u00b7 " + props.what));
  }

  function NodeBadge(props) {
    return h(Badge, { kind: "node", title: "needs node mode" }, "NODE \u00b7 " + (props.what || "needs node mode"));
  }

  function Panel(props) {
    return h("section", {
      className: cx("hd-card", props.className),
      "aria-label": props.label || props.title,
      "data-panel": props.id,
      "data-node-hook": props.nodeHook
    },
    h("div", { className: "hd-card-head" },
      h("div", { className: "hd-card-titles" },
        h("h2", null, props.title),
        props.sub ? h("p", null, props.sub) : null),
      props.badges ? h("div", { className: "hd-badges" }, props.badges) : null),
    props.status ? h("div", { className: "hd-card-status" }, props.status) : null,
    h("div", { className: cx("hd-card-body", props.flush && "hd-flush") }, props.children));
  }

  function CopyButton(props) {
    const [msg, setMsg] = useState(null);
    function done(ok) {
      setMsg(ok ? "copied" : "copy failed: select the text");
      setTimeout(function () { setMsg(null); }, 1600);
    }
    function copy() {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(props.text).then(function () { done(true); }, function () { done(false); });
      } else {
        done(false);
      }
    }
    return h("button", { type: "button", className: "hd-btn hd-btn-xs", onClick: copy, title: "Copy " + props.what },
      msg || "copy");
  }

  function Addr(props) {
    const a = props.address;
    if (typeof a !== "string" || !a) return h("span", { className: "hd-na" }, "address not in snapshot");
    const label = props.full ? a : shortAddr(a);
    if (props.onOpen) {
      return h("button", {
        type: "button", className: "hd-addr hd-addr-btn", title: a + " (open this wallet)",
        onClick: function () { props.onOpen(a); }
      }, label);
    }
    return h("span", { className: "hd-addr", title: a }, label);
  }

  // ------------------------------------------------------------------ banners

  function Banners(props) {
    const out = [];
    const resp = props.resp;
    const noSnap = props.noSnap;
    const col = noSnap ? g(noSnap, "runtime", "collector") : g(resp, "collector");
    const state = col && col.state;
    if (noSnap) {
      out.push(h("div", { key: "nosnap", className: "hd-banner hd-banner-bad", role: "alert" },
        h("b", null, "No snapshot. "),
        noSnap.error || "the snapshot route answered without data",
        resp ? " Showing the last snapshot received (" + resp.snapshot_id + ", built " + dateTime(resp.built_at) + "); its values are frozen." : ""));
    }
    if (state && state !== "connected") {
      const bad = state === "stopped" || state === "disconnected";
      const bits = [];
      if (col.since) bits.push("since " + hms(col.since) + " UTC");
      if (isNum(col.reconnects)) bits.push("reconnects " + intf(col.reconnects));
      if (col.last_error) bits.push("last error: " + col.last_error);
      if (isNum(col.next_retry_s) && state === "disconnected") bits.push("next retry " + col.next_retry_s + " s after that");
      out.push(h("div", { key: "col", className: cx("hd-banner", bad ? "hd-banner-bad" : "hd-banner-warn"), role: "status" },
        h("b", null, "Collector " + String(state).toUpperCase() + ". "),
        state === "stopped"
          ? "Nothing is being collected. Use Start (or /hl start in chat); the tab never starts it by itself. "
          : (state === "disconnected" ? "No connection to Hyperliquid; values turn unavailable as their freshness runs out. " : "Not connected yet. "),
        bits.join(" \u00b7 ")));
    }
    if (props.pollErr) {
      out.push(h("div", { key: "poll", className: "hd-banner hd-banner-warn", role: "status" },
        h("b", null, "Last poll failed"), " at " + localClock(props.pollErr.at) + " (local): " + props.pollErr.message + ". ",
        resp ? "Showing the last good snapshot " + resp.snapshot_id + " (built " + dateTime(resp.built_at) + ", received " + localClock(props.lastOkAt) + " local)." : "No snapshot received yet."));
    }
    if (props.hidden) {
      out.push(h("div", { key: "hidden", className: "hd-banner hd-banner-info" }, "Polling paused while this tab is hidden."));
    }
    return out.length ? h("div", { className: "hd-banners" }, out) : null;
  }

  // ------------------------------------------------------------------ header

  function firstReason(list) {
    for (let i = 0; i < list.length; i++) if (valueOf(list[i]) === null) return reasonOf(list[i]);
    return null;
  }

  function Stat(props) {
    return h("div", { className: "hd-stat" },
      h("div", { className: "hd-stat-label" }, props.label, " ", props.f ? h(StatusDot, { f: props.f }) : null),
      h("div", { className: "hd-stat-value" }, props.value),
      h("div", { className: "hd-stat-sub", title: typeof props.sub === "string" ? props.sub : undefined }, props.sub));
  }

  function HeaderStats(props) {
    const m = props.m;
    const top = g(m, "book", "top") || {};
    const ctx = g(m, "context") || {};
    const fr = g(m, "freshness") || {};
    const bookSub = firstReason([top.best_bid, top.best_ask]) ||
      ("l2Book fast \u00b7 " + (isNum(top.book_time_ms) ? hmsms(top.book_time_ms) + " UTC" : "book time not in snapshot"));
    const spreadSub = firstReason([top.spread, top.spread_bps]) || (valueOf(top.mid) !== null ? "mid " + px(top.mid.value) : "mid: " + reasonOf(top.mid));
    const unitsNote = ctx.units_note || "";
    return h("div", { className: "hd-stats" },
      h(Stat, {
        label: "Bid / ask", f: fr.book_fast,
        value: h(Fragment, null, h(V, { m: top.best_bid, fmt: px, compact: true, className: "hd-bid" }), " / ",
          h(V, { m: top.best_ask, fmt: px, compact: true, className: "hd-ask" })),
        sub: bookSub
      }),
      h(Stat, {
        label: "Spread", f: fr.book_fast,
        value: h(Fragment, null, h(V, { m: top.spread, fmt: px, compact: true }), " ",
          h("span", { className: "hd-dim" }, "("), h(V, { m: top.spread_bps, fmt: bps, compact: true }), h("span", { className: "hd-dim" }, ")")),
        sub: spreadSub
      }),
      h(Stat, {
        label: "Mark / oracle", f: fr.ctx,
        value: h(Fragment, null, h(V, { m: ctx.mark, fmt: px, compact: true }), " / ", h(V, { m: ctx.oracle, fmt: px, compact: true })),
        sub: firstReason([ctx.mark, ctx.oracle]) || (valueOf(ctx.prev_day_px) !== null ? "prev day px " + px(ctx.prev_day_px.value) : "prev day px: " + reasonOf(ctx.prev_day_px))
      }),
      h(Stat, {
        label: "Funding, 1h", f: fr.ctx,
        value: h(V, { m: ctx.funding_pct_per_hour, fmt: pctSigned6, compact: true, title: unitsNote }),
        sub: firstReason([ctx.funding_pct_per_hour]) || ("per 1 hour \u00b7 fraction " + (valueOf(ctx.funding) !== null ? String(ctx.funding.value) : "unavailable"))
      }),
      h(Stat, {
        label: "Open interest", f: fr.ctx,
        value: h(V, { m: ctx.open_interest_usd, fmt: usd, compact: true, title: unitsNote }),
        sub: firstReason([ctx.open_interest_usd]) || (valueOf(ctx.open_interest) !== null ? sz(ctx.open_interest.value) + " " + (ctx.open_interest.unit || "") : "coin: " + reasonOf(ctx.open_interest))
      }),
      h(Stat, {
        label: "Day volume", f: fr.ctx,
        value: h(V, { m: ctx.day_notional_volume, fmt: usd, compact: true, title: unitsNote }),
        sub: firstReason([ctx.day_notional_volume]) || "notional, Hyperliquid's day window"
      }));
  }

  function CollectorBox(props) {
    const c = props.collector || {};
    const state = c.state || "unknown";
    const tone = state === "connected" ? "ok" : (state === "stopped" || state === "disconnected" ? "bad" : "warn");
    const actions = [["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]];
    const a = props.action;
    let result = null;
    if (a) {
      const body = a.body || {};
      const act = g(body, "result", "action");
      const ok = a.error ? false : (act ? act.ok : body.ok);
      const text = a.error || (act ? act.message : (body.error || "no message"));
      result = h("div", { className: cx("hd-collector-result", ok ? "hd-tone-text-ok" : "hd-tone-text-bad") },
        a.action + ": " + text + (act && act.at ? " (" + hms(act.at) + " UTC)" : ""));
    }
    return h("div", { className: "hd-collector", "aria-label": "Collector" },
      h("div", { className: "hd-collector-line" },
        h("span", { className: "hd-sdot hd-tone-" + tone, "aria-hidden": "true" }),
        "Collector ", h("b", null, String(state).toUpperCase()),
        isNum(c.conn) ? " \u00b7 conn #" + c.conn : null,
        isNum(c.reconnects) ? " \u00b7 reconnects " + intf(c.reconnects) : null,
        c.note ? " \u00b7 " + c.note : null),
      c.last_error ? h("div", { className: "hd-collector-err", title: c.last_error }, "last error: " + c.last_error) : null,
      state === "disconnected" && isNum(c.next_retry_s)
        ? h("div", { className: "hd-dim" }, "next retry " + c.next_retry_s + " s after " + hms(c.since) + " UTC") : null,
      h("div", { className: "hd-btn-row", role: "group", "aria-label": "Collector control" },
        actions.map(function (x) {
          return h("button", {
            key: x[0], type: "button", className: "hd-btn", disabled: !!props.busy,
            onClick: function () { props.onControl(x[0]); }
          }, props.busy === x[0] ? x[1] + ELLIPSIS : x[1]);
        })),
      result);
  }

  function Header(props) {
    const r = props.resp;
    const m = g(r, "market");
    const markets = (r && r.markets) || [];
    const prov = g(m, "provenance") || {};
    const cov = g(prov, "coverage", "trades") || {};
    const unavailable = g(m, "unavailable") || [];
    const clock = g(m, "clock") || {};
    const collector = props.noSnap ? g(props.noSnap, "runtime", "collector") : g(r, "collector");
    const unsupported = (r && r.unsupported_markets) || [];
    const shown = m ? m.market : null;
    return h("header", { className: "hd-card hd-header" },
      h("div", { className: "hd-header-top" },
        h("div", { className: "hd-brand" },
          h("svg", { width: 24, height: 24, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", className: "hd-logo", "aria-hidden": "true" },
            h("path", { d: "M3 16l5-6 4 3 5-7 4 5" }), h("path", { d: "M3 20h18" })),
          h("div", null,
            h("h1", null, "Hyperliquid Desk"),
            h("div", { className: "hd-brand-sub" }, "Public Hyperliquid data \u00b7 read-only, no orders"))),
        h("nav", { className: "hd-markets", "aria-label": "Markets" },
          markets.length ? markets.map(function (name) {
            const pressed = name === props.selected;
            return h("button", {
              key: name, type: "button", className: cx("hd-market", pressed && "hd-market-on"),
              "aria-pressed": pressed ? "true" : "false", onClick: function () { props.onMarket(name); }
            }, name);
          }) : h("span", { className: "hd-dim" }, "no markets in the snapshot")),
        h(CollectorBox, { collector: collector, busy: props.busy, action: props.action, onControl: props.onControl })),
      m ? h(HeaderStats, { m: m }) : h("div", { className: "hd-stats hd-dim" }, "waiting for the first snapshot"),
      r ? h("div", { className: "hd-prov" },
        h(ModeBadge, { mode: r.mode, title: prov.source }),
        h("span", { className: "hd-prov-item" }, "snapshot ", h("code", { className: "hd-code" }, r.snapshot_id), " ",
          h(CopyButton, { text: r.snapshot_id, what: "snapshot ID" })),
        h("span", { className: "hd-prov-item" }, "built " + dateTime(r.built_at)),
        r.schema ? h("span", { className: "hd-prov-item", title: r.schema.path }, "schema " + r.schema.schema_id + " v" + r.schema.version) : null,
        h("span", { className: "hd-prov-item" }, "plugin v" + r.plugin_version),
        m ? h("span", { className: "hd-prov-item", title: prov.source }, "source: " + prov.source) : null,
        m ? h("span", { className: "hd-prov-item" }, "dex: " + (g(m, "meta", "dex_label") || prov.dex || "not in snapshot")) : null,
        m ? h("span", { className: "hd-prov-item" },
          "trade coverage since " + (cov.segment_start ? hmsms(cov.segment_start) + " UTC" : "(none)") +
          (isNum(cov.observed_s) ? " \u00b7 observed " + secs(cov.observed_s) : "") +
          " \u00b7 gaps " + ((cov.gaps || []).length)) : null,
        h("span", { className: "hd-prov-sep", "aria-hidden": "true" }),
        h(Badge, { kind: "api", title: prov.source }, "PUBLIC API \u00b7 websocket + info"),
        unavailable.map(function (u) {
          return h("span", { key: u.field, className: "hd-chip hd-tone-" + toneOfStatus(u.status), title: u.reason },
            h("span", { className: "hd-dot", "aria-hidden": "true" }), u.field + ": " + String(u.status).replace("_", " "));
        }),
        clock.clock_skew_suspected ? h("span", { className: "hd-chip hd-tone-warn", title: clock.note },
          "clock skew suspected (" + clock.offset_ms + " ms)") : null,
        unavailable.length ? h("details", { className: "hd-details" },
          h("summary", null, "why unavailable"),
          h("ul", null, unavailable.map(function (u) {
            return h("li", { key: u.field }, h("b", null, u.field + " (" + u.status + "): "), u.reason);
          }))) : null,
        props.selected && shown && props.selected !== shown
          ? h("span", { className: "hd-chip hd-tone-info" }, "loading " + props.selected + ELLIPSIS) : null,
        unsupported.length ? h("span", { className: "hd-prov-item hd-dim" }, "unsupported: " + unsupported.map(function (u) {
          return u.market + (u.reason ? " (" + u.reason + ")" : "");
        }).join("; ")) : null) : null);
  }

  // ------------------------------------------------------------------ ladder

  function Ladder(props) {
    const m = props.m;
    const lad = g(m, "book", "ladder") || {};
    const top = g(m, "book", "top") || {};
    const f = g(m, "freshness", "book_full");
    const bids = lad.bids || [];
    const asks = lad.asks || [];
    const boxRef = useRef(null);
    const spreadRef = useRef(null);
    const centredFor = useRef(null);

    // Centre the spread once per market, not on every poll, so a user's scroll is kept.
    useEffect(function () {
      if (centredFor.current === m.market) return;
      const box = boxRef.current;
      const row = spreadRef.current;
      if (box && row) {
        box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2);
        centredFor.current = m.market;
      }
    });

    // Display order: highest ask at the top, best bid under the spread (presentation only).
    const rows = [];
    asks.slice().reverse().forEach(function (l, i) { rows.push({ kind: "ask", l: l, key: "a" + i }); });
    rows.push({ kind: "spread", key: "spread" });
    bids.forEach(function (l, i) { rows.push({ kind: "bid", l: l, key: "b" + i }); });
    let maxN = 0;
    bids.concat(asks).forEach(function (l) { if (isNum(l.notional_usd) && l.notional_usd > maxN) maxN = l.notional_usd; });
    const lpx = pxWith(decimalsOf(bids.concat(asks).map(function (l) { return l.px; })));

    const nodeTitle = "needs node mode: the stops waiting at each price, the largest single order's share of a level (Top), " +
      "its age and the 10 s change need the node's order-level book; public levels are aggregated";
    const noteText = lad.api_count_note || "API count is Hyperliquid's n for an aggregated level.";

    function nodeLeft() {
      return h("td", { key: "nl", rowSpan: rows.length, className: "hd-node-col", "data-node-hook": "ladder-stops", title: nodeTitle },
        h("span", { className: "hd-vert" }, "needs node mode"));
    }
    function nodeRight() {
      return h("td", { key: "nr", rowSpan: rows.length, colSpan: 3, className: "hd-node-col", "data-node-hook": "ladder-top-age-delta", title: nodeTitle },
        h("span", null, "needs node mode"));
    }

    const body = rows.map(function (r, i) {
      const cells = [];
      if (i === 0) cells.push(nodeLeft());
      if (r.kind === "spread") {
        cells.push(h("td", { key: "s", colSpan: 4, className: "hd-spread-cell", title: "spread and mid from the fast (5-level) l2Book" },
          "spread ", h(V, { m: top.spread, fmt: px, compact: true }), " (", h(V, { m: top.spread_bps, fmt: bps, compact: true }), ")",
          " \u00b7 mid ", h(V, { m: top.mid, fmt: px, compact: true })));
      } else {
        const l = r.l;
        const w = maxN > 0 && isNum(l.notional_usd) ? Math.max(1, Math.round(l.notional_usd / maxN * 100)) : 0;
        cells.push(h("td", { key: "p", className: cx("hd-r", r.kind === "bid" ? "hd-bid" : "hd-ask") }, isNum(l.px) ? lpx(l.px) : missing()));
        cells.push(h("td", { key: "z", className: "hd-r" }, isNum(l.sz) ? sz(l.sz) : missing()));
        cells.push(h("td", { key: "n", className: "hd-r hd-barcell", title: isNum(l.notional_usd) ? usdFull(l.notional_usd) : undefined },
          h("span", { className: cx("hd-bar", r.kind === "bid" ? "hd-bar-bid" : "hd-bar-ask"), style: { width: w + "%" }, "aria-hidden": "true" }),
          h("span", { className: "hd-barval" }, isNum(l.notional_usd) ? usd(l.notional_usd) : missing())));
        cells.push(h("td", { key: "c", className: "hd-r hd-dim", title: noteText }, isNum(l.api_count) ? intf(l.api_count) : missing()));
      }
      if (i === 0) cells.push(nodeRight());
      return h("tr", { key: r.key, ref: r.kind === "spread" ? spreadRef : undefined, className: r.kind === "spread" ? "hd-spread-row" : undefined }, cells);
    });

    const sub = h(Fragment, null,
      "Bid ", h(V, { m: top.best_bid, fmt: px, compact: true }), " \u00b7 ask ", h(V, { m: top.best_ask, fmt: px, compact: true }),
      ". Up to 20 aggregated price levels per side; a level is never an individual order.");

    return h(Panel, {
      id: "ladder", className: "hd-p-ladder", title: "Price ladder (aggregated levels)", label: "Order-level ladder position: aggregated price ladder",
      sub: sub,
      badges: h(Fragment, null, h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "l2Book 20" }), h(NodeBadge, { what: "L4 columns" })),
      status: h(Fragment, null, h(Fresh, { f: f, label: "l2Book full" }),
        isNum(lad.book_time_ms) ? h("span", { className: "hd-dim" }, "book time " + hmsms(lad.book_time_ms) + " UTC") : null),
      flush: true
    },
    lad.reason && !bids.length && !asks.length
      ? h("div", { className: "hd-pad" }, h(Unavailable, { reason: lad.reason, block: true }))
      : h("div", { className: "hd-scroll hd-ladder-scroll", ref: boxRef },
        h("table", { className: "hd-table hd-ladder" },
          h("thead", null, h("tr", null,
            h("th", { scope: "col", className: "hd-node-th", title: nodeTitle }, "Stops"),
            h("th", { scope: "col", className: "hd-r" }, "Price"),
            h("th", { scope: "col", className: "hd-r" }, "Size"),
            h("th", { scope: "col", className: "hd-r" }, "Notional"),
            h("th", { scope: "col", className: "hd-r", title: noteText }, "API count"),
            h("th", { scope: "col", className: "hd-r hd-node-th", title: nodeTitle }, "Top"),
            h("th", { scope: "col", className: "hd-r hd-node-th", title: nodeTitle }, "Age"),
            h("th", { scope: "col", className: "hd-r hd-node-th", title: nodeTitle }, "\u0394 10s"))),
          h("tbody", null, body))),
    h("div", { className: "hd-note" },
      h("b", null, "Levels are aggregated by price. "), noteText,
      lad.aggregated === true ? " (aggregated: true)" : ""));
  }

  // ------------------------------------------------------------------ price chart (Lightweight Charts)

  const LINE_STYLE = { best_bid: "bid", best_ask: "ask", mark: "mark", oracle: "oracle", largest_bid_level: "bidlvl", largest_ask_level: "asklvl" };

  function lineOptions(pl, key) {
    const style = LINE_STYLE[pl.kind];
    const LS = LWC.LineStyle;
    const o = { price: pl.price, title: pl.label || key, lineWidth: 1, axisLabelVisible: true, lineVisible: true };
    if (style === "bid") { o.color = C.bid; o.lineStyle = LS.Solid; }
    else if (style === "ask") { o.color = C.ask; o.lineStyle = LS.Solid; }
    else if (style === "mark") { o.color = C.mark; o.lineStyle = LS.Dotted; }
    else if (style === "oracle") { o.color = C.oracle; o.lineStyle = LS.Dotted; }
    else if (style === "bidlvl") { o.color = C.bid; o.lineStyle = LS.LargeDashed; }
    else if (style === "asklvl") { o.color = C.ask; o.lineStyle = LS.LargeDashed; }
    else { o.color = C.dim; o.lineStyle = LS.Dashed; }
    return o;
  }

  function PriceChart(props) {
    const m = props.m;
    const chart = g(m, "chart") || {};
    const boxRef = useRef(null);
    const api = useRef(null);
    const [failure, setFailure] = useState(LWC ? null : "Lightweight Charts isn't loaded (dist/index.js should carry it)");

    // Create the chart once; destroy it on unmount.
    useEffect(function () {
      if (!LWC || !boxRef.current) return undefined;
      let c = null;
      try {
        c = LWC.createChart(boxRef.current, {
          autoSize: true,
          layout: {
            background: { type: LWC.ColorType.Solid, color: C.pane },
            textColor: C.text, fontFamily: MONO, fontSize: 11,
            attributionLogo: true
          },
          grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
          rightPriceScale: { borderColor: C.border },
          timeScale: { borderColor: C.border, timeVisible: true, secondsVisible: true },
          crosshair: { mode: LWC.CrosshairMode.Normal },
          localization: { locale: "en-US" }
        });
        const series = c.addSeries(LWC.CandlestickSeries, {
          upColor: C.bid, downColor: C.ask, borderVisible: false, wickUpColor: C.bid, wickDownColor: C.ask,
          priceLineVisible: false, lastValueVisible: true
        });
        const markers = typeof LWC.createSeriesMarkers === "function" ? LWC.createSeriesMarkers(series, []) : null;
        api.current = { chart: c, series: series, lines: {}, markers: markers, market: null, decimals: null };
      } catch (e) {
        setFailure("chart could not be created: " + errText(e));
      }
      return function () {
        if (c) c.remove();
        api.current = null;
      };
    }, []);

    // Update the existing chart in place on every poll.
    useEffect(function () {
      const a = api.current;
      if (!a) return;
      try {
        const candles = (chart.candles || []).filter(function (k) {
          return isNum(k.time) && isNum(k.open) && isNum(k.high) && isNum(k.low) && isNum(k.close);
        }).map(function (k) { return { time: k.time, open: k.open, high: k.high, low: k.low, close: k.close }; });
        const lines = (chart.price_lines || []).filter(function (pl) { return isNum(pl.price); });
        const vals = [];
        candles.forEach(function (k) { vals.push(k.open, k.high, k.low, k.close); });
        lines.forEach(function (pl) { vals.push(pl.price); });
        const d = decimalsOf(vals);
        if (d !== a.decimals) {
          a.series.applyOptions({ priceFormat: { type: "price", precision: d, minMove: Math.pow(10, -d) } });
          a.decimals = d;
        }
        a.series.setData(candles);

        const want = {};
        lines.forEach(function (pl, i) {
          const key = pl.kind || ("line" + i);
          want[key] = true;
          const opts = lineOptions(pl, key);
          if (a.lines[key]) a.lines[key].applyOptions(opts);
          else a.lines[key] = a.series.createPriceLine(opts);
        });
        Object.keys(a.lines).forEach(function (k) {
          if (!want[k]) { a.series.removePriceLine(a.lines[k]); delete a.lines[k]; }
        });

        // A gap that ends inside the drawn range gets a marker on the first candle after it.
        if (a.markers) {
          const marks = [];
          const step = isNum(chart.candle_s) ? chart.candle_s : 0;
          (chart.gaps || []).forEach(function (gp) {
            if (!isNum(gp.end_ms) || !candles.length) return;
            const endS = gp.end_ms / 1000;
            if (endS < candles[0].time - step || endS > candles[candles.length - 1].time + step) return;
            for (let i = 0; i < candles.length; i++) {
              if (candles[i].time + step > endS) {
                marks.push({ time: candles[i].time, position: "aboveBar", shape: "square", color: C.gap, text: "gap" });
                break;
              }
            }
          });
          a.markers.setMarkers(marks);
        }
        if (a.market !== m.market) {
          a.chart.timeScale().fitContent();
          a.market = m.market;
        }
      } catch (e) {
        setFailure("chart update failed: " + errText(e));
      }
    }, [m]);

    const gaps = chart.gaps || [];
    const lines = chart.price_lines || [];
    const nCandles = (chart.candles || []).length;
    return h(Panel, {
      id: "chart", className: "hd-p-chart", title: "Price chart",
      label: "Price chart (in the mockup's heatmap position)",
      sub: (isNum(chart.candle_s) ? chart.candle_s + " s candles" : "candles") + " from public trades, times in UTC. Levels are price lines.",
      badges: h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "trades + l2Book + ctx" }),
      status: h(Fragment, null,
        h(Fresh, { f: g(m, "freshness", "trades"), label: "trades" }),
        h(Fresh, { f: g(m, "freshness", "book_fast"), label: "l2Book fast" }),
        h(Fresh, { f: g(m, "freshness", "ctx"), label: "ctx" }))
    },
    h("div", { className: "hd-chart-wrap" },
      h("div", { className: "hd-chart", ref: boxRef, role: "img", "aria-label": "Candlestick chart of " + m.market }),
      failure ? h("div", { className: "hd-chart-overlay" }, h(Unavailable, { reason: failure })) : null,
      !failure && nCandles === 0 ? h("div", { className: "hd-chart-overlay" }, "No candles in this snapshot: no trades in the current coverage segment yet.") : null),
    h("div", { className: "hd-legend-lines" }, lines.length ? lines.map(function (pl, i) {
      return h("span", { key: pl.kind || i, className: "hd-legend-line hd-ll-" + (LINE_STYLE[pl.kind] || "other") },
        h("span", { className: "hd-ll-swatch", "aria-hidden": "true" }), (pl.label || pl.kind) + " ", h("b", { className: "hd-num" }, isNum(pl.price) ? px(pl.price) : "no price"));
    }) : h("span", { className: "hd-dim" }, "No price lines in this snapshot.")),
    gaps.length ? h("div", { className: "hd-note hd-gapnote" }, h("b", null, "Gaps: "), gaps.map(function (gp, i) {
      return h("span", { key: i }, (i ? "; " : "") + hmsms(gp.start) + "\u2013" + hmsms(gp.end) + " UTC (" + (gp.reason || "no reason given") + ")");
    }), ". No candle, regime or window spans a gap.") : null,
    chart.note ? h("div", { className: "hd-note" }, chart.note + ".") : null,
    h("div", { className: "hd-attribution" }, ATTRIBUTION,
      h("a", { href: TV_URL, target: "_blank", rel: "noopener noreferrer" }, TV_URL)));
  }

  // ------------------------------------------------------------------ node-only cards

  function NodeCard(props) {
    return h(Panel, {
      id: props.hook, className: props.className, title: props.title, nodeHook: props.hook,
      sub: props.sub, badges: h(NodeBadge, { what: props.what })
    },
    props.columns ? h("div", { className: "hd-node-cols" }, props.columns.map(function (c) { return h("span", { key: c }, c); })) : null,
    h("div", { className: "hd-node-body" },
      h("div", { className: "hd-node-label" }, "needs node mode"),
      h("div", { className: "hd-dim" }, props.desc),
      props.hookText ? h("div", { className: "hd-dim hd-small" }, "hook: ", h("code", { className: "hd-code" }, props.hookText)) : null));
  }

  // ------------------------------------------------------------------ flow ("Who is aggressive")

  function CvdChart(props) {
    const cvd = props.cvd || {};
    const b = cvd.buckets || [];
    if (!b.length) {
      return h("div", { className: "hd-empty" },
        cvd.reason ? h(Unavailable, { reason: cvd.reason }) : "No buckets in this snapshot yet.");
    }
    const W = 360;
    const H = 120;
    const cum = b.map(function (x) { return x.cumulative_usd; });
    const finite = cum.filter(isNum);
    const lo = Math.min.apply(null, finite.concat([0]));
    const hi = Math.max.apply(null, finite.concat([0]));
    const span = hi - lo || 1;
    const y = function (v) { return (H - 10) - (v - lo) / span * (H - 20); };
    const x = function (i) { return (i + 0.5) / b.length * W; };
    const pts = [];
    b.forEach(function (k, i) { if (isNum(k.cumulative_usd)) pts.push(x(i).toFixed(1) + "," + y(k.cumulative_usd).toFixed(1)); });
    let maxAbs = 0;
    b.forEach(function (k) { if (isNum(k.delta_usd)) maxAbs = Math.max(maxAbs, Math.abs(k.delta_usd)); });
    const bucketS = isNum(cvd.bucket_s) ? cvd.bucket_s : null;
    function tip(k) {
      return hms(k.start) + " UTC" + (bucketS ? " (" + bucketS + " s)" : "") +
        "\nbuy " + (isNum(k.buy_usd) ? usdFull(k.buy_usd) : "unavailable") +
        "\nsell " + (isNum(k.sell_usd) ? usdFull(k.sell_usd) : "unavailable") +
        "\ndelta " + (isNum(k.delta_usd) ? usdFull(k.delta_usd) : "unavailable") +
        "\ncumulative " + (isNum(k.cumulative_usd) ? usdFull(k.cumulative_usd) : "unavailable") +
        (k.complete ? "\ncomplete" : "\nINCOMPLETE: partly outside coverage or still filling");
    }
    const anyIncomplete = b.some(function (k) { return !k.complete; });
    return h("div", { className: "hd-cvd" },
      h("div", { className: "hd-cvd-line" },
        h("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", "aria-hidden": "true" },
          h("line", { x1: 0, x2: W, y1: y(0), y2: y(0), className: "hd-zero" }),
          h("polyline", { points: pts.join(" "), className: "hd-cvd-poly" }),
          b.map(function (k, i) {
            return isNum(k.cumulative_usd) ? h("circle", { key: i, cx: x(i), cy: y(k.cumulative_usd), r: 2.2, className: k.complete ? "hd-cvd-pt" : "hd-cvd-pt hd-cvd-pt-inc" }) : null;
          })),
        h("span", { className: "hd-cvd-tag hd-cvd-tag-hi" }, "cum. " + usd(hi, true)),
        h("span", { className: "hd-cvd-tag hd-cvd-tag-lo" }, "cum. " + usd(lo, true))),
      h("div", { className: "hd-cvd-bars", role: "list", "aria-label": "delta per bucket" }, b.map(function (k, i) {
        const v = isNum(k.delta_usd) ? k.delta_usd : null;
        const hgt = v === null || maxAbs === 0 ? 0 : Math.abs(v) / maxAbs * 48;
        return h("div", { key: i, role: "listitem", className: cx("hd-cvd-slot", !k.complete && "hd-cvd-inc"), title: tip(k) },
          v === null ? h("span", { className: "hd-cvd-na" }, "n/a") : h("div", {
            className: cx("hd-cvd-bar", v >= 0 ? "hd-bg-bid" : "hd-bg-ask"),
            style: { height: hgt.toFixed(1) + "%", bottom: v >= 0 ? "50%" : (50 - hgt).toFixed(1) + "%" }
          }));
      })),
      h("div", { className: "hd-axis" },
        h("span", null, hms(b[0].start)),
        h("span", null, "delta per " + (bucketS ? bucketS + " s" : "bucket") + ", UTC"),
        h("span", null, hms(b[b.length - 1].start))),
      h("div", { className: "hd-note" },
        anyIncomplete ? h(Fragment, null, h("span", { className: "hd-inc-swatch", "aria-hidden": "true" }), "hatched = incomplete bucket. ") : null,
        "Cumulative from " + (cvd.cumulative_from ? hms(cvd.cumulative_from) + " UTC" : "the first complete bucket") +
        (cvd.resets_at_gap ? "; it resets at every gap." : ".")));
  }

  function FlowPanel(props) {
    const m = props.m;
    const fl = g(m, "flow") || {};
    const v = fl.values || {};
    const w = fl.window || {};
    const lp = fl.large_prints || {};
    const net = valueOf(v.net);
    return h(Panel, {
      id: "flow", className: "hd-p-flow", title: "Who is aggressive",
      sub: "Aggressive flow: buys minus sells that crossed the spread, by the documented aggressor side, each trade counted once, over the observed window.",
      badges: h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "trades" }),
      status: h(Fresh, { f: g(m, "freshness", "trades"), label: "trades" })
    },
    h("div", { className: "hd-flow-top" },
      h("span", { className: "hd-flow-net" },
        h(V, { m: v.net, fmt: usdSigned, className: net === null ? null : (net >= 0 ? "hd-bid" : "hd-ask") }), " net"),
      h("span", null, "buy aggressor ", h(V, { m: v.buy_aggressive_notional, fmt: usd })),
      h("span", null, "sell aggressor ", h(V, { m: v.sell_aggressive_notional, fmt: usd })),
      h("span", null, "volume ", h(V, { m: v.volume, fmt: usd })),
      h("span", null, "trades ", h(V, { m: v.trade_count, fmt: intf }))),
    h("div", { className: "hd-window" },
      w.reason ? h(Unavailable, { reason: w.reason })
        : h(Fragment, null, "window (", hmsms(w.start), ", ", hmsms(w.end), "] UTC \u00b7 ",
          isNum(w.length_s) ? secs(w.length_s) : "length not in snapshot", w.since ? " \u00b7 since " + w.since : "")),
    h(CvdChart, { cvd: fl.cvd }),
    h("div", { className: "hd-lp" },
      h("div", null, h("b", null, "Large prints"), " \u2265 ", isNum(lp.threshold_usd) ? usd(lp.threshold_usd) : "threshold not in snapshot",
        ": ", isNum(lp.count) ? intf(lp.count) : ("count unavailable" + (lp.reason ? ": " + lp.reason : "")),
        lp.note ? h("span", { className: "hd-dim" }, " \u00b7 " + lp.note) : null),
      (lp.prints || []).length ? h("ul", { className: "hd-lp-list" }, lp.prints.slice(0, 8).map(function (t, i) {
        return h("li", { key: t.tid || i },
          h("span", { className: "hd-num" }, hmsms(t.time)), " ",
          h("b", { className: t.side === "B" ? "hd-bid" : "hd-ask" }, t.side === "B" ? "Buy" : "Sell"), " ",
          h("span", { className: "hd-num" }, isNum(t.notional_usd) ? usd(t.notional_usd) : missing()), " @ ",
          h("span", { className: "hd-num" }, isNum(t.px) ? px(t.px) : missing()));
      })) : null));
  }

  // ------------------------------------------------------------------ observed depth ("Book pressure")

  function covered(s) { return !!s && s.status === "covered" && valueOf(s.notional_usd) !== null; }

  function DepthSide(props) {
    const s = props.s || {};
    const side = props.side;
    if (!covered(s)) {
      return h("div", { className: cx("hd-depth-side", "hd-depth-" + side) },
        h("div", { className: "hd-depth-nc", title: reasonOf(s.notional_usd) }, "not covered"));
    }
    return h("div", { className: cx("hd-depth-side", "hd-depth-" + side) },
      h("div", { className: "hd-depth-barbox" },
        h("div", {
          className: cx("hd-depth-bar", side === "bid" ? "hd-bg-bid" : "hd-bg-ask"),
          style: { width: Math.max(4, props.w) + "%" }, title: usdFull(s.notional_usd.value) + (isNum(s.size_coin) ? " \u00b7 " + sz(s.size_coin) + " coin" : "")
        }, usd(s.notional_usd.value))),
      h("div", { className: "hd-small hd-dim" },
        (s.book || "book not named") + (isNum(s.edge_resolution) ? " \u00b7 edge \u00b1" + px(s.edge_resolution) : "") +
        (isNum(s.levels_counted) ? " \u00b7 " + intf(s.levels_counted) + " levels" : "")));
  }

  // Written out under a band whose side isn't covered: the reason, then that side's observed
  // depth within the returned levels of each book; and the imbalance reason when it's null.
  function DepthBandNote(props) {
    const b = props.b;
    const obs = props.observed || {};
    const lines = [];
    ["bid", "ask"].forEach(function (side) {
      const s = b[side] || {};
      if (covered(s)) return;
      const kinds = Object.keys(obs);
      lines.push(h("div", { key: side },
        h("b", { className: side === "bid" ? "hd-bid" : "hd-ask" }, side === "bid" ? "bids" : "asks"), ": ",
        h("span", { className: "hd-na" }, reasonOf(s.notional_usd)),
        ". Observed depth within returned levels: ",
        kinds.length ? kinds.map(function (k, i) {
          const o = g(obs, k, side);
          return h("span", { key: k }, (i ? " \u00b7 " : "") + k + " ",
            o ? h(Fragment, null, h(V, { m: o.notional_usd, fmt: usd, compact: true }), isNum(o.span_pct) ? " to " + pct(o.span_pct, 2) + " from mid" : "")
              : h(Unavailable, { reason: g(obs, k, "reason") || "not in snapshot", compact: true }));
        }) : "not in snapshot", "."));
    });
    if (valueOf(b.imbalance) === null) {
      lines.push(h("div", { key: "imb", className: "hd-dim" }, "imbalance unavailable: " + reasonOf(b.imbalance)));
    }
    return lines.length ? h("div", { className: "hd-depth-note" }, lines) : null;
  }

  // The coarse-bucket approximation for one band: never shown as exact.
  function ApproxLine(props) {
    const a = props.a || {};
    function side(s, cls) {
      if (!s || s.status !== "approximate") return h(Unavailable, { reason: reasonOf(s && s.approx_usd) || "unavailable", compact: true });
      const r = s.range_usd || [];
      return h("span", { className: cls, title: (s.aggregation || "") + (isNum(s.bucket_width) ? " \u00b7 bucket " + px(s.bucket_width) : "") },
        "~" + usd(valueOf(s.approx_usd)) + (isNum(r[0]) && isNum(r[1]) ? " [" + usd(r[0]) + "\u2013" + usd(r[1]) + "]" : ""));
    }
    const imb = valueOf(a.imbalance);
    return h("div", { className: "hd-depth-approx hd-small", title: a.convention || "" },
      h("span", { className: "hd-pill" }, "approx"), " ",
      h("span", { className: "hd-dim" }, (g(a, "bid", "aggregation") || g(a, "ask", "aggregation") || "aggregated") + ": "),
      "bids ", side(a.bid, "hd-bid"), " \u00b7 asks ", side(a.ask, "hd-ask"),
      " \u00b7 imbalance ", imb === null ? h("span", { className: "hd-dim" }, "unavailable") : ratioSigned(imb) + " (approx)");
  }

  function DepthPanel(props) {
    const m = props.m;
    const depth = g(m, "book", "depth") || {};
    const bands = depth.bands || [];
    const observed = depth.observed || {};
    const bbo = g(m, "book", "bbo") || {};
    const fr = g(m, "freshness") || {};
    return h(Panel, {
      id: "depth", className: "hd-p-depth", title: "Observed depth",
      label: "Book pressure position: observed depth",
      sub: "Exact notional within \u00b10.25/0.5/1% of mid only from full-precision levels that reach the band; aggregated buckets give a labeled approximation with its range.",
      badges: h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "l2Book" }),
      status: h(Fragment, null, h(Fresh, { f: fr.book_full, label: "full" }), h(Fresh, { f: fr.book_sf4, label: "nSigFigs=4" }), h(Fresh, { f: fr.book_sf3, label: "nSigFigs=3" }))
    },
    h("div", { className: "hd-depth-head" }, h("span", null, "band"), h("span", { className: "hd-bid" }, "bids"), h("span", { className: "hd-ask" }, "asks"), h("span", { className: "hd-r" }, "imbalance")),
    bands.length ? bands.map(function (band) {
      const b = band.exact || {};
      const bid = valueOf(g(b, "bid", "notional_usd"));
      const ask = valueOf(g(b, "ask", "notional_usd"));
      const mx = Math.max(bid || 0, ask || 0);
      const imb = valueOf(b.imbalance);
      return h("div", { key: band.band_pct, className: "hd-depth-row" },
        h("span", { className: "hd-num hd-depth-label" }, "\u00b1" + band.band_pct + "%"),
        h(DepthSide, { s: b.bid, side: "bid", w: mx > 0 && bid !== null ? bid / mx * 100 : 0 }),
        h(DepthSide, { s: b.ask, side: "ask", w: mx > 0 && ask !== null ? ask / mx * 100 : 0 }),
        h("span", { className: "hd-r hd-num", title: "(bid \u2212 ask) / (bid + ask) for this band; positive = more bid depth" },
          imb === null ? h(Unavailable, { reason: reasonOf(b.imbalance), compact: true })
            : h(Fragment, null, ratioSigned(imb), h("span", { className: "hd-dim" }, imb > 0 ? " bid" : (imb < 0 ? " ask" : "")))),
        h(DepthBandNote, { b: b, observed: observed }),
        h(ApproxLine, { a: band.approximate }));
    }) : h("div", { className: "hd-empty" }, "No depth bands in this snapshot."),
    h("div", { className: "hd-sep" }),
    h("div", { className: "hd-subhead" }, "Observed depth within returned levels"),
    Object.keys(observed).length ? Object.keys(observed).map(function (k) {
      const o = observed[k] || {};
      if (o.reason) {
        return h("div", { key: k, className: "hd-obs-row" },
          h("span", { className: "hd-obs-book" }, k),
          h("span", { className: "hd-obs-span2" }, h(Unavailable, { reason: o.reason })));
      }
      function cell(s, side) {
        if (!s) return h("span", { className: "hd-depth-" + side }, h(Unavailable, { reason: "side not in snapshot", compact: true }));
        return h("span", {
          className: "hd-obs-cell hd-depth-" + side,
          title: (s.label || "") + (isNum(s.deepest_px) ? "\ndeepest level " + px(s.deepest_px) : "") + (isNum(s.size_coin) ? "\n" + sz(s.size_coin) + " coin" : "")
        },
        h(V, { m: s.notional_usd, fmt: usd, compact: true, className: side === "bid" ? "hd-bid" : "hd-ask" }),
        h("span", { className: "hd-dim hd-small" }, (isNum(s.levels) ? " " + s.levels + " lv" : "") + (isNum(s.span_pct) ? " \u00b7 to " + pct(s.span_pct, 2) : "")));
      }
      return h("div", { key: k, className: "hd-obs-row" },
        h("span", { className: "hd-obs-book" }, k, isNum(o.edge_resolution) ? h("span", { className: "hd-dim hd-small" }, " \u00b7 res " + px(o.edge_resolution)) : null),
        cell(o.bid, "bid"), cell(o.ask, "ask"));
    }) : h("div", { className: "hd-empty" }, "No observed depth in this snapshot."),
    depth.note ? h("div", { className: "hd-note" }, depth.note + ".") : null,
    h("div", { className: "hd-note" }, h("b", null, "bbo: "), String(bbo.status || "status not in snapshot").replace("_", " "),
      ", prices never shown",
      bbo.last_event_source_time ? " \u00b7 last event " + hmsms(bbo.last_event_source_time) + " UTC" : "",
      bbo.matches_fast_book_top === true ? " \u00b7 matches the fast book top" : (bbo.matches_fast_book_top === false ? " \u00b7 differs from the fast book top" : ""),
      bbo.reason ? h("details", { className: "hd-details" }, h("summary", null, "why"), bbo.reason) : null));
  }

  // ------------------------------------------------------------------ tape

  function TapePanel(props) {
    const m = props.m;
    const tape = g(m, "flow", "tape") || [];
    const anySnapshot = tape.some(function (t) { return t.counted === false; });
    const anyEarlier = tape.some(function (t) { return t.segment && t.segment !== "current"; });
    const tpx = pxWith(decimalsOf(tape.map(function (t) { return t.px; })));
    const flagTitle = "needs node mode: whether a stop, TWAP slice or liquidation caused the trade isn't in public trades";
    return h(Panel, {
      id: "tape", className: "hd-p-tape", title: "Tape",
      sub: "Public trades, newest first, times in UTC. Side is the aggressor: Buy = buyer took, Sell = seller took.",
      badges: h(Fragment, null, h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "trades" }), h(NodeBadge, { what: "flags" })),
      status: h(Fresh, { f: g(m, "freshness", "trades"), label: "trades" }),
      flush: true
    },
    tape.length ? h("div", { className: "hd-scroll hd-tape-scroll" },
      h("table", { className: "hd-table hd-tape" },
        h("thead", null, h("tr", null,
          h("th", { scope: "col" }, "Time"), h("th", { scope: "col" }, "Side"), h("th", { scope: "col", className: "hd-r" }, "Price"),
          h("th", { scope: "col", className: "hd-r" }, "Size"), h("th", { scope: "col", className: "hd-r" }, "Notional"),
          h("th", { scope: "col" }, "Buyer"), h("th", { scope: "col" }, "Seller"),
          h("th", { scope: "col", className: "hd-node-th", title: flagTitle }, "Flag"))),
        h("tbody", null, tape.map(function (t, i) {
          const snap = t.counted === false;
          const earlier = !!t.segment && t.segment !== "current";
          const side = t.side === "B" ? "Buy" : (t.side === "A" ? "Sell" : String(t.side));
          const rowTitle = earlier ? "from an earlier coverage segment (" + t.segment + "): not in the current window"
            : (snap ? "from the subscription snapshot, before coverage: not counted in flow" : undefined);
          return h("tr", { key: (t.tid || "") + ":" + i, className: (snap || earlier) ? "hd-dimrow" : undefined, title: rowTitle },
            h("td", { className: "hd-num" }, hmsms(t.time), earlier ? h("span", { className: "hd-pill" }, "before gap")
              : (snap ? h("span", { className: "hd-pill" }, "snapshot") : null)),
            h("td", { className: t.side === "B" ? "hd-bid hd-b" : "hd-ask hd-b", title: t.aggressor ? "aggressor: " + t.aggressor : undefined }, side),
            h("td", { className: "hd-r hd-num" }, isNum(t.px) ? tpx(t.px) : missing()),
            h("td", { className: "hd-r hd-num" }, isNum(t.sz) ? sz(t.sz) : missing()),
            h("td", { className: "hd-r hd-num" }, isNum(t.notional_usd) ? usd(t.notional_usd) : missing()),
            h("td", null, h(Addr, { address: t.buyer })),
            h("td", null, h(Addr, { address: t.seller })),
            i === 0 ? h("td", { rowSpan: tape.length, className: "hd-node-col", "data-node-hook": "tape-flags", title: flagTitle },
              h("span", { className: "hd-vert" }, "needs node mode")) : null);
        })))) : h("div", { className: "hd-empty" }, "No trades on the tape in this snapshot."),
    anySnapshot ? h("div", { className: "hd-note" }, h("span", { className: "hd-pill" }, "snapshot"),
      " dimmed rows came with the subscription snapshot, before coverage started; flow doesn't count them.") : null,
    anyEarlier ? h("div", { className: "hd-note" }, h("span", { className: "hd-pill" }, "before gap"),
      " dimmed rows belong to an earlier coverage segment, before the last gap; the current windows don't include them.") : null);
  }

  // ------------------------------------------------------------------ Flow Deck ("Alerts")

  function RegimeBadge(props) {
    const r = props.regime;
    return h("span", {
      className: cx("hd-regime", "hd-regime-" + (r ? (REGIME_TONE[r] || "dim") : "none"), props.unconfirmed && "hd-regime-unconf"),
      title: props.title
    }, r || "NO REGIME");
  }

  function DeckNum(props) {
    // One "label value" pair; key = the regime's key number (highlighted with a "<" marker).
    return h("div", { className: cx("hd-dn", props.isKey && "hd-dn-key") },
      h("span", { className: cx("hd-dn-label", props.strong && "hd-dn-strong") }, props.label),
      h("span", { className: "hd-dn-val" }, props.children),
      props.isKey ? h("span", { className: "hd-dn-mark", title: "the number this regime is read by" }, "<") : null);
  }

  function bucketLine(side, n1, m1, n2, m2) {
    const bothNode = valueOf(m1) === null && valueOf(m2) === null &&
      String(reasonOf(m1)).indexOf("needs node mode") === 0 && String(reasonOf(m2)).indexOf("needs node mode") === 0;
    return h("div", { className: "hd-bucket" },
      h("span", { className: "hd-dn-label" }, side), " ",
      bothNode ? h(Fragment, null, n1 + " \u00b7 " + n2 + " ", h("span", { className: "hd-node-inline", title: reasonOf(m1) }, "needs node mode"))
        : h(Fragment, null, n1 + " ", h(V, { m: m1, fmt: usd, compact: true }), " \u00b7 " + n2 + " ", h(V, { m: m2, fmt: usd, compact: true })));
  }

  function DeckCard(props) {
    const name = props.name;
    const d = props.d || {};
    const card = d.card || {};
    const n = card.numbers || {};
    const conf = card.confirmed_regime;              // the latched regime
    const live = card.live_classification;           // what this tick's window numbers classify as
    const pending = card.pending;
    const unavailable = !live;                       // incomplete window, unverified trades, warming up
    const cardReason = unavailable ? reasonOf(card.regime) : null;
    const liveMeaning = live && live.meaning;
    const key = liveMeaning && liveMeaning.key_number ? KEY_NUMBER_FIELD[liveMeaning.key_number] : null;
    const compactIf = function (mm) {
      return !!(mm && mm.reason && ((cardReason && mm.reason === cardReason) || String(mm.reason).indexOf("needs node mode") === 0));
    };
    const tw = n.top_wallet;
    const ab = card.activity_buckets || {};
    const lc = card.last_confirmed;
    function num_(label, field, fmt, strong) {
      return h(DeckNum, { label: label, isKey: key === field, strong: strong },
        h(V, { m: n[field], fmt: fmt, compact: compactIf(n[field]) }));
    }
    return h("article", { className: "hd-deck-card", "aria-label": "Flow Deck " + name },
      h("div", { className: "hd-deck-head" },
        h("span", { className: "hd-deck-mkt" }, name),
        h("span", { className: "hd-deck-px" }, h(V, { m: n.price, fmt: px, compact: true }))),
      unavailable ? h("div", { className: "hd-deck-reason" }, h(Unavailable, { reason: cardReason })) : null,
      lc ? h("div", { className: "hd-dim hd-small" }, "last confirmed: ", h(RegimeBadge, { regime: lc.regime }), " since " + hms(lc.since) + "Z" +
        (isNum(lc.as_of_tick_ms) ? " as of tick " + hms(lc.as_of_tick_ms) + "Z" : "") + " \u00b7 " + (lc.note || "historical")) : null,
      !unavailable ? h("div", { className: "hd-deck-states" },
        h("div", { className: "hd-deck-state" },
          h("span", { className: "hd-dn-label" }, "confirmed"),
          conf ? h(Fragment, null, h(RegimeBadge, { regime: conf.state, title: "the latched regime (needs 2 consecutive ticks)" }),
            h("span", { className: "hd-deck-since" }, " since " + (conf.since ? hms(conf.since) + "Z" : "?") +
              " (" + (isNum(conf.held_s) ? heldS(conf.held_s) : "held unavailable") + ")"),
            conf.tag ? h("span", { className: "hd-deck-tag" }, " [" + conf.tag + "]") : null)
            : h("span", { className: "hd-dim" }, reasonOf(card.regime))),
        h("div", { className: "hd-deck-state" },
          h("span", { className: "hd-dn-label" }, "now"),
          h(RegimeBadge, { regime: live.state, unconfirmed: !conf || conf.state !== live.state, title: "this tick's window classifies as " + live.state }),
          h("span", { className: "hd-deck-since" }, " tick " + hms(live.tick) + "Z" + (live.tag ? " [" + live.tag + "]" : "")),
          live.sparse ? h("span", { className: "hd-pill", title: live.sparse_note || "" }, "sparse") : null),
        pending ? h("div", { className: "hd-deck-state hd-deck-pending" },
          h("span", { className: "hd-dn-label" }, "pending"),
          h(RegimeBadge, { regime: pending.state, unconfirmed: true }),
          h("span", { className: "hd-deck-since" }, " " + pending.ticks + " of " + (pending.of || 2) + " ticks")) : null) : null,
      h("div", { className: "hd-deck-rule", "aria-hidden": "true" }),
      h("div", { className: "hd-deck-grid" },
        num_("move", "move_bps", bpsSigned),
        num_("net flow", "net_flow_usd", usdSigned),
        num_("vol", "volume_usd", usd),
        num_("trades", "trades", intf),
        h("div", { className: "hd-dn-wide", title: valueOf(n.impact) !== null ? n.impact.unit : undefined },
          num_("IMPACT", "impact", function (v) { return num(v, 2, 1) + " bps/$M"; }, true)),
        h("div", { className: "hd-dn-wide", title: "aggressive buy notional / aggressive sell notional" },
          num_("PRESSURE", "pressure", function (v) { return num(v, 2, 2) + " buy/sell"; }, true)),
        h("div", { className: "hd-dn-wide" }, num_("accounts", "accounts", function (v) {
          return intf(v) + (n.accounts && n.accounts.unit ? " " + n.accounts.unit : "");
        }))),
      h("div", { className: "hd-deck-tw" },
        h("span", { className: "hd-dn-label" }, "top wallet"),
        tw && tw.value ? h(Fragment, null,
          h("b", { className: "hd-num" }, isNum(tw.share_pct) ? pct(tw.share_pct, 0) : "share unavailable"), " ",
          h(Addr, { address: tw.value }), " ",
          h("span", { className: "hd-num", title: tw.net_label || undefined },
            "net aggr " + (isNum(tw.net_aggressive_usd) ? usdSigned(tw.net_aggressive_usd) : "unavailable") +
            (isNum(tw.aggressive_bought_usd) && isNum(tw.aggressive_sold_usd)
              ? " (bought " + usd(tw.aggressive_bought_usd) + " \u00b7 sold " + usd(tw.aggressive_sold_usd) + ")" : "")),
          h("div", { className: "hd-dim hd-small" }, (tw.share_label || "share label not in snapshot") + (tw.note ? " \u00b7 " + tw.note : "")))
          : h(Unavailable, { reason: reasonOf(tw), compact: compactIf(tw) })),
      h("div", { className: "hd-deck-buckets", "data-node-hook": "deck-activity-buckets", title: ab.hook ? "hook: " + ab.hook : undefined },
        bucketLine("buy", "new-long", ab.new_long, "cover", ab.cover),
        bucketLine("sell", "new-short", ab.new_short, "exit", ab.exit),
        valueOf(ab.new_long) === null ? h("div", { className: "hd-dim hd-small" }, reasonOf(ab.new_long)) : null),
      liveMeaning ? h("div", { className: "hd-deck-meaning" },
        h("div", { className: "hd-deck-what" }, "now " + liveMeaning.state + " (this window):"),
        h("p", null, liveMeaning.text),
        conf && conf.meaning && conf.state !== live.state ? h(Fragment, null,
          h("div", { className: "hd-deck-what" }, "confirmed " + conf.meaning.state + ":"),
          h("p", null, conf.meaning.text)) : null,
        live.sparse && live.sparse_note ? h("div", { className: "hd-dim hd-small" }, "sparse: " + live.sparse_note) : null,
        h("div", { className: "hd-dim hd-small" }, "Regimes are heuristic classifications of these window numbers; they don't establish a cause.")) : null,
      h("div", { className: "hd-deck-foot" },
        isNum(card.window_start_ms) && isNum(card.window_end_ms)
          ? "window " + hms(card.window_start_ms) + "\u2013" + hms(card.window_end_ms) + " UTC (" + card.window_s + " s, tick every " + card.tick_s + " s)"
          : "window " + (card.window_s || "?") + " s, tick every " + (card.tick_s || "?") + " s",
        " ", h(Fresh, { f: d.trades_freshness, label: "trades" }),
        g(d, "provenance", "mode") && g(d, "provenance", "mode") !== "LIVE" ? h(ModeBadge, { mode: d.provenance.mode, title: d.provenance.source }) : null));
  }

  function EventRow(props) {
    const e = props.e;
    const t = h("span", { className: "hd-ev-time" }, hms(isNum(e.at_ms) ? e.at_ms : e.at));
    const mk = h("span", { className: "hd-ev-mkt" }, e.market);
    if (e.type === "regime_change") {
      return h("li", { className: "hd-ev", title: e.text }, t, mk,
        h("span", { className: "hd-ev-body" }, h(RegimeBadge, { regime: e.from }), " -> ", h(RegimeBadge, { regime: e.to })),
        h("span", { className: "hd-ev-held" }, "(held " + (isNum(e.held_s) ? heldS(e.held_s) : "?") + ")"));
    }
    if (e.type === "regime_ended_by_gap") {
      return h("li", { className: "hd-ev hd-ev-gap", title: e.text }, t, mk,
        h("span", { className: "hd-ev-body" }, h(RegimeBadge, { regime: e.from }), " -> ", h("span", { className: "hd-gaptag" }, "gap"),
          h("span", { className: "hd-dim" }, " " + (e.reason || ""))),
        h("span", { className: "hd-ev-held" }, "(held " + (isNum(e.held_s) ? heldS(e.held_s) : "?") + ")"));
    }
    if (e.type === "burst") {
      const w = e.wallets || {};
      return h("li", { className: "hd-ev hd-ev-burst", title: e.text }, t, mk,
        h("span", { className: "hd-ev-body" },
          h("span", { className: cx("hd-bursttag", e.side === "selling" ? "hd-bg-ask" : "hd-bg-bid") }, "BURST"),
          " aggressive " + e.side + " ", h("b", { className: "hd-num" }, isNum(e.aggressive_notional_usd) ? usd(e.aggressive_notional_usd) : "?"),
          " within 60 s \u00b7 threshold " + (isNum(e.threshold_usd) ? usd(e.threshold_usd) : "?") +
          (isNum(e.executions) ? " \u00b7 " + intf(e.executions) + " executions" : "")),
        h("span", { className: "hd-ev-held" }),
        h("div", { className: "hd-ev-wallets" },
          (w.listed || []).length ? h("ol", null, w.listed.map(function (x) {
            return h("li", { key: x.address },
              h("span", { className: "hd-dim" }, "#" + x.rank + " "), h(Addr, { address: x.address }), " ",
              h("span", { className: "hd-num" }, isNum(x.amount_usd) ? usd(x.amount_usd) : missing()), " ",
              h("b", { className: "hd-num" }, isNum(x.share_pct) ? pct(x.share_pct, 1) : missing()));
          })) : null,
          w.summary ? h("div", { className: "hd-summary" }, w.summary) : null));
    }
    return h("li", { className: "hd-ev" }, t, mk, h("span", { className: "hd-ev-body" }, e.text || e.type));
  }

  function DeckPanel(props) {
    const r = props.resp;
    const deck = r.deck || {};
    const names = (r.markets || []).filter(function (n) { return deck[n]; });
    const events = [];
    names.forEach(function (n) { (deck[n].events || []).forEach(function (e) { events.push(e); }); });
    events.sort(function (a, b) { return (b.at_ms || 0) - (a.at_ms || 0); });
    return h(Panel, {
      id: "deck", className: "hd-p-deck", title: "Flow Deck",
      label: "Alerts position: Flow Deck",
      sub: "Each market's regime over a 90 s window, ported from hl_deck; regime changes and burst alerts below. The agent can explain any card or event (hl_alerts).",
      badges: h(Prov, { mode: r.mode, source: g(deck, names[0], "provenance", "source"), what: "trades" })
    },
    h("div", { className: "hd-deck-cards" }, names.length ? names.map(function (n) {
      return h(DeckCard, { key: n, name: n, d: deck[n] });
    }) : h("div", { className: "hd-empty" }, "No Flow Deck cards in this snapshot.")),
    h("div", { className: "hd-events" },
      h("div", { className: "hd-events-head" }, "EVENTS", h("span", { className: "hd-dim" }, " newest first \u00b7 no regime or burst spans a gap \u00b7 UTC")),
      events.length ? h("ul", null, events.slice(0, 40).map(function (e, i) {
        return h(EventRow, { key: e.type + ":" + e.market + ":" + e.at_ms + ":" + (e.side || "") + ":" + i, e: e });
      })) : h("div", { className: "hd-empty" }, "No events in the current coverage yet.")));
  }

  // ------------------------------------------------------------------ wallets

  function WalletTable(props) {
    const list = props.list;
    const rows = props.rows || {};
    const isBuy = props.side === "buy";
    if (!list) return h(Unavailable, { reason: props.reason || "not in snapshot", block: true });
    const listed = list.listed || [];
    const total = valueOf(list.total_usd);
    const reached = valueOf(list.share_reached_pct);
    function rowVal(a, k, fmt) {
      const r = rows[a];
      if (!r) return h(Unavailable, { reason: "row not in snapshot", compact: true });
      const v = r[k];
      if (v !== null && typeof v === "object") return h(V, { m: v, fmt: fmt, compact: true });
      return isNum(v) ? fmt(v) : h(Unavailable, { reason: "not in snapshot", compact: true });
    }
    return h("div", { className: "hd-wt" },
      h("div", { className: "hd-wt-title" }, h("b", { className: isBuy ? "hd-bid" : "hd-ask" }, isBuy ? "Buyers" : "Sellers"),
        h("span", { className: "hd-dim" }, isBuy ? " ranked by bought notional" : " ranked by sold notional"),
        h("span", { className: "hd-dim" }, " \u00b7 side total "), total === null ? h(Unavailable, { reason: reasonOf(list.total_usd), compact: true }) : h("b", { className: "hd-num" }, usd(total))),
      h("div", { className: "hd-scroll hd-wt-scroll" },
        h("table", { className: "hd-table hd-wallets" },
          h("thead", null, h("tr", null,
            h("th", { scope: "col", className: "hd-r" }, "#"),
            h("th", { scope: "col" }, "Address"),
            h("th", { scope: "col", className: "hd-r" }, isBuy ? "Bought" : "Sold"),
            h("th", { scope: "col", className: "hd-r" }, isBuy ? "Buy share" : "Sell share"),
            h("th", { scope: "col", className: "hd-r" }, isBuy ? "Sold" : "Bought"),
            h("th", { scope: "col", className: "hd-r", title: "the part where this wallet was the taker" }, "Aggr. bought"),
            h("th", { scope: "col", className: "hd-r", title: "the part where this wallet was the taker" }, "Aggr. sold"),
            h("th", { scope: "col", className: "hd-r", title: "bought minus sold notional; not a position change or PnL" }, "Net bought"),
            h("th", { scope: "col", className: "hd-r", title: "bought + sold; not market volume" }, "Participation"),
            h("th", { scope: "col", className: "hd-r" }, "Execs"))),
          h("tbody", null, listed.length ? listed.map(function (x) {
            return h("tr", { key: x.address },
              h("td", { className: "hd-r hd-dim" }, x.rank),
              h("td", null, h(Addr, { address: x.address, onOpen: props.onOpen })),
              h("td", { className: "hd-r hd-num" }, isNum(x.amount_usd) ? usd(x.amount_usd) : missing()),
              h("td", { className: "hd-r hd-num hd-b" }, isNum(x.share_pct) ? pct(x.share_pct, 1) : h(Unavailable, { reason: "share not in snapshot", compact: true })),
              h("td", { className: "hd-r hd-num" }, rowVal(x.address, isBuy ? "sold_usd" : "bought_usd", usd)),
              h("td", { className: "hd-r hd-num" }, rowVal(x.address, "aggressive_bought_usd", usd)),
              h("td", { className: "hd-r hd-num" }, rowVal(x.address, "aggressive_sold_usd", usd)),
              h("td", { className: "hd-r hd-num" }, rowVal(x.address, "net_bought_usd", usdSigned)),
              h("td", { className: "hd-r hd-num" }, rowVal(x.address, "participation_usd", usd)),
              h("td", { className: "hd-r hd-num hd-dim" }, rowVal(x.address, "executions", intf)));
          }) : h("tr", null, h("td", { colSpan: 10, className: "hd-dim" }, "No wallets listed on this side."))))),
      h("div", { className: "hd-summary" }, list.summary || "summary not in snapshot"),
      h("div", { className: "hd-dim hd-small" },
        "share reached ", reached === null ? h(Unavailable, { reason: reasonOf(list.share_reached_pct), compact: true }) : pct(reached, 1),
        " \u00b7 target " + (isNum(list.target_pct) ? pct(list.target_pct, 0) : "?") +
        " \u00b7 cap " + (isNum(list.cap) ? list.cap : "?") +
        " \u00b7 stopped by " + (list.stopped_by || "?") +
        (isNum(list.wallets_on_side) ? " \u00b7 " + intf(list.wallets_on_side) + " wallets on this side" : "")));
  }

  function WalletsPanel(props) {
    const m = props.m;
    const win = props.win;
    const p = g(m, "wallets", win);
    const w = (p && p.window) || {};
    return h(Panel, {
      id: "wallets", className: "hd-p-wallets", title: "Wallets",
      sub: "Public addresses behind this market's volume, from the flow metric's execution set. Each execution counts once to its buyer's bought and once to its seller's sold notional. Addresses only; owners are not identified. Click an address to open it.",
      badges: h(Fragment, null,
        h("div", { className: "hd-toggle", role: "group", "aria-label": "Wallet window" }, WALLET_WINDOWS.map(function (k) {
          return h("button", {
            key: k, type: "button", className: cx("hd-btn hd-btn-xs", win === k && "hd-btn-on"),
            "aria-pressed": win === k ? "true" : "false", onClick: function () { props.onWin(k); }
          }, k.replace("m", " min"));
        })),
        h(Prov, { mode: g(m, "provenance", "mode"), source: g(m, "provenance", "source"), what: "trades (users)" })),
      status: h(Fresh, { f: g(m, "freshness", "trades"), label: "trades" })
    },
    !p ? h(Unavailable, { reason: "the " + win + " wallet window isn't in this snapshot", block: true })
      : p.reason && !p.buyers ? h(Unavailable, { reason: p.reason, block: true })
        : h(Fragment, null,
          h("div", { className: "hd-window" },
            "window (", hmsms(w.start), ", ", hmsms(w.end), "] UTC \u00b7 ",
            isNum(w.length_s) ? secs(w.length_s) : "length not in snapshot",
            isNum(w.requested_min) ? " of " + w.requested_min + " min requested" : "", " \u00b7 ",
            h("span", { className: w.complete ? "hd-tone-text-ok" : "hd-tone-text-warn" }, w.completeness || (w.complete ? "complete" : "incomplete")),
            isNum(p.executions) ? " \u00b7 " + intf(p.executions) + " executions" : "",
            isNum(p.unique_execution_notional_usd) ? " \u00b7 unique execution notional " + usd(p.unique_execution_notional_usd) : ""),
          h("div", { className: "hd-wt-pair" },
            h(WalletTable, { side: "buy", list: p.buyers, rows: p.wallets, onOpen: props.onOpen, reason: p.reason }),
            h(WalletTable, { side: "sell", list: p.sellers, rows: p.wallets, onOpen: props.onOpen, reason: p.reason })),
          (p.notes || []).length ? h("ul", { className: "hd-notes" }, p.notes.map(function (t, i) { return h("li", { key: i }, t); })) : null,
          h("div", { className: "hd-dim hd-small", "data-node-hook": "wallet-activity-split" },
            "Wallet activity split (new long, cover, new short, exit): needs node mode.")));
  }

  function KV(props) {
    return h("div", { className: "hd-kv" }, h("span", { className: "hd-dim" }, props.k), " ", h("b", { className: "hd-num" }, props.children));
  }

  // <pure:snapshotValidity> Whether a served snapshot may still be shown as current. Its age is the age the backend
  // reported when serving it plus all the time since the tab sent the request, so delivery delay and the time it
  // stays on screen count (an upper bound). It is withheld once that age passes the latest-snapshot limit, or once
  // any status it shows as fresh (the chosen market's fields and every market's Flow Deck trades) would no longer
  // be fresh: progress age past its max age, or source age plus the clock bound past its source max age.
  function snapshotValidity(data, requestedPerf, nowPerf, latestMaxS) {
    const served = (data && data.served) || {};
    if (served.latest !== true) {
      return { ok: false, reason: "this answer isn't the latest snapshot, so it isn't shown as current" };
    }
    if (typeof served.age_s !== "number" || typeof requestedPerf !== "number") {
      return { ok: false, reason: "the snapshot's age when served or the time it was requested is unknown, so it isn't shown as current" };
    }
    const age = served.age_s + Math.max(0, nowPerf - requestedPerf) / 1000;
    if (age > latestMaxS) {
      return { ok: false, age: age, reason: "the latest snapshot is now about " + age.toFixed(1) + " s old (limit " + latestMaxS +
        " s), counting the time since the tab asked for it, so its values aren't shown as current" };
    }
    const fields = [];
    const m = data.market || {};
    const fr = m.freshness || {};
    Object.keys(fr).forEach(function (k) { fields.push([(m.market || "market") + " " + k, fr[k]]); });
    const deck = data.deck || {};
    Object.keys(deck).forEach(function (k) { fields.push([k + " trades (Flow Deck)", (deck[k] || {}).trades_freshness]); });
    for (let i = 0; i < fields.length; i++) {
      const label = fields[i][0], f = fields[i][1];
      if (!f || f.status !== "fresh") continue;
      if (typeof f.max_age_s === "number" && typeof f.progress_age_s === "number" && f.progress_age_s + age > f.max_age_s) {
        return { ok: false, age: age, reason: label + " was fresh when the snapshot was built, but by now its last progress is about " +
          (f.progress_age_s + age).toFixed(1) + " s old (max " + f.max_age_s + " s), so the snapshot isn't shown as current" };
      }
      if (typeof f.source_max_age_s === "number" && typeof f.source_age_s === "number") {
        const bound = typeof f.clock_bound_s === "number" ? f.clock_bound_s : 0;
        if (f.source_age_s + age + bound > f.source_max_age_s) {
          return { ok: false, age: age, reason: label + " was fresh when the snapshot was built, but by now its source time is about " +
            (f.source_age_s + age).toFixed(1) + " s old (max " + f.source_max_age_s + " s with a " + bound + " s clock bound), so the snapshot isn't shown as current" };
        }
      }
    }
    return { ok: true, age: age, reason: null };
  }
  // </pure:snapshotValidity>

  // <pure:liveAccountState> An opened wallet's account answer keeps aging, from the moment the tab asked for it.
  // Its age is the source age the backend assessed plus all the time since the request was sent: that covers
  // delivery to the browser and every second the window stays open, and over-counts only the backend's own
  // request time, so it is an upper bound. Once that age, widened by the clock bound, passes the limit, the values
  // are withheld, including immediately on arrival (schema freshness.fields.clearinghouseState).
  function liveAccountState(fr, requestedPerf, nowPerf) {
    if (!fr || fr.status !== "fresh") {
      return { status: fr ? fr.status : null, reason: fr ? fr.reason : null, age: fr ? fr.source_age_s : null, expired: false };
    }
    if (typeof requestedPerf !== "number" || typeof fr.source_age_s !== "number" || typeof fr.source_max_age_s !== "number") {
      return { status: "expired", age: null, expired: true,
        reason: "the time this account answer was requested wasn't recorded, so it can't be shown as current; fetch it again" };
    }
    const age = fr.source_age_s + Math.max(0, nowPerf - requestedPerf) / 1000;
    const bound = typeof fr.clock_bound_s === "number" ? fr.clock_bound_s : 0;
    if (age + bound > fr.source_max_age_s) {
      return { status: "expired", age: age, expired: true,
        reason: "this account state is now about " + age.toFixed(1) + " s old (limit " + fr.source_max_age_s +
          " s), so its values are no longer shown as current; fetch it again" };
    }
    return { status: "fresh", reason: null, age: age, expired: false };
  }
  // </pure:liveAccountState>

  function WalletDrill(props) {
    const d = props.drill;
    const closeRef = useRef(null);
    const backRef = useRef(null);
    // The host page can paint its sidebar above a fixed overlay, so the overlay starts at the tab's own left edge.
    const [left, setLeft] = useState(0);
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") props.onClose(); }
      function measure() {
        const root = backRef.current && backRef.current.closest(".hd-root");
        setLeft(root ? Math.max(0, Math.round(root.getBoundingClientRect().left)) : 0);
      }
      measure();
      document.addEventListener("keydown", onKey);
      window.addEventListener("resize", measure);
      if (closeRef.current) closeRef.current.focus();
      return function () {
        document.removeEventListener("keydown", onKey);
        window.removeEventListener("resize", measure);
      };
    }, []);
    // Re-render every second so the account section ages while the window is open; the age is computed from the
    // clock at render time, so an answer that arrives already too old is withheld on its first render.
    const [, setTick] = useState(0);
    useEffect(function () {
      const id = setInterval(function () { setTick(function (n) { return n + 1; }); }, 1000);
      return function () { clearInterval(id); };
    }, []);
    const nowPerf = performance.now();
    const data = d.data;
    const wallet = g(data, "wallet") || {};
    const trades = wallet.trades || {};
    const cur = wallet.current || {};
    const curFr = g(cur, "account", "freshness") || {};
    const live = liveAccountState(curFr, d.requested_perf, nowPerf);
    const execs = trades.executions || [];
    const tot = trades.totals || {};
    const row = g(data, "panel", "wallets", d.address);
    let content;
    if (d.loading) content = h("div", { className: "hd-empty" }, "Loading " + ELLIPSIS);
    else if (d.error) content = h(Unavailable, { reason: d.error, block: true });
    else if (data && data.ok === false) content = h(Unavailable, { reason: data.error || "the wallets route said no", block: true });
    else if (wallet.error) content = h(Unavailable, { reason: wallet.error, block: true });
    else {
      content = h(Fragment, null,
        h("section", { className: "hd-drill-sec" },
          h("h3", null, "Trades in this panel's window"),
          h("div", { className: "hd-dim hd-small" }, "From the execution set of snapshot ", h("code", { className: "hd-code" }, data ? data.snapshot_id : d.snapshot_id),
            trades.window ? " \u00b7 window (" + hmsms(trades.window.start) + ", " + hmsms(trades.window.end) + "] UTC" + (trades.window.completeness ? " \u00b7 " + trades.window.completeness : "") : "",
            data && data.snapshot_id && data.snapshot_id !== d.snapshot_id ? " (asked for " + d.snapshot_id + ")" : ""),
          trades.error ? h(Unavailable, { reason: trades.error, block: true }) : h(Fragment, null,
            h("div", { className: "hd-scroll hd-drill-scroll" },
              h("table", { className: "hd-table" },
                h("thead", null, h("tr", null,
                  h("th", { scope: "col" }, "Time"), h("th", { scope: "col" }, "Role"), h("th", { scope: "col" }, "Taker side"),
                  h("th", { scope: "col" }, "This wallet"), h("th", { scope: "col", className: "hd-r" }, "Price"),
                  h("th", { scope: "col", className: "hd-r" }, "Size"), h("th", { scope: "col", className: "hd-r" }, "Notional"),
                  h("th", { scope: "col" }, "Counterparty"), h("th", { scope: "col", className: "hd-r" }, "tid"))),
                h("tbody", null, execs.length ? execs.map(function (x, i) {
                  return h("tr", { key: (x.tid || "") + ":" + i },
                    h("td", { className: "hd-num" }, hmsms(x.time)),
                    h("td", null, x.role),
                    h("td", { className: x.aggressor_side === "B" ? "hd-bid" : "hd-ask" }, x.aggressor_side === "B" ? "buyer took" : (x.aggressor_side === "A" ? "seller took" : String(x.aggressor_side))),
                    h("td", null, x.aggressive ? "taker" : "maker"),
                    h("td", { className: "hd-r hd-num" }, isNum(x.px) ? px(x.px) : missing()),
                    h("td", { className: "hd-r hd-num" }, isNum(x.sz) ? sz(x.sz) : missing()),
                    h("td", { className: "hd-r hd-num" }, isNum(x.notional_usd) ? usdFull(x.notional_usd) : missing()),
                    h("td", null, h(Addr, { address: x.counterparty })),
                    h("td", { className: "hd-r hd-dim hd-num" }, x.tid));
                }) : h("tr", null, h("td", { colSpan: 9, className: "hd-dim" }, "No executions for this address in the window."))))),
            h("div", { className: "hd-drill-totals" },
              h("div", { className: "hd-small hd-dim" }, "Totals of the executions above (they add up to the wallet's row in the panel):"),
              h(KV, { k: "bought" }, isNum(tot.bought_usd) ? usdFull(tot.bought_usd) : "not in answer"),
              h(KV, { k: "sold" }, isNum(tot.sold_usd) ? usdFull(tot.sold_usd) : "not in answer"),
              h(KV, { k: "aggressive bought" }, isNum(tot.aggressive_bought_usd) ? usdFull(tot.aggressive_bought_usd) : "not in answer"),
              h(KV, { k: "aggressive sold" }, isNum(tot.aggressive_sold_usd) ? usdFull(tot.aggressive_sold_usd) : "not in answer"),
              h(KV, { k: "net bought" }, isNum(tot.net_bought_usd) ? signOf(tot.net_bought_usd, true) + usdFull(Math.abs(tot.net_bought_usd)) : "not in answer"),
              row ? h("div", { className: "hd-small hd-dim" }, "Panel row: bought " + usdFull(row.bought_usd) + " \u00b7 sold " + usdFull(row.sold_usd) +
                " \u00b7 aggressive bought " + usdFull(row.aggressive_bought_usd) + " \u00b7 aggressive sold " + usdFull(row.aggressive_sold_usd) +
                " \u00b7 net bought " + usdFull(row.net_bought_usd)) : null,
              h("div", { className: "hd-small hd-dim" }, "Net bought is notional, not a position change or PnL; bought + sold is participation, not market volume.")))),
        h("section", { className: "hd-drill-sec hd-drill-current" },
          h("h3", null, live.expired
            ? "account state as of " + (curFr.source_time ? dateTime(curFr.source_time) : "an unknown time") + ": expired, not current"
            : (cur.label || "current, not part of the snapshot")),
          h("div", { className: "hd-small" },
            h("button", { type: "button", className: "hd-btn", disabled: !!(d.loading || d.refreshing), onClick: props.onRefresh },
              d.refreshing ? "Fetching " + ELLIPSIS : "Fetch account again"),
            d.refresh_error ? h(Unavailable, { reason: "fetching again failed: " + d.refresh_error, block: true }) : null),
          cur.error ? h(Unavailable, { reason: cur.error, block: true }) : h(Fragment, null,
            h("div", { className: "hd-small" },
              h(KV, { k: "both answers in hand at" }, cur.fetched_at ? dateTime(cur.fetched_at) : "not recorded"),
              h(KV, { k: "dex" }, cur.dex || "not in answer"),
              h("div", { className: "hd-tone-text-warn" }, cur.coverage || "coverage not stated"),
              cur.source ? h("div", { className: "hd-dim" }, "source: " + cur.source) : null),
            (function () {
              const acc = cur.account || {};
              const fr = acc.freshness || {};
              const ms = acc.margin_summary || {};
              const pos = acc.positions;
              const posList = pos && Array.isArray(pos.value) ? pos.value : null;
              const oo = cur.open_orders || {};
              // Expired values are withheld exactly like any other non-fresh value: null with the reason.
              const shown = function (metric) { return live.expired ? { value: null, reason: live.reason } : metric; };
              return h(Fragment, null,
                h("div", { className: "hd-small" },
                  h(KV, { k: "account state" }, h("span", { className: "hd-tone-text-" + toneOfStatus(live.status) }, String(live.status || "status not in answer").replace("_", " ")),
                    fr.source_time ? " \u00b7 its time " + dateTime(fr.source_time) : "",
                    isNum(fr.source_age_s) ? " \u00b7 " + fr.source_age_s + " s old when served, request time included (limit " + fr.source_max_age_s + " s)" : "",
                    live.status === "fresh" && isNum(live.age) ? " \u00b7 now about " + live.age.toFixed(1) + " s old" : ""),
                  (live.reason || fr.reason) ? h("div", { className: "hd-na" }, live.reason || fr.reason) : null,
                  h(KV, { k: "account value" }, h(V, { m: shown(ms.account_value_usd), fmt: usdFull, compact: true })),
                  h(KV, { k: "total position notional" }, h(V, { m: shown(ms.total_position_notional_usd), fmt: usdFull, compact: true })),
                  h(KV, { k: "margin used" }, h(V, { m: shown(ms.total_margin_used_usd), fmt: usdFull, compact: true }))),
                h("h4", null, "Positions"),
                live.expired ? h(Unavailable, { reason: live.reason, block: true })
                  : posList === null ? h(Unavailable, { reason: reasonOf(pos) || "positions not in answer", block: true })
                  : (posList.length ? h("div", { className: "hd-scroll" }, h("table", { className: "hd-table" },
                    h("thead", null, h("tr", null,
                      h("th", { scope: "col" }, "Coin"), h("th", { scope: "col", className: "hd-r" }, "Size"), h("th", { scope: "col", className: "hd-r" }, "Entry px"),
                      h("th", { scope: "col", className: "hd-r" }, "Position value"), h("th", { scope: "col", className: "hd-r" }, "Unrealized PnL"),
                      h("th", { scope: "col", className: "hd-r" }, "Liq. px"), h("th", { scope: "col", className: "hd-r" }, "Leverage"))),
                    h("tbody", null, posList.map(function (p, i) {
                      return h("tr", { key: (p.coin || "") + i },
                        h("td", null, p.coin || "coin not in answer"),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.size_coin, fmt: function (v) { return signOf(v, true) + sz(Math.abs(v)); }, compact: true })),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.entry_px, fmt: px, compact: true })),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.position_value_usd, fmt: usdFull, compact: true })),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.unrealized_pnl_usd, fmt: usdSigned, compact: true })),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.liquidation_px, fmt: px, compact: true })),
                        h("td", { className: "hd-r hd-num" }, h(V, { m: p.leverage, fmt: function (v) { return v + "\u00d7" + (p.leverage && p.leverage.type ? " " + p.leverage.type : ""); }, compact: true })));
                    })))) : h("div", { className: "hd-dim hd-small" }, "No positions on this dex (the account answer is fresh and lists none).")),
                h("h4", null, "Open orders"),
                h(Unavailable, { reason: reasonOf(oo.orders) || g(oo, "freshness", "reason") || "open orders not in answer", block: true }));
            })())));
    }
    return h("div", { className: "hd-drill-backdrop", ref: backRef, style: { left: left + "px" }, onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("div", { className: "hd-drill", role: "dialog", "aria-modal": "true", "aria-label": "Wallet " + d.address },
        h("div", { className: "hd-drill-head" },
          h("div", null,
            h("div", { className: "hd-drill-title" }, "Wallet ", h("code", { className: "hd-code" }, d.address), " ", h(CopyButton, { text: d.address, what: "address" })),
            h("div", { className: "hd-dim hd-small" }, (wallet.note || "an address, not an identified owner") + " \u00b7 " + d.market + " \u00b7 " + d.window_min + " min window \u00b7 snapshot " + d.snapshot_id)),
          h("button", { type: "button", className: "hd-btn", onClick: props.onClose, ref: closeRef }, "Close")),
        content));
  }

  // ------------------------------------------------------------------ footer

  function Footer() {
    return h("footer", { className: "hd-card hd-footer" },
      h("div", { className: "hd-legend" },
        h("span", { className: "hd-dim" }, "Where each number comes from"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "api" }, "PUBLIC API"), "Hyperliquid public websocket (l2Book, trades, bbo, activeAssetCtx) and info endpoint (l2Book nSigFigs, clearinghouseState, frontendOpenOrders)"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "mode-live" }, "LIVE"), "collected now"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "mode-replay" }, "REPLAY"), "recorded public data replayed from a file"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "mode-synthetic" }, "SYNTHETIC"), "constructed test data, not market data"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "node" }, "NODE"), "needs node mode (not in this version)"),
        h("span", { className: "hd-legend-item" }, h(Badge, { kind: "planned" }, "PLANNED"), "sampled visible liquidity heatmap: not built in v1")),
      h("div", { className: "hd-legend" },
        h("span", { className: "hd-dim" }, "Freshness"),
        FRESHNESS_LEGEND.map(function (x) {
          return h("span", { key: x[0], className: "hd-chip hd-tone-" + toneOfStatus(x[0]), title: x[1] },
            h("span", { className: "hd-dot", "aria-hidden": "true" }), x[0].replace("_", " "));
        }),
        h("span", { className: "hd-dim" }, "Unavailable values show their reason, never 0. Descriptive public data, not trade advice.")));
  }

  // ------------------------------------------------------------------ app

  function App() {
    const [market, setMarket] = useState(null);
    const [resp, setResp] = useState(null);
    const [noSnap, setNoSnap] = useState(null);
    const [pollErr, setPollErr] = useState(null);
    const [lastOkAt, setLastOkAt] = useState(null);
    const [hidden, setHidden] = useState(typeof document !== "undefined" && document.hidden);
    const [action, setAction] = useState(null);
    const [busy, setBusy] = useState(null);
    const [walletWin, setWalletWin] = useState("5m");
    const [drill, setDrill] = useState(null);
    const marketRef = useRef(null);
    const inflight = useRef(false);
    const alive = useRef(false);
    const newest = useRef({});
    const shown = useRef(null);   // {data, requestedPerf}: the snapshot on screen and when the tab asked for it

    const poll = useCallback(function (force) {
      if (!alive.current || document.hidden) return;
      if (inflight.current && !force) return;
      inflight.current = true;
      const asked = marketRef.current;
      const url = API + "/snapshot" + (asked ? "?market=" + encodeURIComponent(asked) : "");
      const requestedPerf = performance.now();
      SDK.fetchJSON(url).then(function (data) {
        if (!alive.current) return;
        if (marketRef.current !== null && asked !== marketRef.current) return; // answer for an earlier selection
        if (data && data.ok === true) {
          const name = data.market ? data.market.market : "";
          const prev = newest.current[name];
          if (isNum(prev) && isNum(data.built_at_ms) && data.built_at_ms < prev) return; // an older answer arriving late
          newest.current[name] = data.built_at_ms;
          // An answer that arrives already too old (a slow delivery) is withheld on arrival, never shown.
          const v = snapshotValidity(data, requestedPerf, performance.now(), LATEST_MAX_S);
          if (!v.ok) {
            shown.current = null;
            setResp(null);
            setNoSnap({ error: v.reason });
            setPollErr(null);
            return;
          }
          shown.current = { data: data, requestedPerf: requestedPerf };
          setResp(data);
          setNoSnap(null);
          setPollErr(null);
          setLastOkAt(Date.now());
          if (marketRef.current === null && name) {
            marketRef.current = name;
            setMarket(name);
          }
        } else {
          // No current snapshot (collector stopped, not publishing, or not running): never keep old values on screen.
          shown.current = null;
          setResp(null);
          setNoSnap(data || { error: "empty answer from the snapshot route" });
          setPollErr(null);
        }
      }).catch(function (e) {
        if (alive.current) setPollErr({ message: errText(e), at: Date.now() });
      }).then(function () {
        inflight.current = false;
      });
    }, []);

    // The snapshot on screen keeps aging: withhold it, and say why, as soon as it is too old or any status it
    // shows as fresh would no longer be fresh (snapshotValidity). Runs while the page is hidden too, so a return
    // to the tab never shows an old snapshot.
    useEffect(function () {
      const id = setInterval(function () {
        const s = shown.current;
        if (s === null) return;
        const v = snapshotValidity(s.data, s.requestedPerf, performance.now(), LATEST_MAX_S);
        if (!v.ok) {
          shown.current = null;
          setResp(null);
          setNoSnap({ error: v.reason });
        }
      }, 250);
      return function () { clearInterval(id); };
    }, []);

    useEffect(function () {
      alive.current = true;
      poll(true);
      const id = setInterval(poll, POLL_MS);
      function onVis() {
        setHidden(document.hidden);
        if (!document.hidden) poll(true);
      }
      document.addEventListener("visibilitychange", onVis);
      return function () {
        alive.current = false;
        clearInterval(id);
        document.removeEventListener("visibilitychange", onVis);
      };
    }, [poll]);

    function chooseMarket(name) {
      if (name === marketRef.current) return;
      marketRef.current = name;
      setMarket(name);
      poll(true);
    }

    function control(which) {
      setBusy(which);
      SDK.fetchJSON(API + "/collector/" + which, { method: "POST" }).then(function (body) {
        if (alive.current) setAction({ action: which, body: body });
      }).catch(function (e) {
        if (alive.current) setAction({ action: which, error: errText(e) });
      }).then(function () {
        if (!alive.current) return;
        setBusy(null);
        poll(true);
      });
    }

    // Fetches a wallet's trades (from the drill's snapshot) and its account state. requested_perf records when the
    // tab asked, so the account section ages from then, delivery included (liveAccountState). A failed refresh keeps
    // the earlier answer, which by then is shown as expired.
    function loadWallet(req, refresh) {
      const requestedPerf = performance.now();
      const url = API + "/wallets?market=" + encodeURIComponent(req.market) + "&window_min=" + req.window_min +
        "&address=" + encodeURIComponent(req.address) + "&snapshot_id=" + encodeURIComponent(req.snapshot_id);
      function settle(patch) {
        if (!alive.current) return;
        setDrill(function (cur) {
          return cur && cur.address === req.address && cur.snapshot_id === req.snapshot_id && cur.window_min === req.window_min
            ? Object.assign({}, cur, patch) : cur;
        });
      }
      SDK.fetchJSON(url).then(function (data) {
        if (refresh && data && data.ok === false) {
          settle({ refreshing: false, refresh_error: data.error || "the wallets route said no" });
        } else {
          settle({ loading: false, refreshing: false, refresh_error: null, data: data,
            requested_perf: requestedPerf, received_perf: performance.now() });
        }
      }).catch(function (e) {
        settle(refresh ? { refreshing: false, refresh_error: errText(e) } : { loading: false, error: errText(e) });
      });
    }

    function openWallet(address) {
      if (!resp || !resp.market) return;
      const req = {
        address: address, market: resp.market.market, window_min: parseInt(walletWin, 10),
        snapshot_id: resp.snapshot_id, loading: true
      };
      setDrill(req);
      loadWallet(req, false);
    }

    function refreshWallet() {
      if (!drill || drill.loading || drill.refreshing) return;
      setDrill(Object.assign({}, drill, { refreshing: true, refresh_error: null }));
      loadWallet(drill, true);
    }

    const m = resp ? resp.market : null;
    return h("div", { className: "hd-root" },
      h(Banners, { resp: resp, noSnap: noSnap, pollErr: pollErr, lastOkAt: lastOkAt, hidden: hidden }),
      h(Header, {
        resp: resp, noSnap: noSnap, selected: market, onMarket: chooseMarket,
        busy: busy, action: action, onControl: control
      }),
      m ? h(Fragment, null,
        h("div", { className: "hd-row hd-row-a" },
          h(Ladder, { m: m }),
          h(PriceChart, { m: m }),
          h(NodeCard, {
            hook: "waiting-stops", className: "hd-p-stops", title: "Waiting stops", what: "L4",
            sub: "Trigger orders the public feed never shows.",
            columns: ["Price", "Buy stops above \u00b7 sell below", "Size"],
            desc: "Stop and trigger orders resting at each price need the node's order-level book; the public API doesn't publish them.",
            hookText: g(m, "node", "hook")
          })),
        h("div", { className: "hd-row hd-row-b" },
          h(FlowPanel, { m: m }),
          h(DepthPanel, { m: m }),
          h(NodeCard, {
            hook: "wall-watch", className: "hd-p-walls", title: "Wall watch", what: "L4",
            sub: "The largest single orders near the price, and what each did when tested.",
            columns: ["Side", "Price", "Order", "Away", "Age", "Behavior", "Status"],
            desc: "Single orders need the node's order-level book: public levels are aggregated, so no level can be shown as one order.",
            hookText: g(m, "node", "hook")
          })),
        h("div", { className: "hd-row hd-row-c" },
          h(TapePanel, { m: m }),
          h(DeckPanel, { resp: resp })),
        h("div", { className: "hd-row hd-row-d" },
          h(WalletsPanel, { m: m, win: walletWin, onWin: setWalletWin, onOpen: openWallet })))
        : h("div", { className: "hd-card hd-empty-state" },
          resp ? "The snapshot has no market to show." :
            (noSnap ? h(Fragment, null, h("b", null, "No snapshot yet. "), noSnap.error || "", " Start the collector above (or /hl start in chat).")
              : (pollErr ? "Couldn't reach the plugin's snapshot route: " + pollErr.message : "Loading the first snapshot" + ELLIPSIS))),
      h(Footer, null),
      drill ? h(WalletDrill, { drill: drill, onRefresh: refreshWallet, onClose: function () { setDrill(null); } }) : null);
  }

  REGISTRY.register(PLUGIN, App);
})();
