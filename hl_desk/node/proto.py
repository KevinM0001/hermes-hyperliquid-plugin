"""Protocol v2 constants and helpers (PROTOCOL.md sections 2-4, 10 and 11)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

VERSION = 2
DOC_REVISION = "2.2"
ALLOWLIST_VERSION = "livefeed-v2.0"

FAMILIES = ("book", "triggers", "depth", "bands", "flow")
KIND_FAMILY = {
    "book_snapshot": "book", "book_delta": "book",
    "trig_snapshot": "triggers", "trig_delta": "triggers",
    "depth": "depth", "bands": "bands", "execs": "flow", "watermark": None,
}
SNAPSHOT_KINDS = {"book_snapshot": "book", "trig_snapshot": "triggers"}
STATE_FAMILIES = ("book", "triggers")       # families with snapshot + delta state
VIEW_FAMILIES = ("depth", "bands")          # self-contained views

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
DECIMAL_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

# Client defaults (PROTOCOL.md sections 6.3 and 8); the producer's own limits arrive in hello.limits.
CLIENT_DEFAULTS = {
    "max_snapshot_bytes": 64 * 1024 * 1024,
    "snapshot_timeout_ms": 10_000,
    # Freshness (Kevin, Nov 8: no toggling): a family's state comes from the node feed's delay, the age of the
    # market's latest watermark (now minus its block time) averaged over the last delay_window_ms. It reads BEHIND
    # ("delayed") above delayed_after_ms and STALE above stale_after_ms, both well above the normal delay.
    "delay_window_ms": 5_000,
    "delayed_after_ms": 10_000,
    "stale_after_ms": 30_000,
    "skew_tolerance_ms": 1_000,
    "buffer_messages": 50_000,       # book/trigger deltas kept since the last gap, for snapshot catch-up
}

# Every field a feed line may carry (allowlist livefeed-v2.0, as PROTOCOL.md documents it), used to
# check test fixtures and recordings the plugin ships. Envelope and control fields:
ENVELOPE_FIELDS = {"v", "epoch", "session", "market", "seq", "kind", "block", "block_time_ms", "pub_ms",
                   "body", "mode", "replay_session"}
CONTROL_FIELDS = {
    "hello": {"v", "control", "epoch", "session", "started_ms", "pub_ms", "mode", "markets", "unknown_markets",
              "limits", "labels", "allowlist_version", "replay_session", "replay"},
    "replay_unavailable": {"v", "control", "market", "requested", "oldest_seq", "next_seq", "start_seq",
                           "mode", "replay_session"},
    "heartbeat": {"v", "control", "epoch", "session", "pub_ms", "replay", "mode", "replay_session"},
    "dropped": {"v", "control", "reason", "last_sent", "mode", "replay_session"},
    "error": {"v", "control", "reason", "supported"},
}
BODY_FIELDS = {
    "book_snapshot": {"snapshot_id", "part", "parts", "through_seq", "coverage", "orders", "total_orders"},
    "trig_snapshot": {"snapshot_id", "part", "parts", "through_seq", "coverage", "triggers", "total_triggers",
                      "excluded"},
    "book_delta": {"changes"},
    "trig_delta": {"changes", "excluded"},
    "depth": {"levels", "fields", "bids", "asks", "available"},
    "bands": {"label", "width", "range_bps", "ref_px", "lo", "hi", "fields", "rows", "outside", "excluded"},
    "execs": {"execs", "fills_known"},
    "watermark": {"through_seq"},
}
BOOK_CHANGE_FIELDS = {"op", "oid", "side", "px", "sz", "user", "time_ms", "prev_sz", "maker_filled", "status"}
TRIG_CHANGE_FIELDS = {"op", "oid", "side", "kind", "trigger_px", "sz", "position_tpsl", "user", "time_ms", "status"}
EXEC_FIELDS = {"tid", "px", "sz", "time_ms", "aggressor", "complete", "participants", "liquidation", "conflict",
               "duplicates"}
PARTICIPANT_FIELDS = {"user", "side", "role", "oid", "effect", "effect_unavailable", "twap_id", "liquidated"}
EFFECT_FIELDS = {"cover", "new_long", "exit", "new_short"}
LIQUIDATION_FIELDS = {"liquidated_user", "mark_px", "method"}
COVERAGE_FIELDS = {"family", "scope", "fields"}
EXCLUDED_FIELDS = {"trailing", "unclassified"}
ORDER_ROW_FIELDS = {"oid", "side", "px", "sz", "user", "time_ms"}
TRIGGER_ROW_FIELDS = {"oid", "side", "kind", "trigger_px", "sz", "position_tpsl", "user", "time_ms"}
DEPTH_ROW_FIELDS = {"px", "total_sz", "orders", "largest_order_sz", "largest_order_time_ms", "oldest_order_time_ms"}
DEPTH_AVAILABLE_FIELDS = {"bids", "asks"}
BAND_ROW_FIELDS = {"lo", "hi", "kind", "side", "orders", "sz", "position_tpsl_orders"}
BAND_OUTSIDE_FIELDS = {"below", "above"}
BAND_OUTSIDE_ROW_FIELDS = {"kind", "side", "orders", "sz", "position_tpsl_orders"}
# Nested control values (PROTOCOL.md section 11, revision 2.2: checked by value as well as by name).
HELLO_MARKET_FIELDS = {"next_seq", "start_seq", "oldest_seq", "snapshot"}
LIMIT_FIELDS = {"watermark_interval_ms", "heartbeat_interval_ms", "depth_interval_ms", "depth_levels",
                "bands_interval_ms", "band_range_bps", "bands_width", "snapshot_interval_ms",
                "snapshot_chunk_max_bytes", "snapshot_max_chunks", "ring_messages", "ring_bytes", "subscriber_buffer"}
LABEL_FIELDS = {"depth.largest_order_sz", "depth.largest_order_time_ms", "depth.oldest_order_time_ms", "bands", "time_ms"}
REPLAY_STATUS_FIELDS = {"state", "speed", "position_ms", "recording_start_ms", "recording_end_ms", "partial_history",
                        "recording_id"}
# Node fields the feed never carries, wherever they would appear (the protocol's "never" list).
NEVER_FIELDS = {"closedPnl", "closed_pnl", "fee", "feeToken", "builderFee", "builder", "deployerFee", "priorityGas",
                "cloid", "hash", "startPosition", "start_position", "dir", "reduceOnly", "tif", "children",
                "signature", "nonce", "vaultAddress"}


def dec(x) -> Decimal | None:
    """An exact decimal from the feed's decimal strings (never floats). None when absent or malformed."""
    if isinstance(x, str) and DECIMAL_RE.match(x):
        try:
            return Decimal(x)
        except InvalidOperation:
            return None
    if isinstance(x, int) and not isinstance(x, bool):
        return Decimal(x)
    return None


