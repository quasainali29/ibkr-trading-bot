"""
Backtest reports (console) and exports (CSV / JSON) - SIMULATION RESULTS.

Every run (tier x cadence x exit variant) is reported on its own. Real
portfolio trades, pre-breaker shadows and post-breaker shadows are separate
sections. Tier 1 and Tier 2 statistics are never combined.
"""

import csv
import hashlib
import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path

from backtest import metrics as M
from backtest.models import POST_BREAKER, TradeRecord

W = 110
LINE = "=" * W
THIN = "-" * W
SURVIVORSHIP = ("WARNING - SURVIVORSHIP BIAS: the universe was chosen with hindsight and is "
                "dominated by recent winners;\n  long-only results are likely OPTIMISTIC. "
                "Control names (INTC, PFE, BA, DIS, NKE) only partly offset this.")
SIM_NOTE = ("Quotes are SIMULATED from completed bars (simulated=True, source=BACKTEST). "
            "No orders were placed. Simulation only.")


def _f(d, places=2, pct=False, money=False):
    if d is None:
        return "-"
    if isinstance(d, bool):
        return "yes" if d else "no"
    if not isinstance(d, Decimal):
        return str(d)
    if pct:
        return f"{(d * 100).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)}%"
    q = d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    return f"${q:,}" if money else f"{q}"


def _pf(s):
    if s.get("profit_factor_infinite"):
        return "inf"
    return _f(s.get("profit_factor"))


def run_name(spec):
    return f"tier{spec.tier}_{spec.cadence}_variant{spec.variant}"


def table(title, groups, capital=None):
    head = (f"  {'GROUP':<26}{'N':>5}{'WIN%':>8}{'PF':>7}{'EXP $':>10}{'EXP R':>8}"
            f"{'MED R':>8}{'T1%':>7}{'STOP%':>7}{'HOLD':>7}{'NET $':>11}")
    out = [title, head]
    for key, s in groups:
        if not s["trades"]:
            out.append(f"  {str(key):<26}{0:>5}  (no trades)")
            continue
        flag = "  *small" if s["insufficient_sample"] else ""
        out.append(f"  {str(key)[:25]:<26}{s['trades']:>5}{_f(s['win_rate'], 1, pct=True):>8}"
                   f"{_pf(s):>7}{_f(s['expectancy']):>10}{_f(s['expectancy_r']):>8}"
                   f"{_f(s['median_r']):>8}{_f(s['target_1_hit_rate'], 0, pct=True):>7}"
                   f"{_f(s['stop_hit_rate'], 0, pct=True):>7}"
                   f"{_f(s['average_holding_sessions'], 1):>7}{_f(s['net_pnl']):>11}{flag}")
    return out


def cost_lines(s, capital):
    if not s.get("trades"):
        return ["  (no trades)"]
    return [
        f"  Gross P/L:        {_f(s['gross_pnl'], money=True)}",
        f"  Commission cost:  {_f(s['commission_cost'], money=True)}",
        f"  Spread cost:      {_f(s['spread_cost'], money=True)}",
        f"  Slippage cost:    {_f(s['slippage_cost'], money=True)}",
        f"  Net P/L:          {_f(s['net_pnl'], money=True)}",
        f"  Total costs:      {_f(s['total_costs'], money=True)}  = "
        f"{_f(s.get('costs_pct_gross_profit'), 1, pct=True)} of gross profit, "
        f"{_f(s.get('costs_pct_starting_capital'), 1, pct=True)} of starting capital",
    ]


