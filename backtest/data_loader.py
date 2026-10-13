"""
Historical data for backtests.

* HistoryStore: backtest/data/<SYMBOL>/<tf>.csv (+ .meta.json with SHA-256,
  conId and the covered date range) and contract.json. Only bars that pass
  Phase 2 validate_bars() are stored; writes are atomic.
* HistoryDownloader: pages backwards through IBKR history with explicit end
  dates using the SAME read-only request path, parser and pacing guard as
  live market data. Resumable: only missing ranges are requested. After the
  first download, backtests run offline.
"""

import csv
import hashlib
import io
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from market_data.candle_store import atomic_write_text, merge_bars
from market_data.models import Bar, ContractInfo, TradingSession, get_timeframe
from market_data.validation import validate_bars
from money import to_decimal

log = logging.getLogger(__name__)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "wap", "bar_count"]
SCHEMA = 1
DURATION_DAYS = {"D": 1, "W": 7, "M": 30, "Y": 365}


class HistoryError(Exception):
    pass


def duration_days(duration):
    n, unit = duration.split()
    return int(n) * DURATION_DAYS[unit]


def read_universe(path):
    symbols = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if s and s not in symbols:
            symbols.append(s)
    if not symbols:
        raise HistoryError(f"universe file {path} contains no symbols")
    return symbols


class HistoryStore:
    def __init__(self, root, cfg):
        self.root = Path(root)
        self.cfg = cfg

    def _paths(self, symbol, tf):
        base = self.root / symbol.upper() / tf
        return base.with_suffix(".csv"), base.with_suffix(".meta.json")

    # --- contract metadata ----------------------------------------------
    def save_contract(self, info):
        d = asdict(info)
        d["min_tick"] = None if info.min_tick is None else str(info.min_tick)
        atomic_write_text(self.root / info.symbol / "contract.json", json.dumps(d, indent=2))

    def load_contract(self, symbol):
        p = self.root / symbol.upper() / "contract.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        d["min_tick"] = Decimal(d["min_tick"]) if d.get("min_tick") else None
        return ContractInfo(**d)

    # --- bars -------------------------------------------------------------
    def meta(self, symbol, tf):
        _, mp = self._paths(symbol, tf)
        return json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else None

    def coverage(self, symbol, tf):
        m = self.meta(symbol, tf)
        if not m:
            return None
        return date.fromisoformat(m["covered_start"]), date.fromisoformat(m["covered_end"])

    def checksum(self, symbol, tf):
        m = self.meta(symbol, tf)
        return m["sha256"] if m else None

    def save(self, symbol, con_id, tf, bars, covered_start, covered_end):
        bars = list(bars)
        if bars:
            res = validate_bars(bars, datetime.now(timezone.utc), self.cfg)
            if not res.ok:
                raise HistoryError(f"{symbol} {tf}: refusing to store invalid bars: "
                                   + "; ".join(res.errors))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(COLUMNS)
        for b in bars:
            w.writerow([b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume,
                        "" if b.wap is None else b.wap,
                        "" if b.bar_count is None else b.bar_count])
        text = buf.getvalue()
        csv_path, meta_path = self._paths(symbol, tf)
        atomic_write_text(csv_path, text)
        atomic_write_text(meta_path, json.dumps({
            "schema": SCHEMA, "symbol": symbol.upper(), "con_id": con_id, "timeframe": tf,
            "rows": len(bars), "covered_start": covered_start.isoformat(),
            "covered_end": covered_end.isoformat(),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}, indent=2))

    def load(self, symbol, tf, start=None, end=None):
        """Bars for [start, end] (dates, inclusive). Verifies the checksum."""
        csv_path, _ = self._paths(symbol, tf)
        m = self.meta(symbol, tf)
        if m is None or not csv_path.exists():
            return []
        text = csv_path.read_text(encoding="utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != m["sha256"]:
            raise HistoryError(f"{symbol} {tf}: checksum mismatch - delete and re-download")
        lo = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) if start else None
        hi = (datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
              if end else None)
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            ts = datetime.fromisoformat(row["timestamp"])
            if (lo and ts < lo) or (hi and ts >= hi):
                continue
            out.append(Bar(ts, to_decimal(row["open"]), to_decimal(row["high"]),
                           to_decimal(row["low"]), to_decimal(row["close"]),
                           to_decimal(row["volume"]),
                           to_decimal(row["wap"]) if row["wap"] else None,
                           int(row["bar_count"]) if row["bar_count"] else None))
        return out

    def missing_ranges(self, symbol, tf, start, end):
        cov = self.coverage(symbol, tf)
        if cov is None:
            return [(start, end)]
        out = []
        if start < cov[0]:
            out.append((start, cov[0]))
        if end > cov[1]:
            out.append((cov[1], end))
        return out


