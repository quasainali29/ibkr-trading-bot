"""
Persistent local storage for VALIDATED historical candles (CSV).

Layout:  market_data/cache/<SYMBOL>/<timeframe>[_ext].csv
         market_data/cache/<SYMBOL>/<timeframe>[_ext].meta.json

* Only bars that passed validation are ever written.
* Each row records whether the bar was COMPLETE when fetched. Incomplete
  (in-progress) bars are dropped on load so they are always re-downloaded.
* Writes are atomic (temp file + fsync + os.replace); the meta file holds a
  SHA-256 of the CSV plus the IBKR conId. A checksum/conId mismatch or any
  validation failure quarantines the file and forces a fresh download.
"""

import csv
import hashlib
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data.models import Bar
from market_data.sessions import bar_is_complete  # noqa: F401 (re-export)
from market_data.validation import validate_bars
from money import MoneyError, to_decimal

log = logging.getLogger(__name__)

SCHEMA = 1
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "wap",
           "bar_count", "complete"]


class CandleStoreError(Exception):
    pass


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_bars(old, new):
    """Union by timestamp; bars in `new` replace overlapping bars in `old`."""
    by_ts = {b.timestamp: b for b in old}
    by_ts.update({b.timestamp: b for b in new})
    return [by_ts[k] for k in sorted(by_ts)]


class CandleStore:
    def __init__(self, root, cfg):
        self.root = Path(root)
        self.cfg = cfg

    def paths(self, symbol, timeframe, session):
        base = self.root / symbol.upper() / f"{timeframe.key}{session.file_suffix}"
        return base.with_suffix(".csv"), base.with_suffix(".meta.json")

    # ------------------------------------------------------------------
    def save(self, symbol, con_id, timeframe, session, bars, calendar, fetched_at):
        """Validate and atomically persist bars (trimmed to the cache cap)."""
        bars = list(bars)[-timeframe.max_cached_bars:]
        result = validate_bars(bars, fetched_at, self.cfg)
        if not result.ok:
            raise CandleStoreError("refusing to store invalid bars: "
                                   + "; ".join(result.errors))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(COLUMNS)
        for b in bars:
            w.writerow([
                b.timestamp.astimezone(timezone.utc).isoformat(),
                str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume),
                "" if b.wap is None else str(b.wap),
                "" if b.bar_count is None else b.bar_count,
                1 if bar_is_complete(b, timeframe, calendar, fetched_at) else 0,
            ])
        text = buf.getvalue()
        meta = {
            "schema": SCHEMA, "symbol": symbol.upper(), "con_id": con_id,
            "timeframe": timeframe.key, "session": session.value,
            "rows": len(bars), "updated_at": fetched_at.isoformat(),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        csv_path, meta_path = self.paths(symbol, timeframe, session)
        atomic_write_text(csv_path, text)
        atomic_write_text(meta_path, json.dumps(meta, indent=2))
        log.debug("Stored %d %s %s bars", len(bars), symbol, timeframe.key)

    def load(self, symbol, con_id, timeframe, session, now):
        """Return validated COMPLETE cached bars, or [] if none/unusable."""
        csv_path, meta_path = self.paths(symbol, timeframe, session)
        if not csv_path.exists():
            return []
        try:
            return self._load(csv_path, meta_path, con_id, timeframe, session, now)
        except CandleStoreError as exc:
            log.warning("Discarding cached %s %s candles: %s", symbol, timeframe.key, exc)
            self._quarantine(csv_path, meta_path)
            return []

    def _load(self, csv_path, meta_path, con_id, timeframe, session, now):
        try:
            text = csv_path.read_text(encoding="utf-8")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CandleStoreError(f"unreadable cache: {exc}") from None
        if meta.get("schema") != SCHEMA:
            raise CandleStoreError("unsupported cache schema")
        if meta.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise CandleStoreError("checksum mismatch")
        if meta.get("con_id") != con_id:
            raise CandleStoreError(f"cached conId {meta.get('con_id')} != {con_id}")
        if meta.get("timeframe") != timeframe.key or meta.get("session") != session.value:
            raise CandleStoreError("cache metadata does not match request")

        bars = []
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != COLUMNS:
            raise CandleStoreError("unexpected CSV columns")
        for row in reader:
            if row["complete"] != "1":
                continue
            try:
                bars.append(Bar(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=to_decimal(row["open"]), high=to_decimal(row["high"]),
                    low=to_decimal(row["low"]), close=to_decimal(row["close"]),
                    volume=to_decimal(row["volume"]),
                    wap=to_decimal(row["wap"]) if row["wap"] else None,
                    bar_count=int(row["bar_count"]) if row["bar_count"] else None,
                ))
            except (MoneyError, ValueError) as exc:
                raise CandleStoreError(f"malformed row: {exc}") from None
        result = validate_bars(bars, now, self.cfg) if bars else None
        if result is not None and not result.ok:
            raise CandleStoreError("; ".join(result.errors))
        return bars

    @staticmethod
    def _quarantine(*paths):
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        for p in paths:
            if p.exists():
                try:
                    p.replace(p.with_name(f"{p.name}.corrupt-{stamp}"))
                except OSError:
                    log.exception("Could not quarantine %s", p)
