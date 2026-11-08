"""Market metadata from the official info endpoint (perpDexs and meta per dex).

A market is supported only if it appears, not delisted, in the universe of
its dex's meta response. HIP-3 markets are named '{dex}:{coin}' (docs,
asset-ids page). Anything else is unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import dex_of


@dataclass(frozen=True)
class MarketMeta:
    name: str
    dex: str
    sz_decimals: int
    max_leverage: int | None
    index_in_meta: int

    def as_dict(self) -> dict:
        return {"name": self.name, "dex": self.dex, "dex_label": self.dex or "(default perp dex)",
                "sz_decimals": self.sz_decimals, "max_leverage": self.max_leverage,
                "index_in_meta": self.index_in_meta}


class MarketRegistry:
    def __init__(self) -> None:
        self.dexes: list[str] = []
        self.markets: dict[str, MarketMeta] = {}
        self.fetched_ms: int | None = None

    def load_perp_dexs(self, response: list) -> None:
        # The docs' example starts with null for the first (default) perp dex.
        self.dexes = ["" if d is None else str(d.get("name", "")) for d in response]

    def load_meta(self, dex: str, response: dict) -> None:
        for i, u in enumerate(response.get("universe", [])):
            name = u.get("name")
            if not name or u.get("isDelisted"):
                continue
            if dex and not name.startswith(dex + ":"):
                continue
            self.markets[name] = MarketMeta(
                name=name, dex=dex, sz_decimals=int(u.get("szDecimals", 0)),
                max_leverage=u.get("maxLeverage"), index_in_meta=i)

    def check(self, market: str) -> dict:
        """The metadata check: is this market supported, and why or why not."""
        dex = dex_of(market)
        if self.dexes and dex not in self.dexes:
            return {"market": market, "supported": False,
                    "reason": f"dex '{dex}' is not in perpDexs"}
        meta = self.markets.get(market)
        if meta is None:
            return {"market": market, "supported": False,
                    "reason": f"'{market}' is not in the meta universe of dex '{dex or '(default)'}' (unknown or delisted)"}
        return {"market": market, "supported": True, "meta": meta.as_dict()}