class HistoryDownloader:
    """Read-only chunked downloader on top of MarketDataService."""

    def __init__(self, market_data_service, store, cfg, progress=print):
        self.md = market_data_service
        self.store = store
        self.cfg = cfg
        self.progress = progress

    def ensure(self, symbol, timeframes, start, end):
        """Make sure [start - warmup, end] is stored for each timeframe."""
        from market_data.market_data import MarketDataUnavailable
        from market_data.sessions import TradingCalendar
        cc = self.md.resolve_contract(symbol)
        if not cc.ok:
            raise HistoryError(f"{symbol}: contract rejected: {'; '.join(cc.reasons)}")
        info = cc.contract
        self.store.save_contract(info)
        calendar = TradingCalendar.from_contract(info)
        for tf_key in timeframes:
            tf = get_timeframe(tf_key)
            want_start = start - timedelta(days=self.cfg.BACKTEST_WARMUP_DAYS[tf_key])
            for a, b in self.store.missing_ranges(symbol, tf_key, want_start, end):
                self._download_range(info, tf, calendar, a, b, MarketDataUnavailable)

    def _download_range(self, info, tf, calendar, start, end, Unavailable):
        existing = self.store.load(info.symbol, tf.key)
        cov = self.store.coverage(info.symbol, tf.key)
        cov_start = min(cov[0], start) if cov else start
        cov_end = max(cov[1], end) if cov else end
        cursor = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        floor = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        chunks = list(self.cfg.BACKTEST_DOWNLOAD_CHUNKS[tf.key])
        while cursor > floor:
            got, used = None, None
            for duration in chunks:
                try:
                    got = self.md._fetch_bars(info, tf, TradingSession.RTH, duration, calendar,
                                              end_datetime=cursor.strftime("%Y%m%d-%H:%M:%S"))
                    used = duration
                    break
                except Unavailable as exc:
                    text = str(exc).lower()
                    if "no data" in text or "returned no data" in text:
                        got, used = [], duration
                        break
                    if duration == chunks[-1]:
                        raise HistoryError(f"{info.symbol} {tf.key}: {exc}") from None
                    log.info("%s %s: %s failed (%s); trying smaller chunk",
                             info.symbol, tf.key, duration, exc)
                    chunks = chunks[chunks.index(duration) + 1:]
            valid = [b for b in got if b.timestamp is not None and b.timestamp < cursor]
            res = validate_bars(valid, datetime.now(timezone.utc), self.cfg) if valid else None
            if res is not None and not res.ok:
                raise HistoryError(f"{info.symbol} {tf.key}: invalid bars from IBKR: "
                                   + "; ".join(res.errors[:3]))
            existing = merge_bars(existing, valid)
            new_cursor = (min(b.timestamp for b in valid) if valid
                          else cursor - timedelta(days=duration_days(used)))
            self.progress(f"  {info.symbol} {tf.key}: {len(valid)} bars before "
                          f"{cursor:%Y-%m-%d} ({used})")
            cursor = min(new_cursor, cursor - timedelta(minutes=1))
            # Persist bars after every chunk. Coverage is only extended when the
            # whole range is done (or, with no prior coverage, contiguously from
            # `end` backwards), so an interrupted download never claims a gap.
            if cov is None:
                self.store.save(info.symbol, info.con_id, tf.key, existing,
                                min(max(cursor.date() + timedelta(days=1), start), end), end)
            else:
                self.store.save(info.symbol, info.con_id, tf.key, existing, cov[0], cov[1])
        self.store.save(info.symbol, info.con_id, tf.key, existing, cov_start, cov_end)
