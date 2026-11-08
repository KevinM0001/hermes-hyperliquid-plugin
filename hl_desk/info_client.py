"""Read-only calls to Hyperliquid's info endpoint (POST https://api.hyperliquid.xyz/info).

Only these documented request types are used: perpDexs, meta, l2Book,
clearinghouseState, frontendOpenOrders (see docs/ENDPOINTS.md). There is no
exchange endpoint, no signing and no trading or wallet key anywhere in this plugin. Calls are
blocking (urllib) and are made only from the collector thread or a tool's
worker thread; dashboard routes wrap them in asyncio.to_thread.
"""

from __future__ import annotations

import json
import time
import urllib.request

ALLOWED_TYPES = {"perpDexs", "meta", "l2Book", "clearinghouseState", "frontendOpenOrders"}


def post_info(url: str, body: dict, timeout: float = 10.0) -> object:
    if body.get("type") not in ALLOWED_TYPES:
        raise ValueError(f"info request type {body.get('type')!r} is not one this plugin uses")
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_metadata(url: str, dexes_needed: set[str]) -> dict:
    out = {"fetched_ms": int(time.time() * 1000), "perpDexs": post_info(url, {"type": "perpDexs"}), "meta": {}}
    for dex in sorted(dexes_needed):
        out["meta"][dex] = post_info(url, {"type": "meta", "dex": dex})
    return out


def fetch_wallet_current(url: str, user: str, dex: str) -> dict:
    """Current positions and open orders for one address on one dex (default dex is '').

    Times (local wall clock, ms): requested_ms before the first request, state_received_ms and
    orders_received_ms as each answer arrives, and returned_ms when both are in hand. The account's age is
    judged at returned_ms, so network delay and the wait for the second answer count as age."""
    requested = int(time.time() * 1000)
    state = post_info(url, {"type": "clearinghouseState", "user": user, "dex": dex})
    state_received = int(time.time() * 1000)
    orders = post_info(url, {"type": "frontendOpenOrders", "user": user, "dex": dex})
    orders_received = int(time.time() * 1000)
    return {"requested_ms": requested, "state_received_ms": state_received, "orders_received_ms": orders_received,
            "returned_ms": int(time.time() * 1000), "clearinghouseState": state, "frontendOpenOrders": orders}