def num(d: Decimal | None) -> float | None:
    """A JSON number for display. Totals are kept as exact decimals; this converts once at the edge."""
    return None if d is None else float(d)


def fmt(d: Decimal | None) -> str | None:
    """An exact decimal string for display tables that keep the feed's precision."""
    return None if d is None else format(d.normalize(), "f")


def fixture_violations(line: dict) -> list[str]:
    """Field names in one feed line (or a recording line's msg) that protocol v2 doesn't list."""
    out: list[str] = [f"never:{k}" for k in _keys(line) if k in NEVER_FIELDS]
    if "control" in line:
        allowed = CONTROL_FIELDS.get(line.get("control"))
        if allowed is None:
            return [f"control:{line.get('control')}"]
        out += [f"control.{k}" for k in line if k not in allowed]
        return out + control_value_violations(line)
    out += [f"envelope.{k}" for k in line if k not in ENVELOPE_FIELDS]
    kind = line.get("kind")
    body = line.get("body") if isinstance(line.get("body"), dict) else {}
    allowed = BODY_FIELDS.get(kind)
    if allowed is None:
        return out + [f"kind:{kind}"]
    out += [f"{kind}.{k}" for k in body if k not in allowed]
    if kind in ("book_snapshot", "trig_snapshot") and isinstance(body.get("coverage"), dict):
        out += [f"{kind}.coverage.{k}" for k in body["coverage"] if k not in COVERAGE_FIELDS]
    for key in ("excluded",):
        if isinstance(body.get(key), dict):
            out += [f"{kind}.excluded.{k}" for k in body[key] if k not in EXCLUDED_FIELDS]
    # Snapshot, depth and bands rows are positional: their column names (the coverage's or body's `fields`) must be
    # listed, and every row must have exactly that many columns.
    tables = {"book_snapshot": (("orders",), ORDER_ROW_FIELDS), "trig_snapshot": (("triggers",), TRIGGER_ROW_FIELDS),
              "depth": (("bids", "asks"), DEPTH_ROW_FIELDS), "bands": (("rows",), BAND_ROW_FIELDS)}
    if kind in tables:
        keys, allowed_cols = tables[kind]
        cols = (body.get("coverage") or {}).get("fields") if kind.endswith("_snapshot") else body.get("fields")
        cols = cols if isinstance(cols, list) else []
        out += [f"{kind}.fields.{c}" for c in cols if c not in allowed_cols]
        for key in keys:
            for r in body.get(key) or []:
                if not isinstance(r, list) or len(r) != len(cols):
                    out.append(f"{kind}.{key}: a row that isn't {len(cols)} columns")
                    break
    if kind == "depth" and isinstance(body.get("available"), dict):
        out += [f"depth.available.{k}" for k in body["available"] if k not in DEPTH_AVAILABLE_FIELDS]
    if kind == "bands" and isinstance(body.get("outside"), dict):
        out += [f"bands.outside.{k}" for k in body["outside"] if k not in BAND_OUTSIDE_FIELDS]
        for where in BAND_OUTSIDE_FIELDS:
            for r in body["outside"].get(where) or []:
                if not isinstance(r, list) or len(r) != len(BAND_OUTSIDE_ROW_FIELDS):
                    out.append(f"bands.outside.{where}: a row that isn't {len(BAND_OUTSIDE_ROW_FIELDS)} columns")
                    break
    if kind == "book_delta":
        for c in body.get("changes") or []:
            out += [f"book_delta.change.{k}" for k in c if k not in BOOK_CHANGE_FIELDS]
    if kind == "trig_delta":
        for c in body.get("changes") or []:
            out += [f"trig_delta.change.{k}" for k in c if k not in TRIG_CHANGE_FIELDS]
    if kind == "execs":
        for e in body.get("execs") or []:
            out += [f"execs.exec.{k}" for k in e if k not in EXEC_FIELDS]
            if isinstance(e.get("liquidation"), dict):
                out += [f"execs.liquidation.{k}" for k in e["liquidation"] if k not in LIQUIDATION_FIELDS]
            for p in e.get("participants") or []:
                out += [f"execs.participant.{k}" for k in p if k not in PARTICIPANT_FIELDS]
                if isinstance(p.get("effect"), dict):
                    out += [f"execs.effect.{k}" for k in p["effect"] if k not in EFFECT_FIELDS]
    return out


