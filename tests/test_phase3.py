"""Phase 3: market interpretation + advisory proposal engine."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from analysis import candle_interpretation as CI
from analysis.market_structure import (DOWN, MIXED, UP, StructureResult,
                                       classify_swings, is_compression,
                                       is_consolidation)
from analysis.regime import classify_regime
from analysis.relative_strength import benchmarks_for, compare, sector_etf_for
from analysis.setup_context import build_context, view_from_bars
from analysis.volume_analysis import classify_pressure
from conftest import make_bar, make_cfg, make_info, snapshot
from market_data.features import compute_features
from market_data.models import TIMEFRAMES
from p3_helpers import (ET, NOW, bars_from_closes, bench, make_smd, range_then,
                        zigzag)
from risk.risk_engine import Reason as RR
from risk.risk_engine import RiskEngine, RiskRequest
from risk.system_state import SystemState
from strategy import proposal_engine as PE
from strategy.models import (AnalysisMode, PriceState, ProposedAction, Setup,
                             SetupClass, SetupType, StopPlan, TargetPlan)
from strategy.no_trade_rules import analytical_reasons
from strategy.opportunity_score import classify_score, score_alignment, score_rr

D = Decimal
CFG = make_cfg()
TF1D = TIMEFRAMES["1d"]


def view(closes, volumes=None, regime="RISK_ON", key="1d"):
    return view_from_bars(key, bars_from_closes(closes, volumes=volumes), "FRESH", ET,
                          regime, CFG)


# ==========================================================================
# 1. Market structure
# ==========================================================================
class TestStructure:
    def test_hh_hl(self):
        s = view(zigzag(100, 12)).structure
        assert s.label == UP and s.higher_high and s.higher_low

    def test_lh_ll(self):
        s = view(zigzag(200, 12, up_len=4, down_len=5)).structure
        assert s.label == DOWN and s.lower_high and s.lower_low

    @pytest.mark.parametrize("highs,lows,label", [
        ([D(1), D(2)], [D(0), D(1)], UP),
        ([D(2), D(1)], [D(1), D(0)], DOWN),
        ([D(2), D(3)], [D(1), D(0)], MIXED),          # higher high, lower low
        ([D(2)], [D(1), D(2)], "INSUFFICIENT"),
    ])
    def test_classify_swings(self, highs, lows, label):
        assert classify_swings(highs, lows)[0] == label

    def test_consolidation(self):
        tight = bars_from_closes([D(100) + (D("0.3") if i % 2 else 0) for i in range(30)])
        ok, hi, lo = is_consolidation(tight, D(1), CFG)
        assert ok and hi - lo <= D("2.5")
        trending = bars_from_closes([100 + i for i in range(30)])
        assert not is_consolidation(trending, D(1), CFG)[0]

    def test_compression(self):
        wide = [D(100) + (D(4) if i % 2 else 0) for i in range(20)]
        narrow = [D(102) + (D("0.2") if i % 2 else 0) for i in range(5)]
        assert is_compression(bars_from_closes(wide + narrow, spread=D("0.1")), CFG)
        assert not is_compression(bars_from_closes(wide + wide[:5], spread=D("0.1")), CFG)

    def test_distances_are_atr_normalized(self):
        s = view(zigzag(100, 12)).structure
        assert s.atr > 0
        assert s.dist_ma20_atr == (s.close - s.ma20) / s.atr


# ==========================================================================
# 5. Breakout quality
# ==========================================================================
BASE = range_then(D(100), D(110), 8, [])


class TestBreakouts:
    def test_confirmed(self):
        vols = [1000] * len(BASE) + [1000, 3500, 1500]
        v = view(BASE + [D(108), D(113), D(115)], volumes=vols)
        assert v.structure.breakout and v.breakout.status == "CONFIRMED"
        assert v.breakout.trigger_row.rvol == D("3.5")

    def test_weak_without_volume(self):
        v = view(BASE + [D(108), D(113), D(115)])
        assert v.breakout.status == "WEAK"
        assert any("volume weak" in r for r in v.breakout.reasons)

    def test_failed(self):
        v = view(BASE + [D(108), D(112), D(109)])
        assert v.structure.failed_breakout and v.breakout.status == "FAILED"

    def test_not_present(self):
        assert view(BASE + [D(104), D(105), D(104)]).breakout.status == "NOT_PRESENT"


# ==========================================================================
# 3. Candles in context
# ==========================================================================
def _rows(bars):
    return list(compute_features(bars, TF1D, ET, CFG).rows)


def _hammer_series(volume_last=1000):
    """25 flat bars then a lower-wick rejection candle at ~99."""
    bars = bars_from_closes([D(100)] * 25)
    t = bars[-1].timestamp + timedelta(days=1)
    bars.append(make_bar(t, D("99.6"), D("100"), D("97"), D("99.8"), volume_last))
    return bars


def _structure(label=MIXED, trend="SIDEWAYS / MIXED", supports=(), resistances=()):
    return StructureResult("1d", label, trend, supports=list(supports),
                           resistances=list(resistances))


class TestCandleContext:
    zone = NS(low=D("96.8"), high=D("97.5"), touches=3)

    def test_shape_alone_is_neutral(self):
        rows = _rows(_hammer_series())
        r = CI.interpret_candle(rows, len(rows) - 1, _structure(), "NEUTRAL", CFG)
        assert CI.REJECTION_LOW in r.shapes
        assert r.bias == "NEUTRAL"
        assert any("without supporting context" in x for x in r.reasons)

    def test_same_shape_at_support_in_uptrend_with_volume_is_bullish(self):
        rows = _rows(_hammer_series(volume_last=3000))
        s = _structure(UP, "UPTREND", supports=[self.zone])
        r = CI.interpret_candle(rows, len(rows) - 1, s, "RISK_ON", CFG)
        assert r.bias == "BULLISH" and r.strength == "STRONG"

    def test_same_shape_in_downtrend_mid_range_not_bullish(self):
        rows = _rows(_hammer_series())
        r = CI.interpret_candle(rows, len(rows) - 1, _structure(DOWN, "DOWNTREND"),
                                "NEUTRAL", CFG)
        assert r.bias != "BULLISH"

    def test_risk_off_discounts_bullish(self):
        rows = _rows(_hammer_series(volume_last=3000))
        s = _structure(UP, "UPTREND", supports=[self.zone])
        on = CI.interpret_candle(rows, len(rows) - 1, s, "RISK_ON", CFG)
        off = CI.interpret_candle(rows, len(rows) - 1, s, "RISK_OFF", CFG)
        assert off.bull_points < on.bull_points

    def test_engulfing_inside_outside_shapes(self):
        t = bars_from_closes([D(100)] * 25)
        ts = t[-1].timestamp
        down = make_bar(ts + timedelta(days=1), 101, D("101.2"), D("99.8"), 100)
        engulf = make_bar(ts + timedelta(days=2), D("99.9"), D("102.5"), D("99.5"), D("101.5"))
        inside = make_bar(ts + timedelta(days=3), D("101"), D("102"), D("100"), D("101.2"))
        rows = _rows(t + [down, engulf, inside])
        assert CI.ENGULF_BULL in CI.detect_shapes(rows, len(rows) - 2, CFG)
        assert CI.OUTSIDE in CI.detect_shapes(rows, len(rows) - 2, CFG)
        assert CI.INSIDE in CI.detect_shapes(rows, len(rows) - 1, CFG)

    def test_indecision(self):
        bars = bars_from_closes([D(100)] * 25)
        bars.append(make_bar(bars[-1].timestamp + timedelta(days=1), 100, 101, 99, D("100.05")))
        rows = _rows(bars)
        assert CI.INDECISION in CI.detect_shapes(rows, len(rows) - 1, CFG)


# ==========================================================================
# 4. Pressure
# ==========================================================================
class TestPressure:
    def test_buying_pressure(self):
        rows = _rows(bars_from_closes([100 + 2 * i for i in range(30)], spread=D("0.1"),
                                      volumes=[1000] * 25 + [2500] * 5))
        p = classify_pressure(rows, None, CFG)
        assert p.buying == "STRONG" and p.selling == "WEAK"

    def test_selling_pressure(self):
        rows = _rows(bars_from_closes([200 - 2 * i for i in range(30)], spread=D("0.1")))
        p = classify_pressure(rows, None, CFG)
        assert p.selling == "STRONG" and p.buying == "WEAK"

    def test_dojis_are_weak_both_sides(self):
        bars = bars_from_closes([D(100)] * 30, spread=D("0.5"))
        p = classify_pressure(_rows(bars), None, CFG)
        assert p.buying != "STRONG" and p.selling != "STRONG"


# ==========================================================================
# 6. Relative strength / 7. Regime
# ==========================================================================
class TestRelativeStrength:
    bench_bars = bars_from_closes([100 + D("0.1") * i for i in range(40)])

    @pytest.mark.parametrize("step,label", [(D(1), "OUTPERFORMING"),
                                            (D("0.1"), "NEUTRAL"),
                                            (D("-0.5"), "UNDERPERFORMING")])
    def test_labels(self, step, label):
        stock = bars_from_closes([100 + step * i for i in range(40)])
        assert compare(stock, self.bench_bars, "SPY", ET, CFG).label == label

    def test_needs_20_sessions(self):
        short = bars_from_closes([100] * 15)
        assert compare(short, self.bench_bars, "SPY", ET, CFG) is None

    def test_benchmarks(self):
        tech = make_info(symbol="AAPL", primary="NASDAQ", industry="Technology")
        assert benchmarks_for(tech, CFG) == ["SPY", "QQQ", "XLK"]
        bank = make_info(symbol="JPM", primary="NYSE", industry="Financial")
        assert benchmarks_for(bank, CFG) == ["SPY", "XLF"]
        etf = make_info(symbol="SPY", stock_type="ETF", primary="ARCA")
        assert benchmarks_for(etf, CFG) == []
        reit = make_info(symbol="O", stock_type="REIT", industry="Financial", category="REITS")
        assert sector_etf_for(reit, CFG) == "XLRE"


class TestRegime:
    up = bars_from_closes([100 + i for i in range(80)])
    down = bars_from_closes([200 - i for i in range(80)])

    def test_risk_on(self):
        assert classify_regime(self.up, self.up, ET, CFG).label == "RISK_ON"

    def test_risk_off(self):
        assert classify_regime(self.down, self.down, ET, CFG).label == "RISK_OFF"

    def test_choppy(self):
        chop = bars_from_closes([100 + (3 if (i // 2) % 2 else -3) for i in range(80)])
        assert classify_regime(chop, chop, ET, CFG).label == "CHOPPY"

    def test_unknown_without_spy(self):
        assert classify_regime(None, self.up, ET, CFG).label == "UNKNOWN"

    def test_spy_only_is_noted(self):
        r = classify_regime(self.up, None, ET, CFG)
        assert r.label == "RISK_ON" and any("QQQ missing" in d for d in r.details)


# ==========================================================================
# 8. Opportunity score
# ==========================================================================
class TestScore:
    @pytest.mark.parametrize("total,cls", [
        (None, SetupClass.NO_TRADE), (49, SetupClass.NO_TRADE), (50, SetupClass.WATCH),
        (64, SetupClass.WATCH), (65, SetupClass.CONDITIONAL_SETUP),
        (74, SetupClass.CONDITIONAL_SETUP), (75, SetupClass.STRONG_SETUP),
        (84, SetupClass.STRONG_SETUP), (85, SetupClass.EXCEPTIONAL_SETUP),
        (100, SetupClass.EXCEPTIONAL_SETUP)])
    def test_bands(self, total, cls):
        assert classify_score(total, CFG) is cls

    @pytest.mark.parametrize("rr,pts", [(D("3.1"), 15), (D("2.5"), 12), (D("2.0"), 9),
                                        (D("1.9"), 4), (D("1.4"), 0), (None, 0)])
    def test_reward_risk_points(self, rr, pts):
        assert score_rr(TargetPlan(None, "", None, "", rr), CFG).points == pts

    def test_missing_optional_not_redistributed(self):
        bull = NS(fresh=True, structure=NS(bullish=True, bearish=False, trend="UPTREND"))
        missing = NS(fresh=False, structure=None)
        ctx = NS(timeframes={"1d": bull, "1h": bull, "15m": bull, "5m": missing})
        pol = PE.policy_for(SetupType.PULLBACK_TO_SUPPORT, CFG)        # SWING: 5m optional
        c = score_alignment(ctx, pol, CFG)
        assert c.missing_optional == ["5m"] and c.missing_required == []
        assert c.points == D(15) * D("0.80") / D("0.92")                # 5m share is lost
        assert c.points < 15

    def test_weights_sum_to_100(self):
        assert sum(CFG.SCORE_WEIGHTS.values()) == 100


# ==========================================================================
# Timeframe policy per setup type
# ==========================================================================
@pytest.mark.parametrize("setup,required,optional", [
    (SetupType.BREAKOUT, ("1d", "1h", "15m", "5m"), ("1m",)),
    (SetupType.PULLBACK_TO_SUPPORT, ("1d", "1h", "15m"), ("5m",)),
    (SetupType.TREND_CONTINUATION, ("1d", "1h"), ("5m",)),
])
def test_setup_specific_timeframe_policy(setup, required, optional):
    pol = PE.policy_for(setup, CFG)
    assert pol.required == required and pol.optional == optional
    if setup is SetupType.TREND_CONTINUATION:
        assert pol.preferred == ("15m",)


# ==========================================================================
# 12. No-trade rules + decision table
# ==========================================================================
def _stub_ctx(**kw):
    s = NS(bearish=False, bullish=True, trend="UPTREND", ma20=D(100), breakdown=False,
           label=UP)
    row = NS(rvol=D("1.2"))
    daily = NS(fresh=True, structure=s, features=NS(rows=[row] * 80), last_row=row,
               pressure=NS(buying="MODERATE", selling="WEAK"), breakout=None)
    base = dict(daily=daily, atr=D(2), current_price=D(101), regime=NS(label="RISK_ON"),
                timeframes={"1d": daily})
    base.update(kw)
    return NS(**base)


def _setup(t=SetupType.PULLBACK_TO_SUPPORT):
    return Setup(t, "SWING", "1d", D(101), D(100), D("101.5"))


GOOD_STOP = StopPlan(D(99), "BELOW_SUPPORT_ZONE", "x", D(2), D(1))
POL = PE.policy_for(SetupType.PULLBACK_TO_SUPPORT, CFG)


def _codes(ctx=None, setup="default", ambiguous=False, stop=GOOD_STOP, rr=D("2.5")):
    setup = _setup() if setup == "default" else setup
    return [r.code for r in analytical_reasons(ctx or _stub_ctx(), setup, ambiguous, stop,
                                               TargetPlan(D(106), "", None, "", rr), POL, CFG)]


class TestNoTradeRules:
    def test_clean_setup_has_no_reasons(self):
        assert _codes() == []

    def test_poor_reward_risk_1_9_is_no_trade(self):
        assert "POOR_REWARD_RISK" in _codes(rr=D("1.9"))

    def test_extended_price(self):
        assert "PRICE_EXTENDED" in _codes(_stub_ctx(current_price=D(104)))  # 2 ATR above MA20

    def test_hostile_regime(self):
        assert "HOSTILE_REGIME" in _codes(_stub_ctx(regime=NS(label="RISK_OFF")))

    def test_structure_conflict(self):
        ctx = _stub_ctx()
        ctx.daily.structure.bearish, ctx.daily.structure.trend = True, "DOWNTREND"
        assert "STRUCTURE_CONFLICT" in _codes(ctx)

    def test_weak_volume(self):
        ctx = _stub_ctx()
        ctx.daily.pressure = NS(buying="WEAK", selling="STRONG")
        assert "WEAK_VOLUME" in _codes(ctx)

    def test_insufficient_data(self):
        ctx = _stub_ctx()
        ctx.daily.fresh = False
        assert _codes(ctx) == ["INSUFFICIENT_DATA"]

    def test_ambiguous_and_no_setup(self):
        assert "SETUP_AMBIGUOUS" in _codes(setup=None, ambiguous=True)
        assert "NO_SETUP" in _codes(setup=None)

    def test_stop_too_wide(self):
        wide = StopPlan(D(90), "X", "", D(11), D("5.5"), valid=False,
                        problem="stop 5.50 ATR from entry exceeds 3.0 ATR")
        assert "STOP_TOO_WIDE" in _codes(stop=wide)


EXEC, ANA = AnalysisMode.EXECUTION_READY, AnalysisMode.ANALYSIS_ONLY
IN = PriceState.IN_RANGE


class TestDecision:
    @pytest.mark.parametrize("analytical,score,mode,state,action", [
        (["X"], 95, EXEC, IN, ProposedAction.NO_TRADE),            # any NO TRADE rule wins
        ([], 49, EXEC, IN, ProposedAction.NO_TRADE),
        ([], 64, EXEC, IN, ProposedAction.WATCH),
        ([], 74, EXEC, IN, ProposedAction.WATCH),                  # CONDITIONAL -> WATCH
        ([], 75, EXEC, IN, ProposedAction.BUY),
        ([], 99, ANA, IN, ProposedAction.WATCH),                   # analysis only never BUY
        ([], 90, EXEC, PriceState.WAIT_FOR_PULLBACK, ProposedAction.WATCH),
        ([], 90, EXEC, PriceState.AWAITING_TRIGGER, ProposedAction.WATCH),
        ([], None, EXEC, IN, ProposedAction.NO_TRADE),
    ])
    def test_table(self, analytical, score, mode, state, action):
        assert PE.decide_action(analytical, score, mode, state, CFG) is action


# ==========================================================================
# Modes end-to-end (synthetic SymbolMarketData, no IBKR)
# ==========================================================================
UP_DAILY = [D(100) + D("0.5") * i for i in range(80)]


def _evaluate(smd, state=SystemState.SAFE, snap="default"):
    benches = {"SPY": bench("SPY", UP_DAILY), "QQQ": bench("QQQ", UP_DAILY),
               "XLK": bench("XLK", UP_DAILY)}
    ctx = build_context(smd, benches, CFG, NOW)
    snap = snapshot(CFG) if snap == "default" else snap
    return ctx, PE.evaluate(ctx, CFG, state, snap, RiskEngine(CFG), NOW)


class TestModes:
    def test_live_fresh_data_is_execution_ready(self):
        ctx, p = _evaluate(make_smd(UP_DAILY))
        assert p.analysis_mode is EXEC, p.execution_blockers
        assert p.execution_blockers == []

    @pytest.mark.parametrize("qtype", ["DELAYED", "DELAYED_FROZEN", "FROZEN"])
    def test_delayed_quote_downgrades(self, qtype):
        _, p = _evaluate(make_smd(UP_DAILY, quote_type=qtype))
        assert p.analysis_mode is ANA
        assert "QUOTE_DELAYED_OR_FROZEN" in [b.code for b in p.execution_blockers]
        assert p.proposed_action is not ProposedAction.BUY
        assert not p.execution_eligible and p.risk_status.startswith("NOT EVALUATED")
        assert p.opportunity_score is not None        # still analysed and scored

    def test_stale_required_intraday_downgrades(self):
        _, p = _evaluate(make_smd(UP_DAILY, statuses={"15m": "STALE"}))
        assert p.analysis_mode is ANA
        assert "REQUIRED_TIMEFRAME_STALE" in [b.code for b in p.execution_blockers]

    def test_stale_optional_timeframe_only_lowers_confidence(self):
        _, p = _evaluate(make_smd(UP_DAILY, statuses={"5m": "STALE", "1m": "STALE"}))
        assert p.timeframe_policy.horizon == "SWING"          # no setup -> SWING policy
        assert p.analysis_mode is EXEC                         # 5m optional for SWING
        assert "5m" in p.missing_optional

    def test_unavailable_quote_and_closed_market(self):
        _, p = _evaluate(make_smd(UP_DAILY, quote_available=False))
        assert "QUOTE_UNAVAILABLE" in [b.code for b in p.execution_blockers]

    def test_system_safe_mode_blocks(self):
        _, p = _evaluate(make_smd(UP_DAILY), state=SystemState.SAFE_MODE)
        assert "SYSTEM_NOT_SAFE" in [b.code for b in p.execution_blockers]

    def test_missing_benchmark_blocks_execution(self):
        smd = make_smd(UP_DAILY)
        ctx = build_context(smd, {}, CFG, NOW)
        p = PE.evaluate(ctx, CFG, SystemState.SAFE, snapshot(CFG), RiskEngine(CFG), NOW)
        assert ctx.regime.label == "UNKNOWN" and p.analysis_mode is ANA
        assert "BENCHMARK_DATA_MISSING" in [b.code for b in p.execution_blockers]

    def test_monotonic_rally_is_extended_no_trade(self):
        _, p = _evaluate(make_smd(UP_DAILY))
        assert p.proposed_action is ProposedAction.NO_TRADE
        assert "PRICE_EXTENDED" in [r.code for r in p.no_trade_reasons]


# ==========================================================================
# Risk engine veto is final
# ==========================================================================
@pytest.fixture
def forced_buy(monkeypatch):
    """Force a BUY decision so the risk-engine hand-off can be tested."""
    def install(entry=D("101"), stop=D("99")):
        setup = Setup(SetupType.PULLBACK_TO_SUPPORT, "SWING", "1d", entry, entry - 1,
                      entry + D("0.5"))
        monkeypatch.setattr(PE, "candidate_setups", lambda ctx, cfg: [setup])
        monkeypatch.setattr(PE, "plan_stop", lambda s, c, cfg: StopPlan(stop, "T", "inv"))
        monkeypatch.setattr(PE, "plan_targets", lambda s, st, c, cfg:
                            TargetPlan(entry + 10, "R", None, "", D(5)))
        monkeypatch.setattr(PE, "analytical_reasons", lambda *a: [])
        monkeypatch.setattr(PE, "price_state", lambda s, p: PriceState.IN_RANGE)
        total = NS(total=95, components=[])
        monkeypatch.setattr(PE, "opportunity_score", lambda *a: total)
    return install


class TestRiskVeto:
    def test_buy_approved_when_risk_engine_agrees(self, forced_buy):
        forced_buy(entry=D("101"), stop=D("99"))
        smd = make_smd([D(100)] * 79 + [D(101)])
        _, p = _evaluate(smd)
        assert p.analysis_mode is EXEC
        assert p.risk_status == "APPROVED" and p.execution_eligible
        assert p.proposed_action is ProposedAction.BUY
        assert p.risk_quantity >= 1

    def test_drawdown_veto_overrides_score_95(self, forced_buy):
        forced_buy()
        smd = make_smd([D(100)] * 79 + [D(101)])
        broke = snapshot(CFG, cash="600.00", peak="800.00", realized="-200.00")
        _, p = _evaluate(smd, snap=broke)
        assert p.risk_status == "REJECTED" and not p.execution_eligible
        assert p.proposed_action is ProposedAction.NO_TRADE
        assert "RISK_VETO" in [r.code for r in p.no_trade_reasons]

    def test_unaffordable_stock_vetoed_not_analytical(self, forced_buy):
        forced_buy(entry=D("900"), stop=D("890"))
        smd = make_smd([D(900)] * 80)
        _, p = _evaluate(smd)
        assert "AFFORDABILITY" in p.affordability_note
        assert p.risk_status == "REJECTED" and p.proposed_action is ProposedAction.NO_TRADE

    def test_analysis_only_never_reaches_risk_engine(self, forced_buy, monkeypatch):
        forced_buy()
        called = []
        monkeypatch.setattr(RiskEngine, "evaluate", lambda *a, **k: called.append(1))
        _, p = _evaluate(make_smd([D(100)] * 79 + [D(101)], quote_type="DELAYED"))
        assert called == [] and p.proposed_action is ProposedAction.WATCH
        assert p.risk_status == "NOT EVALUATED (analysis only)"

    @pytest.mark.parametrize("mode", [None, "ANALYSIS_ONLY", "execution_ready", ""])
    def test_risk_engine_rejects_anything_not_execution_ready(self, mode):
        from conftest import proposal
        d = RiskEngine(CFG).evaluate(proposal(analysis_mode=mode), snapshot(CFG),
                                     SystemState.SAFE)
        assert not d.approved and RR.NOT_EXECUTION_READY in d.reasons

    def test_risk_engine_accepts_mode_enum(self):
        from conftest import proposal
        d = RiskEngine(CFG).evaluate(proposal(analysis_mode=EXEC), snapshot(CFG),
                                     SystemState.SAFE)
        assert d.approved, d.rejections


def test_trade_proposal_is_long_only_and_serializable():
    _, p = _evaluate(make_smd(UP_DAILY))
    assert p.direction == "LONG"
    import json
    from journal.trade_journal import _default
    json.dumps(p.to_dict(), default=_default)


def test_no_execution_code_in_strategy_or_analysis():
    import re
    from pathlib import Path
    import config
    for pkg in ("strategy", "analysis"):
        for f in (Path(config.BASE_DIR) / pkg).glob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert not re.search(r"placeOrder|reqIds|OrderManager", text), f


def test_evaluating_spy_itself_has_regime_and_rs():
    spy = make_smd(UP_DAILY, symbol="SPY", stock_type="ETF", long_name="SPDR S&P 500",
                   primary="ARCA", industry="")
    ctx = build_context(spy, {"SPY": spy, "QQQ": bench("QQQ", UP_DAILY)}, CFG, NOW)
    assert ctx.regime.label == "RISK_ON"
    assert ctx.relative_strength.label == "NEUTRAL"
    p = PE.evaluate(ctx, CFG, SystemState.SAFE, snapshot(CFG), RiskEngine(CFG), NOW)
    assert "BENCHMARK_DATA_MISSING" not in [b.code for b in p.execution_blockers]
