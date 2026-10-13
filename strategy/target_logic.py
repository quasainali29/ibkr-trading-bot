"""
Targets from market structure - every target names its source.

Target 1: low of the next resistance zone above entry (minus a small
          buffer); if there is no resistance overhead, entry + 2 x ATR
          (ATR_PROJECTION).
Target 2: the following resistance zone; for breakouts / continuations a
          measured move (base height added to the breakout level); otherwise
          none - no arbitrary targets.
Reward/risk is measured to Target 1.
"""

from decimal import ROUND_FLOOR, Decimal

from analysis.market_report import cluster_zones, find_swings
from strategy.models import SetupType, TargetPlan

CENT = Decimal("0.01")


def down(d):
    return d.quantize(CENT, rounding=ROUND_FLOOR)


def overhead_zones(ctx, entry, cfg):
    """All daily resistance zones above the entry, nearest first."""
    d = ctx.daily
    atr = ctx.atr
    bars = [r.bar for r in d.features.rows][-cfg.SR_LOOKBACK_BARS:]
    zones = cluster_zones(find_swings(bars, cfg.SWING_WINDOW),
                          atr * cfg.SR_ZONE_ATR_MULT, atr * cfg.SR_MIN_ZONE_ATR_MULT)
    buf = cfg.TARGET_BUFFER_ATR * atr
    return sorted((z for z in zones if z.low - buf > entry), key=lambda z: z.low)


def plan_targets(setup, stop, ctx, cfg):
    if setup is None or stop is None or not stop.valid or ctx.atr is None:
        return TargetPlan(None, "", None, "", None)
    atr, entry = ctx.atr, setup.entry
    buf = cfg.TARGET_BUFFER_ATR * atr
    zones = overhead_zones(ctx, entry, cfg)

    if zones:
        z1 = zones[0]
        t1, s1 = down(z1.low - buf), f"RESISTANCE_ZONE {z1.low}-{z1.high}"
    else:
        t1 = down(entry + cfg.TARGET_ATR_PROJECTION * atr)
        s1 = f"ATR_PROJECTION ({cfg.TARGET_ATR_PROJECTION} x ATR, no resistance overhead)"

    t2, s2 = None, "none (no second structural target)"
    if len(zones) > 1:
        z2 = zones[1]
        t2, s2 = down(z2.low - buf), f"RESISTANCE_ZONE {z2.low}-{z2.high}"
    elif setup.setup_type in (SetupType.BREAKOUT, SetupType.TREND_CONTINUATION):
        s = ctx.daily.structure
        base_low = s.consolidation_low
        level = setup.reference_zone.high if setup.reference_zone else s.consolidation_high
        if base_low is not None and level is not None and level > base_low:
            mm = down(level + (level - base_low))
            if mm > t1:
                t2, s2 = mm, f"MEASURED_MOVE (base {base_low}-{level})"

    risk = entry - stop.stop
    rr = (t1 - entry) / risk if risk > 0 else None
    return TargetPlan(t1, s1, t2, s2, rr)
