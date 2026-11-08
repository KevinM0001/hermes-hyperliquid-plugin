"""The real collector against a local stand-in for Hyperliquid (127.0.0.1 only, network off).

A local websocket server replays the recorded BTC messages of each recorded
connection and drops the first connection; a local HTTP server answers the
info calls from the recorded responses. Checks: explicit start, live
freshness, the visible disconnected state with backoff, the recorded gap,
the reconnect, and shutdown.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time

from websockets.asyncio.server import serve

from hl_desk.config import DeskConfig
from hl_desk.runtime import Runtime

from helpers import INFO, recorded_records

COIN = "BTC"


def _per_conn_messages():
    msgs, http = {1: [], 2: []}, {}
    for r in recorded_records():
        m = r.get("msg")
        if m:
            d = m.get("data")
            coin = d[0]["coin"] if isinstance(d, list) and d else (d or {}).get("coin")
            if coin == COIN and m.get("channel") in ("l2Book", "trades", "bbo", "activeAssetCtx"):
                msgs[r["conn"]].append(m)
        elif r.get("http") == "l2Book" and r["request"]["coin"] == COIN:
            http.setdefault(r["request"]["nSigFigs"], r["response"])
    return msgs, http


class LocalHyperliquid:
    def __init__(self):
        self.msgs, self.books = _per_conn_messages()
        self.connections = 0
        self.subscriptions = []
        self.ws_port = None
        self.loop = None
        self.http = None
        self.last_t = 0

    def stamp(self, m):
        """Restamp a recorded message with the current time as its source time (strictly increasing), as a live
        feed would carry, so source-recency checks see a current feed."""
        now = max(self.last_t + 1, int(time.time() * 1000))
        self.last_t = now
        m = json.loads(json.dumps(m))
        if m.get("channel") == "trades":
            for t in m["data"]:
                t["time"] = now
        elif m.get("channel") in ("l2Book", "bbo"):
            m["data"]["time"] = now
        return m

    def start(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            self.loop.run_until_complete(self._serve(ready))

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        info = {"perpDexs": json.loads((INFO / "perpDexs.json").read_text())["response"],
                "meta": json.loads((INFO / "meta_default.json").read_text())["response"]}
        books = self.books
        hl = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                t = body.get("type")
                resp = info.get(t) if t in info else books.get(body.get("nSigFigs")) if t == "l2Book" else None
                if t == "l2Book" and resp is not None:
                    resp = dict(resp, time=max(hl.last_t, int(time.time() * 1000)))
                data = json.dumps(resp).encode()
                self.send_response(200 if resp is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    async def _serve(self, ready):
        self.stop_ev = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.ws_port = server.sockets[0].getsockname()[1]
            ready.set()
            await self.stop_ev.wait()

    async def _handler(self, ws):
        self.connections += 1
        conn = min(self.connections, 2)
        deadline = time.monotonic() + 2
        while len(self.subscriptions) < 5 * self.connections and time.monotonic() < deadline:
            try:
                self.subscriptions.append(json.loads(await asyncio.wait_for(ws.recv(), 0.5)))
            except asyncio.TimeoutError:
                break
        for m in self.msgs[conn]:
            await ws.send(json.dumps(self.stamp(m)))
            await asyncio.sleep(0.002)
        if conn == 1:
            await asyncio.sleep(0.5)
            await ws.close()           # the server drops the first connection
            return
        await self.stop_ev.wait()

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.stop_ev.set)
        if self.http:
            self.http.shutdown()


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return None


def test_collector_end_to_end_with_reconnect(tmp_path):
    hl = LocalHyperliquid()
    hl.start()
    cfg = DeskConfig(markets=[COIN], ws_url=f"ws://127.0.0.1:{hl.ws_port}",
                     info_url=f"http://127.0.0.1:{hl.http.server_address[1]}/info", depth_poll_s=0.3)
    rt = Runtime(cfg_factory=lambda: cfg, data_dir_factory=lambda: tmp_path)
    states = []
    try:
        assert rt.start()["action"]["message"] == "started"

        def snap_where(pred):
            def check():
                s, _ = rt.snapshot(None)
                states.append((s or {}).get("collector", {}).get("state"))
                return s if s and pred(s) else None
            return check

        s = _wait(snap_where(lambda s: s["collector"]["state"] == "connected"
                             and s["markets"][COIN]["freshness"]["book_fast"]["status"] == "fresh"))
        assert s, states[-5:]
        assert s["mode"] == "LIVE" and s["markets"][COIN]["book"]["top"]["best_bid"]["value"] is not None
        assert {"method": "subscribe", "subscription": {"type": "l2Book", "coin": COIN, "fast": True}} in hl.subscriptions
        # The server drops the connection: the state is visibly disconnected, with a backoff retry.
        d = _wait(snap_where(lambda s: s["collector"]["state"] == "disconnected"))
        assert d, states[-5:]
        assert d["collector"]["last_error"] and 0.8 <= d["collector"]["next_retry_s"] <= 1.2
        assert all(f["status"] == "disconnected" for f in d["markets"][COIN]["freshness"].values())
        assert d["markets"][COIN]["book"]["top"]["best_bid"]["value"] is None
        # It reconnects by itself, and the gap is recorded.
        r = _wait(snap_where(lambda s: s["collector"]["state"] == "connected" and s["collector"]["conn"] == 2
                             and s["markets"][COIN]["provenance"]["coverage"]["trades"]["gaps"]))
        assert r, states[-5:]
        assert r["collector"]["reconnects"] == 1 and hl.connections == 2
        gap = r["markets"][COIN]["provenance"]["coverage"]["trades"]["gaps"][0]
        assert gap["start_ms"] is not None and "ConnectionClosed" in gap["reason"]
        # Another process of this profile reads the same snapshot from the owner over its socket.
        from hl_desk import owner
        sock = owner.sock_path(tmp_path)
        assert sock.exists() and (sock.stat().st_mode & 0o777) == 0o600
        remote = owner.request(sock, {"op": "snapshot", "snapshot_id": r["snapshot_id"]})
        assert remote["ok"] and remote["snapshot"]["snapshot_id"] == r["snapshot_id"]
        assert not (tmp_path / "latest_snapshot.json").exists()
    finally:
        res = rt.stop()
        hl.stop()
    assert res["action"]["message"] == "stopped"
    final, err = rt.snapshot(None)
    assert final is None and "isn't running" in err          # nothing is served as current once stopped
    from hl_desk import owner
    assert not owner.sock_path(tmp_path).exists()
