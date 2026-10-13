"""
Setup-instance accounting for SHADOW research trades: one shadow trade per
unique setup instance. SIMULATION ONLY - this never affects real (portfolio)
trades, strategy decisions, scoring or risk.

A setup instance is identified by its setup type and its structural anchor
(strategy.entry_logic.setup_anchor): the support/resistance zone, the daily
consolidation box, or "pullback to MA20". An instance is CONSUMED when its
shadow entry fills. Later evaluations of the same type whose anchor overlaps
a consumed instance are the SAME setup and are suppressed - whether the
earlier shadow is still open, stopped out or at target.

A new shadow trade for that symbol / setup type needs a deterministic RESET:
  NEW_STRUCTURE  the anchor no longer overlaps any consumed instance: a new
                 resistance zone broken, a different support zone, a new
                 consolidation box (new swing structure).
  CONDITION_GONE the setup type was absent at EVERY decision time of at least
                 one complete session after the instance was consumed (the
                 prior setup condition disappeared, then formed again).
Elapsed time alone never resets an unchanged setup.
"""

from collections import defaultdict
from dataclasses import dataclass

NEW_STRUCTURE, CONDITION_GONE = "NEW_STRUCTURE", "CONDITION_GONE"


def anchors_match(a, b):
    """Same structure? Unknown anchors match everything (conservative)."""
    if a is None or b is None:
        return True
    if a[0] != b[0]:
        return False
    if a[1] is None or a[2] is None or b[1] is None or b[2] is None:
        return True                      # MA20 pullback: no fixed level
    return a[1] <= b[2] and b[1] <= a[2]   # price ranges overlap


def describe(anchor):
    if anchor is None:
        return "UNKNOWN"
    kind, low, high = anchor
    if low is None or high is None:
        return kind
    return f"{kind} {low:.2f}-{high:.2f}"


@dataclass
class _Instance:
    instance_id: str
    setup_type: str
    anchor: tuple | None
    consumed_on: object               # session date of the entry fill


class SetupInstanceTracker:
    """Per symbol, chronological. Call observe() at every decision time
    (after bars up to that time were processed), consume() when a shadow
    entry fills, and is_consumed() before placing a new shadow order."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.consumed = defaultdict(list)      # setup type -> [_Instance]
        self._serial = defaultdict(int)
        self._day = None
        self._present_today = set()
        self.stats = {"instances": 0, "suppressed_signals": 0,
                      "resets_new_structure": 0, "resets_condition_gone": 0}

    def observe(self, session_date, present_types):
        if self._day is not None and session_date != self._day:
            self._close_session(self._day)
        if session_date != self._day:
            self._day, self._present_today = session_date, set()
        self._present_today |= set(present_types)

    def _close_session(self, day):
        """A complete session in which a consumed type never appeared
        releases every instance of that type consumed before it."""
        for stype, insts in self.consumed.items():
            if stype in self._present_today:
                continue
            keep = [i for i in insts if i.consumed_on >= day]
            if len(keep) != len(insts):
                self.stats["resets_condition_gone"] += 1
                self.consumed[stype] = keep

    def is_consumed(self, signal):
        return any(anchors_match(i.anchor, signal.setup_anchor)
                   for i in self.consumed.get(signal.setup_type, ()))

    def suppress(self):
        self.stats["suppressed_signals"] += 1

    def consume(self, signal, session_date):
        stype = signal.setup_type
        if self.consumed.get(stype):
            self.stats["resets_new_structure"] += 1   # earlier instance still held
        self._serial[stype] += 1
        inst = _Instance(f"{self.symbol}:{stype}#{self._serial[stype]} "
                         f"[{describe(signal.setup_anchor)}]",
                         stype, signal.setup_anchor, session_date)
        self.consumed[stype].append(inst)
        self.stats["instances"] += 1
        return inst.instance_id
