"""Desk configuration: which markets to follow and the per-market thresholds.

Defaults come from the versioned schema. Plugin settings
(plugins.entries.hyperliquid-desk.settings in Hermes config.yaml) may
override the market list and the per-market alert thresholds; the schema's
formulas never change at runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import schema

# The followed markets by default (Kevin, Nov 10: ETH and SOL added, the two most traded perps after BTC); any
# perp the venue lists can be followed through the markets setting.
DEFAULT_MARKETS = ["BTC", "ETH", "SOL", "HYPE", "xyz:SP500", "xyz:XYZ100"]
WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class DeskConfig:
    markets: list[str] = field(default_factory=lambda: list(DEFAULT_MARKETS))
    burst_min_usd: dict[str, float] = field(default_factory=dict)
    large_print_usd: dict[str, float] = field(default_factory=dict)
    wallet_target_share_pct: float = 50.0
    wallet_cap: int = 50
    snapshot_interval_s: float = 1.0
    depth_poll_s: float = 5.0
    clock_bound_s: float | None = None     # configured local clock error bound; None = ask the OS (hl_desk.clock)
    ws_url: str = WS_URL
    info_url: str = INFO_URL
    node_feed: str = ""                    # a Unix socket path or a ws(s):// relay URL; "" = public mode only
    node_replay_file: str = ""             # a recording, played only when a replay is started explicitly
    # Node mode: a manual burst threshold per market (USD per 60 s); a market not listed (or listed as "auto") uses the
    # automatic threshold from the dated burst calibration (hl_desk/node/calibration).
    node_burst_min_usd: dict[str, float] = field(default_factory=dict)
    liq_burst_count: int = 2
    liq_burst_usd: float = 1_000_000.0

    def burst_threshold(self, market: str) -> float:
        if market in self.burst_min_usd:
            return float(self.burst_min_usd[market])
        return float(schema.by_market(
            schema.load()["alerts"]["burst"]["threshold"]["min_notional_usd_by_market"], market))

    def large_print_threshold(self, market: str) -> float:
        if market in self.large_print_usd:
            return float(self.large_print_usd[market])
        return float(schema.by_market(
            schema.load()["metrics"]["flow.large_print"]["threshold"]["large_print_usd_by_market"], market))

    def trades_max_age_s(self, market: str) -> float:
        return float(schema.by_market(schema.freshness_field("trades")["max_age_s_by_market"], market))

    def ctx_max_age_s(self, market: str) -> float:
        return float(schema.by_market(schema.freshness_field("activeAssetCtx")["max_age_s_by_market"], market))

    @staticmethod
    def book_max_age_s(kind: str) -> float:
        return float(schema.freshness_field("l2Book")["subscriptions"][kind]["max_age_s"])

    @staticmethod
    def book_source_max_age_s(kind: str) -> float:
        return float(schema.freshness_field("l2Book")["subscriptions"][kind]["source_max_age_s"])

    def trades_source_max_age_s(self, market: str) -> float:
        return float(schema.by_market(schema.freshness_field("trades")["source_max_age_s_by_market"], market))

    @staticmethod
    def stall_reconnect_s() -> float:
        return float(schema.freshness_field("l2Book")["stall_reconnect_s"])


def from_settings(get) -> DeskConfig:
    """Build a config from a settings getter (ctx.get_config or a dict's get)."""
    cfg = DeskConfig()
    markets = get("markets", None)
    if isinstance(markets, list) and markets:
        cfg.markets = [str(m) for m in markets]
    for key in ("burst_min_usd", "large_print_usd"):
        v = get(key, None)
        if isinstance(v, dict):
            setattr(cfg, key, {str(k): float(x) for k, x in v.items()})
    cb = get("clock_error_bound_ms", None)
    if isinstance(cb, (int, float)) and not isinstance(cb, bool) and cb > 0:
        cfg.clock_bound_s = float(cb) / 1000.0
    t = get("wallet_target_share_pct", None)
    if isinstance(t, (int, float)) and 0 < t <= 100:
        cfg.wallet_target_share_pct = float(t)
    for key in ("node_feed", "node_replay_file"):
        v = get(key, None)
        if isinstance(v, str):
            setattr(cfg, key, v.strip())
    nb = get("node_burst_min_usd", None)
    if isinstance(nb, dict):
        # A finite positive number (or numeric text) is a manual threshold; "auto", a missing market or anything
        # else leaves that market on its automatic threshold.
        manual = {}
        for k, x in nb.items():
            if isinstance(x, str):
                try:
                    x = float(x)
                except ValueError:
                    continue
            if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0:
                manual[str(k)] = float(x)
        cfg.node_burst_min_usd = manual
    n = get("liq_burst_count", None)
    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
        cfg.liq_burst_count = n
    u = get("liq_burst_usd", None)
    if isinstance(u, (int, float)) and not isinstance(u, bool) and u > 0:
        cfg.liq_burst_usd = float(u)
    return cfg


def node_key() -> str | None:
    """The optional node feed access key, read only through Hermes's secret scope (never from a file or
    the environment directly). None outside Hermes or when unset. Hermes's own error (for example no secret
    scope bound on a multiplexing gateway) is raised to the caller, which reports the key as unreadable."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return None
    v = get_secret("HL_DESK_NODE_KEY")
    return v or None
