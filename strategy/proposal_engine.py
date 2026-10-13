"""
Proposal engine - deterministic, advisory, long-only. NO execution code.

Order of operations:
  1. select a setup (or none / ambiguous)
  2. setup-specific timeframe policy -> REQUIRED / PREFERRED / OPTIONAL
  3. setup-specific market-data check (required timeframes only)
  4. stop -> targets
  5. analytical NO TRADE reasons and execution blockers
  6. mode: EXECUTION_READY only when there are NO execution blockers
  7. opportunity score and setup classification
  8. action: NO_TRADE / WATCH / BUY (BUY needs EXECUTION_READY, score >=
     BUY_MIN_SCORE, no analytical reason, price in the entry range)
  9. BUY only: size with the fixed risk formula and ask the risk engine.
     A rejection turns the proposal into NO_TRADE. The veto is final.
"""

from datetime import datetime, timezone
from decimal import Decimal

from environment import Environment
from market_data.validation import build_market_data_check
from money import floor_int
from risk.risk_engine import EXECUTION_READY, RiskRequest, calculate_max_quantity
from strategy.entry_logic import (candidate_setups, policy_for, price_state, select_setup,
                                  setup_anchor)
from strategy.models import (AnalysisMode, PriceState, ProposedAction, Reason,
                             SetupType, TradeProposal)
from strategy.no_trade_rules import analytical_reasons, execution_blockers
from strategy.opportunity_score import classify_score, opportunity_score
from strategy.stop_logic import plan_stop
from strategy.target_logic import plan_targets

DEFAULT_SETUP_FOR_POLICY = SetupType.PULLBACK_TO_SUPPORT   # SWING policy when no setup


def setup_market_data_check(ctx, policy, cfg, now, environment=Environment.LIVE):
    """Rebuild the per-symbol check so only the setup's REQUIRED timeframes
    can block trading; optional timeframes are reported but never block."""
    smd = ctx.smd
    if smd is None or smd.contract_check is None:
        return None
    return build_market_data_check(smd.symbol, smd.contract_check, smd.quote_status,
                                   smd.timeframe_statuses, smd.calendar, now, cfg,
                                   required=policy.required, environment=environment)


def risk_review(symbol, contract, maximum_entry, stop_price, md_check, bot_snapshot,
                system_state, risk_engine, now, cfg):
    """The single hand-off from a BUY proposal to the risk engine.

    Quantity comes ONLY from the fixed sizing formula at the maximum entry
    (worst fill). Returns (RiskDecision, quantity). Used by evaluate() and by
    the backtester's portfolio replay, so both apply the identical review.
    """
    qty = 0
    if bot_snapshot is not None:
        *_, qty = calculate_max_quantity(bot_snapshot.bot_strategy_equity,
                                         bot_snapshot.bot_available_capital,
                                         maximum_entry, stop_price, cfg.MAX_RISK_PER_TRADE)
    req = RiskRequest(symbol=symbol, sec_type=contract.sec_type, currency=contract.currency,
                      exchange="SMART", primary_exchange=contract.primary_exchange, side="BUY",
                      quantity=max(qty, 1), entry_price=maximum_entry, stop_price=stop_price,
                      market_data=md_check, analysis_mode=EXECUTION_READY)
    return risk_engine.evaluate(req, bot_snapshot, system_state, now), qty


def decide_action(analytical, score_total, mode, pstate, cfg):
    """Pure decision table (before the risk engine)."""
    if analytical or score_total is None or score_total < cfg.SCORE_WATCH_MIN:
        return ProposedAction.NO_TRADE
    if (score_total < cfg.BUY_MIN_SCORE or mode is not AnalysisMode.EXECUTION_READY
            or pstate is not PriceState.IN_RANGE):
        return ProposedAction.WATCH
    return ProposedAction.BUY


