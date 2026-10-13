"""
Setup detection and entry levels (long-only, deterministic).

Candidates are evaluated on the COMPLETED daily structure (plus 1h when
fresh). At most one setup is selected by priority:
    BREAKOUT > PULLBACK_TO_SUPPORT > TREND_CONTINUATION >
    REVERSAL_FROM_SUPPORT > MEAN_REVERSION
A trend-following candidate together with a counter-trend candidate is
AMBIGUOUS (-> NO TRADE). Entries never chase: maximum entry is
entry + MAX_ENTRY_ATR x ATR; above it the price state is WAIT_FOR_PULLBACK.
"""

from decimal import ROUND_CEILING, Decimal

from strategy.models import PriceState, Setup, SetupType, TimeframePolicy

CENT = Decimal("0.01")
TREND_FOLLOWING = {SetupType.BREAKOUT, SetupType.PULLBACK_TO_SUPPORT,
                   SetupType.TREND_CONTINUATION}
COUNTER_TREND = {SetupType.REVERSAL_FROM_SUPPORT, SetupType.MEAN_REVERSION}


def up(d):
    return d.quantize(CENT, rounding=ROUND_CEILING)


def policy_for(setup_type, cfg):
    horizon = cfg.SETUP_HORIZON.get(getattr(setup_type, "value", setup_type), "SWING")
    p = cfg.TIMEFRAME_POLICY[horizon]
    return TimeframePolicy(horizon, tuple(p["required"]), tuple(p["preferred"]),
                           tuple(p["optional"]))


def _bullish_signal(view):
    """Most recent completed candle reading that is BULLISH, if any."""
    for reading in reversed(view.candles if view else []):
        if reading.bias == "BULLISH":
            return reading
    return None


def _make(stype, trigger_tf, entry, entry_low, atr, cfg, zone=None, signal=None, facts=()):
    entry = up(entry)
    return Setup(stype, cfg.SETUP_HORIZON[stype.value], trigger_tf, entry,
                 min(entry_low, entry), up(entry + cfg.MAX_ENTRY_ATR * atr),
                 zone, signal, list(facts))


