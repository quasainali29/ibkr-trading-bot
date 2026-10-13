"""Phase 3.5 backtesting: look-ahead, sizing, risk, breaker, fills, costs,
aggregation, competition, simulated-quote safety, determinism."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backtest import metrics as M
from backtest.asof import SymbolHistory
from backtest.engine import (Backtester, _load_histories, decision_times, generate_signals,
                             make_record, score_band, simulate_portfolio, simulate_shadow)
from backtest.execution_simulator import (CompactBars, EntryOrder, OpenPosition, SimBar,
                                          check_exit, commission_for, half_spread,
                                          open_position, slippage, try_fill_entry)
from backtest.models import (EXIT_STOP_AMBIGUOUS, EXIT_STOP_GAP, EXIT_TARGET,
                             EXIT_TARGET_GAP, POST_BREAKER, PRE_BREAKER, CostModel, Fill,
                             RunSpec, Signal)
from bt_helpers import build_store
from conftest import live_check, make_cfg, make_info, snapshot
from environment import Environment
from market_data.models import Bar, MarketDataType
from market_data.sessions import TradingCalendar
from risk.risk_engine import Reason as RR
from risk.risk_engine import RiskEngine, calculate_max_quantity
from risk.system_state import SystemState
from strategy.proposal_engine import risk_review

D = Decimal
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
CFG = make_cfg()
CM = CostModel.from_config(CFG)
ZERO_CM = CostModel(D(0), D(0), D(1), D(0), D(0), D(0))
SIM = MarketDataType.SIMULATED_BACKTEST


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def et(d, h, m):
    return datetime.combine(d, time(h, m), tzinfo=ET).astimezone(UTC)


def bar(start, o, h, l, c, secs=300):
    return SimBar(start, start + timedelta(seconds=secs), D(str(o)), D(str(h)), D(str(l)), D(str(c)))


def mk_signal(sym="TST", ts=None, score=90, rr=D(3), entry=D(100), max_entry=D("100.5"),
              stop=D(98), t1=D(106), t2=D(110), setup="BREAKOUT", horizon="SHORT_TERM",
              sector="XLK", pre_risk_buy=True):
    ts = ts or et(date(2025, 3, 3), 10, 0)
    md = live_check(sym, data_type=SIM, checked_at=ts)
    return Signal(sym, ts, "BUY", pre_risk_buy, "EXECUTION_READY", "BACKTEST", score, "STRONG SETUP",
                  setup, horizon, "HIGH", "RISK_ON", entry, entry, max_entry, stop, t1, t2, rr,
                  sector=sector, md_check=md if pre_risk_buy else None)


def bars_5m(d, prices):
    """5m Bars on session date d from 09:30, prices = [(o,h,l,c), ...]."""
    return [Bar(et(d, 9, 30) + timedelta(minutes=5 * i), D(str(o)), D(str(h)), D(str(l)),
                D(str(c)), D(1000)) for i, (o, h, l, c) in enumerate(prices)]


def calendar_for(days):
    flat = []
    for d in days:
        flat += bars_5m(d, [(1, 1, 1, 1)] * 78)
    return TradingCalendar.from_bars(flat, 300, ET)


SPEC_A = RunSpec(1, "15m", "A", True)
SPEC_B = RunSpec(1, "15m", "B", True)


# ==========================================================================
# Look-ahead
# ==========================================================================
@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bt")
    start, end = date(2025, 3, 3), date(2025, 3, 21)
    store = build_store(tmp, ["SPY", "QQQ", "AAPL"], start, end, CFG,
                        drifts={"AAPL": 0.0015})
    bt = Backtester(CFG, store, 1, ["AAPL"], start, end, workers=1, cache_dir=tmp / "cache",
                    progress=lambda m: None)
    hist = _load_histories(store, bt._symbols_tfs("AAPL"), bt.calendar, CFG, start, end)
    return bt, hist, tmp


def _mutate_future(hist, t, truncate=False):
    out = {}
    for sym, h in hist.items():
        series = {}
        for key, bars in h.series.items():
            new = []
            for b, e in zip(bars, h.ends[key]):
                if e <= t:
                    new.append(b)
                elif not truncate:
                    new.append(Bar(b.timestamp, D(1), D(99999), D("0.5"), D(5000), D(10 ** 9)))
            series[key] = new
        out[sym] = SymbolHistory(sym, h.contract, series, h.calendar, CFG)
    return out


class TestNoLookAhead:
    def _sig(self, bt, hist, t):
        return generate_signals("AAPL", hist, bt.calendar, [t], CFG, bt.timeframes, CM)[0]

    def test_future_mutation_does_not_change_decision(self, synth):
        bt, hist, _ = synth
        for t in bt.signal_times[::40]:
            assert self._sig(bt, _mutate_future(hist, t), t) == self._sig(bt, hist, t)

    def test_truncation_identical(self, synth):
        bt, hist, _ = synth
        for t in bt.signal_times[5::53]:
            assert self._sig(bt, _mutate_future(hist, t, truncate=True), t) == self._sig(bt, hist, t)

    def test_todays_daily_bar_invisible_until_close(self, synth):
        bt, hist, _ = synth
        h = hist["AAPL"]
        t = bt.signal_times[30]                       # inside a session
        visible = h.completed("1d", t, 300)
        assert bt.calendar.local_date(visible[-1].timestamp) < bt.calendar.local_date(t)

    def test_quote_is_simulated_and_labelled(self, synth):
        from backtest.asof import AsOfBuilder
        bt, hist, _ = synth
        t = bt.signal_times[10]
        smd = AsOfBuilder(hist, bt.calendar, CFG, lambda p: half_spread(p, CM)).smd_at(
            "AAPL", t, bt.timeframes)
        q = smd.quote
        assert q.simulated and q.source == "BACKTEST" and q.data_type is SIM
        assert q.last_timestamp <= t
        assert smd.check.environment is Environment.BACKTEST

    def test_entries_never_before_signal(self, synth):
        bt, _, _ = synth
        for r in bt.run():
            for tr in r.trades + r.shadow:
                assert tr.entry_ts >= tr.signal_ts


# ==========================================================================
# Simulated quotes never authorize REAL trades
# ==========================================================================
class TestSimulatedQuoteSafety:
    def test_live_risk_engine_rejects_simulated(self):
        from conftest import proposal
        d = RiskEngine(CFG).evaluate(proposal(market_data=live_check("SPY", data_type=SIM)),
                                     snapshot(CFG), SystemState.SAFE)
        assert not d.approved and RR.MARKET_DATA_SIMULATED in d.reasons

    def test_backtest_risk_engine_accepts_simulated(self):
        from conftest import proposal
        d = RiskEngine(CFG, Environment.BACKTEST).evaluate(
            proposal(market_data=live_check("SPY", data_type=SIM)), snapshot(CFG), SystemState.SAFE)
        assert d.approved, d.rejections

    def test_flagged_quote_rejected_even_if_typed_live(self):
        from conftest import proposal
        md = live_check("SPY")
        md.quote.simulated = True
        d = RiskEngine(CFG).evaluate(proposal(market_data=md), snapshot(CFG), SystemState.SAFE)
        assert RR.MARKET_DATA_SIMULATED in d.reasons

    def test_live_environment_blocks_simulated_quotes(self):
        from market_data.validation import QuoteStatus, quote_execution_blocker
        qs = QuoteStatus(True, True, SIM, False, simulated=True, source="BACKTEST")
        assert "SIMULATED" in quote_execution_blocker(qs, Environment.LIVE, CFG)
        assert quote_execution_blocker(qs, Environment.BACKTEST, CFG) is None
        live = QuoteStatus(True, True, MarketDataType.DELAYED, False)
        assert quote_execution_blocker(live, Environment.BACKTEST, CFG)   # delayed still blocked

    def test_proposal_engine_default_is_live(self):
        import inspect
        from strategy.proposal_engine import evaluate
        assert inspect.signature(evaluate).parameters["environment"].default is Environment.LIVE
        assert RiskEngine(CFG).environment is Environment.LIVE

    def test_simulated_type_cannot_be_requested_from_ibkr(self):
        import config
        with pytest.raises(config.ConfigError):
            config.validate_config(make_cfg(MARKET_DATA_TYPE="SIMULATED_BACKTEST"))


# ==========================================================================
# Sizing and the 2% rule
# ==========================================================================
class TestSizingAndRisk:
    def test_quantity_from_fixed_formula_at_maximum_entry(self):
        s = mk_signal()
        snap = snapshot(CFG)
        dec, qty = risk_review("TST", make_info(symbol="TST"), s.maximum_entry, s.stop, s.md_check,
                               snap, SystemState.SAFE, RiskEngine(CFG, Environment.BACKTEST),
                               s.ts, CFG)
        *_, expected = calculate_max_quantity(D(800), D(800), s.maximum_entry, s.stop, D("0.02"))
        assert dec.approved and qty == expected == 6          # $16 / $2.50
        assert qty * (s.maximum_entry - s.stop) <= D(16)

    def test_actual_fill_risk_never_exceeds_plan(self):
        s = mk_signal()
        order = EntryOrder("TST", s, 6, s.maximum_entry, s.ts, s.ts + timedelta(hours=1))
        for o in ("99.0", "100.0", "100.5"):
            f = try_fill_entry(order, bar(s.ts, o, "101", "98.5", "100"), CM)
            assert f.price <= s.maximum_entry
            assert (f.price - s.stop) * 6 <= D(16)

    def test_two_percent_enforced(self):
        s = mk_signal(stop=D(80))                          # $20.50 risk/share > $16 budget
        dec, qty = risk_review("TST", make_info(symbol="TST"), s.maximum_entry, s.stop, s.md_check,
                               snapshot(CFG), SystemState.SAFE,
                               RiskEngine(CFG, Environment.BACKTEST), s.ts, CFG)
        assert qty == 0 and not dec.approved
        assert RR.RISK_TOO_HIGH in dec.reasons

    def test_competition_uses_reduced_cash(self):
        s = mk_signal()
        snap = replace(snapshot(CFG), bot_cash=D(150))     # $650 already reserved
        dec, qty = risk_review("TST", make_info(symbol="TST"), s.maximum_entry, s.stop, s.md_check,
                               snap, SystemState.SAFE, RiskEngine(CFG, Environment.BACKTEST),
                               s.ts, CFG)
        assert qty == 1                                    # floor(150 / 100.5)


# ==========================================================================
# Fills, gaps, same-bar handling, costs
# ==========================================================================
def _pos(fill_mode="OPEN", fill_px="100", stop="98", t1="106", t2="110"):
    s = mk_signal(stop=D(stop), t1=D(t1), t2=D(t2))
    f = Fill(s.ts, D(fill_px), D(fill_px), D(0), D(0), D(1), fill_mode)
    return OpenPosition("TST", s, 6, f, D(stop), D(t1), D(t2), s.ts, set(), D(fill_px), D(fill_px))


class TestExits:
    t = et(date(2025, 3, 4), 10, 0)

    def test_gap_through_stop_fills_at_open(self):
        f, reason, amb, gap = check_exit(_pos(), bar(self.t, "95", "96", "94", "95.5"), ZERO_CM)
        assert reason == EXIT_STOP_GAP and gap and f.ref_price == D(95) and f.price == D(95)

    def test_gap_through_target_fills_at_open(self):
        f, reason, amb, gap = check_exit(_pos(), bar(self.t, "108", "109", "107", "108"), CM)
        assert reason == EXIT_TARGET_GAP and gap
        assert f.ref_price == D(108) and D(106) <= f.price < D(108)

    def test_same_bar_conservative_stop(self):
        f, reason, amb, gap = check_exit(_pos(), bar(self.t, "100", "107", "97", "101"), ZERO_CM)
        assert reason == EXIT_STOP_AMBIGUOUS and amb and f.ref_price == D(98)

    def test_same_bar_resolved_by_finer_bars(self):
        finer = [bar(self.t, "100", "106.5", "99.5", "106", 60), bar(self.t, "106", "106", "97", "97", 60)]
        f, reason, amb, gap = check_exit(_pos(), bar(self.t, "100", "107", "97", "101"), ZERO_CM,
                                         finer_bars=finer)
        assert reason == EXIT_TARGET and not amb

    def test_limit_entry_bar_ignores_target(self):
        p = _pos(fill_mode="LIMIT", fill_px="100.5")
        assert check_exit(p, bar(self.t, "101", "107", "100.2", "101"), ZERO_CM, entry_bar=True) is None

    def test_open_below_stop_on_entry_stops_immediately(self):
        r = check_exit(_pos(fill_px="97"), bar(self.t, "97", "98", "96", "97"), ZERO_CM, entry_bar=True)
        assert r[1] == EXIT_STOP_GAP

    def test_limit_not_filled_when_price_stays_above(self):
        s = mk_signal()
        order = EntryOrder("TST", s, 6, s.maximum_entry, s.ts, s.ts + timedelta(hours=1))
        assert try_fill_entry(order, bar(s.ts, "101", "102", "100.6", "101.5"), CM) is None
        assert try_fill_entry(order, bar(s.ts - timedelta(minutes=5), "99", "99", "99", "99"), CM) is None
        assert try_fill_entry(order, bar(s.ts + timedelta(hours=1), "99", "99", "99", "99"), CM) is None


class TestCosts:
    def test_commission_min_per_share_and_cap(self):
        assert commission_for(6, D(100), CM) == D("1.00")              # minimum
        assert commission_for(1000, D(100), CM) == D("5.00")           # 0.005 x 1000
        assert commission_for(10, D("0.50"), CM) == D("0.05")          # capped at 1% of $5
        assert commission_for(0, D(100), CM) == 0

    def test_spread_and_slippage(self):
        assert half_spread(D(100), CM) == D("0.005")                   # 0.5 bp = $0.005 (=min)
        assert half_spread(D(1000), CM) == D("0.05")
        assert slippage(D(100), CM) == D("0.02")                        # 2 bps

    def test_entry_fill_cost_split(self):
        s = mk_signal()
        order = EntryOrder("TST", s, 6, D("200"), s.ts, s.ts + timedelta(hours=1))
        f = try_fill_entry(order, bar(s.ts, "100", "101", "99", "100"), CM)
        assert f.mode == "OPEN" and f.price == D("100.025")
        assert f.spread_cost == D("0.030") and f.slippage_cost == D("0.120")

    def test_record_cost_identity(self):
        p = _pos()
        p.fill = Fill(p.signal.ts, D("100.025"), D(100), D("0.03"), D("0.12"), D(1), "OPEN")
        f, reason, *_ = check_exit(p, bar(et(date(2025, 3, 4), 10, 0), "99", "99", "97", "97.5"), CM)
        rec = make_record("REAL", SPEC_A, p, f, reason, False, False, PRE_BREAKER)
        assert rec.gross_pnl - rec.commission - rec.spread_cost - rec.slippage_cost == rec.realized_pnl
        assert rec.realized_pnl < 0
        # R is measured against the PLANNED (sizing) risk: (100.5 - 98) x 6 = $15
        assert rec.initial_risk == D(15)
        assert rec.r_multiple == rec.realized_pnl / D(15)
        # fill-to-stop risk is kept as a separate diagnostic
        assert rec.fill_risk == (D("100.025") - D(98)) * 6
        assert rec.fill_r_multiple == rec.realized_pnl / rec.fill_risk


# ==========================================================================
# Portfolio: breaker, competition, time exits
# ==========================================================================
DAYS = [date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5), date(2025, 3, 6), date(2025, 3, 7)]


def _flat_day(d, price):
    return bars_5m(d, [(price, price + 0.2, price - 0.2, price)] * 78)


def _run(signals, bar_map, spec=SPEC_A, times=None):
    cal = calendar_for(DAYS)
    sim = {s: CompactBars(b, 300) for s, b in bar_map.items()}
    contracts = {s: make_info(symbol=s, con_id=500 + i) for i, s in enumerate(sorted(bar_map))}
    times = times or sorted({s.ts for lst in signals.values() for s in lst}
                            | {et(d, 15, 45) for d in DAYS})
    return simulate_portfolio(spec, signals, sim, contracts, cal, times, CFG, ZERO_CM,
                              DAYS[0], DAYS[-1], D(800))


class TestPortfolio:
    def test_circuit_breaker_stops_new_entries_but_shadows_continue(self):
        d0, d1 = DAYS[0], DAYS[1]
        bars = _flat_day(d0, 100) + bars_5m(d1, [(80, 81, 79, 80)] * 78) \
            + _flat_day(DAYS[2], 80) + _flat_day(DAYS[3], 80)
        s1 = mk_signal(ts=et(d0, 10, 0))                                   # loses ~20%
        later = [mk_signal(ts=et(d, 10, 0), entry=D(80), max_entry=D("80.5"), stop=D(78),
                           t1=D(86)) for d in (DAYS[2], DAYS[3])]
        res = _run({"TST": [s1] + later}, {"TST": bars})
        assert res.breaker_ts is not None and res.breaker_ts <= et(d1, 15, 45)
        assert len(res.trades) == 1 and res.trades[0].exit_reason == EXIT_STOP_GAP
        cal = calendar_for(DAYS)
        times = sorted({s.ts for s in [s1] + later} | {et(d, 15, 45) for d in DAYS})
        shadows = simulate_shadow(SPEC_A, "TST", [s1] + later, CompactBars(bars, 300), cal, times,
                                  CFG, ZERO_CM, D(800), res.breaker_ts)
        labels = {t.signal_ts: t.label for t in shadows}
        assert labels[s1.ts] == PRE_BREAKER
        assert all(labels[s.ts] == POST_BREAKER for s in later if s.ts in labels)
        assert any(l == POST_BREAKER for l in labels.values())

    def test_candidates_compete_for_the_same_cash(self):
        d0 = DAYS[0]
        t = et(d0, 10, 0)
        a = mk_signal("AAA", ts=t, score=80)
        b = mk_signal("BBB", ts=t, score=92)
        bars = {"AAA": _flat_day(d0, 100), "BBB": _flat_day(d0, 100)}
        res = _run({"AAA": [a], "BBB": [b]}, bars)
        by = {tr.ticker: tr for tr in res.trades}
        assert by["BBB"].quantity == 6                 # higher score funded first, full size
        assert by["AAA"].quantity == 1                 # only $196 left: floor(196 / 100.5)
        total = sum(tr.position_value for tr in res.trades)
        assert total <= D(800)

    @pytest.mark.parametrize("a_kw,b_kw,winner", [
        (dict(score=85, rr=D(3)), dict(score=85, rr=D("2.5")), "AAA"),
        (dict(score=85, rr=D(3)), dict(score=85, rr=D(3)), "AAA"),           # ticker tie-break
    ])
    def test_ranking_tie_breaks(self, a_kw, b_kw, winner):
        d0 = DAYS[0]
        t = et(d0, 10, 0)
        res = _run({"AAA": [mk_signal("AAA", ts=t, **a_kw)], "BBB": [mk_signal("BBB", ts=t, **b_kw)]},
                   {"AAA": _flat_day(d0, 100), "BBB": _flat_day(d0, 100)})
        by = {tr.ticker: tr.quantity for tr in res.trades}
        assert by[winner] == 6 and sum(by.values()) == 7    # winner sized first

    def test_sector_diversification_preference(self):
        d0 = DAYS[0]
        held = mk_signal("HLD", ts=et(d0, 10, 0), max_entry=D("20.5"), entry=D(20), stop=D(19),
                         t1=D(30), sector="XLK")
        t2 = et(d0, 11, 0)
        same = mk_signal("AAA", ts=t2, score=85, sector="XLK", max_entry=D("50.5"), entry=D(50),
                         stop=D(48), t1=D(60))
        other = mk_signal("ZZZ", ts=t2, score=85, sector="XLE", max_entry=D("50.5"), entry=D(50),
                          stop=D(48), t1=D(60))
        bars = {"HLD": _flat_day(d0, 20), "AAA": _flat_day(d0, 50), "ZZZ": _flat_day(d0, 50)}
        res = _run({"HLD": [held], "AAA": [same], "ZZZ": [other]}, bars)
        q = {tr.ticker: tr.quantity for tr in res.trades}
        # HLD (XLK) holds $205; ZZZ (new sector XLE) is funded first at full size,
        # AAA (same sector as HLD) gets what is left.
        assert q["HLD"] == 10 and q["ZZZ"] == 6 and q["AAA"] == 5

    def test_variant_b_time_exit_variant_a_holds(self):
        days_bars = []
        for d in DAYS:
            days_bars += _flat_day(d, 100)
        s = mk_signal(ts=et(DAYS[0], 10, 0), stop=D(90), t1=D(130), horizon="SHORT_TERM")
        a = _run({"TST": [s]}, {"TST": days_bars}, SPEC_A)
        b = _run({"TST": [s]}, {"TST": days_bars}, SPEC_B)
        assert a.trades[0].exit_reason == "END_OF_DATA"
        assert b.trades[0].exit_reason == "TIME_EXIT" and b.trades[0].holding_sessions == 5

    def test_shadow_trades_never_touch_portfolio(self):
        d0 = DAYS[0]
        s = mk_signal(ts=et(d0, 10, 0), pre_risk_buy=False)    # not a BUY: shadow only
        res = _run({"TST": [s]}, {"TST": _flat_day(d0, 100)})
        assert res.trades == [] and res.equity[-1].equity == D(800)


# ==========================================================================
# Aggregation
# ==========================================================================
def _tr(setup, score, pnl, r, reason="TARGET_1"):
    from backtest.models import TradeRecord
    z = D(0)
    return TradeRecord("REAL", 1, "15m", "A", "TST", et(DAYS[0], 10, 0), et(DAYS[0], 10, 0),
                       setup, "SWING", score, score_band(score), "HIGH", "RISK_ON", "XLK", "X", "BUY",
                       D(100), D(100), 1, D(100), D(98), D(106), None, D(2), gross_pnl=D(pnl),
                       realized_pnl=D(pnl), r_multiple=D(r), exit_reason=reason,
                       exit_ts=et(DAYS[0], 15, 0))


class TestAggregation:
    @pytest.mark.parametrize("score,band", [(None, "NO SCORE"), (0, "0-49"), (49, "0-49"),
                                            (50, "50-64"), (64, "50-64"), (65, "65-74"),
                                            (74, "65-74"), (75, "75-84"), (84, "75-84"),
                                            (85, "85-100"), (100, "85-100")])
    def test_score_band_edges(self, score, band):
        assert score_band(score) == band

    def test_score_band_aggregation(self):
        trades = [_tr("BREAKOUT", 80, 10, 1), _tr("BREAKOUT", 82, -5, -0.5, "STOP"),
                  _tr("PULLBACK_TO_SUPPORT", 55, -4, -0.4, "STOP")]
        g = dict(M.group(trades, lambda t: t.score_band, 1, M.BAND_ORDER))
        assert g["75-84"]["trades"] == 2 and g["75-84"]["net_pnl"] == D(5)
        assert g["75-84"]["win_rate"] == D("0.5") and g["75-84"]["profit_factor"] == D(2)
        assert g["50-64"]["trades"] == 1 and g["85-100"]["trades"] == 0

    def test_setup_aggregation_and_expectancy(self):
        trades = [_tr("BREAKOUT", 80, 10, 1), _tr("BREAKOUT", 80, -5, -0.5, "STOP"),
                  _tr("MEAN_REVERSION", 80, -2, -0.2, "STOP")]
        g = dict(M.group(trades, lambda t: t.setup_type, 1, M.SETUP_ORDER))
        assert g["BREAKOUT"]["expectancy"] == D("2.5")
        assert g["BREAKOUT"]["expectancy_r"] == D("0.25")
        assert g["BREAKOUT"]["target_1_hit_rate"] == D("0.5")
        assert g["MEAN_REVERSION"]["profit_factor"] == 0          # losses only
        assert M.best_worst(list(g.items())) == ("BREAKOUT", "MEAN_REVERSION")

    def test_small_samples_never_ranked(self):
        g = M.group([_tr("BREAKOUT", 80, 10, 1)], lambda t: t.setup_type, 30)
        assert g[0][1]["insufficient_sample"] and M.best_worst(g) == (None, None)

    def test_drawdown_contribution(self):
        t1, t2 = _tr("BREAKOUT", 80, -30, -1, "STOP"), _tr("MEAN_REVERSION", 80, -10, -1, "STOP")
        c = M.drawdown_contribution([t1, t2], et(DAYS[0], 9, 30), et(DAYS[0], 16, 0))
        assert c == {"BREAKOUT": D("0.75"), "MEAN_REVERSION": D("0.25")}


# ==========================================================================
# Calendar / decision times / determinism
# ==========================================================================
def test_calendar_from_bars_with_half_day():
    d1, d2 = date(2025, 11, 26), date(2025, 11, 28)
    bars = bars_5m(d1, [(1, 1, 1, 1)] * 78) + bars_5m(d2, [(1, 1, 1, 1)] * 42)   # 13:00 close
    cal = TradingCalendar.from_bars(bars, 300, ET)
    assert [s.end for s in cal.sessions] == [et(d1, 16, 0), et(d2, 13, 0)]
    times = decision_times([b for b in bars], "5m", cal, d1, d2)
    assert et(d2, 13, 0) not in times and et(d2, 12, 55) in times


def test_deterministic_repeatability(synth, tmp_path):
    bt, _, tmp = synth
    from backtest.reports import run_id
    a = Backtester(CFG, bt.store, 1, ["AAPL"], bt.start, bt.end, workers=1,
                   cache_dir=tmp_path / "c1", progress=lambda m: None).run()
    b = Backtester(CFG, bt.store, 1, ["AAPL"], bt.start, bt.end, workers=1,
                   cache_dir=tmp_path / "c2", progress=lambda m: None).run()
    assert [r.trades for r in a] == [r.trades for r in b]
    assert [r.shadow for r in a] == [r.shadow for r in b]
    assert [r.equity for r in a] == [r.equity for r in b]
    m = {"tier": 1, "symbols": ["AAPL"], "start": bt.start}
    assert run_id(m) == run_id(dict(m))


def test_variants_and_cadences_reported_separately(synth):
    bt, _, _ = synth
    specs = [(r.spec.cadence, r.spec.variant) for r in bt.run()]
    assert specs == [("15m", "A"), ("15m", "B"), ("1h", "A"), ("1h", "B")]


def test_backtest_uses_the_live_strategy_functions():
    import backtest.engine as E
    import strategy.proposal_engine as PE
    assert E.evaluate is PE.evaluate and E.risk_review is PE.risk_review
    import backtest.asof as A
    import analysis.setup_context as SC
    assert A.build_context is SC.build_context


def test_no_order_code_in_backtest():
    import re
    from pathlib import Path
    import config
    for f in (Path(config.BASE_DIR) / "backtest").glob("*.py"):
        assert not re.search(r"placeOrder|OrderManager|reqIds", f.read_text(encoding="utf-8")), f


# ==========================================================================
# Downloader (fake IBKR service): paging, chunk fallback, resume
# ==========================================================================
class FakeMD:
    """Serves synthetic daily bars for any (end, duration) request."""

    def __init__(self, fail_large=False):
        from market_data.contracts import ContractCheck
        self.calls = []
        self.fail_large = fail_large
        self.cc = ContractCheck("TST", True, make_info(symbol="TST", con_id=77))

    def resolve_contract(self, symbol):
        return self.cc

    def _fetch_bars(self, info, tf, session, duration, calendar, end_datetime=""):
        from backtest.data_loader import duration_days
        from market_data.market_data import MarketDataUnavailable
        self.calls.append((tf.key, duration, end_datetime))
        if self.fail_large and duration == "10 Y":
            raise MarketDataUnavailable("IBKR 162: request too large", 162)
        end = datetime.strptime(end_datetime, "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC)
        out, d = [], (end - timedelta(days=duration_days(duration))).date()
        while datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC) < end:
            if d.weekday() < 5:
                ts = datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)
                out.append(Bar(ts, D(100), D(101), D(99), D(100), D(1000)))
            d += timedelta(days=1)
        return out


@pytest.mark.parametrize("fail_large", [False, True])
def test_downloader_pages_and_resumes(tmp_path, fail_large):
    from backtest.data_loader import HistoryDownloader, HistoryStore
    store = HistoryStore(tmp_path, CFG)
    md = FakeMD(fail_large)
    dl = HistoryDownloader(md, store, CFG, progress=lambda m: None)
    start, end = date(2025, 1, 6), date(2025, 3, 28)
    dl.ensure("TST", ("1d",), start, end)
    bars = store.load("TST", "1d")
    assert bars and bars[-1].timestamp.astimezone(ET).date() <= end
    cov = store.coverage("TST", "1d")
    assert cov[0] <= start - timedelta(days=CFG.BACKTEST_WARMUP_DAYS["1d"]) and cov[1] == end
    if fail_large:
        assert any(c[1] == "1 Y" for c in md.calls)          # fell back to smaller chunk
    n = len(md.calls)
    dl.ensure("TST", ("1d",), start, end)                     # resume: nothing missing
    assert len(md.calls) == n
    assert store.load_contract("TST").con_id == 77


# ==========================================================================
# REGRESSION: pilot bug 1 - R measured against fill-to-stop risk
# ==========================================================================
class TestRegressionPlannedR:
    """Pilot 2026-10-11: TREND_CONTINUATION filled at 327.99 with stop 327.97
    (fill risk $0.02) and was reported as -98R. Planned risk was $19.92."""

    def _record(self, fill_px, max_entry="347.89", stop="327.97", qty=1, exit_px=None):
        s = mk_signal(max_entry=D(max_entry), stop=D(stop), t1=D(360), setup="TREND_CONTINUATION")
        f = Fill(s.ts, D(fill_px), D(fill_px), D(0), D(0), D("1.00"), "LIMIT")
        pos = OpenPosition("TST", s, qty, f, D(stop), D(360), None, s.ts, set(), D(fill_px), D(fill_px))
        x = D(exit_px or stop)
        exit_fill = Fill(s.ts, x - D("0.08"), x, D("0.03"), D("0.05"), D("1.00"), "MARKET")
        return make_record("SHADOW", SPEC_A, pos, exit_fill, "STOP_GAP", False, True, PRE_BREAKER)

    def test_near_stop_fill_reported_against_planned_risk(self):
        rec = self._record("327.99", exit_px="327.99")
        assert rec.initial_risk == D("19.92")
        assert rec.r_multiple == rec.realized_pnl / D("19.92")
        assert D("-0.2") < rec.r_multiple < 0                    # was about -98R
        assert rec.fill_risk == D("0.02") and rec.fill_r_multiple < -50   # diagnostic only

    def test_mfe_mae_and_gross_r_use_planned_risk(self):
        rec = self._record("330.00", exit_px="327.97")
        planned_ps = D("347.89") - D("327.97")
        assert rec.gross_r == (D("327.97") - D("330.00")) / planned_ps
        assert rec.mae_r == rec.mae / planned_ps

    @pytest.mark.parametrize("max_entry,stop", [("100", "100"), ("99", "100")])
    def test_zero_or_negative_planned_risk_is_safe(self, max_entry, stop):
        rec = self._record("100.5", max_entry=max_entry, stop=stop, exit_px="101")
        assert rec.risk_invalid and rec.r_multiple == 0 and rec.initial_risk == 0
        assert rec.mfe_r == 0 and rec.gross_r == 0

    def test_metrics_aggregate_planned_r(self):
        recs = [self._record("327.99", exit_px="327.99"), self._record("340", exit_px="350")]
        s = M.summarize(recs, None, 1)
        assert s["average_r"] == (recs[0].r_multiple + recs[1].r_multiple) / 2
        assert s["average_fill_r_diagnostic"] < s["average_r"]


# ==========================================================================
# REGRESSION: pilot bug 2 - shadow entries ignored the trigger
# ==========================================================================
from backtest.execution_simulator import StopLimitOrder, fill_entry, try_fill_stop_limit


class TestRegressionTriggeredEntries:
    """Pilot 2026-10-11: AWAITING_TRIGGER signal (price 331.92, trigger 346.07,
    maximum entry 347.89) was 'filled' at 327.99 by a plain limit order."""
    TRIG, LIM, STOP = D("346.07"), D("347.89"), D("327.97")

    def _order(self, t):
        s = mk_signal(entry=self.TRIG, max_entry=self.LIM, stop=self.STOP, t1=D(370),
                      setup="TREND_CONTINUATION", pre_risk_buy=False, ts=t)
        s.price_state, s.entry_zone_low = "AWAITING_TRIGGER", self.TRIG
        return StopLimitOrder("TST", s, 1, self.LIM, t, t + timedelta(hours=1), trigger=self.TRIG)

    def test_no_fill_below_trigger(self):
        t = et(date(2025, 3, 4), 10, 0)
        o = self._order(t)
        assert fill_entry(o, bar(t, "331", "333", "327.99", "328"), CM) is None   # old bug filled here
        assert not o.triggered

    def test_intrabar_trigger_fills_at_trigger_not_below(self):
        t = et(date(2025, 3, 4), 10, 0)
        f = fill_entry(self._order(t), bar(t, "345", "347", "344", "346.5"), CM)
        assert f.mode == "TRIGGER" and f.ref_price == self.TRIG
        assert self.TRIG <= f.price <= self.LIM

    def test_gap_open_between_trigger_and_limit_fills_at_open(self):
        t = et(date(2025, 3, 4), 10, 0)
        f = fill_entry(self._order(t), bar(t, "347", "348", "346.5", "347.5"), CM)
        assert f.mode == "OPEN" and f.ref_price == D(347) and f.price <= self.LIM

    def test_gap_above_limit_waits_for_pullback_to_limit(self):
        t = et(date(2025, 3, 4), 10, 0)
        o = self._order(t)
        assert fill_entry(o, bar(t, "350", "352", "349", "351"), CM) is None
        assert o.triggered                                   # triggered, but not marketable
        f = fill_entry(o, bar(t + timedelta(minutes=5), "349", "349.5", "347.5", "348"), CM)
        assert f.mode == "LIMIT" and f.price == self.LIM

    def test_trigger_bar_conservative_exit_handling(self):
        t = et(date(2025, 3, 4), 10, 0)
        o = self._order(t)
        b = bar(t, "345", "371", "327", "346")               # trigger, target AND stop all inside
        f = fill_entry(o, b, CM)
        pos = open_position(o, f, b, date(2025, 3, 4))
        r = check_exit(pos, b, ZERO_CM, entry_bar=True)
        assert r[1] == "STOP"                                 # target ignored, stop assumed

    def test_never_fills_before_submission_or_after_expiry(self):
        t = et(date(2025, 3, 4), 10, 0)
        o = self._order(t)
        assert try_fill_stop_limit(o, bar(t - timedelta(minutes=5), "347", "348", "346", "347"), CM) is None
        assert try_fill_stop_limit(o, bar(t + timedelta(hours=1), "347", "348", "346", "347"), CM) is None

    def test_shadow_simulation_uses_stop_limit_for_awaiting_trigger(self):
        d0 = DAYS[0]
        t = et(d0, 10, 0)
        s = mk_signal(entry=D(110), max_entry=D("110.5"), stop=D(98), t1=D(125),
                      setup="TREND_CONTINUATION", pre_risk_buy=False, ts=t)
        s.price_state, s.entry_zone_low = "AWAITING_TRIGGER", D(110)
        flat = _flat_day(d0, 100)                              # never reaches 110
        times = [t, et(d0, 15, 45)]
        out = simulate_shadow(SPEC_A, "TST", [s], CompactBars(flat, 300), calendar_for(DAYS),
                              times, CFG, ZERO_CM, D(800))
        assert out == []                                       # old code: filled at 100
        s2 = replace(s)
        s2.price_state = "IN_RANGE"                            # in range -> plain limit, fills
        out2 = simulate_shadow(SPEC_A, "TST", [s2], CompactBars(flat, 300), calendar_for(DAYS),
                               times, CFG, ZERO_CM, D(800))
        assert len(out2) == 1 and out2[0].entry_order_type == "LIMIT"


# ==========================================================================
# Real and shadow accounting stay independent
# ==========================================================================
class TestRealShadowIndependence:
    def test_real_results_unchanged_by_shadow_only_signals(self):
        d0 = DAYS[0]
        buy = mk_signal(ts=et(d0, 10, 0))
        noise = [mk_signal(ts=et(d0, 11, 0) + timedelta(minutes=15 * i), pre_risk_buy=False,
                           setup="PULLBACK_TO_SUPPORT") for i in range(8)]
        bars = {"TST": _flat_day(d0, 100) + _flat_day(DAYS[1], 101)}
        times = sorted({s.ts for s in [buy] + noise} | {et(d, 15, 45) for d in DAYS})
        a = _run({"TST": [buy]}, bars, times=times)          # same decision times,
        b = _run({"TST": [buy] + noise}, bars, times=times)  # only the signals differ
        assert a.trades == b.trades and a.equity == b.equity and a.shadow == b.shadow == []

    def test_shadow_results_unchanged_by_portfolio_state(self):
        d0 = DAYS[0]
        sigs = [mk_signal(ts=et(d0, 10, 0)), mk_signal(ts=et(DAYS[1], 10, 0), setup="PULLBACK_TO_SUPPORT")]
        bars = CompactBars(_flat_day(d0, 100) + _flat_day(DAYS[1], 100), 300)
        times = sorted({s.ts for s in sigs} | {et(d, 15, 45) for d in DAYS[:2]})
        cal = calendar_for(DAYS)
        no_breaker = simulate_shadow(SPEC_A, "TST", sigs, bars, cal, times, CFG, ZERO_CM, D(800))
        breaker = simulate_shadow(SPEC_A, "TST", sigs, bars, cal, times, CFG, ZERO_CM, D(800),
                                  breaker_ts=et(d0, 12, 0))
        strip = lambda trs: [replace(t, label="") for t in trs]
        assert strip(no_breaker) == strip(breaker)             # only the labels differ
        assert {t.label for t in breaker} == {PRE_BREAKER, POST_BREAKER}

    def test_shadow_simulation_has_no_ledger_access(self):
        import inspect
        src = inspect.getsource(simulate_shadow)
        assert "ledger" not in src and "cash" not in src
