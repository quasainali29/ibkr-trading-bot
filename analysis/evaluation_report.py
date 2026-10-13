"""Console output for --evaluate (display only). The analysis mode is shown
in the header and repeated wherever an action or eligibility is printed."""

from analysis.formatting import LINE, THIN, fmt_price
from strategy.models import AnalysisMode, ProposedAction


def _p(d):
    return fmt_price(d) if d is not None else "-"


def _tf_line(ctx, key):
    v = ctx.timeframes.get(key)
    if v is None or v.status == "UNAVAILABLE":
        return f"UNAVAILABLE{f' ({v.note})' if v and v.note else ''}"
    s = v.structure
    base = f"trend {s.trend}; structure {s.label}" if s else "n/a"
    if v.status == "STALE":
        when = v.last_bar.astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M") if v.last_bar else "?"
        return f"STALE - last completed bar {when}; described only, NOT used for timing [{base}]"
    return base


def _zones(zones):
    if not zones:
        return "none identified"
    return "; ".join(f"{_p(z.low)}-{_p(z.high)} ({z.touches}x)" for z in zones)


def render_evaluation(ctx, p):
    mode = p.analysis_mode
    analysis_only = mode is AnalysisMode.ANALYSIS_ONLY
    tag = "   [ANALYSIS ONLY - NOT EXECUTABLE]" if analysis_only else ""
    d = ctx.daily
    s = d.structure if d else None
    out = [LINE, "TRADE EVALUATION (advisory; risk engine veto is final; no orders are placed)",
           LINE, f"ANALYSIS MODE: {mode.value}{tag}"
           + ("   [BACKTEST SIMULATION - simulated quotes]" if p.environment == "BACKTEST" else "")]
    if analysis_only:
        for b in p.execution_blockers:
            out.append(f"  - {b.message}")
    out += [
        "",
        f"Ticker:            {p.ticker}" + (f"   ({ctx.contract.long_name})" if ctx.contract else ""),
        f"Current price:     {_p(ctx.current_price)}   (source: {ctx.price_source})",
        f"Market regime:     {ctx.regime.label}   [{'; '.join(ctx.regime.details)}]",
        f"Daily trend:       {_tf_line(ctx, '1d')}",
        f"Hourly trend:      {_tf_line(ctx, '1h')}",
        f"15m trend:         {_tf_line(ctx, '15m')}",
        f"5m trend:          {_tf_line(ctx, '5m')}",
        f"1m trend:          {_tf_line(ctx, '1m')}",
        "",
        f"Support:           {_zones(s.supports if s else [])}",
        f"Resistance:        {_zones(s.resistances if s else [])}",
        "",
        f"Buyer pressure:    {d.pressure.buying if d and d.pressure else 'n/a'}",
        f"Seller pressure:   {d.pressure.selling if d and d.pressure else 'n/a'}",
        f"Breakout status:   {d.breakout.status if d and d.breakout else 'n/a'}",
        f"Relative strength: {p.relative_strength}",
        f"Candle context:    {p.candle_context or 'n/a'}",
        f"Market structure:  {p.market_structure or 'n/a'}",
        "",
    ]
    if p.score_breakdown:
        out.append(f"Opportunity score: {p.opportunity_score} / 100")
        for c in p.score_breakdown.components:
            miss = ""
            if c.missing_required:
                miss += f"  [missing REQUIRED: {', '.join(c.missing_required)}]"
            if c.missing_optional:
                miss += f"  [missing optional: {', '.join(c.missing_optional)}]"
            out.append(f"  {c.name:<28}{c.points:>5.1f} / {c.max_points:<3}{miss}")
    else:
        out.append("Opportunity score: not calculated (insufficient data)")
    out.append(f"Setup classification: {p.setup_classification.value}"
               f"   (strategy: {p.strategy_type.value}, confidence {p.confidence})")
    pol = p.timeframe_policy
    if pol:
        out.append(f"Timeframes for {pol.horizon}: required {', '.join(pol.required)}"
                   + (f"; preferred {', '.join(pol.preferred)}" if pol.preferred else "")
                   + (f"; optional {', '.join(pol.optional)}" if pol.optional else ""))

    out += ["", THIN, f"PROPOSED ACTION: {p.proposed_action.value.replace('_', ' ')}{tag}"]
    if p.proposed_action is not ProposedAction.NO_TRADE or p.entry is not None:
        out += [
            f"  Entry:           {_p(p.entry)}",
            f"  Maximum entry:   {_p(p.maximum_entry)}   (price state: {p.price_state.value})",
            f"  Stop:            {_p(p.stop)}   ({p.stop_type})",
            f"  Target 1:        {_p(p.target_1)}   ({p.target_1_source})",
            f"  Target 2:        {_p(p.target_2)}   ({p.target_2_source})",
            f"  Reward/Risk:     {p.reward_risk:.2f}" if p.reward_risk is not None else "  Reward/Risk:     -",
            f"  Holding period:  {p.expected_holding_period}",
            f"  Invalidation:    {p.invalidation or '-'}",
        ]
    if p.technical_thesis:
        out.append(f"  Thesis:          {p.technical_thesis}")
    for n in p.notes:
        out.append(f"  Note: {n}")
    if p.no_trade_reasons:
        out.append("  NO TRADE reasons:")
        out += [f"    - [{r.code}] {r.message}" for r in p.no_trade_reasons]
    if p.affordability_note:
        out.append(f"  {p.affordability_note}")
    if p.risk_factors:
        out.append("  Risk factors:")
        out += [f"    - {r}" for r in p.risk_factors]

    out += ["", f"EXECUTION ELIGIBLE: {'YES' if p.execution_eligible else 'NO'}{tag}"]
    if not p.execution_eligible:
        reasons = [b.message for b in p.execution_blockers]
        if p.proposed_action is not ProposedAction.BUY and not reasons:
            reasons.append(f"proposed action is {p.proposed_action.value.replace('_', ' ')}")
        reasons += [r.message for r in p.no_trade_reasons if r.code == "RISK_VETO"]
        out += [f"  - {r}" for r in reasons]
    out.append(f"RISK ENGINE: {p.risk_status}")
    if p.risk_decision is not None:
        for r in p.risk_decision.rejections:
            out.append(f"  - [{r.reason.value}] {r.message}")
        if p.risk_decision.approved:
            out.append(f"  quantity {p.risk_quantity}, planned risk "
                       f"{fmt_price(p.risk_decision.planned_risk)} "
                       f"(budget {fmt_price(p.risk_decision.risk_budget)}) - advisory; no order placed")
    out.append(LINE)
    return "\n".join(out)
