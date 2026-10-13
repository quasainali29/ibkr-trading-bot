"""
Candle interpretation IN CONTEXT (deterministic).

Step 1 detects shapes from geometry only. Step 2 scores each shape against
context - trend/structure, proximity to support/resistance zones, relative
volume, prior candles, size vs ATR, market regime. A shape alone NEVER yields
a bullish/bearish reading: without supporting context the result is NEUTRAL.
"""

from dataclasses import dataclass, field

REJECTION_LOW = "LOWER_WICK_REJECTION"
REJECTION_HIGH = "UPPER_WICK_REJECTION"
ENGULF_BULL = "BULLISH_ENGULFING"
ENGULF_BEAR = "BEARISH_ENGULFING"
MOMENTUM_UP = "MOMENTUM_UP"
MOMENTUM_DOWN = "MOMENTUM_DOWN"
EXHAUSTION_UP = "EXHAUSTION_AFTER_RALLY"
EXHAUSTION_DOWN = "EXHAUSTION_AFTER_DECLINE"
INDECISION = "INDECISION"
INSIDE = "INSIDE_BAR"
OUTSIDE = "OUTSIDE_BAR"


@dataclass
class CandleReading:
    timestamp: object
    shapes: list
    bias: str            # BULLISH / BEARISH / NEUTRAL
    strength: str        # STRONG / MODERATE / WEAK / NONE
    bull_points: int = 0
    bear_points: int = 0
    reasons: list = field(default_factory=list)


def detect_shapes(rows, i, cfg):
    """Geometric shapes of candle i (no interpretation)."""
    r = rows[i]
    b, g = r.bar, r.geometry
    shapes = []
    if g.range <= 0:
        return [INDECISION]
    prev = rows[i - 1] if i > 0 else None
    atr = r.atr

    if g.body <= cfg.INDECISION_BODY_RANGE_FRAC * g.range:
        shapes.append(INDECISION)
    if (g.lower_wick >= cfg.REJECTION_WICK_BODY_MULT * g.body
            and g.lower_wick >= cfg.REJECTION_WICK_RANGE_FRAC * g.range):
        shapes.append(REJECTION_LOW)
    if (g.upper_wick >= cfg.REJECTION_WICK_BODY_MULT * g.body
            and g.upper_wick >= cfg.REJECTION_WICK_RANGE_FRAC * g.range):
        shapes.append(REJECTION_HIGH)
    # Range-vs-ATR uses the duration-adjusted ATR (short opening 1h bar).
    if atr and g.body >= cfg.MOMENTUM_BODY_RANGE_FRAC * g.range \
            and g.range >= cfg.MOMENTUM_RANGE_ATR * r.atr_ref:
        shapes.append(MOMENTUM_UP if g.direction == "UP" else MOMENTUM_DOWN)
    if prev is not None:
        pb, pg = prev.bar, prev.geometry
        p_top, p_bot = max(pb.open, pb.close), min(pb.open, pb.close)
        top, bot = max(b.open, b.close), min(b.open, b.close)
        if g.direction == "UP" and pg.direction == "DOWN" and top >= p_top and bot <= p_bot \
                and g.body > pg.body:
            shapes.append(ENGULF_BULL)
        if g.direction == "DOWN" and pg.direction == "UP" and top >= p_top and bot <= p_bot \
                and g.body > pg.body:
            shapes.append(ENGULF_BEAR)
        if b.high < pb.high and b.low > pb.low:
            shapes.append(INSIDE)
        if b.high > pb.high and b.low < pb.low:
            shapes.append(OUTSIDE)
    if atr and r.sma_fast is not None and g.range >= cfg.EXHAUSTION_RANGE_ATR * r.atr_ref:
        ext = (b.close - r.sma_fast) / atr
        climactic = r.rvol is not None and r.rvol >= cfg.EXHAUSTION_RVOL
        if ext >= cfg.EXHAUSTION_EXTENSION_ATR and (climactic or REJECTION_HIGH in shapes):
            shapes.append(EXHAUSTION_UP)
        if ext <= -cfg.EXHAUSTION_EXTENSION_ATR and (climactic or REJECTION_LOW in shapes):
            shapes.append(EXHAUSTION_DOWN)
    return shapes


def _near_zone(price, zones, atr, cfg):
    """A zone the price touched/pierced or is within ZONE_PROXIMITY_ATR of."""
    if atr is None:
        return None
    tol = cfg.ZONE_PROXIMITY_ATR * atr
    for z in zones:
        if z.low - tol <= price <= z.high + tol:
            return z
    return None


