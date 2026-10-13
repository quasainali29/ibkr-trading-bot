"""
Append-only event journal (JSON Lines).

Records startup checks, SAFE_MODE transitions, reconciliation results and
risk decisions. Future phases will also record orders and fills here.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


class TradeJournal:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, event, **payload):
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": event, **payload}
        line = json.dumps(entry, default=_default, sort_keys=True)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError:
            # The journal must never crash the safety loop; the log still has it.
            log.exception("Failed to write journal event %s", event)
