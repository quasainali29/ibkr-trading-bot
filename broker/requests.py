"""
Request-ID registry.

Allocates IBKR request IDs and tracks pending requests so replies and
request-scoped errors are routed to the code that issued the request.
"""

import itertools
import threading
from dataclasses import dataclass, field


@dataclass
class PendingRequest:
    req_id: int
    kind: str                     # e.g. "account_summary", "contract", "quote"
    label: str                    # human readable, e.g. "AAPL 5m bars"
    scope: str = "SYSTEM"         # "SYSTEM" (account) or "SYMBOL" (market data)
    done: threading.Event = field(default_factory=threading.Event)
    error: tuple | None = None    # (code, text)
    items: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def fail(self, code, text):
        if self.error is None:
            self.error = (code, text)
        self.done.set()


class RequestRegistry:
    def __init__(self, start=1000):
        self._ids = itertools.count(start)
        self._pending = {}
        self._lock = threading.Lock()

    def register(self, kind, label, scope="SYSTEM"):
        with self._lock:
            req = PendingRequest(next(self._ids), kind, label, scope)
            self._pending[req.req_id] = req
            return req

    def get(self, req_id):
        with self._lock:
            return self._pending.get(req_id)

    def remove(self, req_id):
        with self._lock:
            return self._pending.pop(req_id, None)

    def fail_all(self, code, text):
        """Fail every pending request (e.g. on connection loss)."""
        with self._lock:
            pending = list(self._pending.values())
        for req in pending:
            req.fail(code, text)
