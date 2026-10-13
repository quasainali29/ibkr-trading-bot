"""
Buyer/seller pressure and breakout quality (deterministic).

Pressure uses, over the last PRESSURE_LOOKBACK_BARS completed bars:
body size vs ATR by direction, close location within the range, wick
structure, relative-volume weighting, repeated rejections at a zone and
breakout follow-through. Each side is STRONG / MODERATE / WEAK.
"""

from dataclasses import dataclass, field
from decimal import Decimal

HALF = Decimal("0.5")
LOW_W, HIGH_W = Decimal("0.5"), Decimal("3")


@dataclass
class PressureResult:
    buying: str
    selling: str
    buy_score: Decimal
    sell_score: Decimal
    reasons: list = field(default_factory=list)


@dataclass
class BreakoutQuality:
    status: str                    # CONFIRMED / WEAK / FAILED / NOT_PRESENT
    zone: object = None
    reasons: list = field(default_factory=list)
    trigger_row: object = None     # FeatureRow of the breakout bar


def _level(score, cfg):
    if score >= cfg.PRESSURE_STRONG:
        return "STRONG"
    if score >= cfg.PRESSURE_MODERATE:
        return "MODERATE"
    return "WEAK"


def classify_pressure(rows, structure, cfg):
    rows = list(rows)[-cfg.PRESSURE_LOOKBACK_BARS:]
    if not rows or rows[-1].atr is None or rows[-1].atr <= 0:
        return PressureResult("WEAK", "WEAK", Decimal(0), Decimal(0),
                              ["insufficient data (ATR unavailable)"])
    atr = rows[-1].atr
    buy = sell = Decimal(0)
    for r in rows:
        g, b = r.geometry, r.bar
        # body / wick sizes of a short opening 1h bar are judged against the
        # duration-adjusted ATR (sqrt of its span); identical for normal bars
        ref = atr if not r.short_bar else atr * r.span.sqrt()
        w = Decimal(1) if r.rvol is None else min(max(r.rvol, LOW_W), HIGH_W)
        clv = (b.close - b.low) / g.range if g.range > 0 else HALF
        bb = (g.body / ref if g.direction == "UP" else 0) + HALF * g.lower_wick / ref \
            + max(clv - HALF, 0)
        sb = (g.body / ref if g.direction == "DOWN" else 0) + HALF * g.upper_wick / ref \
            + max(HALF - clv, 0)
        buy += bb * w
        sell += sb * w
    n = Decimal(len(rows))
    buy, sell = buy / n, sell / n
    reasons = [f"avg buy score {buy:.2f}, sell score {sell:.2f} over {len(rows)} bars"]

    tol = cfg.ZONE_PROXIMITY_ATR * atr
    if structure is not None and structure.supports:
        z = structure.supports[0]
        hits = sum(1 for r in rows if r.geometry.range > 0
                   and r.geometry.lower_wick >= cfg.REJECTION_WICK_RANGE_FRAC * r.geometry.range
                   and z.low - tol <= r.bar.low <= z.high + tol)
        if hits >= 2:
            buy += Decimal("0.3")
            reasons.append(f"{hits} lower-wick rejections at support")
    if structure is not None and structure.resistances:
        z = structure.resistances[0]
        hits = sum(1 for r in rows if r.geometry.range > 0
                   and r.geometry.upper_wick >= cfg.REJECTION_WICK_RANGE_FRAC * r.geometry.range
                   and z.low - tol <= r.bar.high <= z.high + tol)
        if hits >= 2:
            sell += Decimal("0.3")
            reasons.append(f"{hits} upper-wick rejections at resistance")
    if structure is not None and structure.breakout and rows[-1].geometry.direction == "UP":
        buy += Decimal("0.3")
        reasons.append("breakout follow-through")
    if structure is not None and structure.breakdown and rows[-1].geometry.direction == "DOWN":
        sell += Decimal("0.3")
        reasons.append("breakdown follow-through")
    return PressureResult(_level(buy, cfg), _level(sell, cfg), buy, sell, reasons)


def breakout_quality(rows, structure, cfg):
    """CONFIRMED / WEAK / FAILED / NOT_PRESENT for the reference resistance."""
    if structure is None or structure.breakout_zone is None:
        return BreakoutQuality("NOT_PRESENT", None, ["no reference resistance zone"])
    zone = structure.breakout_zone
    if structure.failed_breakout:
        return BreakoutQuality("FAILED", zone,
                               [f"breached {zone.high} but closed back below within "
                                f"{cfg.BREAK_LOOKBACK_BARS} bars"])
    if not structure.breakout:
        return BreakoutQuality("NOT_PRESENT", zone, ["no close above resistance"])

    window = list(rows)[-cfg.BREAK_LOOKBACK_BARS:]
    idx = next(i for i, r in enumerate(window) if r.bar.close > zone.high)
    br = window[idx]
    atr = br.atr
    reasons, ok = [], True
    beyond = br.bar.close - zone.high
    if atr is None or beyond < cfg.BREAKOUT_CLOSE_BEYOND_ATR * atr:
        ok = False
        reasons.append("close only marginally beyond the zone")
    else:
        reasons.append(f"closed {beyond:.2f} beyond zone ({beyond / atr:.2f} ATR)")
    if br.rvol is None or br.rvol < cfg.BREAKOUT_MIN_RVOL:
        ok = False
        reasons.append(f"breakout volume weak (RVOL {br.rvol if br.rvol is None else f'{br.rvol:.2f}'})")
    else:
        reasons.append(f"breakout RVOL {br.rvol:.2f}")
    if atr is None or br.geometry.range < cfg.BREAKOUT_MIN_RANGE_ATR * br.atr_ref:
        ok = False
        reasons.append("breakout candle small relative to ATR")
    later = window[idx + 1:]
    follow = any(r.bar.close > zone.high for r in later)
    retest = any(r.bar.low <= zone.high + (atr or 0) * cfg.BREAKOUT_CLOSE_BEYOND_ATR
                 and r.bar.close > zone.high for r in later)
    if follow or retest:
        reasons.append("follow-through" + (" with successful retest" if retest else ""))
    else:
        ok = False
        reasons.append("no follow-through yet")
    return BreakoutQuality("CONFIRMED" if ok else "WEAK", zone, reasons, br)