def candidate_setups(ctx, cfg):
    """All setups whose deterministic conditions are met."""
    d = ctx.daily
    if d is None or not d.fresh or d.structure is None or ctx.atr is None:
        return []
    s, atr, price = d.structure, ctx.atr, ctx.current_price
    if price is None:
        return []
    last = d.last_row
    out = []

    # 1. BREAKOUT: confirmed (or weak - volume rule decides) daily breakout;
    #    1h breakout counts when 1h is fresh.
    for key in ("1d", "1h"):
        v = ctx.timeframes.get(key)
        if v is not None and v.fresh and v.breakout and v.breakout.status in ("CONFIRMED", "WEAK"):
            zone = v.breakout.zone
            out.append(_make(SetupType.BREAKOUT, key, zone.high + cfg.ENTRY_BUFFER_ATR * atr,
                             zone.high, atr, cfg, zone,
                             facts=[f"{key} close above resistance {zone.low}-{zone.high} "
                                    f"({v.breakout.status})"]))
            break

    # 2. PULLBACK TO SUPPORT: uptrend, price within ZONE_PROXIMITY_ATR of a
    #    support zone or MA20, with bullish candle context or buying pressure.
    uptrend = s.bullish or s.trend == "UPTREND"
    if uptrend:
        tol = cfg.ZONE_PROXIMITY_ATR * atr
        zone = next((z for z in s.supports if z.low - tol <= price <= z.high + tol), None)
        near_ma = s.ma20 is not None and s.ma20 <= price <= s.ma20 + tol
        signal = _bullish_signal(d)
        pressure_ok = d.pressure is not None and d.pressure.buying in ("STRONG", "MODERATE")
        if (zone is not None or near_ma) and (signal is not None or pressure_ok):
            if zone is not None:
                entry, low, why = zone.high, zone.low, f"pullback into support {zone.low}-{zone.high}"
            else:
                entry, low, why = s.ma20 + tol, s.ma20, f"pullback to MA20 {s.ma20:.2f}"
            out.append(_make(SetupType.PULLBACK_TO_SUPPORT, "1d", entry, low, atr, cfg, zone,
                             signal, [why, "uptrend structure intact"]))

    # 3. TREND CONTINUATION: HH/HL + MA trend, consolidating/compressing,
    #    entry on a break of the consolidation high.
    if s.continuation and s.bullish and (s.consolidation or s.compression) \
            and s.consolidation_high is not None:
        trigger = s.consolidation_high + cfg.ENTRY_BUFFER_ATR * atr
        out.append(_make(SetupType.TREND_CONTINUATION, "1d", trigger, trigger, atr, cfg,
                         facts=[f"consolidation {s.consolidation_low}-{s.consolidation_high} "
                                f"within HH/HL uptrend"]))

    # 4. REVERSAL FROM STRONG SUPPORT: strong zone + STRONG bullish candle.
    strong = next((z for z in s.supports if z.touches >= cfg.STRONG_SUPPORT_TOUCHES
                   and z.low - cfg.ZONE_PROXIMITY_ATR * atr <= last.bar.low
                   <= z.high + cfg.ZONE_PROXIMITY_ATR * atr), None)
    signal = _bullish_signal(d)
    if strong is not None and signal is not None and signal.strength == "STRONG" \
            and ctx.regime.label != "RISK_OFF" and not uptrend:
        out.append(_make(SetupType.REVERSAL_FROM_SUPPORT, "1d",
                         last.bar.high + cfg.ENTRY_BUFFER_ATR * atr, last.bar.high, atr, cfg,
                         strong, signal,
                         [f"strong support {strong.low}-{strong.high} ({strong.touches} touches)",
                          f"bullish candle: {', '.join(signal.shapes)}"]))

    # 5. MEAN REVERSION: only when clearly justified.
    if (s.dist_ma20_atr is not None and s.dist_ma20_atr <= -cfg.MEAN_REVERSION_MIN_ATR_BELOW_MA20
            and strong is not None and signal is not None
            and any("REJECTION" in sh for sh in signal.shapes)
            and ctx.regime.label in ("RISK_ON", "NEUTRAL")):
        out.append(_make(SetupType.MEAN_REVERSION, "1d",
                         last.bar.high + cfg.ENTRY_BUFFER_ATR * atr, last.bar.high, atr, cfg,
                         strong, signal,
                         [f"{-s.dist_ma20_atr:.1f} ATR below MA20 at strong support"]))
    return out


def select_setup(candidates):
    """(setup or None, ambiguous)."""
    if not candidates:
        return None, False
    types = {c.setup_type for c in candidates}
    if types & TREND_FOLLOWING and types & COUNTER_TREND:
        return None, True
    order = list(SetupType)
    return sorted(candidates, key=lambda c: order.index(c.setup_type))[0], False


def setup_anchor(setup, ctx):
    """Structural identity of a setup instance: (kind, low, high).

    INFORMATIONAL ONLY - it never changes a decision. The backtester uses it
    to count each setup instance once in shadow research:
      ZONE          - the resistance / support zone the setup is built on
      CONSOLIDATION - the daily consolidation box of a trend continuation
      MA20          - a pullback to the 20-day MA (no fixed level)
    """
    if setup is None:
        return None
    if setup.setup_type == SetupType.TREND_CONTINUATION:
        s = ctx.daily.structure if ctx.daily else None
        if s is not None and s.consolidation_high is not None:
            return ("CONSOLIDATION", s.consolidation_low, s.consolidation_high)
    zone = setup.reference_zone
    if zone is not None:
        return ("ZONE", zone.low, zone.high)
    if setup.setup_type == SetupType.PULLBACK_TO_SUPPORT:
        return ("MA20", None, None)
    return ("LEVEL", setup.entry_zone_low, setup.entry_zone_low)


def price_state(setup, price):
    if setup is None or price is None:
        return PriceState.UNKNOWN
    if price > setup.maximum_entry:
        return PriceState.WAIT_FOR_PULLBACK
    if price < setup.entry_zone_low:
        return PriceState.AWAITING_TRIGGER
    return PriceState.IN_RANGE
