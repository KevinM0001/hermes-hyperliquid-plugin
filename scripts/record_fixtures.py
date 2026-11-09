"""Record public Hyperliquid data for the offline test fixtures.

Developer tool, not part of the plugin runtime. It only reads public data:
the info endpoint (perpDexs, meta, l2Book, clearinghouseState,
frontendOpenOrders) and the public websocket (l2Book, trades, bbo,
activeAssetCtx). It never touches the exchange endpoint.

It records what the collector itself uses: on the websocket, the fast
5-level book and the 20-level book at full precision, trades, bbo and
activeAssetCtx; over the info endpoint, l2Book with nSigFigs 4, 3 and 2 every
5 seconds. Each record stores the local receive time (wall clock ms and
monotonic ns) and a connection number. The script closes and reopens the
socket once, so the fixture holds a real reconnect.

Usage:
    python scripts/record_fixtures.py --out <dir> --seconds 75
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request
from pathlib import Path

import websockets

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
COINS = ["BTC", "HYPE", "xyz:SP500", "xyz:XYZ100"]
POLL_SIG_FIGS = [4, 3, 2]
POLL_EVERY_S = 5.0


def info(body: dict) -> object:
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def subscriptions(coins: list[str]) -> list[dict]:
    subs = []
    for coin in coins:
        subs.append({"type": "l2Book", "coin": coin, "fast": True})
        subs.append({"type": "l2Book", "coin": coin})
        subs.append({"type": "trades", "coin": coin})
        subs.append({"type": "bbo", "coin": coin})
        subs.append({"type": "activeAssetCtx", "coin": coin})
    return subs


def stamp() -> dict:
    return {"recv_wall_ms": int(time.time() * 1000), "recv_mono_ns": time.monotonic_ns()}


async def poll_books(out, conn: int, coins: list[str], stop: asyncio.Event) -> None:
    while not stop.is_set():
        for coin in coins:
            for sf in POLL_SIG_FIGS:
                body = {"type": "l2Book", "coin": coin, "nSigFigs": sf}
                try:
                    resp = await asyncio.to_thread(info, body)
                except Exception as exc:  # recorded so the fixture shows it
                    out.write(json.dumps({"http": "error", "conn": conn, "request": body,
                                          "error": repr(exc), **stamp()}) + "\n")
                    continue
                out.write(json.dumps({"http": "l2Book", "conn": conn, "request": body,
                                      "response": resp, **stamp()}) + "\n")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_EVERY_S)
        except asyncio.TimeoutError:
            pass


async def record_connection(out, conn: int, coins: list[str], seconds: float) -> None:
    stop = asyncio.Event()
    async with websockets.connect(WS_URL, max_size=2**23) as ws:
        out.write(json.dumps({"event": "connect", "conn": conn, **stamp()}) + "\n")
        for sub in subscriptions(coins):
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
        poller = asyncio.create_task(poll_books(out, conn, coins, stop))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            out.write(json.dumps({"conn": conn, **stamp(), "msg": json.loads(raw)}) + "\n")
        stop.set()
        await poller
    out.write(json.dumps({"event": "disconnect", "conn": conn, **stamp()}) + "\n")


async def record_ws(path: Path, coins: list[str], seconds: float, pause: float) -> None:
    with path.open("w") as out:
        await record_connection(out, 1, coins, seconds)
        await asyncio.sleep(pause)
        await record_connection(out, 2, coins, seconds)


def first_takers(ws_path: Path) -> dict[str, str]:
    """One public taker address per dex, for the wallet drill-down fixture."""
    found: dict[str, str] = {}
    for line in ws_path.read_text().splitlines():
        rec = json.loads(line)
        msg = rec.get("msg") or {}
        if msg.get("channel") != "trades":
            continue
        for t in msg["data"]:
            dex = t["coin"].split(":")[0] if ":" in t["coin"] else ""
            taker = t["users"][0] if t["side"] == "B" else t["users"][1]
            found.setdefault(dex, taker)
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=75.0)
    ap.add_argument("--pause", type=float, default=6.0)
    args = ap.parse_args()
    info_dir = args.out / "info"
    ws_dir = args.out / "ws"
    info_dir.mkdir(parents=True, exist_ok=True)
    ws_dir.mkdir(parents=True, exist_ok=True)

    fetched_ms = int(time.time() * 1000)
    (info_dir / "perpDexs.json").write_text(json.dumps(
        {"fetched_ms": fetched_ms, "request": {"type": "perpDexs"}, "response": info({"type": "perpDexs"})}))
    for dex in ["", "xyz"]:
        body = {"type": "meta", "dex": dex}
        (info_dir / f"meta_{dex or 'default'}.json").write_text(json.dumps(
            {"fetched_ms": fetched_ms, "request": body, "response": info(body)}))

    ws_path = ws_dir / "session.jsonl"
    asyncio.run(record_ws(ws_path, COINS, args.seconds, args.pause))

    for dex, user in first_takers(ws_path).items():
        for kind in ["clearinghouseState", "frontendOpenOrders"]:
            body = {"type": kind, "user": user, "dex": dex}
            (info_dir / f"{kind}_{dex or 'default'}.json").write_text(json.dumps(
                {"fetched_ms": int(time.time() * 1000), "request": body, "response": info(body)}))


if __name__ == "__main__":
    main()
