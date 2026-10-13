"""
Deterministic 0-100 opportunity score (long setups).

Components and maxima (config.SCORE_WEIGHTS):
  technical_structure 25, volume_confirmation 15, multi_timeframe_alignment 15,
  support_resistance_quality 10, relative_strength 10,
  market_regime_alignment 10, reward_risk 15

Missing inputs score 0 for their share and are reported as
missing_required / missing_optional. Points are NEVER redistributed to other
inputs. The score never overrides safety, data or risk rules.
"""

from decimal import Decimal

from analysis.relative_strength import rs_points
from strategy.no_trade_rules import trigger_row
from strategy.models import ScoreBreakdown, ScoreComponent, SetupClass

D = Decimal
REGIME_POINTS = {"RISK_ON": 10, "NEUTRAL": 6, "CHOPPY": 3, "DEFENSIVE": 2,
                 "RISK_OFF": 0, "UNKNOWN": 0}


def agreement(view):
    """1 bullish, 0.5 mixed/sideways, 0 bearish; None if not FRESH."""
    if view is None or not view.fresh or view.structure is None:
        return None
    s = view.structure
    if s.bullish or s.trend == "UPTREND":
        return D(1)
    if s.bearish or s.trend == "DOWNTREND":
        return D(0)
    return D("0.5")


def _weighted(ctx, keys, cfg):
    """(weighted agreement fraction over ALL keys, missing keys). Missing keys
    contribute 0 - the denominator always includes them (no redistribution)."""
    total = sum((cfg.TIMEFRAME_WEIGHTS[k] for k in keys), D(0))
    got, missing = D(0), []
    for k in keys:
        a = agreement(ctx.timeframes.get(k))
        if a is None:
            missing.append(k)
        else:
            got += cfg.TIMEFRAME_WEIGHTS[k] * a
    return (got / total if total else D(0)), missing


def _split_missing(keys, policy):
    req = [k for k in keys if k in policy.required]
    opt = [k for k in keys if k not in policy.required]
    return req, opt


def score_structure(ctx, setup, policy, cfg):
    c = ScoreComponent("technical_structure", D(0), cfg.SCORE_WEIGHTS["technical_structure"])
    d = ctx.daily
    s = d.structure
    if s.bullish:
        c.points += 10
        c.reasons.append("daily HH/HL structure (+10)")
    elif s.label == "MIXED":
        c.points += 3
        c.reasons.append("daily structure mixed (+3)")
    bo = d.breakout.status if d.breakout else "NOT_PRESENT"
    if bo == "CONFIRMED":
        c.points += 8
        c.reasons.append("confirmed daily breakout (+8)")
    elif s.continuation and s.bullish:
        c.points += 8
        c.reasons.append("uptrend continuation (+8)")
    elif bo == "WEAK":
        c.points += 4
        c.reasons.append("weak daily breakout (+4)")
    intraday = [k for k in policy.all if k != "1d"]
    if intraday:
        frac, missing = _weighted(ctx, intraday, cfg)
        pts = D(7) * frac
        c.points += pts
        c.reasons.append(f"intraday structure agreement {frac:.0%} (+{pts:.1f})")
        c.missing_required, c.missing_optional = _split_missing(missing, policy)
    return c


def score_volume(ctx, setup, cfg):
    c = ScoreComponent("volume_confirmation", D(0), cfg.SCORE_WEIGHTS["volume_confirmation"])
    d = ctx.daily
    row = trigger_row(ctx, setup)
    rvol = row.rvol if row else None
    if rvol is None:
        c.missing_required.append("RVOL")
        c.reasons.append("RVOL unavailable (+0)")
    else:
        pts = 6 if rvol >= D("1.5") else 4 if rvol >= 1 else 2 if rvol >= D("0.8") else 0
        c.points += pts
        c.reasons.append(f"trigger RVOL {rvol:.2f} (+{pts})")
    p = d.pressure
    if p is not None:
        pts = {"STRONG": 5, "MODERATE": 3}.get(p.buying, 0)
        c.points += pts
        c.reasons.append(f"buying pressure {p.buying} (+{pts})")
    if setup is not None and setup.setup_type.value in ("BREAKOUT", "TREND_CONTINUATION"):
        bo = d.breakout.status if d.breakout else "NOT_PRESENT"
        pts = {"CONFIRMED": 4, "WEAK": 1}.get(bo, 0)
        c.reasons.append(f"breakout volume quality {bo} (+{pts})")
    else:
        pts = {"WEAK": 4, "MODERATE": 2}.get(p.selling if p else "STRONG", 0)
        c.reasons.append(f"selling pressure {p.selling if p else 'n/a'} (+{pts})")
    c.points += pts
    return c


