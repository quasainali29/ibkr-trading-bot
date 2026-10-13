"""Pilot follow-up fixes:
  1. one shadow trade per unique setup instance (no re-entry of an unchanged
     setup after a stop-out; a genuine reset is required)
  2. the 09:30-10:00 opening 1h bar is a SHORT bar (30 minutes)."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest import metrics as M
from backtest.asof import SymbolHistory
from backtest.engine import decision_times, simulate_shadow
from backtest.execution_simulator import CompactBars
from backtest.setup_instances import SetupInstanceTracker, anchors_match
from bt_helpers import aggregate, synth_5m
from conftest import make_info
from market_data.features import FeatureRow, atr, candle_geometry, compute_features
from market_data.models import Bar, get_timeframe
from market_data.sessions import bar_end, bar_is_complete, bar_span
from strategy.entry_logic import setup_anchor
from strategy.models import Setup, SetupType
from test_backtest import (CFG, DAYS, ET, SPEC_A, ZERO_CM, bars_5m, calendar_for, et,
                           mk_signal)

D = Decimal
ZONE_A = ("ZONE", D("99.00"), D("100.00"))
ZONE_B = ("ZONE", D("104.00"), D("105.00"))


# ==========================================================================
# 1. Shadow re-entry: one shadow trade per setup instance
# ==========================================================================
def _sig(t, anchor=ZONE_A, setup="BREAKOUT"):
    s = mk_signal(ts=t, entry=D(100), max_entry=D("100.5"), stop=D(98), t1=D(106),
                  setup=setup, pre_risk_buy=False)
    s.price_state, s.setup_anchor = "IN_RANGE", anchor
    return s


def _whipsaw_day(d):
    """100 all day except three 30-minute dips to 97 (through the 98 stop)
    at 10:30, 12:00 and 13:30 - the pilot's 2026-10-01 pattern."""
    dips = set(range(12, 18)) | set(range(30, 36)) | set(range(48, 54))
    return bars_5m(d, [(97, 97.2, 96.8, 97) if i in dips else (100, 100.2, 99.8, 100)
                       for i in range(78)])


def _flat(d):
    return bars_5m(d, [(100, 100.2, 99.8, 100)] * 78)


def _times(d):
    return [et(d, 10, 0) + timedelta(minutes=15 * i) for i in range(24)]   # 10:00-15:45


def _shadow(signals, days_bars, times):
    stats = {}
    bars = CompactBars([b for day in days_bars for b in day], 300)
    out = simulate_shadow(SPEC_A, "TST", signals, bars, calendar_for(DAYS), times, CFG,
                          ZERO_CM, D(800), stats=stats)
    return out, stats