def render_run(r, min_sample):
    spec = r.spec
    cap = r.starting_capital
    out = [LINE, f"BACKTEST - TIER {spec.tier} | cadence {spec.cadence} | exit variant {spec.variant}"
           f" ({'T1 + stop + data-end' if spec.variant == 'A' else 'T1 + stop + time exit + data-end'})",
           f"{r.start} to {r.end} | {len(r.symbols)} symbols | {r.decision_times} decision times",
           SIM_NOTE, SURVIVORSHIP, LINE]

    if spec.portfolio:
        s = M.summarize(r.trades, cap, min_sample)
        pm = M.portfolio_metrics(r)
        by_setup = M.group(r.trades, lambda t: t.setup_type, min_sample, M.SETUP_ORDER, cap)
        by_band = M.group(r.trades, lambda t: t.score_band, min_sample, M.BAND_ORDER, cap)
        bs, ws = M.best_worst(by_setup)
        bb, wb = M.best_worst(by_band)
        out += ["BACKTEST SUMMARY  (live-policy portfolio, $800, real simulated trades)",
                f"  Portfolio return:  {_f(pm['total_return'], 2, pct=True)}   "
                f"(ending equity {_f(pm['ending_equity'], money=True)}; annualized "
                f"{_f(pm['annualized_return'], 1, pct=True)})",
                f"  Maximum drawdown:  {_f(pm['max_drawdown'], 2, pct=True)} "
                f"({_f(pm['max_drawdown_dollars'], money=True)})",
                f"  Trades:            {s['trades']}",
                f"  Win rate:          {_f(s.get('win_rate'), 1, pct=True)}",
                f"  Profit factor:     {_pf(s) if s['trades'] else '-'}",
                f"  Expectancy:        {_f(s.get('expectancy'), money=True)} per trade "
                f"({_f(s.get('expectancy_r'))} R)",
                f"  Average R:         {_f(s.get('average_r'))}   median {_f(s.get('median_r'))}",
                f"  Best setup:        {bs or 'insufficient sample'}",
                f"  Worst setup:       {ws or 'insufficient sample'}",
                f"  Best score band:   {bb or 'insufficient sample'}",
                f"  Worst score band:  {wb or 'insufficient sample'}",
                f"  Exposure:          {_f(pm['exposure_pct'], 1, pct=True)} of decision times",
                f"  Trades / month:    {_f(pm['trades_per_month'], 1)}   re-entries within 1 day: "
                f"{pm['reentries_within_1_day']}",
                f"  Circuit breaker:   "
                f"{pm['breaker_ts'].isoformat() if pm['breaker_ts'] else 'not triggered'}",
                f"  Candidates: {r.candidates}  risk-rejected: {r.rejected_by_risk}  "
                f"already in position: {r.skipped_in_position}  cancelled entries: "
                f"{r.cancelled_entries}",
                ""]
        if r.risk_rejection_reasons:
            out.append("  Risk-engine rejections: " + ", ".join(
                f"{k} x{v}" for k, v in sorted(r.risk_rejection_reasons.items())))
        out += ["", "COSTS (real trades)"] + cost_lines(s, cap) + [""]
        out += table("BY SETUP TYPE (real)", by_setup) + [""]
        contrib = M.drawdown_contribution(r.trades, pm["drawdown_peak"], pm["drawdown_trough"])
        if contrib:
            out.append("  Max-drawdown loss contribution: " + ", ".join(
                f"{k} {_f(v, 0, pct=True)}" for k, v in sorted(contrib.items())))
        out += [""] + table("BY SCORE BAND (real)", by_band) + [""]
        out += table("BY REGIME (real)", M.group(r.trades, lambda t: t.market_regime, min_sample)) + [""]
        out += table("BY HORIZON (real)", M.group(r.trades, lambda t: t.horizon, min_sample)) + [""]
        out += table("BY SECTOR (real)", M.group(r.trades, lambda t: t.sector or "n/a", min_sample)) + [""]
        out += table("BY TICKER (real)", M.group(r.trades, lambda t: t.ticker, min_sample)) + [""]
        out += table("BY EXIT REASON (real)", M.group(r.trades, lambda t: t.exit_reason, min_sample))
    else:
        out += ["(no live-policy portfolio for this tier - research / shadow trades only)"]

    pre = [t for t in r.shadow if t.label != POST_BREAKER]
    post = [t for t in r.shadow if t.label == POST_BREAKER]
    title = "SHADOW RESEARCH TRADES" + (" - PRE-BREAKER / no breaker" if spec.portfolio else "")
    out += ["", THIN, title + "  (never affect portfolio cash or equity)", THIN]
    if r.shadow_dedupe:
        k = r.shadow_dedupe
        out += [f"  One shadow trade per setup instance: {k.get('instances', 0)} instances; "
                f"{k.get('suppressed_signals', 0)} repeat evaluations of an already-traded "
                f"instance suppressed; resets - new structure {k.get('resets_new_structure', 0)}, "
                f"condition gone a full session {k.get('resets_condition_gone', 0)}"]
    out += _shadow_sections(pre, min_sample)
    if post:
        out += ["", THIN, "SHADOW TRADES AFTER THE CIRCUIT BREAKER (POST_CIRCUIT_BREAKER)", THIN]
        out += _shadow_sections(post, min_sample)
    out.append(LINE)
    return "\n".join(out)


def _shadow_sections(shadow, min_sample):
    s = M.summarize(shadow, None, min_sample)
    out = [f"  Shadow trades: {s['trades']}   win rate {_f(s.get('win_rate'), 1, pct=True)}   "
           f"expectancy {_f(s.get('expectancy_r'))} R   avg gross R {_f(s.get('average_gross_r'))}",
           f"  (unaffordable at $800 - sized as 1 share: "
           f"{sum(1 for t in shadow if t.unaffordable)})", ""]
    out += table("SCORE VALIDATION - BY SCORE BAND (shadow)",
                 M.group(shadow, lambda t: t.score_band, min_sample, M.BAND_ORDER)) + [""]
    out += table("SETUP VALIDATION - BY SETUP TYPE (shadow)",
                 M.group(shadow, lambda t: t.setup_type, min_sample, M.SETUP_ORDER)) + [""]
    out += table("BY ACTION AT SIGNAL (shadow)", M.group(shadow, lambda t: t.action, min_sample)) + [""]
    out += table("BY MODE AT SIGNAL (shadow)", M.group(shadow, lambda t: t.mode, min_sample)) + [""]
    out += table("BY NO-TRADE REASON PRESENT AT SIGNAL (shadow)", M.reason_breakdown(shadow, min_sample))
    return out


