"""
Deterministic price-action and volume features.

These are the ONLY place where candle geometry, ATR, moving averages, VWAP
and relative volume are calculated. Strategy code and any future AI layer
consume these values; they must never recompute them.

All functions are pure, take validated Bar sequences, use Decimal, and
return lists aligned 1:1 with the input bars (None where a value is not
defined because there is not enough data).
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from market_data.sessions import bar_span
from money import ZERO

THREE = Decimal(3)
ONE = Decimal(1)


@dataclass(frozen=True)
class CandleGeometry:
    body: Decimal          # |close - open|
    upper_wick: Decimal    # high - max(open, close)
    lower_wick: Decimal    # min(open, close) - low
    range: Decimal         # high - low
    direction: str         # "UP", "DOWN" or "DOJI" (close == open)


def candle_geometry(bar):
    top, bottom = max(bar.open, bar.close), min(bar.open, bar.close)
    if bar.close > bar.open:
        direction = "UP"
    elif bar.close < bar.open:
        direction = "DOWN"
    else:
        direction = "DOJI"
    return CandleGeometry(
        body=top - bottom,
        upper_wick=bar.high - top,
        lower_wick=bottom - bar.low,
        range=bar.high - bar.low,
        direction=direction,
    )


def pct_changes(bars):
    """Close-to-close change as a fraction (0.0123 = +1.23%)."""
    out = [None] * len(bars)
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        if prev:
            out[i] = (bars[i].close - prev) / prev
    return out


def true_ranges(bars):
    """TR_0 = high-low; TR_i = max(h-l, |h-prev_close|, |l-prev_close|)."""
    out = []
    for i, b in enumerate(bars):
        hl = b.high - b.low
        if i == 0:
            out.append(hl)
        else:
            pc = bars[i - 1].close
            out.append(max(hl, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars, period=14, spans=None):
    """Wilder's ATR. First value (index period-1) is the simple mean of the
    first `period` true ranges; then ATR_i = (ATR_{i-1}*(n-1) + TR_i) / n.

    `spans` (optional, aligned with bars) is each bar's fraction of the
    nominal bar length. Short bars (span < 1, e.g. the 09:30-10:00 opening
    1h bar) are EXCLUDED from the average - their range covers less time -
    and carry the previous ATR value. Without short bars the result is
    exactly the classic ATR."""
    if period <= 0:
        raise ValueError("period must be positive")
    trs = true_ranges(bars)
    out = [None] * len(bars)
    n = Decimal(period)
    seed, value = [], None
    for i, tr in enumerate(trs):
        if spans is not None and spans[i] < ONE:
            out[i] = value
            continue
        if value is None:
            seed.append(tr)
            if len(seed) == period:
                value = sum(seed, ZERO) / n
        else:
            value = (value * (n - 1) + tr) / n
        out[i] = value
    return out


def sma(values, period):
    """Simple moving average; None until `period` non-None values exist."""
    if period <= 0:
        raise ValueError("period must be positive")
    out = [None] * len(values)
    n = Decimal(period)
    # Running window sum. Additions are done at high precision so the sum is
    # exact, which makes every value identical to summing each window afresh.
    with localcontext() as ctx:
        ctx.prec = 80
        run, valid = ZERO, 0
        sums = [None] * len(values)
        for i, v in enumerate(values):
            if v is not None:
                run += v
                valid += 1
            if i >= period:
                old = values[i - period]
                if old is not None:
                    run -= old
                    valid -= 1
            if i >= period - 1 and valid == period:
                sums[i] = run
    for i, s in enumerate(sums):
        if s is not None:
            out[i] = s / n
    return out


def session_vwap(bars, session_key):
    """Session-anchored VWAP for intraday bars.

    Uses IBKR's per-bar WAP when available, otherwise typical price
    (H+L+C)/3. Resets whenever session_key(timestamp) changes.
    """
    out = [None] * len(bars)
    cur_key, pv, vol = object(), ZERO, ZERO
    for i, b in enumerate(bars):
        key = session_key(b.timestamp)
        if key != cur_key:
            cur_key, pv, vol = key, ZERO, ZERO
        price = b.wap if (b.wap is not None and b.wap > 0) else (b.high + b.low + b.close) / THREE
        pv += price * b.volume
        vol += b.volume
        out[i] = pv / vol if vol > 0 else None
    return out


def relative_volume_daily(bars, lookback=20):
    """Volume / mean volume of the previous `lookback` bars (needs all of them)."""
    out = [None] * len(bars)
    for i in range(lookback, len(bars)):
        window = [b.volume for b in bars[i - lookback:i]]
        avg = sum(window, ZERO) / Decimal(lookback)
        if avg > 0:
            out[i] = bars[i].volume / avg
    return out


def relative_volume_intraday(bars, session_key, slot_key, min_sessions=5, max_sessions=20):
    """Volume / mean volume of the same time-of-day slot in prior sessions.

    Requires at least `min_sessions` prior samples for that slot; uses at most
    the `max_sessions` most recent ones.
    """
    out = [None] * len(bars)
    history = {}   # slot -> list of (session, volume) in chronological order
    for i, b in enumerate(bars):
        sess, slot = session_key(b.timestamp), slot_key(b.timestamp)
        prior = [v for s, v in history.get(slot, []) if s != sess][-max_sessions:]
        if len(prior) >= min_sessions:
            avg = sum(prior, ZERO) / Decimal(len(prior))
            if avg > 0:
                out[i] = b.volume / avg
        history.setdefault(slot, []).append((sess, b.volume))
    return out


# ---------------------------------------------------------------------------
# Feature table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeatureRow:
    bar: object
    geometry: CandleGeometry
    pct_change: Decimal | None
    atr: Decimal | None
    sma_fast: Decimal | None
    sma_slow: Decimal | None
    vwap: Decimal | None        # intraday only
    rvol: Decimal | None
    span: Decimal = ONE         # fraction of the nominal bar length (0.5 = short opening 1h bar)

    @property
    def short_bar(self):
        return self.span < ONE

    @property
    def atr_ref(self):
        """ATR to compare THIS bar's range/body against. A short bar covers
        less time, so the full-bar ATR is scaled by sqrt(span) (volatility
        grows with the square root of time). Equals `atr` for normal bars."""
        if self.atr is None or not self.short_bar:
            return self.atr
        with localcontext() as ctx:
            ctx.prec = 28
            return self.atr * self.span.sqrt()


@dataclass(frozen=True)
class FeatureSet:
    timeframe_key: str
    rows: tuple
    atr_period: int
    fast_period: int
    slow_period: int

    @property
    def latest(self):
        return self.rows[-1] if self.rows else None

    def column(self, name):
        return [getattr(r, name) for r in self.rows]


def cached_features(bars, timeframe, tz, cfg, cache=None, prefix=""):
    """compute_features with optional memoization (backtests evaluate the
    same windows many times). The cache key identifies the exact window, and
    FeatureSets are immutable, so results are identical with or without it."""
    if cache is None or not bars:
        return compute_features(bars, timeframe, tz, cfg)
    key = (prefix, timeframe.key, bars[0].timestamp, bars[-1].timestamp, len(bars))
    fs = cache.get(key)
    if fs is None:
        fs = cache[key] = compute_features(bars, timeframe, tz, cfg)
    return fs


def compute_features(bars, timeframe, tz, cfg):
    """Compute every feature for a VALIDATED bar sequence.

    `tz` is the exchange time zone, used for session (VWAP) and time-of-day
    (intraday RVOL) grouping.
    """
    bars = list(bars)
    closes = [b.close for b in bars]
    spans = ([bar_span(b.timestamp, timeframe.seconds, tz) for b in bars]
             if timeframe.intraday and tz is not None else [ONE] * len(bars))
    atrs = atr(bars, cfg.ATR_PERIOD, spans)
    fast = sma(closes, cfg.MA_FAST_PERIOD)
    slow = sma(closes, cfg.MA_SLOW_PERIOD)
    chg = pct_changes(bars)

    def session_of(ts):
        return ts.astimezone(tz).date()

    def slot_of(ts):
        return ts.astimezone(tz).strftime("%H:%M")

    if timeframe.intraday:
        vwaps = session_vwap(bars, session_of)
        rvols = relative_volume_intraday(bars, session_of, slot_of,
                                         cfg.RVOL_MIN_SESSIONS, cfg.RVOL_MAX_SESSIONS)
    else:
        vwaps = [None] * len(bars)
        rvols = relative_volume_daily(bars, cfg.RVOL_DAILY_LOOKBACK)

    rows = tuple(
        FeatureRow(b, candle_geometry(b), chg[i], atrs[i], fast[i], slow[i], vwaps[i], rvols[i],
                   spans[i])
        for i, b in enumerate(bars))
    return FeatureSet(timeframe.key, rows, cfg.ATR_PERIOD, cfg.MA_FAST_PERIOD,
                      cfg.MA_SLOW_PERIOD)