class TestShadowSetupInstances:
    def test_repeated_evaluations_of_same_setup_create_one_shadow_trade(self):
        d0 = DAYS[0]
        times = _times(d0)
        out, stats = _shadow([_sig(t) for t in times], [_whipsaw_day(d0)], times)
        assert len(out) == 1                       # previous rule: re-entered after each stop
        assert out[0].exit_reason.startswith("STOP")
        assert stats["instances"] == 1 and stats["suppressed_signals"] > 10

    def test_unchanged_setup_is_not_reset_by_time_alone(self):
        times = [t for d in DAYS[:3] for t in _times(d)]
        out, stats = _shadow([_sig(t) for t in times],
                             [_whipsaw_day(DAYS[0]), _whipsaw_day(DAYS[1]), _whipsaw_day(DAYS[2])],
                             times)
        assert len(out) == 1                       # present at every decision time for 3 days
        assert stats["resets_condition_gone"] == 0

    def test_new_structure_creates_a_new_shadow_trade(self):
        d0 = DAYS[0]
        times = _times(d0)
        sigs = [_sig(t, ZONE_A if t < et(d0, 11, 0) else ZONE_B) for t in times]
        out, stats = _shadow(sigs, [_whipsaw_day(d0)], times)
        assert len(out) == 2                       # A once, then the new zone B once
        assert len({t.setup_instance for t in out}) == 2
        assert "99.00-100.00" in out[0].setup_instance and "104.00-105.00" in out[1].setup_instance
        assert stats["resets_new_structure"] == 1

    def test_condition_gone_for_a_full_session_then_new_setup_forms(self):
        d0, d1, d2 = DAYS[:3]
        times = _times(d0) + _times(d1) + _times(d2)
        sigs = ([_sig(t) for t in _times(d0)]
                + [_sig(t, setup="NONE") for t in _times(d1)]      # setup gone all session
                + [_sig(t) for t in _times(d2)])                   # same zone, formed again
        out, stats = _shadow(sigs, [_whipsaw_day(d0), _flat(d1), _flat(d2)], times)
        assert len(out) == 2 and stats["resets_condition_gone"] == 1
        assert out[1].entry_ts.astimezone(ET).date() == d2

    def test_partial_session_absence_is_not_a_reset(self):
        d0, d1, d2 = DAYS[:3]
        times = _times(d0) + _times(d1) + _times(d2)
        day1 = [_sig(t, setup="NONE") for t in _times(d1)[:-1]] + [_sig(_times(d1)[-1])]
        sigs = [_sig(t) for t in _times(d0)] + day1 + [_sig(t) for t in _times(d2)]
        out, stats = _shadow(sigs, [_whipsaw_day(d0), _flat(d1), _flat(d2)], times)
        assert len(out) == 1 and stats["resets_condition_gone"] == 0

    def test_other_setup_types_are_independent(self):
        d0 = DAYS[0]
        times = _times(d0)
        sigs = [_sig(t) for t in times] + [_sig(t, setup="PULLBACK_TO_SUPPORT") for t in times]
        out, _ = _shadow(sigs, [_whipsaw_day(d0)], times)
        assert sorted(t.setup_type for t in out) == ["BREAKOUT", "PULLBACK_TO_SUPPORT"]

    def test_shadow_statistics_not_inflated_by_reentry(self):
        d0 = DAYS[0]
        times = _times(d0)
        out, _ = _shadow([_sig(t) for t in times], [_whipsaw_day(d0)], times)
        s = M.summarize(out, None, 1)
        assert s["trades"] == 1                      # one event counted once
        by_setup = M.group(out, lambda t: t.setup_type, 1)
        assert dict(by_setup)["BREAKOUT"]["trades"] == 1

    def test_unfilled_orders_do_not_consume_the_instance(self):
        d0 = DAYS[0]
        times = _times(d0)
        sigs = [_sig(t) for t in times]
        for s in sigs[:4]:                          # 10:00-10:45: price above max entry
            s.max_entry = s.maximum_entry = D("99.5")
        out, stats = _shadow(sigs, [_flat(d0)], times)
        assert len(out) == 1 and stats["instances"] == 1

    @pytest.mark.parametrize("a,b,same", [
        (ZONE_A, ("ZONE", D("99.80"), D("100.40")), True),        # drifting, overlapping zone
        (ZONE_A, ZONE_B, False),                                  # different zone
        (ZONE_A, ("CONSOLIDATION", D(99), D(100)), False),        # different structure kind
        (("MA20", None, None), ("MA20", None, None), True),       # MA20: no fixed level
        (None, ZONE_A, True),                                     # unknown -> conservative
    ])
    def test_anchor_matching(self, a, b, same):
        assert anchors_match(a, b) is same

    def test_tracker_resets_only_on_complete_absent_session(self):
        tr = SetupInstanceTracker("TST")
        s = _sig(et(DAYS[0], 10, 0))
        tr.observe(DAYS[0], {"BREAKOUT"})
        tr.consume(s, DAYS[0])
        tr.observe(DAYS[1], set())                  # DAYS[0] closes (it was present)
        assert tr.is_consumed(s)
        tr.observe(DAYS[2], {"BREAKOUT"})           # DAYS[1] closes absent -> reset
        assert not tr.is_consumed(s)

    def test_setup_anchor_is_structural(self):
        from analysis.market_report import Zone
        z = Zone(D("99"), D("100"), 3, et(DAYS[0], 9, 30))
        brk = Setup(SetupType.BREAKOUT, "SHORT_TERM", "1d", D(100), D(100), D("101"), z)
        assert setup_anchor(brk, None) == ("ZONE", D("99"), D("100"))
        ma = Setup(SetupType.PULLBACK_TO_SUPPORT, "SWING", "1d", D(100), D(99), D("101"))
        assert setup_anchor(ma, None) == ("MA20", None, None)

    def test_real_portfolio_does_not_use_shadow_dedupe(self):
        import inspect
        from backtest.engine import simulate_portfolio
        src = inspect.getsource(simulate_portfolio)
        assert "SetupInstanceTracker" not in src and "setup_anchor" not in src


# ==========================================================================
# 2. The 09:30-10:00 opening 1h bar is a short (30-minute) bar
# ==========================================================================
H1 = get_timeframe("1h")


def _hourly(days=DAYS):
    return aggregate(synth_5m(days[0], days[-1], seed=7), 60)


