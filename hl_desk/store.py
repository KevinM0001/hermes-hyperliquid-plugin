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
