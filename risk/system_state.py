"""
System safety state: INITIALIZING -> SAFE / SAFE_MODE.

The controller holds a set of keyed SAFE_MODE reasons. The system is SAFE
only when a full verification cycle has completed AND no reasons remain.
Any component may add a reason at any time (e.g. the IBKR reader thread on
connection loss); reasons are cleared only by the verification cycle that
re-checks the underlying condition.
"""

import logging
import threading
from enum import Enum

log = logging.getLogger(__name__)


class SystemState(str, Enum):
    INITIALIZING = "INITIALIZING"
    SAFE = "SAFE"
    SAFE_MODE = "SAFE_MODE"


class StateController:
    def __init__(self, on_change=None):
        self._lock = threading.RLock()
        self._reasons = {}          # code -> human readable message
        self._verified = False      # a full verification cycle has completed
        self._state = SystemState.INITIALIZING
        self._on_change = on_change

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def reasons(self):
        with self._lock:
            return dict(self._reasons)

    def trading_permitted(self):
        """True only when state is SAFE. Phase 1 never trades regardless."""
        return self.state is SystemState.SAFE

    def enter_safe_mode(self, code, message):
        with self._lock:
            is_new = self._reasons.get(code) != message
            self._reasons[code] = message
            if is_new:
                log.warning("SAFE_MODE reason [%s]: %s", code, message)
            self._recompute()

    def clear(self, code):
        with self._lock:
            if self._reasons.pop(code, None) is not None:
                log.info("SAFE_MODE reason cleared [%s]", code)
            self._recompute()

    def set_check(self, code, ok, message=""):
        """Record the result of one verification check."""
        if ok:
            self.clear(code)
        else:
            self.enter_safe_mode(code, message or code)

    def mark_verification_complete(self):
        with self._lock:
            self._verified = True
            self._recompute()

    def invalidate_verification(self):
        """Require a fresh full cycle before SAFE can be reached again."""
        with self._lock:
            self._verified = False
            self._recompute()

    def _recompute(self):
        if self._reasons:
            new = SystemState.SAFE_MODE
        elif self._verified:
            new = SystemState.SAFE
        else:
            new = (SystemState.INITIALIZING
                   if self._state is SystemState.INITIALIZING
                   else SystemState.SAFE_MODE)
        if new is not self._state:
            old, self._state = self._state, new
            log.info("System state %s -> %s", old.value, new.value)
            if self._on_change:
                try:
                    self._on_change(old, new, dict(self._reasons))
                except Exception:  # never let a listener break safety state
                    log.exception("State change listener failed")