def score_alignment(ctx, policy, cfg):
    c = ScoreComponent("multi_timeframe_alignment", D(0),
                       cfg.SCORE_WEIGHTS["multi_timeframe_alignment"])
    keys = list(dict.fromkeys(("1d",) + policy.all))
    frac, missing = _weighted(ctx, keys, cfg)
    c.points = D(c.max_points) * frac
    c.reasons.append(f"weighted alignment {frac:.0%} over {', '.join(keys)}")
    c.missing_required, c.missing_optional = _split_missing(missing, policy)
    if missing:
        c.reasons.append(f"not fresh (0 points, not redistributed): {', '.join(missing)}")
    return c


def score_sr(ctx, setup, cfg):
    c = ScoreComponent("support_resistance_quality", D(0),
                       cfg.SCORE_WEIGHTS["support_resistance_quality"])
    d = ctx.daily
    zone = (setup.reference_zone if setup and setup.reference_zone is not None
            else (d.structure.supports[0] if d.structure.supports else None))
    if zone is None:
        c.reasons.append("no defining zone (+0)")
        return c
    pts = 5 if zone.touches >= 3 else 3 if zone.touches == 2 else 1
    c.points += pts
    c.reasons.append(f"zone {zone.low}-{zone.high}: {zone.touches} touches (+{pts})")
    rows = d.features.rows
    recent = rows[-20].bar.timestamp if len(rows) >= 20 else rows[0].bar.timestamp
    if zone.last_touch >= recent:
        c.points += 2
        c.reasons.append("touched within last 20 sessions (+2)")
    if setup is not None and ctx.atr:
        dist = abs(setup.entry - zone.high) / ctx.atr
        pts = 3 if dist <= cfg.ZONE_PROXIMITY_ATR else 1 if dist <= 1 else 0
        c.points += pts
        c.reasons.append(f"entry {dist:.2f} ATR from zone (+{pts})")
    return c


def score_rs(ctx, cfg):
    c = ScoreComponent("relative_strength", D(0), cfg.SCORE_WEIGHTS["relative_strength"])
    rs = ctx.relative_strength
    if rs is None or rs.primary is None:
        c.missing_required.append("SPY")
        c.reasons.append("relative strength vs SPY unavailable (+0)")
        return c
    c.points = rs_points(rs)
    c.reasons.append(", ".join(f"{x.benchmark} {x.label} ({x.composite:+.2%})"
                               for x in rs.comparisons))
    c.missing_optional = list(rs.missing)
    return c


def score_regime(ctx, cfg):
    c = ScoreComponent("market_regime_alignment", D(0), cfg.SCORE_WEIGHTS["market_regime_alignment"])
    c.points = D(REGIME_POINTS.get(ctx.regime.label, 0))
    c.reasons.append(f"regime {ctx.regime.label}")
    if ctx.regime.label == "UNKNOWN":
        c.missing_required.append("SPY")
    return c


def score_rr(targets, cfg):
    c = ScoreComponent("reward_risk", D(0), cfg.SCORE_WEIGHTS["reward_risk"])
    rr = targets.reward_risk if targets else None
    if rr is None:
        c.reasons.append("no reward/risk (+0)")
        return c
    pts = 15 if rr >= 3 else 12 if rr >= D("2.5") else 9 if rr >= 2 else 4 if rr >= D("1.5") else 0
    c.points = D(pts)
    c.reasons.append(f"R:R {rr:.2f} to Target 1 (+{pts})")
    return c


def opportunity_score(ctx, setup, targets, policy, cfg):
    """ScoreBreakdown, or None when daily data is not usable for analysis."""
    d = ctx.daily
    if d is None or not d.fresh or d.structure is None or ctx.atr is None:
        return None
    comps = [score_structure(ctx, setup, policy, cfg), score_volume(ctx, setup, cfg),
             score_alignment(ctx, policy, cfg), score_sr(ctx, setup, cfg),
             score_rs(ctx, cfg), score_regime(ctx, cfg), score_rr(targets, cfg)]
    for comp in comps:
        comp.points = min(comp.points, D(comp.max_points))
    return ScoreBreakdown(comps)


def classify_score(total, cfg):
    if total is None or total < cfg.SCORE_WATCH_MIN:
        return SetupClass.NO_TRADE
    if total < cfg.SCORE_CONDITIONAL_MIN:
        return SetupClass.WATCH
    if total < cfg.SCORE_STRONG_MIN:
        return SetupClass.CONDITIONAL_SETUP
    if total < cfg.SCORE_EXCEPTIONAL_MIN:
        return SetupClass.STRONG_SETUP
    return SetupClass.EXCEPTIONAL_SETUP
