"""
Stop placement: technical invalidation first, ATR sanity second.

* Technical level by setup: breakout zone low, support zone low, MA20-pullback
  swing low, consolidation low, or signal-candle low - minus STOP_BUFFER_ATR.
* Closer than STOP_MIN_ATR -> moved DOWN to entry - STOP_MIN_ATR x ATR
  (more room; never above the technical level).
* Wider than STOP_MAX_ATR -> invalid (STOP_TOO_WIDE -> NO TRADE).
The risk engine validates the stop again, independently.
"""

from decimal import ROUND_FLOOR, Decimal

from strategy.models import SetupType, StopPlan

CENT = Decimal("0.01")


def down(d):
    return d.quantize(CENT, rounding=ROUND_FLOOR)


def _last_swing_low_below(view, price, cfg):
    from analysis.market_report import find_swings
    bars = [r.bar for r in view.features.rows]
    lows = [p[1] for p in find_swings(bars, cfg.SWING_WINDOW) if p[2] == "L" and p[1] < price]
    return lows[-1] if lows else None


def _signal_low(view, signal):
    if signal is None:
        return None
    return next((r.bar.low for r in view.features.rows
                 if r.bar.timestamp == signal.timestamp), None)


def plan_stop(setup, ctx, cfg):
    atr = ctx.atr
    if setup is None or atr is None:
        return StopPlan(None, "", "", valid=False, problem="no setup / ATR")
    t = setup.setup_type
    zone = setup.reference_zone
    s = ctx.daily.structure

    if t == SetupType.BREAKOUT:
        level, stype = zone.low, "BELOW_BREAKOUT_LEVEL"
        why = f"daily close back below the breakout zone ({zone.low})"
    elif t == SetupType.PULLBACK_TO_SUPPORT and zone is not None:
        level, stype = zone.low, "BELOW_SUPPORT_ZONE"
        why = f"daily close below support zone {zone.low}-{zone.high}"
    elif t == SetupType.PULLBACK_TO_SUPPORT:
        level = _last_swing_low_below(ctx.daily, setup.entry_zone_low, cfg)
        if level is None:
            return StopPlan(None, "BELOW_SWING_LOW", "", valid=False,
                            problem="no swing low below MA20 for invalidation")
        stype, why = "BELOW_SWING_LOW", f"break of swing low {level}"
    elif t == SetupType.TREND_CONTINUATION:
        level, stype = s.consolidation_low, "BELOW_CONSOLIDATION_LOW"
        why = f"break below consolidation low {level}"
    else:  # reversal / mean reversion
        candidates = [x for x in (zone.low if zone else None,
                                  _signal_low(ctx.daily, setup.signal_bar)) if x is not None]
        if not candidates:
            return StopPlan(None, "BELOW_SUPPORT_ZONE", "", valid=False,
                            problem="no support / signal candle for invalidation")
        level, stype = min(candidates), "BELOW_SUPPORT_ZONE"
        why = f"break below support / signal-candle low {level}"

    raw = level - cfg.STOP_BUFFER_ATR * atr
    plan = StopPlan(down(raw), stype, why)
    if plan.stop <= 0 or plan.stop >= setup.entry:
        plan.valid, plan.problem = False, "technical stop is not below entry"
        return plan
    floor_stop = down(setup.entry - cfg.STOP_MIN_ATR * atr)
    if setup.entry - plan.stop < cfg.STOP_MIN_ATR * atr:
        plan.stop, plan.adjusted = min(plan.stop, floor_stop), True
        plan.stop_type += "+ATR_FLOOR"
    plan.distance = setup.entry - plan.stop
    plan.distance_atr = plan.distance / atr
    if plan.distance_atr > cfg.STOP_MAX_ATR:
        plan.valid = False
        plan.problem = (f"stop {plan.distance_atr:.2f} ATR from entry exceeds "
                        f"{cfg.STOP_MAX_ATR} ATR")
    return plan