def _uint_or_str(v) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool) and v >= 0) or isinstance(v, str)


def _map(where: str, v, fields, value_ok=_uint_or_str) -> list[str]:
    """A nested map: only listed keys (any key when fields is None), each value of its one shape."""
    if not isinstance(v, dict):
        return [f"{where}: not a map"]
    out = []
    for k, x in v.items():
        if fields is not None and k not in fields:
            out.append(f"{where}.{k}: not listed")
        elif not value_ok(x):
            out.append(f"{where}.{k}: wrong shape")
    return out


def limits_violations(where: str, v) -> list[str]:
    if not isinstance(v, dict):
        return [f"{where}: not a map"]
    out = _map(where, {k: x for k, x in v.items() if k != "bands_width"}, LIMIT_FIELDS - {"bands_width"})
    if "bands_width" in v:
        out += _map(f"{where}.bands_width", v["bands_width"], None, lambda x: isinstance(x, str))
    return out


def control_value_violations(line: dict) -> list[str]:
    """Each control field's value in its one shape: an unsigned integer or a string, except the maps (markets,
    limits, limits.bands_width, labels, last_sent, replay), the lists of one scalar shape (unknown_markets,
    supported), and the replay status's speed (a number) and partial_history (a boolean). A nested object under
    a scalar field, or an unlisted key in a nested map, is a violation."""
    out = []
    for k, v in line.items():
        where = f"control.{line.get('control')}.{k}"
        if k == "markets":
            if not isinstance(v, dict):
                out.append(f"{where}: not a map")
                continue
            for mk, mv in v.items():
                out += _map(f"{where}.{mk}", mv, HELLO_MARKET_FIELDS)
        elif k == "limits":
            out += limits_violations(where, v)
        elif k == "labels":
            out += _map(where, v, LABEL_FIELDS, lambda x: isinstance(x, str))
        elif k == "replay":
            out += replay_status_violations(where, v)
        elif k == "last_sent":
            out += _map(where, v, None)
        elif k in ("unknown_markets", "supported"):
            if not isinstance(v, list) or len({type(x) for x in v}) > 1 or not all(_uint_or_str(x) for x in v):
                out.append(f"{where}: not a list of one scalar shape")
        elif not _uint_or_str(v):
            out.append(f"{where}: wrong shape")
    return out


def replay_status_violations(where: str, v) -> list[str]:
    def ok(k, x):
        if k == "speed":
            return isinstance(x, (int, float)) and not isinstance(x, bool)
        if k == "partial_history":
            return isinstance(x, bool)
        return _uint_or_str(x)
    if not isinstance(v, dict):
        return [f"{where}: not a map"]
    return [f"{where}.{k}: " + ("not listed" if k not in REPLAY_STATUS_FIELDS else "wrong shape")
            for k, x in v.items() if k not in REPLAY_STATUS_FIELDS or not ok(k, x)]


def _keys(o):
    """Every key at any depth of a JSON value."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _keys(v)
