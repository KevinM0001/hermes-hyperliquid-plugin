"""The state owner: one runtime per Hermes profile, holding the collector and its snapshots.

Mechanisms used, all from the Hermes plugin docs (developer-guide/plugins):
- 'Thread-safe lazy singletons': the runtime lives in a SingletonSlot
  (plugins.plugin_utils), built on first use; building it starts nothing.
- 'Register slash commands': /hl start, /hl stop and /hl restart are the
  explicit start, shutdown and restart (the dashboard's buttons call the
  same functions).
- 'Background threads' (developer-guide/gateway-internals.md) and the threading contract in
  memory-provider-plugin.md: the worker thread is made with agent.memory_provider.spawn_context_thread,
  so it runs under the starting profile's context.
- 'Store durable state': plugin_data_dir('hyperliquid-desk') holds the
  owner lock and, while the collector runs, the owner service's socket,
  under the active profile's HERMES_HOME. Snapshots stay in memory.

One owner per profile: start() takes an exclusive flock on
<plugin-data>/collector.lock, so a second process (or a reloaded copy of
this module) can't start a second collector for the same profile. The
owner also answers the profile's other Hermes processes (a dashboard
process, a plugin host) through hl_desk.owner on <plugin-data>/owner.sock:
snapshots by ID, frozen wallet execution sets, status, stop and restart.
When no owner answers, nothing is running, and nothing old is shown as
current.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import PLUGIN_NAME
from .collector import THREAD_NAME, Collector
from .config import DeskConfig
from .store import SnapshotStore
from . import owner
from .util import iso_ms

try:  # Hermes-provided helpers (documented); absent only when running the tests outside Hermes.
    from plugins.plugin_utils import SingletonSlot
except Exception:  # pragma: no cover - exercised only outside Hermes
    class SingletonSlot:  # minimal stand-in with the same get/reset contract
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._value = None

        def get(self, factory):
            if self._value is None:
                with self._lock:
                    if self._value is None:
                        self._value = factory()
            return self._value

        def reset(self) -> None:
            with self._lock:
                self._value = None


def default_data_dir() -> Path:
    """<HERMES_HOME>/plugin-data/hyperliquid-desk via the documented helper (creates it on first use).

    Inside Hermes that profile directory is always used. HL_DESK_DATA_DIR is honored only where Hermes's plugin
    storage helper can't be imported, that is outside Hermes (the tests); it can never move a profile's state."""
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        plugin_data_dir = None
    if plugin_data_dir is not None:
        return plugin_data_dir(PLUGIN_NAME)
    override = os.environ.get("HL_DESK_DATA_DIR")
    if not override:
        raise RuntimeError("Hermes's plugin storage isn't available here (not running inside Hermes), "
                           "and HL_DESK_DATA_DIR isn't set")
    p = Path(override)
    p.mkdir(parents=True, exist_ok=True)
    return p


class Runtime:
    def __init__(self, cfg_factory=DeskConfig, data_dir_factory=default_data_dir) -> None:
        self._cfg_factory = cfg_factory
        self._data_dir_factory = data_dir_factory
        self._lock = threading.Lock()
        self.collector: Collector | None = None
        self.store: SnapshotStore | None = None
        self._lock_fh = None
        self.last_action: dict | None = None

    # --- ownership -----------------------------------------------------------
    def _acquire(self, data_dir: Path) -> str | None:
        try:
            import fcntl   # POSIX only; the catalog entry declares linux and macos
        except ImportError:
            return "nobody: file locking isn't available on this platform, so the collector can't guarantee one owner"
        fh = open(data_dir / "collector.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip()
            fh.close()
            return holder or "another process"
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since {iso_ms(int(time.time() * 1000))}\n")
        fh.flush()
        self._lock_fh = fh
        return None

    def _release(self) -> None:
        if self._lock_fh is not None:
            import fcntl
            try:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_fh.close()
                self._lock_fh = None

    @staticmethod
    def _any_collector_thread() -> bool:
        return any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())

    def _orphan_thread(self, data_dir: Path | None = None):
        """A collector thread for THIS profile (same data directory) started by another copy of
        this module in the same process. A multi-profile gateway can run one collector per
        profile; a collector of another profile is never adopted."""
        if not self._any_collector_thread():
            return None
        try:
            want = str(data_dir if data_dir is not None else self._data_dir_factory())
        except Exception:
            return None
        for t in threading.enumerate():
            if (t.name == THREAD_NAME and t.is_alive() and hasattr(t, "collector")
                    and getattr(t, "hl_desk_data_dir", None) == want):
                return t
        return None

    # --- explicit control (blocking: call from a worker thread, never the event loop) ---
    def _own(self) -> bool:
        self._adopt()
        return self.collector is not None and self.collector.alive()

    def _ask(self, req: dict) -> dict | None:
        """Ask this profile's owner process; None when no owner is listening."""
        try:
            data_dir = self._data_dir_factory()
        except Exception:
            return None
        return owner.request(owner.sock_path(data_dir), req)

    def start(self) -> dict:
        with self._lock:
            if self._own():
                return self._result("already running")
            data_dir = self._data_dir_factory()
            orphan = self._orphan_thread(data_dir)
            if orphan is not None:
                self.collector = orphan.collector
                self.store = orphan.collector.store
                return self._result("adopted this profile's collector already running in this process")
            pong = owner.request(owner.sock_path(data_dir), {"op": "ping"})
            if pong and pong.get("ok"):
                return self._result(f"already running in process {pong.get('pid')} (this profile's owner)")
            holder = self._acquire(data_dir)
            if holder is not None:
                return self._result(f"not started: the collector for this profile is owned by {holder}", ok=False)
            cfg = self._cfg_factory()
            self.store = SnapshotStore(data_dir)
            self.collector = Collector(cfg, self.store, owner_api=self._serve)
            self.collector.start()
            return self._result("started")

    def stop(self) -> dict:
        with self._lock:
            if not self._own():
                remote = self._ask({"op": "stop"})
                if remote is None:
                    return self._result("not running")
                return self._result(remote.get("message") or remote.get("error") or "stop sent to the owner",
                                    ok=bool(remote.get("ok")))
            stopped = self.collector.stop()
            self._release()
            return self._result("stopped" if stopped else "stop requested; the worker is still closing", ok=stopped)

    def replay(self, cmd: str, arg=None) -> dict:
        """Control the explicit node replay (start [path], pause, resume, seek <ms>, speed <x>, restart, stop) in the
        process that owns the collector. A replay is never started automatically."""
        if not self._own():
            remote = self._ask({"op": "replay", "cmd": cmd, "arg": arg})
            if remote is None:
                return {"ok": False, "error": "the collector isn't running for this profile (start it with /hl start)"}
            return remote
        return self._local_replay(cmd, arg)

    def _local_replay(self, cmd, arg) -> dict:
        c = self.collector
        if c is None or not c.alive():
            return {"ok": False, "error": "the collector isn't running for this profile (start it with /hl start)"}
        try:
            return c.node.call(lambda: c.node.replay_command(str(cmd), arg))
        except Exception as exc:
            return {"ok": False, "error": f"replay {cmd} failed: {type(exc).__name__}: {exc}"}

    def restart(self) -> dict:
        if not self._own():
            remote = self._ask({"op": "restart"})
            if remote is not None:
                return self._result(remote.get("message") or remote.get("error") or "restart sent to the owner",
                                    ok=bool(remote.get("ok")))
        self.stop()
        return self.start()

    def _result(self, message: str, ok: bool = True) -> dict:
        self.last_action = {"message": message, "ok": ok, "at": iso_ms(int(time.time() * 1000))}
        return self.status() | {"action": self.last_action}

    # --- the owner service: answers other processes of this profile (runs on the collector's loop) ---
    def _serve(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "status":
            return {"ok": True, "status": self._local_status()}
        if op == "snapshot":
            snap, err = self._local_snapshot(req.get("snapshot_id"))
            return {"ok": snap is not None, "snapshot": snap, "error": err}
        if op == "wallet_trades":
            return {"ok": True, "result": self._local_wallet_trades(
                req.get("snapshot_id"), req.get("market"), req.get("window_min"), req.get("address"))}
        if op == "replay":
            return self._local_replay(req.get("cmd"), req.get("arg"))
        if op in ("stop", "restart"):
            _spawn(getattr(self, op), f"{THREAD_NAME}-{op}")
            return {"ok": True, "message": f"{op} requested from another process; the owner (pid {os.getpid()}) is doing it"}
        return {"ok": False, "error": f"unknown request {op!r}"}

    # --- reads -----------------------------------------------------------------
    def _adopt(self) -> None:
        """Share a collector another copy of this module started in this process."""
        if self.collector is None:
            orphan = self._orphan_thread()
            if orphan is not None:
                self.collector = orphan.collector
                self.store = orphan.collector.store

    def _local_status(self) -> dict:
        c = self.collector
        running = c is not None and c.alive()
        return {"owner": running, "collector": (c.desk.collector if c else {"state": "stopped"}), "pid": os.getpid()}

    def status(self) -> dict:
        if self._own():
            return self._local_status()
        remote = self._ask({"op": "status"})
        if remote and remote.get("ok"):
            st = remote["status"]
            return {"owner": False, "owner_pid": st.get("pid"), "collector": st.get("collector"), "pid": os.getpid()}
        return {"owner": False, "collector": {"state": "stopped"}, "pid": os.getpid()}

    def _local_snapshot(self, snapshot_id: str | None) -> tuple[dict | None, str | None]:
        if self.store is None:
            return None, "no snapshot yet: the collector isn't running (start it with /hl start)"
        now_ms = int(time.time() * 1000)
        if snapshot_id:
            snap = self.store.get(snapshot_id)
            if snap is None:
                return None, f"snapshot {snapshot_id} is no longer retained (the last {self.store.keep} are kept)"
            return _served(snap, now_ms, latest=False), None
        snap = self.store.latest()
        if snap is None:
            return None, "no snapshot yet: the collector is starting"
        state = (snap.get("collector") or {}).get("state")
        if state == "stopped":
            return None, "the collector is stopped (start it with /hl start)"
        age = (now_ms - snap["built_at_ms"]) / 1000
        if age > LATEST_MAX_AGE_S:
            return None, (f"the latest snapshot is {age:.1f} s old: the collector isn't publishing, "
                          "so it isn't shown as current")
        return _served(snap, now_ms, latest=True), None

    def snapshot(self, snapshot_id: str | None = None) -> tuple[dict | None, str | None]:
        """Return (snapshot, error): from memory in the owner process, otherwise from the owner."""
        if self._own():
            return self._local_snapshot(snapshot_id)
        remote = self._ask({"op": "snapshot", "snapshot_id": snapshot_id})
        if remote is None:
            return None, "no snapshot: the collector isn't running for this profile (start it with /hl start)"
        if remote.get("ok"):
            return remote["snapshot"], None
        return None, remote.get("error") or "the owner returned no snapshot"

    def _local_wallet_trades(self, snapshot_id, market, window_min, address) -> dict:
        from .tools import drilldown
        snap = self.store.get(snapshot_id) if self.store is not None and snapshot_id else None
        if snap is None:
            return {"error": f"snapshot {snapshot_id} is no longer retained"}
        return drilldown(snap, self.store.frozen(snapshot_id), market, window_min, address)

    def wallet_trades(self, snapshot_id: str, market: str, window_min: int, address: str) -> dict:
        """A wallet's executions from exactly the execution set the snapshot's panel used."""
        if self._own():
            return self._local_wallet_trades(snapshot_id, market, window_min, address)
        remote = self._ask({"op": "wallet_trades", "snapshot_id": snapshot_id, "market": market,
                            "window_min": window_min, "address": address})
        if remote is None:
            return {"error": "the collector isn't running for this profile, so the execution set is gone"}
        return remote.get("result") or {"error": remote.get("error") or "the owner returned nothing"}

    def clock_bound(self) -> tuple:
        """(bound in seconds or None, basis): the same clock relationship the collector uses."""
        from .clock import ClockBound
        try:
            fixed = self._cfg_factory().clock_bound_s
        except Exception:
            fixed = None
        return ClockBound(fixed).get()

    def fetch_wallet_current(self, user: str, dex: str) -> dict:
        """Blocking info calls (clearinghouseState, frontendOpenOrders): call from a worker thread."""
        from . import info_client
        from .config import INFO_URL
        return info_client.fetch_wallet_current(INFO_URL, user, dex)


LATEST_MAX_AGE_S = 5.0


def _served(snap: dict, now_ms: int, latest: bool) -> dict:
    """A shallow copy labeled with how old it is when served, so a historical snapshot is never taken as current."""
    age = round((now_ms - snap["built_at_ms"]) / 1000, 3)
    out = dict(snap)
    out["served"] = {"latest": latest, "age_s": age, "served_at": iso_ms(now_ms),
                     "note": ("the latest snapshot" if latest else
                              "a retained historical snapshot: its freshness statuses are as of its build time, not now")}
    return out


def _spawn(fn, name: str) -> None:
    try:
        from agent.memory_provider import spawn_context_thread
        t = spawn_context_thread(fn, name=name, daemon=True)
    except ImportError:
        t = threading.Thread(target=fn, name=name, daemon=True)
    t.start()


_slot: SingletonSlot = SingletonSlot()


def get_runtime() -> Runtime:
    """The profile's runtime. Building it opens no socket, starts no thread and writes nothing."""
    return _slot.get(lambda: Runtime(cfg_factory=_cfg_factory))


_settings_getter = None


def set_settings_getter(getter) -> None:
    """register(ctx) passes ctx.get_config so start() reads the user's plugin settings."""
    global _settings_getter
    _settings_getter = getter


def _cfg_factory() -> DeskConfig:
    from .config import from_settings
    if _settings_getter is None:
        return DeskConfig()
    return from_settings(_settings_getter)
