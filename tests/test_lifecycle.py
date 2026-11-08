"""Lifecycle: importing or registering the plugin starts nothing; start, stop and restart are explicit;
one state owner per profile (data directory)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from hl_desk.collector import THREAD_NAME
from hl_desk.config import DeskConfig
from hl_desk.runtime import Runtime

from helpers import use_standalone_storage

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = dict(info_url="http://127.0.0.1:9/info", ws_url="ws://127.0.0.1:9/ws")


class FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.skills = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description="", **kw):
        self.commands[name] = handler

    def register_skill(self, name, path, **kw):
        self.skills[name] = Path(path)

    def get_config(self, key, default=None):
        return default


def load_plugin_like_hermes(module_name="hermes_plugins_test.hyperliquid_desk"):
    """Import the plugin directory the way Hermes does: a package from <root>/__init__.py."""
    parent = module_name.rsplit(".", 1)[0]
    if parent not in sys.modules:
        pkg = types.ModuleType(parent)
        pkg.__path__ = []
        sys.modules[parent] = pkg
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def collector_threads():
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


def test_import_and_register_start_nothing(monkeypatch, tmp_path):
    attempts = []
    real = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: attempts.append(addr) or real(self, addr))
    use_standalone_storage(monkeypatch, tmp_path / "data")
    before = set(threading.enumerate())
    mod = load_plugin_like_hermes()
    ctx = FakeCtx()
    mod.register(ctx)
    assert set(ctx.tools) == {"hl_market", "hl_flow", "hl_context", "hl_status", "hl_alerts", "hl_wallets",
                              "node_status", "node_book", "node_stops"}
    assert set(ctx.commands) == {"hl"}
    assert set(ctx.skills) == {"hyperliquid-commentary"}
    assert set(threading.enumerate()) == before            # no thread started
    assert attempts == []                                  # no socket opened
    assert not (tmp_path / "data").exists()                # nothing written
    # A tool call without a running collector reports it; it never starts one. Its only connection
    # attempt is a local Unix-socket request to this profile's owner service, which isn't there.
    out = json.loads(ctx.tools["hl_status"][2]({}))
    assert out["ok"] is False and "/hl start" in out["error"]
    assert not collector_threads()
    from hl_desk import owner
    expected = str(owner.sock_path(tmp_path / "data"))      # owner.sock, or the short fallback for a long path
    assert attempts and all(a == expected for a in attempts)


def _manifest_tools():
    lines = (ROOT / "plugin.yaml").read_text().splitlines()
    out, on = [], False
    for ln in lines:
        if ln.startswith("provides_tools:"):
            on = True
            continue
        if on:
            if ln.startswith("  - "):
                out.append(ln[4:].strip())
            else:
                break
    return out


def test_the_manifest_is_valid_yaml():
    """Hermes discovery skips a manifest it can't parse. With a YAML parser it parses; without one (the test
    environments have none), no unquoted value holds what a plain YAML scalar can't (": " or " #")."""
    text = (ROOT / "plugin.yaml").read_text()
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        assert isinstance(yaml.safe_load(text), dict)
    for n, ln in enumerate(text.splitlines(), 1):
        m = re.match(r"^\s*(?:- )?[A-Za-z_][\w-]*: (.+)$", ln)
        if m and m.group(1)[0] not in "\"'[{|>":
            assert ": " not in m.group(1) and " #" not in m.group(1), f"plugin.yaml line {n}: {ln.strip()}"


def test_provides_tools_matches_registered():
    mod = load_plugin_like_hermes("hermes_plugins_test2.hyperliquid_desk")
    ctx = FakeCtx()
    mod.register(ctx)
    assert sorted(_manifest_tools()) == sorted(ctx.tools)


def _runtime(tmp_path):
    return Runtime(cfg_factory=lambda: DeskConfig(**OFFLINE), data_dir_factory=lambda: tmp_path)


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_explicit_start_stop_restart_and_visible_disconnected_state(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.status()["collector"]["state"] == "stopped"
    res = rt.start()
    assert res["action"]["message"] == "started"
    assert len(collector_threads()) == 1
    assert rt.start()["action"]["message"] == "already running"
    assert len(collector_threads()) == 1
    # Offline: the metadata request is refused, so the state is visibly disconnected with a retry.
    assert _wait(lambda: rt.status()["collector"].get("state") == "disconnected")
    st = rt.status()["collector"]
    assert "metadata" in st["last_error"] and st["next_retry_s"] >= 1
    snap, err = rt.snapshot(None)
    assert err is None and snap["collector"]["state"] == "disconnected"
    assert not (tmp_path / "latest_snapshot.json").exists()
    first = rt.collector
    res = rt.restart()
    assert res["action"]["message"] == "started" and rt.collector is not first
    assert not first.alive()
    assert len(collector_threads()) == 1
    res = rt.stop()
    assert res["action"]["message"] == "stopped" and not collector_threads()
    assert rt.stop()["action"]["message"] in ("stopped", "not running")


def test_one_state_owner_per_profile(tmp_path):
    rt = _runtime(tmp_path)
    rt.start()
    try:
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from hl_desk.runtime import Runtime\n"
            "from hl_desk.config import DeskConfig\n"
            "from pathlib import Path\n"
            "rt = Runtime(cfg_factory=lambda: DeskConfig(info_url='http://127.0.0.1:9/info', ws_url='ws://127.0.0.1:9/ws'),"
            " data_dir_factory=lambda: Path(%r))\n"
            "print(rt.start()['action']['message'])\n"
        ) % (str(ROOT), str(tmp_path))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        # Another process finds this profile's owner and doesn't start a second collector.
        assert f"already running in process {os.getpid()} (this profile's owner)" in out.stdout, out.stderr
        # In the same process, a second runtime (e.g. a reloaded module) adopts the running collector.
        rt2 = _runtime(tmp_path)
        assert rt2.start()["action"]["message"] in ("already running", "adopted this profile's collector already running in this process")
        assert rt2.collector is rt.collector and len(collector_threads()) == 1
    finally:
        rt.stop()
    assert not collector_threads()
