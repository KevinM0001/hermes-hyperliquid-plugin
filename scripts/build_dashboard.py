#!/usr/bin/env python3
"""Build dashboard/dist/index.js for the hyperliquid-desk Hermes plugin.

Standard library only, deterministic, no minification. The bundle is:

    dashboard/vendor/lightweight-charts/lightweight-charts.standalone.development.js  (verbatim)
    a separator comment
    dashboard/src/app.js                                                              (verbatim)

The Lightweight Charts file is checked against its pinned sha256 first (the readable standalone
build of lightweight-charts 5.2.1 from the npm tarball, see vendor/lightweight-charts/README.md),
and app.js is scanned for constructs the tab must never use (eval, dynamic import, script
injection, raw fetch, sockets other than the dashboard's own JSON-RPC socket via SDK.buildWsUrl, exchange endpoints).

Usage:
    python3 scripts/build_dashboard.py [PLUGIN_ROOT] [--check]

PLUGIN_ROOT is the directory that holds dashboard/ (default: found next to this script, or
one level up when the script lives in scripts/). --check builds in memory and exits 1 if
dashboard/dist/index.js isn't byte-identical to the result, without writing anything.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

LWC_REL = Path("dashboard/vendor/lightweight-charts/lightweight-charts.standalone.development.js")
APP_REL = Path("dashboard/src/app.js")
OUT_REL = Path("dashboard/dist/index.js")
LWC_VERSION = "5.2.1"
LWC_SHA256 = "d7b74180a63bde3d282e78f8b0bee5a648a85777c21dfa8a5090593a6db8d84a"
LWC_HEADER = b"/*!\n * @license\n * TradingView Lightweight Charts\xe2\x84\xa2 v" + LWC_VERSION.encode() + b"\n"

# Patterns the tab source must not contain (comments included, to keep the check simple).
FORBIDDEN = [
    (r"\beval\s*\(", "eval"),
    (r"\bnew\s+Function\b", "new Function"),
    (r"\bimport\s*\(", "dynamic import"),
    (r"^\s*import\s", "static import"),
    (r"createElement\(\s*['\"]script", "script tag injection"),
    (r"\.innerHTML\b|dangerouslySetInnerHTML", "raw HTML injection"),
    (r"(?<![\w.])fetch\s*\(", "raw fetch (use SDK.fetchJSON)"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
    (r"/exchange\b", "exchange endpoint"),
    (r"fonts\.googleapis|fonts\.gstatic|cdn\.|unpkg\.com|jsdelivr", "remote resource"),
]

SEPARATOR = (
    "\n"
    "/* ==========================================================================\n"
    " * hyperliquid-desk dashboard bundle, built by scripts/build_dashboard.py. Do not edit by hand.\n"
    " * Above: TradingView Lightweight Charts(TM) v{ver}, dist/lightweight-charts.standalone.development.js,\n"
    " *   verbatim (sha256 {sha}). Apache License 2.0; see dashboard/vendor/lightweight-charts/\n"
    " *   LICENSE, NOTICE and README.md. It defines window.LightweightCharts.\n"
    " * Below: the tab source, dashboard/src/app.js, verbatim.\n"
    " * ========================================================================== */\n"
    "\n"
)


def default_root() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent):
        if (cand / "dashboard").is_dir():
            return cand
    return here.parent


def build(root: Path) -> bytes:
    lwc_path, app_path = root / LWC_REL, root / APP_REL
    lwc = lwc_path.read_bytes()
    got = hashlib.sha256(lwc).hexdigest()
    if got != LWC_SHA256:
        raise SystemExit(f"error: {lwc_path} has sha256 {got}, expected {LWC_SHA256} (lightweight-charts {LWC_VERSION})")
    if not lwc.startswith(LWC_HEADER):
        raise SystemExit(f"error: {lwc_path} doesn't start with the /*! @license */ header of v{LWC_VERSION}")
    app = app_path.read_bytes()
    try:
        app_text = app.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"error: {app_path} isn't UTF-8: {exc}")
    problems = []
    for pattern, what in FORBIDDEN:
        for mo in re.finditer(pattern, app_text, flags=re.MULTILINE):
            line = app_text.count("\n", 0, mo.start()) + 1
            problems.append(f"{app_path}:{line}: {what}")
    # The one allowed socket: the dashboard's own JSON-RPC socket (/api/ws) with the URL built by the SDK, which is
    # how the plugin SDK says a plugin must open a socket (the token is never read by the tab). Written exactly as
    # `new WebSocket(wsUrl)` next to `SDK.buildWsUrl("/api/ws")`; any other use of the constructor is forbidden.
    ws_uses = len(re.findall(r"\bWebSocket\b", app_text))
    ws_allowed = app_text.count("new WebSocket(wsUrl)")
    if ws_uses != ws_allowed or (ws_allowed and 'SDK.buildWsUrl("/api/ws")' not in app_text):
        problems.append(f"{app_path}: WebSocket is allowed only as `new WebSocket(wsUrl)` with wsUrl from SDK.buildWsUrl(\"/api/ws\")")
    if problems:
        raise SystemExit("error: the tab source uses forbidden constructs:\n  " + "\n  ".join(problems))
    if 'REGISTRY.register(PLUGIN, App)' not in app_text or 'const PLUGIN = "hyperliquid-desk"' not in app_text:
        raise SystemExit(f"error: {app_path} doesn't register the hyperliquid-desk component")
    if any(b > 127 for b in app):
        raise SystemExit(f"error: {app_path} must be ASCII-only (write other characters as \\uXXXX escapes)")
    if b"\r" in app:
        raise SystemExit(f"error: {app_path} has CR line endings; use LF")
    if not lwc.endswith(b"\n"):
        lwc += b"\n"
    if not app.endswith(b"\n"):
        app += b"\n"
    sep = SEPARATOR.format(ver=LWC_VERSION, sha=LWC_SHA256).encode("utf-8")
    return lwc + sep + app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plugin_root", nargs="?", type=Path, default=None,
                    help="directory that contains dashboard/ (default: next to or above this script)")
    ap.add_argument("--check", action="store_true", help="verify dist/index.js is up to date; write nothing")
    args = ap.parse_args(argv)
    root = (args.plugin_root or default_root()).resolve()
    if not (root / "dashboard").is_dir():
        print(f"error: {root} has no dashboard/ directory", file=sys.stderr)
        return 2
    data = build(root)
    out = root / OUT_REL
    digest = hashlib.sha256(data).hexdigest()
    if args.check:
        current = out.read_bytes() if out.exists() else None
        if current != data:
            print(f"{OUT_REL} is out of date (expected sha256 {digest}); run scripts/build_dashboard.py", file=sys.stderr)
            return 1
        print(f"{OUT_REL} is up to date ({len(data)} bytes, sha256 {digest})")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.read_bytes() == data:
        print(f"{OUT_REL} unchanged ({len(data)} bytes, sha256 {digest})")
    else:
        out.write_bytes(data)
        print(f"wrote {OUT_REL} ({len(data)} bytes, sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