def _confidence(score_total, missing_optional, score, cfg):
    if score_total is None:
        return "LOW"
    level = 2 if score_total >= cfg.SCORE_STRONG_MIN else 1 if score_total >= cfg.SCORE_CONDITIONAL_MIN else 0
    if missing_optional or (score and any(c.missing_optional for c in score.components)):
        level -= 1
    if score and any(c.missing_required for c in score.components):
        level = 0
    return ("LOW", "MEDIUM", "HIGH")[max(level, 0)]


def _describe(ctx, p, setup, cfg):
    d = ctx.daily
    s = d.structure if d else None
    if s is not None:
        flags = [f for f, on in (("consolidation", s.consolidation), ("compression", s.compression),
                                 ("breakout", s.breakout), ("failed breakout", s.failed_breakout),
                                 ("breakdown", s.breakdown), ("failed breakdown", s.failed_breakdown))
                 if on]
        p.market_structure = (f"1d {s.label} (trend {s.trend})"
                              + (f"; {', '.join(flags)}" if flags else "")
                              + (f"; {s.dist_ma20_atr:+.2f} ATR vs MA20" if s.dist_ma20_atr is not None else ""))
        reading = d.candles[-1] if d.candles else None
        if reading:
            p.candle_context = (f"{reading.bias} {reading.strength}: "
                                + ("; ".join(reading.reasons[:3]) or "no notable shape"))
        if d.pressure:
            rv = d.last_row.rvol if d.last_row else None
            p.volume_context = (f"buying {d.pressure.buying}, selling {d.pressure.selling}"
                                + (f", RVOL {rv:.2f}" if rv is not None else ""))
    rs = ctx.relative_strength
    p.relative_strength = (", ".join(f"{c.benchmark} {c.label} ({c.composite:+.2%})"
                                     for c in rs.comparisons) if rs and rs.comparisons
                           else "UNKNOWN")
    p.market_regime = f"{ctx.regime.label} ({'; '.join(ctx.regime.details[:1])})"
    if setup is not None:
        p.technical_thesis = f"{setup.setup_type.value}: " + "; ".join(setup.facts)

    risks = []
    if ctx.regime.label in ("CHOPPY", "DEFENSIVE"):
        risks.append(f"market regime {ctx.regime.label}")
    if rs and rs.primary and rs.primary.label == "UNDERPERFORMING":
        risks.append("underperforming SPY")
    if p.missing_optional:
        risks.append(f"optional timeframes not fresh: {', '.join(p.missing_optional)}")
    if s is not None and s.dist_resistance_atr is not None and s.dist_resistance_atr < 1:
        risks.append(f"resistance {s.dist_resistance_atr:.2f} ATR overhead")
    risks.append("fundamentals, news, earnings and catalysts NOT evaluated (deterministic phase)")
    p.risk_factors = risks


def _affordability(ctx, setup, stop, bot_snapshot, cfg):
    if bot_snapshot is None:
        return "BOT ledger unavailable: sizing unknown"
    price = ctx.current_price or (setup.entry if setup else None)
    avail = bot_snapshot.bot_available_capital
    if price is None:
        return ""
    if price > avail:
        return (f"AFFORDABILITY: 1 share at ${price:,.2f} exceeds BOT available capital "
                f"${avail:,.2f}; whole-share sizing impossible (the risk engine would reject).")
    by_cap = floor_int(avail / price)
    note = f"Sizing preview (not an order): up to {by_cap} whole share(s) by BOT capital"
    if setup is not None and stop is not None and stop.valid:
        *_, mx = calculate_max_quantity(bot_snapshot.bot_strategy_equity, avail,
                                        setup.maximum_entry, stop.stop, cfg.MAX_RISK_PER_TRADE)
        note += f", {mx} within the {cfg.MAX_RISK_PER_TRADE:.0%} risk rule at maximum entry"
    return note