def interpret_candle(rows, i, structure, regime_label, cfg):
    shapes = detect_shapes(rows, i, cfg)
    r = rows[i]
    b, g, atr = r.bar, r.geometry, r.atr
    bull, bear, reasons = 0, 0, []
    supports = structure.supports if structure else []
    resistances = structure.resistances if structure else []
    at_support = _near_zone(b.low, supports, atr, cfg)
    at_resistance = _near_zone(b.high, resistances, atr, cfg)
    uptrend = structure is not None and (structure.bullish or structure.trend == "UPTREND")
    downtrend = structure is not None and (structure.bearish or structure.trend == "DOWNTREND")
    vol_ok = r.rvol is not None and r.rvol >= cfg.CANDLE_CONTEXT_RVOL
    hostile = regime_label == "RISK_OFF"

    def ctx_bull(base_reason):
        nonlocal bull
        pts = 0
        if at_support:
            pts += 2
            reasons.append(f"{base_reason} at support {at_support.low}-{at_support.high}")
        if uptrend:
            pts += 1
            reasons.append(f"{base_reason} within uptrend structure")
        if vol_ok:
            pts += 1
            reasons.append(f"{base_reason} on above-average volume (RVOL {r.rvol:.2f})")
        if hostile and pts:
            pts -= 1
            reasons.append("risk-off regime discounts bullish reading")
        if pts == 0:
            reasons.append(f"{base_reason} without supporting context -> neutral")
        bull += pts

    def ctx_bear(base_reason):
        nonlocal bear
        pts = 0
        if at_resistance:
            pts += 2
            reasons.append(f"{base_reason} at resistance {at_resistance.low}-{at_resistance.high}")
        if downtrend:
            pts += 1
            reasons.append(f"{base_reason} within downtrend structure")
        if vol_ok:
            pts += 1
            reasons.append(f"{base_reason} on above-average volume (RVOL {r.rvol:.2f})")
        if pts == 0:
            reasons.append(f"{base_reason} without supporting context -> neutral")
        bear += pts

    if REJECTION_LOW in shapes:
        ctx_bull("lower-wick rejection")
    if REJECTION_HIGH in shapes:
        ctx_bear("upper-wick rejection")
    if ENGULF_BULL in shapes:
        ctx_bull("bullish engulfing")
    if ENGULF_BEAR in shapes:
        ctx_bear("bearish engulfing")
    if MOMENTUM_UP in shapes:
        ctx_bull("upside momentum candle")
        if structure is not None and structure.breakout:
            bull += 1
            reasons.append("momentum through resistance (breakout)")
    if MOMENTUM_DOWN in shapes:
        ctx_bear("downside momentum candle")
        if structure is not None and structure.breakdown:
            bear += 1
            reasons.append("momentum through support (breakdown)")
    if EXHAUSTION_UP in shapes:
        bear += 2
        reasons.append("exhaustion after extended rally: do not chase")
    if EXHAUSTION_DOWN in shapes:
        if at_support:
            bull += 2
            reasons.append("selling climax into support")
        else:
            reasons.append("exhaustion after decline away from support -> neutral")
    if INDECISION in shapes:
        if at_resistance and uptrend:
            reasons.append("indecision at resistance after advance (caution)")
        elif at_support and downtrend:
            reasons.append("indecision at support after decline (possible pause)")
        else:
            reasons.append("indecision")
    if INSIDE in shapes:
        reasons.append("inside bar: compression, direction undecided")
    if OUTSIDE in shapes:
        clv = (b.close - b.low) / g.range if g.range else None
        if clv is not None and clv >= 0.75:
            ctx_bull("outside bar closing near high")
        elif clv is not None and clv <= 0.25:
            ctx_bear("outside bar closing near low")
        else:
            reasons.append("outside bar closing mid-range -> neutral")

    net = bull - bear
    if net >= 2:
        bias = "BULLISH"
    elif net <= -2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    mag = abs(net)
    strength = ("STRONG" if mag >= 4 else "MODERATE" if mag == 3 else
                "WEAK" if mag == 2 else "NONE")
    return CandleReading(b.timestamp, shapes, bias, strength, bull, bear, reasons)


def interpret_recent(rows, structure, regime_label, cfg, count=3):
    rows = list(rows)
    start = max(1, len(rows) - count)
    return [interpret_candle(rows, i, structure, regime_label, cfg)
            for i in range(start, len(rows))]