def render_cadence_comparison(results):
    rows = [r for r in results if r.spec.portfolio]
    if len({r.spec.cadence for r in rows}) < 2:
        return ""
    out = [LINE, "CADENCE SENSITIVITY (Tier 1 portfolio; each variant compared separately)",
           f"  {'RUN':<28}{'TRADES':>8}{'EXP R':>9}{'EXP $':>10}{'MAX DD':>9}{'TR/MONTH':>10}"
           f"{'RE-ENTRY':>10}{'RETURN':>9}"]
    for r in rows:
        s = M.summarize(r.trades)
        pm = M.portfolio_metrics(r)
        out.append(f"  {run_name(r.spec):<28}{s['trades']:>8}{_f(s.get('expectancy_r')):>9}"
                   f"{_f(s.get('expectancy')):>10}{_f(pm['max_drawdown'], 1, pct=True):>9}"
                   f"{_f(pm['trades_per_month'], 1):>10}{pm['reentries_within_1_day']:>10}"
                   f"{_f(pm['total_return'], 1, pct=True):>9}")
    out.append(LINE)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _plain(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (tuple, list)):
        return "|".join(str(x) for x in v)
    return v


def _json_default(v):
    if isinstance(v, (Decimal, datetime, Enum)):
        return _plain(v)
    return str(v)


def write_trades(path, trades):
    cols = [f.name for f in fields(TradeRecord)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in trades:
            w.writerow([_plain(getattr(t, c)) for c in cols])


def run_id(manifest):
    blob = json.dumps(manifest, sort_keys=True, default=_json_default).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def export(results, manifest, root, min_sample):
    rid = run_id(manifest)
    out_dir = Path(root) / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{rid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_manifest.json").write_text(
        json.dumps({**manifest, "run_id": rid}, indent=2, default=_json_default), encoding="utf-8")
    for r in results:
        d = out_dir / run_name(r.spec)
        d.mkdir(exist_ok=True)
        write_trades(d / "trades.csv", r.trades)
        write_trades(d / "shadow_trades.csv", r.shadow)
        with open(d / "equity_curve.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "cash", "equity", "peak", "drawdown_pct", "open_positions"])
            for p in r.equity:
                w.writerow([_plain(p.ts), p.cash, p.equity, p.peak, p.drawdown_pct,
                            p.open_positions])
        cap = r.starting_capital
        pre = [t for t in r.shadow if t.label != POST_BREAKER]
        post = [t for t in r.shadow if t.label == POST_BREAKER]
        data = {
            "spec": asdict(r.spec), "symbols": r.symbols, "start": r.start, "end": r.end,
            "real": M.summarize(r.trades, cap, min_sample) if r.spec.portfolio else None,
            "portfolio": M.portfolio_metrics(r) if r.spec.portfolio else None,
            "real_by_setup": M.group(r.trades, lambda t: t.setup_type, min_sample),
            "real_by_score_band": M.group(r.trades, lambda t: t.score_band, min_sample),
            "real_by_regime": M.group(r.trades, lambda t: t.market_regime, min_sample),
            "real_by_ticker": M.group(r.trades, lambda t: t.ticker, min_sample),
            "real_by_sector": M.group(r.trades, lambda t: t.sector, min_sample),
            "real_by_horizon": M.group(r.trades, lambda t: t.horizon, min_sample),
            "shadow_pre_breaker": M.summarize(pre, None, min_sample),
            "shadow_pre_by_score_band": M.group(pre, lambda t: t.score_band, min_sample),
            "shadow_pre_by_setup": M.group(pre, lambda t: t.setup_type, min_sample),
            "shadow_pre_by_no_trade_reason": M.reason_breakdown(pre, min_sample),
            "shadow_post_breaker": M.summarize(post, None, min_sample),
            "shadow_post_by_score_band": M.group(post, lambda t: t.score_band, min_sample),
            "breaker_ts": r.breaker_ts, "notes": r.notes, "signal_counts": r.signal_counts,
            "shadow_dedupe": r.shadow_dedupe,
            "risk_rejection_reasons": r.risk_rejection_reasons,
            "cancelled_entries": r.cancelled_entries, "candidates": r.candidates,
        }
        (d / "metrics.json").write_text(json.dumps(data, indent=2, default=_json_default),
                                        encoding="utf-8")
    return out_dir, rid
