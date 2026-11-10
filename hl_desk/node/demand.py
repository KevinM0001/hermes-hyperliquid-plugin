"""What node data the plugin needs from a hosted relay right now (handoff 1d step 7; Kevin, Nov 8).

Node data flows only while it is being used:

- while a node panel is on screen (the dashboard tab is visible: it polls the snapshot once a second with
  `node=visible` and the market it has open), the relay subscription is the open market in full plus the
  executions of every market, so the alerts cover all of them;
- 60 seconds after the last panel was seen (the tab hidden or closed), the subscription ends; the connection
  closes unless the background alerts setting keeps the executions of every market (no full market);
- coming back starts again from a fresh snapshot (the relay client forgets the old state when it goes idle);
- a tool call that needs a market the panel subscription doesn't carry in full opens its own short subscription
  for that market and releases it once it has answered (manager.lease / release).

The tab polls only while it is visible, so a poll is the signal; a panel counts as on screen for VISIBLE_GRACE_S
after the last one.
"""

from __future__ import annotations

import threading
import time

LINGER_S = 60.0
VISIBLE_GRACE_S = 5.0


class Demand:
    def __init__(self, markets: list[str], alerts_in_background: bool = False, linger_s: float = LINGER_S,
                 grace_s: float = VISIBLE_GRACE_S) -> None:
        self.markets = list(markets)
        self.alerts = bool(alerts_in_background)
        self.linger_s = float(linger_s)
        self.grace_s = float(grace_s)
        self.open_market: str | None = None
        self.last_panel: float | None = None     # monotonic seconds of the last poll from a visible tab
        self._lock = threading.Lock()

    def panel(self, market: str | None, now: float | None = None) -> None:
        """A node panel is on screen with this market open (any thread)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if market in self.markets:
                self.open_market = market
            self.last_panel = now

    def want(self, now: float):
        """(full market or None, [markets whose executions are wanted]) or None when nothing wants node data."""
        with self._lock:
            seen = self.last_panel is not None and now - self.last_panel <= self.grace_s + self.linger_s
            full = self.open_market if seen else None
            execs = list(self.markets) if (seen or self.alerts) else []
        if full is None and not execs:
            return None
        return (full, execs)

    def status(self, now: float) -> dict:
        with self._lock:
            last, open_market = self.last_panel, self.open_market
        on_screen = last is not None and now - last <= self.grace_s
        lingering = last is not None and not on_screen and now - last <= self.grace_s + self.linger_s
        want = self.want(now)
        return {"panel_on_screen": on_screen, "open_market": open_market,
                "seconds_since_panel": None if last is None else round(now - last, 1),
                "unsubscribes_in_s": (round(self.grace_s + self.linger_s - (now - last), 1) if lingering else None),
                "linger_s": self.linger_s, "alerts_in_background": self.alerts,
                "subscription": None if want is None else {"full": want[0], "execs": want[1]}}
