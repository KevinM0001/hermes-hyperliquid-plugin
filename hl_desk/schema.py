"""The versioned metric schema (schema/metrics.v1.json), loaded once and read-only."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "metrics.v1.json"


@lru_cache(maxsize=1)
def load() -> dict:
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def version() -> str:
    return load()["version"]


def schema_ref() -> dict:
    s = load()
    return {"schema_id": s["schema_id"], "version": s["version"], "path": "schema/metrics.v1.json"}


def by_market(table: dict, market: str) -> float:
    return table.get(market, table["default"])


def regime_constants() -> dict:
    return load()["regimes"]["constants"]


def meaning(state: str) -> dict | None:
    return load()["regimes"]["meanings"].get(state)


def freshness_field(name: str) -> dict:
    return load()["freshness"]["fields"][name]