class TestShortOpeningHourlyBar:
    def test_bar_end_and_span(self):
        d = DAYS[0]
        assert bar_end(et(d, 9, 30), 3600, ET) == et(d, 10, 0)
        assert bar_end(et(d, 10, 0), 3600, ET) == et(d, 11, 0)
        assert bar_end(et(d, 9, 30), 900, ET) == et(d, 9, 45)       # 15m unaffected
        assert bar_span(et(d, 9, 30), 3600, ET) == D("0.5")
        assert bar_span(et(d, 14, 0), 3600, ET) == 1

    def test_synthetic_hourly_bars_are_clock_aligned(self):
        starts = [b.timestamp for b in _hourly() if b.timestamp.astimezone(ET).date() == DAYS[0]]
        assert starts[:3] == [et(DAYS[0], 9, 30), et(DAYS[0], 10, 0), et(DAYS[0], 11, 0)]

    def test_short_bar_complete_at_10_and_not_before(self):
        d, cal = DAYS[1], calendar_for(DAYS)
        b = Bar(et(d, 9, 30), D(1), D(1), D(1), D(1), D(1))
        assert not bar_is_complete(b, H1, cal, et(d, 9, 59))       # no look-ahead
        assert bar_is_complete(b, H1, cal, et(d, 10, 0))           # was 10:30 before the fix

    def test_asof_visibility_of_short_bar(self):
        d, cal = DAYS[1], calendar_for(DAYS)
        h = SymbolHistory("TST", make_info(symbol="TST"), {"1h": _hourly()}, cal, CFG)
        assert h.completed("1h", et(d, 9, 59), 50)[-1].timestamp.astimezone(ET).date() == DAYS[0]
        assert h.completed("1h", et(d, 10, 0), 50)[-1].timestamp == et(d, 9, 30)
        assert h.completed("1h", et(d, 10, 59), 50)[-1].timestamp == et(d, 9, 30)

    def test_hourly_decision_times_are_regular(self):
        d = DAYS[0]
        ts = decision_times(_hourly(), "1h", calendar_for(DAYS), d, d)
        assert ts == [et(d, h, 0) for h in range(10, 16)]          # was 10:30, 11:00, 12:00...

    def test_simulation_bar_ends_at_10(self):
        cb = CompactBars(_hourly(), 3600, ET)
        assert cb.bar(0).end == et(DAYS[0], 10, 0) and cb.bar(1).end == et(DAYS[0], 11, 0)

    def test_atr_excludes_short_bars(self):
        bars, spans = [], []
        for d in DAYS:
            for k, h in enumerate([9.5, 10, 11, 12, 13, 14, 15]):
                t = et(d, int(h), 30 if h == 9.5 else 0)
                rng = D("0.5") if k == 0 else D(2)
                bars.append(Bar(t, D(100), D(100) + rng / 2, D(100) - rng / 2, D(100), D(1)))
                spans.append(D("0.5") if k == 0 else D(1))
        with_spans = atr(bars, 14, spans)
        classic = atr(bars, 14)
        assert with_spans[-1] == D(2)                     # only full-hour ranges
        assert classic[-1] < D(2)                         # half bars used to drag ATR down
        assert with_spans[7] == with_spans[6]             # short bar carries the previous ATR

    def test_atr_identical_without_short_bars(self):
        bars15 = aggregate(synth_5m(DAYS[0], DAYS[-1], seed=3), 15)
        fs = compute_features(bars15, get_timeframe("15m"), ET, CFG)
        assert all(r.span == 1 and not r.short_bar for r in fs.rows)
        assert [r.atr for r in fs.rows] == atr(bars15, CFG.ATR_PERIOD)
        assert all(r.atr_ref == r.atr for r in fs.rows)

    def test_feature_rows_mark_the_short_bar(self):
        fs = compute_features(_hourly(), H1, ET, CFG)
        for r in fs.rows:
            first = r.bar.timestamp.astimezone(ET).strftime("%H:%M") == "09:30"
            assert r.short_bar is first
            if first and r.atr is not None:
                assert r.atr_ref == r.atr * D("0.5").sqrt()

    def test_rvol_of_short_bar_compares_only_opening_bars(self):
        bars = aggregate(synth_5m(date(2025, 3, 3), date(2025, 3, 14), seed=5), 60)
        fs = compute_features(bars, H1, ET, CFG)
        opens = [r for r in fs.rows if r.short_bar]
        last = opens[-1]
        prior = [r.bar.volume for r in opens[:-1]][-CFG.RVOL_MAX_SESSIONS:]
        assert last.rvol == last.bar.volume / (sum(prior, D(0)) / len(prior))

    def test_range_vs_atr_uses_duration_adjusted_atr(self):
        from analysis.candle_interpretation import MOMENTUM_UP, detect_shapes
        b = Bar(et(DAYS[0], 9, 30), D(100), D(101), D(100), D("100.95"), D(1))   # range 1.0
        row = lambda span: FeatureRow(b, candle_geometry(b), None, D(1), None, None, None,
                                      None, D(span))
        # full bar: 1.0 < 1.2 x ATR -> not momentum; short bar: 1.0 >= 1.2 x ATR x sqrt(0.5)
        assert MOMENTUM_UP not in detect_shapes([row(1)], 0, CFG)
        assert MOMENTUM_UP in detect_shapes([row("0.5")], 0, CFG)
