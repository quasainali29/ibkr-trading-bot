"""
Market regime from SPY / QQQ daily trend (simple, deterministic).

Rules, first match wins:
  RISK_OFF   SPY DOWNTREND, below MA50, and QQQ DOWNTREND (or QQQ missing)
  CHOPPY     SPY crossed MA20 >= REGIME_CHOP_CROSSES times in the lookback
             (and QQQ too, when available)
  RISK_ON    SPY UPTREND and QQQ UPTREND (or QQQ missing)
  DEFENSIVE  SPY below MA20 but above MA50, or QQQ lagging SPY by more than
             REGIME_QQQ_LAG over 20 sessions
  NEUTRAL    otherwise
SPY is required; without it the regime is UNKNOWN.
"""

from dataclasses import dataclass, field

from analysis.market_report import classify_trend
from market_data.features import cached_features
from market_data.models import TIMEFRAMES

RISK_ON, NEUTRAL, CHOPPY, DEFENSIVE, RISK_OFF, UNKNOWN = (
    "RISK_ON", "NEUTRAL", "CHOPPY", "DEFENSIVE", "RISK_OFF", "UNKNOWN")


@dataclass
class RegimeResult:
    label: str
    spy_trend: str | None = None
    qqq_trend: str | None = None
    details: list = field(default_factory=list)


def ma_crosses(features, lookback):
    """Number of times close crossed MA20 within the last `lookback` bars."""
    rows = [r for r in features.rows[-(lookback + 1):] if r.sma_fast is not None]
    sides = [r.bar.close > r.sma_fast for r in rows]
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b)


def _ret(features, n):
    rows = features.rows
    if len(rows) <= n:
        return None
    return rows[-1].bar.close / rows[-1 - n].bar.close - 1


def classify_regime(spy_bars, qqq_bars, tz, cfg, feature_cache=None):
    tf = TIMEFRAMES["1d"]
    if not spy_bars or len(spy_bars) < cfg.MA_SLOW_PERIOD + cfg.TREND_SLOPE_LOOKBACK:
        return RegimeResult(UNKNOWN, details=["SPY daily data unavailable"])
    spy = cached_features(spy_bars, tf, tz, cfg, feature_cache, "SPY")
    qqq = (cached_features(qqq_bars, tf, tz, cfg, feature_cache, "QQQ")
           if qqq_bars and len(qqq_bars) >= cfg.MA_SLOW_PERIOD + cfg.TREND_SLOPE_LOOKBACK
           else None)
    spy_t = classify_trend(spy, cfg).label
    qqq_t = classify_trend(qqq, cfg).label if qqq else None
    res = RegimeResult(NEUTRAL, spy_t, qqq_t)
    last = spy.latest
    res.details.append(f"SPY {spy_t}" + (f", QQQ {qqq_t}" if qqq else ", QQQ unavailable"))
    if qqq is None:
        res.details.append("QQQ missing: regime from SPY only (lower confidence)")

    spy_cross = ma_crosses(spy, cfg.REGIME_CHOP_LOOKBACK)
    qqq_cross = ma_crosses(qqq, cfg.REGIME_CHOP_LOOKBACK) if qqq else None
    res.details.append(f"MA20 crosses (20 bars): SPY {spy_cross}"
                       + (f", QQQ {qqq_cross}" if qqq else ""))

    if (spy_t == "DOWNTREND" and last.sma_slow is not None and last.bar.close < last.sma_slow
            and (qqq is None or qqq_t == "DOWNTREND")):
        res.label = RISK_OFF
    elif spy_cross >= cfg.REGIME_CHOP_CROSSES and (qqq is None or qqq_cross >= cfg.REGIME_CHOP_CROSSES):
        res.label = CHOPPY
    elif spy_t == "UPTREND" and (qqq is None or qqq_t == "UPTREND"):
        res.label = RISK_ON
    else:
        below20 = last.sma_fast is not None and last.bar.close < last.sma_fast
        above50 = last.sma_slow is not None and last.bar.close >= last.sma_slow
        lag = None
        if qqq is not None:
            q20, s20 = _ret(qqq, 20), _ret(spy, 20)
            lag = q20 - s20 if q20 is not None and s20 is not None else None
        if (below20 and above50) or (lag is not None and lag < cfg.REGIME_QQQ_LAG):
            res.label = DEFENSIVE
            if lag is not None and lag < cfg.REGIME_QQQ_LAG:
                res.details.append(f"QQQ lagging SPY by {lag:.2%} over 20 sessions")
    return res
