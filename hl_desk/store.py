"""Published snapshots: an in-memory ring keyed by snapshot ID, held by the state owner.

Each retained snapshot keeps, beside it, the exact execution set it was built
from (an immutable tuple of trades per market). A wallet drill-down on a
retained snapshot therefore always matches that snapshot's panel, even after
late trades arrive or old trades leave the live store. Nothing is written to
disk: other processes read the owner's snapshots through the owner service
(hl_desk.owner), so no process can mistake an old copy for current data.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

KEEP = 180
NODE_KEEP = 60      # node views (the largest part of a snapshot) are kept in the newest 60 snapshots only


class SnapshotStore:
    def __init__(self, data_dir=None, keep: int = KEEP) -> None:
        self.data_dir = data_dir
        self.keep = keep
        self._lock = threading.Lock()
        self._ring: OrderedDict[str, dict] = OrderedDict()
        self._frozen: dict[str, dict] = {}
        self._latest: dict | None = None

    def publish(self, snap: dict, frozen: dict | None = None) -> None:
        with self._lock:
            sid = snap["snapshot_id"]
            self._ring[sid] = snap
            if frozen is not None:
                self._frozen[sid] = frozen
            while len(self._ring) > self.keep:
                old, _ = self._ring.popitem(last=False)
                self._frozen.pop(old, None)
            if len(self._ring) > NODE_KEEP:
                # The snapshot that just left the newest NODE_KEEP: replace it with a copy whose node views are a
                # labelled stub. The published object itself is never edited (a reader may be serializing it).
                sid_old = list(self._ring.keys())[-(NODE_KEEP + 1)]
                self._ring[sid_old] = _without_node_views(self._ring[sid_old])
            self._latest = snap

    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def get(self, snapshot_id: str | None) -> dict | None:
        with self._lock:
            if not snapshot_id:
                return self._latest
            return self._ring.get(snapshot_id)

    def frozen(self, snapshot_id: str) -> dict | None:
        """{market: (segment_start_ms, tuple_of_trades)} for a retained snapshot, or None."""
        with self._lock:
            return self._frozen.get(snapshot_id)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._ring.keys())


def _without_node_views(snap: dict) -> dict:
    if snap.get("node_views_trimmed"):
        return snap
    stub = {"available": False, "retained": False,
            "reason": f"node views are kept only in the newest {NODE_KEEP} snapshots (about a minute); "
                      "the public-data panels of this snapshot are unchanged"}
    markets = {}
    for m, ms in (snap.get("markets") or {}).items():
        node = ms.get("node")
        if isinstance(node, dict):
            node = {k: v for k, v in node.items() if k not in ("live", "replay")} | {"live": stub, "replay": stub}
        markets[m] = dict(ms) | {"node": node}
    return dict(snap) | {"markets": markets, "node_views_trimmed": True}