def evaluate(ctx, cfg, system_state, bot_snapshot, risk_engine, now=None,
             environment=Environment.LIVE):
    """environment=BACKTEST is used only by the backtester (simulated as-of-T
    quotes). The risk engine applies its OWN environment independently, so a
    LIVE risk engine still rejects simulated data."""
    now = now or datetime.now(timezone.utc)
    environment = Environment(environment)
    candidates = candidate_setups(ctx, cfg)
    setup, ambiguous = select_setup(candidates)
    policy = policy_for(setup.setup_type if setup else DEFAULT_SETUP_FOR_POLICY, cfg)
    md_check = setup_market_data_check(ctx, policy, cfg, now, environment)
    stop = plan_stop(setup, ctx, cfg) if setup else None
    targets = plan_targets(setup, stop, ctx, cfg) if setup else None

    analytical = analytical_reasons(ctx, setup, ambiguous, stop, targets, policy, cfg)
    blockers, missing_optional = execution_blockers(ctx, policy, md_check, system_state, cfg,
                                                    environment)
    mode = AnalysisMode.EXECUTION_READY if not blockers else AnalysisMode.ANALYSIS_ONLY

    score = opportunity_score(ctx, setup, targets, policy, cfg)
    total = score.total if score else None
    pstate = price_state(setup, ctx.current_price)

    p = TradeProposal(
        ticker=ctx.symbol, analysis_mode=mode,
        strategy_type=setup.setup_type if setup else SetupType.NONE,
        market_data_check=md_check, timeframe_policy=policy,
        missing_optional=missing_optional, opportunity_score=total,
        setup_classification=classify_score(total, cfg), price_state=pstate,
        no_trade_reasons=list(analytical), execution_blockers=list(blockers),
        score_breakdown=score, expected_holding_period=cfg.HOLDING_PERIOD[policy.horizon],
        environment=environment.value)
    if ambiguous:
        p.notes.append("candidates: " + ", ".join(c.setup_type.value for c in candidates))
    if setup is not None:
        p.entry, p.maximum_entry = setup.entry, setup.maximum_entry
        p.entry_zone_low = setup.entry_zone_low
        p.setup_anchor = setup_anchor(setup, ctx)
    if stop is not None:
        p.stop, p.stop_type, p.invalidation = stop.stop, stop.stop_type, stop.invalidation
    if targets is not None:
        p.target_1, p.target_2 = targets.target_1, targets.target_2
        p.target_1_source, p.target_2_source = targets.target_1_source, targets.target_2_source
        p.reward_risk = targets.reward_risk

    p.proposed_action = decide_action(analytical, total, mode, pstate, cfg)
    if p.proposed_action is ProposedAction.WATCH and pstate is PriceState.WAIT_FOR_PULLBACK:
        p.notes.append("WAIT FOR PULLBACK: price is above the maximum entry - do not chase")
    if p.proposed_action is ProposedAction.WATCH and pstate is PriceState.AWAITING_TRIGGER:
        p.notes.append("AWAITING TRIGGER: price has not reached the entry level")
    p.confidence = _confidence(total, missing_optional, score, cfg)
    p.affordability_note = _affordability(ctx, setup, stop, bot_snapshot, cfg)
    _describe(ctx, p, setup, cfg)

    # --- risk engine: ONLY for a BUY, which is only possible when EXECUTION_READY
    if p.proposed_action is ProposedAction.BUY:
        if mode is not AnalysisMode.EXECUTION_READY:   # decide_action guarantees this
            raise RuntimeError("BUY outside EXECUTION_READY - refusing")
        decision, qty = risk_review(ctx.symbol, ctx.contract, setup.maximum_entry, stop.stop,
                                    md_check, bot_snapshot, system_state, risk_engine, now, cfg)
        p.risk_decision = decision
        if decision.approved:
            p.risk_status, p.risk_quantity, p.execution_eligible = "APPROVED", qty, True
        else:
            p.risk_status = "REJECTED"
            p.proposed_action = ProposedAction.NO_TRADE
            p.execution_eligible = False
            p.no_trade_reasons.append(Reason(
                "RISK_VETO", "; ".join(r.message for r in decision.rejections)))
    else:
        p.risk_status = ("NOT EVALUATED (analysis only)"
                         if mode is AnalysisMode.ANALYSIS_ONLY
                         else f"NOT EVALUATED (action {p.proposed_action.value})")
    return p
