"""The clock relationship used to judge source recency (schema freshness.clock_relationship).

A source timestamp is recent only if local_now - source_time, widened by the
local clock's error bound, is within the field's source-age limit. The bound
comes from the operating system when it reports one (Linux adjtimex: the
kernel's maximum error while NTP-synchronized), or from the plugin setting
clock_error_bound_ms. With no known bound, nothing source-timestamped can be
fresh: it is unverified. Hyperliquid's own source timestamps are taken as
accurate; that is an assumption, stated in the schema.
"""

from __future__ import annotations

import ctypes
import sys
import time

TIME_ERROR = 5
STA_UNSYNC = 0x0040
_CACHE: dict = {"at": 0.0, "value": None, "basis": None}


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                ("time", _Timeval), ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long),
                ("shift", ctypes.c_int), ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long),
                ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long),
                ("tai", ctypes.c_int), ("_pad", ctypes.c_int * 11)]


def _libc():
    """The C library already loaded into this process (dlopen of the program itself). ctypes.util.find_library
    isn't used, because it can launch an external command (ldconfig) to search for the library."""
    try:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.adjtimex           # resolves the symbol, or raises AttributeError
        return lib
    except (OSError, AttributeError):
        return ctypes.CDLL("libc.so.6", use_errno=True)


def os_bound() -> tuple[float | None, str]:
    """(bound in seconds or None, basis). Read-only query of the kernel clock state (modes=0)."""
    if not sys.platform.startswith("linux"):
        return None, f"the OS ({sys.platform}) doesn't report a clock error bound to this plugin"
    now = time.monotonic()
    if now - _CACHE["at"] < 10.0 and _CACHE["basis"] is not None:
        return _CACHE["value"], _CACHE["basis"]
    try:
        libc = _libc()
        tx = _Timex()
        tx.modes = 0
        state = libc.adjtimex(ctypes.byref(tx))
        if state < 0 or state == TIME_ERROR or tx.status & STA_UNSYNC:
            value, basis = None, "the system clock isn't synchronized (adjtimex), so its error bound is unknown"
        else:
            value, basis = tx.maxerror / 1e6, "kernel maximum clock error (adjtimex maxerror, NTP-synchronized)"
    except Exception as exc:
        value, basis = None, f"the clock error bound couldn't be read: {type(exc).__name__}"
    _CACHE.update(at=now, value=value, basis=basis)
    return value, basis


class ClockBound:
    """fixed_s: a configured bound (setting clock_error_bound_ms, or a test value); otherwise the OS bound."""

    def __init__(self, fixed_s: float | None = None) -> None:
        self.fixed_s = fixed_s

    def get(self) -> tuple[float | None, str]:
        if self.fixed_s is not None:
            return float(self.fixed_s), "configured clock_error_bound_ms"
        return os_bound()
