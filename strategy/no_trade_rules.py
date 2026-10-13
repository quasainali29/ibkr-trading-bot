"""
NO TRADE rules, kept in two separate lists:

* ANALYTICAL reasons - the setup itself is not acceptable -> NO TRADE.
* EXECUTION BLOCKERS - the setup may be fine but cannot be acted on now ->
  ANALYSIS_ONLY (action capped at WATCH).

Missing OPTIONAL / PREFERRED timeframes are neither: they only lower
confidence (reported as missing_optional).
"""

from decimal import Decimal

from environment import Environment
from market_data.validation import quote_execution_blocker

from analysis.regime import RISK_OFF, UNKNOWN
from analysis.setup_context import FRESH
from risk.system_state import SystemState
from strategy.entry_logic import TREND_FOLLOWING
from strategy.models import Reason, SetupType


def trigger_row(ctx, setup):
    """The bar that triggered the setup: the breakout bar for BREAKOUT,
    otherwise the latest completed bar of the trigger timeframe."""
    v = ctx.timeframes.get(setup.trigger_timeframe) if setup else ctx.daily
    if v is None:
        return None
    if setup is not None and setup.setup_type is SetupType.BREAKOUT \
            and v.breakout is not None and v.breakout.trigger_row is not None:
        return v.breakout.trigger_row
    return v.last_row


def analytical_reasons(ctx, setup, ambiguous, stop, targets, policy, cfg):
    out = []
    d = ctx.daily
    min_bars = cfg.MA_SLOW_PERIOD + cfg.TREND_SLOPE_LOOKBACK
    if (d is None or not d.fresh or d.structure is None or ctx.atr is None
            or d.features is None or len(d.features.rows) < min_bars):
        out.append(Reason("INSUFFICIENT_DATA",
                          "completed daily candles are missing, stale or too few for analysis"))
        return out
    if ctx.current_price is None:
        out.append(Reason("INSUFFICIENT_DATA", "no current price"))
        return out

    s, atr = d.structure, ctx.atr
    if ambiguous:
        out.append(Reason("SETUP_AMBIGUOUS",
                          "trend-following and counter-trend setups both qualify"))
    elif setup is None:
        out.append(Reason("NO_SETUP", "no qualifying long setup"))

    if ctx.regime.label == RISK_OFF:
        out.append(Reason("HOSTILE_REGIME", "market regime is RISK_OFF"))

    if s.ma20 is not None:
        ext = (ctx.current_price - s.ma20) / atr
        if ext >= cfg.EXTENDED_FROM_MA20_ATR:
            out.append(Reason("PRICE_EXTENDED",
                              f"price {ext:.2f} ATR above daily MA20 "
                              f"(limit {cfg.EXTENDED_FROM_MA20_ATR})"))

    if setup is not None:
        if setup.setup_type in TREND_FOLLOWING and (s.bearish or s.trend == "DOWNTREND"):
            out.append(Reason("STRUCTURE_CONFLICT",
                              "trend-following setup against daily downtrend structure"))
        if s.breakdown:
            out.append(Reason("STRUCTURE_CONFLICT", "daily close below support (breakdown)"))
        for key in policy.required:
            v = ctx.timeframes.get(key)
            if key != "1d" and v is not None and v.fresh and v.structure is not None \
                    and v.structure.bearish and v.pressure and v.pressure.selling == "STRONG":
                out.append(Reason("STRUCTURE_CONFLICT",
                                  f"{key} downtrend structure with strong selling pressure"))
        trig_row = trigger_row(ctx, setup)
        if setup.setup_type in (SetupType.BREAKOUT, SetupType.TREND_CONTINUATION) \
                and (trig_row is None or trig_row.rvol is None
                     or trig_row.rvol < cfg.WEAK_VOLUME_RVOL):
            out.append(Reason("WEAK_VOLUME",
                              f"{setup.trigger_timeframe} relative volume below "
                              f"{cfg.WEAK_VOLUME_RVOL}"))
        if d.pressure is not None and d.pressure.buying == "WEAK" and d.pressure.selling == "STRONG":
            out.append(Reason("WEAK_VOLUME", "sellers dominate daily pressure"))
        if stop is None or not stop.valid:
            code = "STOP_TOO_WIDE" if stop is not None and "exceeds" in stop.problem else "INVALID_STOP"
            out.append(Reason(code, stop.problem if stop else "no stop"))
        elif targets is None or targets.reward_risk is None:
            out.append(Reason("POOR_REWARD_RISK", "no valid target"))
        elif targets.reward_risk < cfg.MIN_REWARD_RISK:
            out.append(Reason("POOR_REWARD_RISK",
                              f"reward/risk {targets.reward_risk:.2f} below "
                              f"{cfg.MIN_REWARD_RISK} (Target 1)"))
    return out


def execution_blockers(ctx, policy, md_check, system_state, cfg,
                       environment=Environment.LIVE):
    """(blockers, missing_optional). Any blocker forces ANALYSIS_ONLY."""
    out = []
    if system_state is not SystemState.SAFE:
        out.append(Reason("SYSTEM_NOT_SAFE", f"system state is "
                          f"{getattr(system_state, 'value', system_state)}"))
    if md_check is None:
        out.append(Reason("MARKET_DATA_CHECK_MISSING", "no market data check"))
    else:
        q = md_check.quote
        if not md_check.contract_ok:
            out.append(Reason("CONTRACT_INVALID", "; ".join(md_check.contract_errors)))
        if not q.available:
            out.append(Reason("QUOTE_UNAVAILABLE", "live quote unavailable"))
        else:
            dt = q.data_type
            type_problem = quote_execution_blocker(q, environment, cfg)
            if dt is None:
                out.append(Reason("QUOTE_TYPE_UNKNOWN", "quote data type not reported"))
            elif q.simulated or dt.is_simulated:
                if type_problem:
                    out.append(Reason("QUOTE_SIMULATED", type_problem))
            elif dt.is_delayed or dt.is_frozen:
                out.append(Reason("QUOTE_DELAYED_OR_FROZEN",
                                  f"quote is {dt.label}: cannot authorize trades"))
            if not q.valid:
                out.append(Reason("QUOTE_INVALID", "; ".join(q.errors)))
            if q.stale:
                out.append(Reason("QUOTE_STALE", "; ".join(q.stale_reasons) or "quote stale"))
        if not md_check.market_open:
            out.append(Reason("MARKET_CLOSED", "regular market closed"))

    for key in policy.required:
        v = ctx.timeframes.get(key)
        if v is None or v.status != FRESH:
            status = v.status if v else "UNAVAILABLE"
            out.append(Reason(f"REQUIRED_TIMEFRAME_{status}",
                              f"required {key} bars {status.lower()}"
                              + (f": {v.note}" if v and v.note else "")))
    if ctx.regime.label == UNKNOWN:
        out.append(Reason("BENCHMARK_DATA_MISSING", "market regime unknown (SPY data missing)"))
    if ctx.relative_strength is None or ctx.relative_strength.primary is None:
        out.append(Reason("BENCHMARK_DATA_MISSING", "relative strength vs SPY unavailable"))
    if md_check is not None and not out and not md_check.usable_for_trading:
        out.append(Reason("MARKET_DATA_NOT_USABLE", " / ".join(md_check.trading_blockers)))

    missing_optional = [k for k in policy.preferred + policy.optional
                        if ctx.timeframes.get(k) is None or not ctx.timeframes[k].fresh]
    return out, missing_optional
