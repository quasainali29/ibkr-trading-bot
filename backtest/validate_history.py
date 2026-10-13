"""
Independent validation of downloaded backtest history (read-only).

Checks, per symbol / timeframe, over the window a backtest will actually use:
  integrity     - checksum (via HistoryStore.load), Phase 2 validate_bars
                  (OHLC sanity, duplicates, ordering, future timestamps)
  timezone      - all timestamps tz-aware; intraday bars inside 09:30-16:00
                  America/New_York; daily bars stamped at local midnight
  completeness  - every expected NYSE session present (holidays excluded),
                  no unexpected sessions, full bar count per session
                  (early closes recognised); gaps are FAILURES, not warnings
  consistency   - 5m bars re-aggregated to 15m / 1h / 1d must match the
                  stored bars (open/high/low/close)
  corporate     - large overnight jumps flagged as possible unadjusted splits;
  actions         optional comparison with IBKR ADJUSTED_LAST (dividends)
Nothing here changes data; failures are reported, never patched.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from market_data.validation import validate_bars

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# NYSE full-day closures and early (13:00 ET) closes. Covers the pilot / Tier 1
# windows; extend before validating other years (unknown dates are FLAGGED).
NYSE_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}
NYSE_EARLY_CLOSES = {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
                     date(2026, 11, 27), date(2026, 12, 24)}
BARS_PER_SESSION = {"5m": (78, 42), "15m": (26, 14), "1h": (7, 4)}   # (full, early close)
TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


@dataclass
class CheckResult:
    symbol: str
    tf: str
    window: tuple
    bars: int = 0
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.failures


def expected_sessions(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in NYSE_HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def _local(ts):
    return ts.astimezone(ET)


def check_series(symbol, tf, bars, start, end):
    r = CheckResult(symbol, tf, (start, end), len(bars))
    if not bars:
        r.failures.append("no bars in window")
        return r
    v = validate_bars(bars, datetime.now(UTC), _cfg())
    if not v.ok:
        r.failures += [f"validate_bars: {e}" for e in v.errors]
    if any(b.timestamp.tzinfo is None for b in bars):
        r.failures.append("naive (timezone-less) timestamps present")
        return r
    seen = defaultdict(list)
    for b in bars:
        seen[_local(b.timestamp).date()].append(b)
    exp = expected_sessions(start, end)
    missing = [d for d in exp if d not in seen]
    extra = sorted(d for d in seen if d not in set(exp))
    if missing:
        r.failures.append(f"{len(missing)} missing session(s): "
                          + ", ".join(map(str, missing[:10])) + (" ..." if len(missing) > 10 else ""))
    if extra:
        r.failures.append(f"{len(extra)} unexpected session(s) (holiday table?): "
                          + ", ".join(map(str, extra[:10])))
    if tf == "1d":
        bad = [b for b in bars if _local(b.timestamp).time() != time(0)]
        if bad:
            r.failures.append(f"{len(bad)} daily bars not stamped at local midnight ET")
        r.info.append(f"{len(seen)} sessions {min(seen)} .. {max(seen)}")
        return r
    full, early = BARS_PER_SESSION[tf]
    short, outside = [], 0
    for d, lst in sorted(seen.items()):
        open_t = datetime.combine(d, time(9, 30), tzinfo=ET)
        close_t = datetime.combine(d, time(13 if d in NYSE_EARLY_CLOSES else 16), tzinfo=ET)
        for b in lst:
            if not (open_t <= _local(b.timestamp) < close_t):
                outside += 1
        want = early if d in NYSE_EARLY_CLOSES else full
        if len(lst) != want:
            short.append(f"{d}: {len(lst)}/{want}")
        if _local(lst[0].timestamp).time() != time(9, 30):
            r.failures.append(f"{d}: first bar at {_local(lst[0].timestamp).time()} not 09:30 ET")
    if outside:
        r.failures.append(f"{outside} bars outside regular trading hours (ET)")
    if short:
        r.failures.append(f"{len(short)} session(s) with wrong bar count: " + ", ".join(short[:8]))
    r.info.append(f"{len(seen)} sessions, {len(bars)} bars, first {_local(bars[0].timestamp)}, "
                  f"last {_local(bars[-1].timestamp)}")
    return r


def bucket_key(ts, tf):
    """IBKR bar alignment: 15m bars start at 09:30 + n*15; 1h bars are CLOCK-
    aligned (09:30-10:00 is a half bar, then 10:00, 11:00, ... 15:00)."""
    loc = _local(ts)
    if tf == "1d":
        return loc.date()
    if tf == "1h":
        return (loc.date(), loc.hour)
    mins = loc.hour * 60 + loc.minute - (9 * 60 + 30)
    return (loc.date(), mins // TF_MINUTES[tf])


def aggregate_5m(bars5, tf):
    groups = defaultdict(list)
    for b in bars5:
        groups[bucket_key(b.timestamp, tf)].append(b)
    return {k: (g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close)
            for k, g in groups.items()}


def check_consistency(symbol, bars5, other, tf):
    """Re-aggregated 5m vs stored 15m / 1h / 1d (OHLC must match exactly)."""
    r = CheckResult(symbol, f"5m->{tf}", ())
    if not bars5 or not other:
        r.warnings.append("insufficient overlap to compare")
        return r
    days5 = {_local(b.timestamp).date() for b in bars5}
    agg = aggregate_5m(bars5, tf)
    tol = Decimal("0.0005")
    compared, exact, auction, bad = 0, 0, [], []
    for b in other:
        k = bucket_key(b.timestamp, tf)
        d = k if tf == "1d" else k[0]
        if d not in days5 or k not in agg:
            continue
        compared += 1
        o, h, l, c = agg[k]
        diffs = [(n, x, y) for n, x, y in (("open", o, b.open), ("high", h, b.high),
                                           ("low", l, b.low), ("close", c, b.close)) if x != y]
        if not diffs:
            exact += 1
            continue
        text = f"{k}: " + ", ".join(f"{n} 5m={x} {tf}={y}" for n, x, y in diffs)
        # Daily bars carry the official opening / closing AUCTION prints, which
        # the 5m bars (continuous trades) may not contain. A difference is
        # explained only if it is confined to open/close, or to a high/low
        # that equals the official open/close print and still contains the
        # 5m range.
        explained = tf == "1d" and all(
            n in ("open", "close")
            or (n == "high" and y >= x and y in (b.open, b.close))
            or (n == "low" and y <= x and y in (b.open, b.close))
            for n, x, y in diffs)
        worst = max(abs(x - y) / y for _, x, y in diffs)
        if explained:
            auction.append(text)
        elif worst > tol:
            bad.append(text)
        else:
            r.warnings.append(f"<=5 bps: {text}")
    r.bars = compared
    if not compared:
        r.failures.append("no overlapping bars compared")
        return r
    r.info.append(f"{exact}/{compared} {tf} bars match re-aggregated 5m exactly")
    if auction:
        r.warnings.append(f"{len(auction)} daily bar(s) differ only by official open/close "
                          f"auction prints (expected; daily candles use official prints)")
        r.info += auction
    if bad:
        r.failures.append(f"{len(bad)}/{compared} {tf} bars disagree with re-aggregated 5m: "
                          + "; ".join(bad[:4]))
    return r


def check_splits(symbol, daily):
    """Overnight moves that look like unadjusted splits (ratio near 1:2, 1:3, 2:1...)."""
    r = CheckResult(symbol, "1d corporate actions", ())
    r.bars = len(daily)
    ratios = (Decimal(2), Decimal(3), Decimal(4), Decimal("1.5"), Decimal(5), Decimal(10))
    for a, b in zip(daily, daily[1:]):
        if a.close <= 0:
            continue
        x = b.open / a.close
        for q in ratios:
            for target in (q, 1 / q):
                if abs(x - target) / target < Decimal("0.03"):
                    r.failures.append(f"possible unadjusted split between {_local(a.timestamp).date()} "
                                      f"and {_local(b.timestamp).date()}: open/prev close = {x:.3f}")
        if abs(x - 1) > Decimal("0.15"):
            r.warnings.append(f"overnight move {x - 1:+.1%} on {_local(b.timestamp).date()}")
    if not r.failures:
        r.info.append("no split-like overnight discontinuities")
    return r


def compare_adjusted(symbol, trades_daily, adjusted_daily):
    """TRADES (split-adjusted, not dividend-adjusted) vs IBKR ADJUSTED_LAST.
    A changing ratio reveals dividend adjustments on ex-dates."""
    r = CheckResult(symbol, "1d dividend adjustment", ())
    adj = {_local(b.timestamp).date(): b.close for b in adjusted_daily}
    pts = [(_local(b.timestamp).date(), adj[_local(b.timestamp).date()] / b.close)
           for b in trades_daily if _local(b.timestamp).date() in adj and b.close > 0]
    r.bars = len(pts)
    if not pts:
        r.warnings.append("no overlapping ADJUSTED_LAST data")
        return r
    steps = [(d1, (q1 / q0 - 1)) for (d0, q0), (d1, q1) in zip(pts, pts[1:])
             if abs(q1 / q0 - 1) > Decimal("0.0002")]
    first, last = pts[0][1], pts[-1][1]
    r.info.append(f"adjusted/raw ratio {first:.5f} ({pts[0][0]}) -> {last:.5f} ({pts[-1][0]}); "
                  f"total dividend effect over window {(1 - first / last):.2%}")
    for d, s in steps:
        r.info.append(f"  adjustment step on {d}: {s:+.3%} (dividend ex-date / corporate action)")
    big = [x for x in steps if abs(x[1]) > Decimal("0.05")]
    if big:
        r.failures.append(f"large adjustment step(s) not explained by dividends: {big[:3]}")
    return r


def _cfg():
    import config
    return config
