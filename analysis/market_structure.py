"""
Market structure (deterministic, COMPLETED validated bars only).

Per timeframe: swing sequence (HH/HL/LH/LL), continuation, consolidation,
compression, breakout / breakdown and their failures, distances to
support/resistance zones and to MA20/MA50, and ATR-normalized movement.
No trading decisions are made here.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from analysis.market_report import (classify_trend, cluster_zones, find_swings,
                                    support_resistance)

UP = "UPTREND_STRUCTURE"
DOWN = "DOWNTREND_STRUCTURE"
MIXED = "MIXED"
INSUFFICIENT = "INSUFFICIENT"


@dataclass
class StructureResult:
    timeframe: str
    label: str                              # UP / DOWN / MIXED / INSUFFICIENT
    trend: str                              # classify_trend label
    higher_high: bool | None = None
    higher_low: bool | None = None
    lower_high: bool | None = None
    lower_low: bool | None = None
    continuation: bool = False              # structure and MA trend agree
    consolidation: bool = False
    compression: bool = False
    breakout: bool = False                  # latest close above reference resistance
    breakdown: bool = False                 # latest close below reference support
    failed_breakout: bool = False
    failed_breakdown: bool = False
    breakout_zone: object = None            # reference resistance Zone
    breakdown_zone: object = None           # reference support Zone
    close: Decimal | None = None
    atr: Decimal | None = None
    ma20: Decimal | None = None
    ma50: Decimal | None = None
    dist_ma20_atr: Decimal | None = None    # (close - MA20) / ATR
    dist_ma50_atr: Decimal | None = None
    dist_ma20_pct: Decimal | None = None
    supports: list = field(default_factory=list)     # zones below price (nearest first)
    resistances: list = field(default_factory=list)  # zones above price (nearest first)
    dist_support_atr: Decimal | None = None          # price - nearest support high
    dist_resistance_atr: Decimal | None = None       # nearest resistance low - price
    move_5_atr: Decimal | None = None       # (close - close 5 bars ago) / ATR
    move_20_atr: Decimal | None = None
    consolidation_high: Decimal | None = None
    consolidation_low: Decimal | None = None
    notes: list = field(default_factory=list)

    @property
    def bullish(self):
        return self.label == UP

    @property
    def bearish(self):
        return self.label == DOWN


def swing_sequence(bars, window):
    """(highs, lows) swing prices in chronological order."""
    points = find_swings(bars, window)
    return ([p[1] for p in points if p[2] == "H"], [p[1] for p in points if p[2] == "L"])


def classify_swings(highs, lows):
    """HH/HL -> UP, LH/LL -> DOWN, else MIXED; < 2 swings each -> INSUFFICIENT."""
    if len(highs) < 2 or len(lows) < 2:
        return INSUFFICIENT, None, None, None, None
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    lh, ll = highs[-1] < highs[-2], lows[-1] < lows[-2]
    if hh and hl:
        label = UP
    elif lh and ll:
        label = DOWN
    else:
        label = MIXED
    return label, hh, hl, lh, ll


def is_consolidation(bars, atr, cfg):
    n = cfg.CONSOLIDATION_BARS
    if atr is None or atr <= 0 or len(bars) < n:
        return False, None, None
    window = bars[-n:]
    hi, lo = max(b.high for b in window), min(b.low for b in window)
    return (hi - lo) <= cfg.CONSOLIDATION_MAX_ATR * atr, hi, lo


def is_compression(bars, cfg):
    r, b = cfg.COMPRESSION_RECENT_BARS, cfg.COMPRESSION_BASE_BARS
    if len(bars) < r + b:
        return False
    recent = bars[-r:]
    base = bars[-(r + b):-r]
    avg_recent = sum((x.high - x.low for x in recent), Decimal(0)) / r
    avg_base = sum((x.high - x.low for x in base), Decimal(0)) / b
    return avg_base > 0 and avg_recent < cfg.COMPRESSION_RATIO * avg_base


def reference_zones(bars, atr, cfg):
    """Zones formed BEFORE the break window, split around the price at that
    time: returns (nearest resistance above, nearest support below)."""
    lb = cfg.BREAK_LOOKBACK_BARS
    if atr is None or len(bars) <= lb + 2 * cfg.SWING_WINDOW:
        return None, None
    ref = bars[:-lb][-cfg.SR_LOOKBACK_BARS:]
    zones = cluster_zones(find_swings(ref, cfg.SWING_WINDOW),
                          atr * cfg.SR_ZONE_ATR_MULT, atr * cfg.SR_MIN_ZONE_ATR_MULT)
    ref_close = ref[-1].close
    above = sorted((z for z in zones if z.low > ref_close), key=lambda z: z.low)
    below = sorted((z for z in zones if z.high < ref_close), key=lambda z: -z.high)
    return (above[0] if above else None), (below[0] if below else None)


def break_status(bars, resistance, support, cfg):
    """(breakout, failed_breakout, breakdown, failed_breakdown) over the
    last BREAK_LOOKBACK_BARS bars relative to the reference zones."""
    window = bars[-cfg.BREAK_LOOKBACK_BARS:]
    last = bars[-1].close
    bo = fbo = bd = fbd = False
    if resistance is not None:
        breached = any(b.high > resistance.high for b in window)
        bo = last > resistance.high
        fbo = breached and not bo
    if support is not None:
        breached = any(b.low < support.low for b in window)
        bd = last < support.low
        fbd = breached and not bd
    return bo, fbo, bd, fbd


def analyze_structure(bars, features, timeframe_key, cfg, current_price=None):
    """Full structure read for one timeframe. `bars` = completed bars and
    `features` = compute_features(bars, ...) on the same bars."""
    bars = list(bars)[-cfg.STRUCTURE_LOOKBACK_BARS:]
    rows = list(features.rows)[-len(bars):] if features else []
    if len(bars) < 2 * cfg.SWING_WINDOW + 2 or not rows:
        return StructureResult(timeframe_key, INSUFFICIENT, "INSUFFICIENT DATA",
                               notes=["not enough completed bars"])
    last = rows[-1]
    atr, close = last.atr, last.bar.close
    price = current_price if current_price is not None else close

    highs, lows = swing_sequence(bars, cfg.SWING_WINDOW)
    label, hh, hl, lh, ll = classify_swings(highs, lows)
    trend = classify_trend(features, cfg).label
    res = StructureResult(timeframe_key, label, trend, hh, hl, lh, ll,
                          close=close, atr=atr, ma20=last.sma_fast, ma50=last.sma_slow)
    res.continuation = ((label == UP and trend == "UPTREND")
                        or (label == DOWN and trend == "DOWNTREND"))
    res.consolidation, res.consolidation_high, res.consolidation_low = \
        is_consolidation(bars, atr, cfg)
    res.compression = is_compression(bars, cfg)

    if atr is not None and atr > 0:
        if res.ma20 is not None:
            res.dist_ma20_atr = (close - res.ma20) / atr
            res.dist_ma20_pct = (close - res.ma20) / res.ma20
        if res.ma50 is not None:
            res.dist_ma50_atr = (close - res.ma50) / atr
        if len(bars) > 5:
            res.move_5_atr = (close - bars[-6].close) / atr
        if len(bars) > 20:
            res.move_20_atr = (close - bars[-21].close) / atr

        r_zone, s_zone = reference_zones(bars, atr, cfg)
        res.breakout_zone, res.breakdown_zone = r_zone, s_zone
        res.breakout, res.failed_breakout, res.breakdown, res.failed_breakdown = \
            break_status(bars, r_zone, s_zone, cfg)

        res.supports, res.resistances = support_resistance(bars, atr, price, cfg)
        if res.supports:
            res.dist_support_atr = (price - res.supports[0].high) / atr
        if res.resistances:
            res.dist_resistance_atr = (res.resistances[0].low - price) / atr
    else:
        res.notes.append("ATR unavailable: zone/break analysis skipped")
    return res
