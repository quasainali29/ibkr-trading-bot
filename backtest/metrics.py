"""
Backtest metrics - pure functions over TradeRecords / equity points.

Groups smaller than BACKTEST_MIN_SAMPLE are flagged INSUFFICIENT SAMPLE and
never named "best"/"worst". REAL and SHADOW trades, Tier 1 and Tier 2,
Variant A and B, and pre/post circuit-breaker shadows are always summarised
separately by the callers - nothing here mixes them.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import median

from backtest.models import (EXIT_STOP, EXIT_STOP_AMBIGUOUS, EXIT_STOP_GAP, EXIT_TARGET,
                             EXIT_TARGET_GAP)

D = Decimal
ZERO = D(0)
BAND_ORDER = ["0-49", "50-64", "65-74", "75-84", "85-100", "NO SCORE"]
SETUP_ORDER = ["BREAKOUT", "PULLBACK_TO_SUPPORT", "TREND_CONTINUATION",
               "REVERSAL_FROM_SUPPORT", "MEAN_REVERSION"]
STOP_EXITS = {EXIT_STOP, EXIT_STOP_GAP, EXIT_STOP_AMBIGUOUS}
TARGET_EXITS = {EXIT_TARGET, EXIT_TARGET_GAP}


def _mean(xs):
    xs = list(xs)
    return sum(xs, ZERO) / D(len(xs)) if xs else None


def summarize(trades, starting_capital=None, min_sample=30):
    trades = list(trades)
    n = len(trades)
    out = {"trades": n, "insufficient_sample": n < min_sample}
    if n == 0:
        return out
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl <= 0]
    gross_win = sum((t.realized_pnl for t in wins), ZERO)
    gross_loss = -sum((t.realized_pnl for t in losses), ZERO)
    rs = [t.r_multiple for t in trades]
    gross_profit = sum((t.gross_pnl for t in trades if t.gross_pnl > 0), ZERO)
    costs = sum((t.total_costs for t in trades), ZERO)
    out.update({
        "win_rate": D(len(wins)) / n,
        "loss_rate": D(len(losses)) / n,
        "average_win": _mean(t.realized_pnl for t in wins),
        "average_loss": _mean(t.realized_pnl for t in losses),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "profit_factor_infinite": gross_loss == 0 and gross_win > 0,
        "expectancy": _mean(t.realized_pnl for t in trades),
        "expectancy_r": _mean(rs),
        "average_r": _mean(rs),
        "median_r": D(median(rs)),
        "average_gross_r": _mean(t.gross_r for t in trades),
        "average_fill_r_diagnostic": _mean(t.fill_r_multiple for t in trades),
        "risk_invalid_trades": sum(1 for t in trades if t.risk_invalid),
        "stop_limit_entries": sum(1 for t in trades if t.entry_order_type == "STOP_LIMIT"),
        "target_1_hit_rate": D(sum(1 for t in trades if t.exit_reason in TARGET_EXITS)) / n,
        "stop_hit_rate": D(sum(1 for t in trades if t.exit_reason in STOP_EXITS)) / n,
        "time_exit_rate": D(sum(1 for t in trades if t.exit_reason == "TIME_EXIT")) / n,
        "target_2_touch_rate": D(sum(1 for t in trades if t.t2_touched)) / n,
        "ambiguous_bar_exits": sum(1 for t in trades if t.ambiguous_bar),
        "gap_fills": sum(1 for t in trades if t.gap_fill),
        "average_holding_sessions": _mean(D(t.holding_sessions) for t in trades),
        "average_holding_hours": _mean(t.holding_hours for t in trades),
        "average_mfe_r": _mean(t.mfe_r for t in trades),
        "average_mae_r": _mean(t.mae_r for t in trades),
        "gross_pnl": sum((t.gross_pnl for t in trades), ZERO),
        "commission_cost": sum((t.commission for t in trades), ZERO),
        "spread_cost": sum((t.spread_cost for t in trades), ZERO),
        "slippage_cost": sum((t.slippage_cost for t in trades), ZERO),
        "total_costs": costs,
        "net_pnl": sum((t.realized_pnl for t in trades), ZERO),
        "gross_profit": gross_profit,
        "costs_pct_gross_profit": (costs / gross_profit) if gross_profit > 0 else None,
    })
    if starting_capital:
        out["costs_pct_starting_capital"] = costs / starting_capital
    return out


def group(trades, keyfn, min_sample=30, order=None, starting_capital=None):
    buckets = defaultdict(list)
    for t in trades:
        buckets[keyfn(t)].append(t)
    keys = list(order or []) + sorted(k for k in buckets if k not in (order or []))
    return [(k, summarize(buckets.get(k, []), starting_capital, min_sample)) for k in keys
            if k in buckets or (order and k in order)]


def best_worst(groups, metric="expectancy_r"):
    """(best_key, worst_key) among groups with a sufficient sample."""
    eligible = [(k, s) for k, s in groups
                if s["trades"] and not s["insufficient_sample"] and s.get(metric) is not None]
    if not eligible:
        return None, None
    ranked = sorted(eligible, key=lambda ks: (ks[1][metric], str(ks[0])))
    return ranked[-1][0], ranked[0][0]


def max_drawdown(equity):
    """(max drawdown fraction, dollars, peak_ts, trough_ts) from equity points."""
    peak = None
    best = (ZERO, ZERO, None, None)
    for p in equity:
        if peak is None or p.equity > peak[0]:
            peak = (p.equity, p.ts)
        dd_d = peak[0] - p.equity
        dd = dd_d / peak[0] if peak[0] > 0 else ZERO
        if dd > best[0]:
            best = (dd, dd_d, peak[1], p.ts)
    return best


def portfolio_metrics(result):
    eq = result.equity
    cap = result.starting_capital
    end_equity = eq[-1].equity if eq else cap
    dd, dd_d, peak_ts, trough_ts = max_drawdown(eq)
    out = {"starting_capital": cap, "ending_equity": end_equity,
           "total_return": (end_equity - cap) / cap, "max_drawdown": dd,
           "max_drawdown_dollars": dd_d, "drawdown_peak": peak_ts, "drawdown_trough": trough_ts,
           "exposure_pct": result.exposure_pct, "breaker_ts": result.breaker_ts}
    days = (result.end - result.start).days
    if days >= 182 and end_equity > 0:
        out["annualized_return"] = D(str((float(end_equity / cap)) ** (365.0 / days) - 1))
    else:
        out["annualized_return"] = None
    months = max(D(days) / D("30.44"), D(1))
    out["trades_per_month"] = D(len(result.trades)) / months
    out["reentries_within_1_day"] = reentries(result.trades)
    return out


def reentries(trades, window=timedelta(days=1)):
    """Same ticker re-entered within `window` of a previous exit (overtrading)."""
    by = defaultdict(list)
    for t in trades:
        by[t.ticker].append(t)
    n = 0
    for lst in by.values():
        lst.sort(key=lambda t: t.entry_ts)
        for a, b in zip(lst, lst[1:]):
            if a.exit_ts and b.entry_ts - a.exit_ts <= window:
                n += 1
    return n


def drawdown_contribution(trades, peak_ts, trough_ts, keyfn=lambda t: t.setup_type):
    """Share of net losses realized inside the max-drawdown window, by key."""
    if peak_ts is None or trough_ts is None:
        return {}
    inside = [t for t in trades if t.exit_ts and peak_ts <= t.exit_ts <= trough_ts
              and t.realized_pnl < 0]
    total = -sum((t.realized_pnl for t in inside), ZERO)
    if total <= 0:
        return {}
    out = defaultdict(lambda: ZERO)
    for t in inside:
        out[keyfn(t)] += -t.realized_pnl
    return {k: v / total for k, v in out.items()}


def reason_breakdown(shadow, min_sample=30):
    """Shadow outcomes per NO TRADE reason present at signal time."""
    buckets = defaultdict(list)
    for t in shadow:
        for code in t.no_trade_codes or ("(none)",):
            buckets[code].append(t)
    return [(k, summarize(v, None, min_sample)) for k, v in sorted(buckets.items())]
