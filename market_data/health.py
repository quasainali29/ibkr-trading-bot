"""
Market-data health: separates per-ticker problems from system-level ones.

* Per-ticker failures (unknown symbol, no subscription for that symbol,
  rejected contract, malformed bars) only make that ticker unusable.
* SYSTEM-level failures (timeouts, pacing/HMDS service errors, connection
  loss) from several DISTINCT symbols within a window, with no successful
  request since, indicate a widespread outage -> global SAFE_MODE.
"""

import threading
import time
from collections import deque

R_MD_WIDESPREAD = "MARKET_DATA_WIDESPREAD_FAILURE"

# IBKR codes that indicate a service problem rather than a symbol problem.
# 10197 = competing live session (the live username is logged in elsewhere,
# so the paper account receives no market data for ANY symbol).
SYSTEM_LEVEL_CODES = frozenset({-1, 162, 420, 504, 1100, 2110, 10197})


def diagnose(messages):
    """Plain-language hints for well-known IBKR market-data failures."""
    text = " ".join(messages).lower()
    hints = []
    if "competing live session" in text or "different ip address" in text:
        hints.append(
            "Your LIVE IBKR username is logged in elsewhere (IBKR Mobile, Client "
            "Portal, or another TWS/Gateway). Paper accounts share the live "
            "account's market data and receive none while that session is active. "
            "Log out of the other session, then retry.")
    # 2188 also says "requires additional subscription" but has its own hint.
    general = text.replace("up-to-the-second historical data requires additional subscription", "")
    if "not subscribed" in general or "requires additional subscription" in general:
        hints.append(
            "No market data subscription for this instrument. Request delayed "
            "data (--data-type delayed) or add a subscription in IBKR Account "
            "Management (and share it with the paper account).")
    if "up-to-the-second" in text or "ibkr 2188" in text:
        hints.append(
            "IBKR returns no bars from the CURRENT session without a live market-data "
            "subscription (error 2188). Intraday candles stop at the previous session, "
            "so they are stale while the market is open; daily analysis on completed "
            "sessions is unaffected.")
    if "pacing" in text:
        hints.append("IBKR historical-data pacing limit reached; wait a few "
                     "minutes or use cached candles.")
    return hints


class MarketDataHealth:
    def __init__(self, state, cfg, clock=time.monotonic):
        self.state = state
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.Lock()
        self._failures = deque()        # (t, symbol)
        self.last_success = None
        self.last_failure = None

    def record_success(self, symbol):
        with self._lock:
            self.last_success = (self.clock(), symbol)
            self._failures.clear()
        self.state.clear(R_MD_WIDESPREAD)

    def record_failure(self, symbol, message, system_level):
        with self._lock:
            now = self.clock()
            self.last_failure = (now, symbol, message)
            if not system_level:
                return
            self._failures.append((now, symbol))
            cutoff = now - self.cfg.MD_WIDESPREAD_WINDOW_SECONDS
            while self._failures and self._failures[0][0] < cutoff:
                self._failures.popleft()
            symbols = {s for _, s in self._failures}
            widespread = len(symbols) >= self.cfg.MD_WIDESPREAD_MIN_SYMBOLS
        if widespread:
            self.state.enter_safe_mode(
                R_MD_WIDESPREAD,
                f"system-level market-data failures for {len(symbols)} symbols "
                f"within {self.cfg.MD_WIDESPREAD_WINDOW_SECONDS}s")

    @staticmethod
    def is_system_level(code):
        return code is None or code in SYSTEM_LEVEL_CODES
