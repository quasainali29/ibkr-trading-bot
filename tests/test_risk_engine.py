from decimal import Decimal

import pytest

from conftest import make_cfg, proposal, snapshot
from risk.risk_engine import Reason, RiskEngine, calculate_max_quantity
from risk.system_state import SystemState

SAFE = SystemState.SAFE


@pytest.fixture
def engine(cfg):
    return RiskEngine(cfg)


def ev(engine, cfg, state=SAFE, snap=None, **kw):
    return engine.evaluate(proposal(**kw), snap or snapshot(cfg), state)


# --------------------------------------------------------------------------
# Sizing formula
# --------------------------------------------------------------------------
class TestSizing:
    def test_initial_budgets(self, cfg):
        s = snapshot(cfg)
        assert s.bot_strategy_equity == Decimal("800.00")
        assert s.max_risk_per_trade_dollars == Decimal("16.00")
        assert s.max_drawdown_dollars == Decimal("120.00")

    def test_formula_risk_bound(self):
        # $16 / $0.30 = 53.33 -> 53 by risk; capital cap 800/10 = 80
        r = calculate_max_quantity(Decimal("800"), Decimal("800"),
                                   Decimal("10.00"), Decimal("9.70"), Decimal("0.02"))
        risk_dollars, rps, by_risk, by_cap, mx = r
        assert risk_dollars == Decimal("16.00")
        assert rps == Decimal("0.30")
        assert (by_risk, by_cap, mx) == (53, 80, 53)

    def test_formula_capital_bound(self):
        # $16 / $0.50 = 32 by risk, but only floor(800/50) = 16 affordable
        *_, by_risk, by_cap, mx = calculate_max_quantity(
            Decimal("800"), Decimal("800"), Decimal("50"), Decimal("49.50"),
            Decimal("0.02"))
        assert (by_risk, by_cap, mx) == (32, 16, 16)

    def test_floor_never_rounds_up(self):
        # 16 / 3 = 5.333 -> 5
        *_, mx = calculate_max_quantity(Decimal("800"), Decimal("800"),
                                        Decimal("20"), Decimal("17"), Decimal("0.02"))
        assert mx == 5

    def test_risk_scales_with_bot_equity_not_ibkr(self, engine, cfg):
        s = snapshot(cfg, cash="700.00", peak="800.00")  # equity fell to 700
        d = engine.evaluate(proposal(quantity=7, entry_price="10", stop_price="8"), s, SAFE)
        assert d.risk_budget == Decimal("14.00")
        assert d.max_quantity == 7
        assert d.approved

    def test_decimal_no_float_artifacts(self, engine, cfg):
        # 0.1 + 0.2 style float errors must not affect the result
        d = ev(engine, cfg, quantity=160, entry_price=0.3, stop_price=0.2)
        assert d.risk_per_share == Decimal("0.1")
        assert d.planned_risk == Decimal("16.0")
        assert d.approved


# --------------------------------------------------------------------------
# Approvals and the 2% boundary
# --------------------------------------------------------------------------
class TestRiskLimit:
    def test_valid_trade_approved(self, engine, cfg):
        d = ev(engine, cfg, quantity=8)       # 8 * $2 = $16, cost $800
        assert d.approved, d.rejections
        assert d.planned_risk == Decimal("16.00")

    def test_exactly_two_percent_is_allowed(self, engine, cfg):
        d = ev(engine, cfg, quantity=16, entry_price="40", stop_price="39")
        assert d.planned_risk == Decimal("16")
        assert d.approved, d.rejections

    def test_one_cent_over_two_percent_rejected(self, engine, cfg):
        # 1 share * $16.01 risk > $16.00
        d = ev(engine, cfg, quantity=1, entry_price="100.00", stop_price="83.99")
        assert Reason.RISK_TOO_HIGH in d.reasons
        assert not d.approved

    def test_quantity_above_max_rejected(self, engine, cfg):
        d = ev(engine, cfg, quantity=9, entry_price="50", stop_price="48")
        assert Reason.QUANTITY_EXCEEDS_MAX in d.reasons
        assert Reason.RISK_TOO_HIGH in d.reasons

    def test_engine_never_upsizes(self, engine, cfg):
        d = ev(engine, cfg, quantity=1)
        assert d.approved and d.max_quantity == 8
        # decision reports the cap but the requested quantity is what was checked
        assert d.planned_risk == Decimal("2.00")


# --------------------------------------------------------------------------
# Capital / margin
# --------------------------------------------------------------------------
class TestCapital:
    def test_exceeds_available_bot_capital(self, engine, cfg):
        # risk OK ($0.10 * 100 = $10) but cost $1000 > $800
        d = ev(engine, cfg, quantity=100, entry_price="10.00", stop_price="9.90")
        assert Reason.EXCEEDS_BOT_CAPITAL in d.reasons
        assert not d.approved

    def test_ibkr_million_is_irrelevant(self, engine, cfg):
        # $10,000 position: affordable in IBKR paper account, not for the bot
        d = ev(engine, cfg, quantity=100, entry_price="100", stop_price="99.90")
        assert Reason.EXCEEDS_BOT_CAPITAL in d.reasons

    def test_uses_available_cash_not_initial(self, engine, cfg):
        s = snapshot(cfg, cash="300.00", peak="800.00", realized="-500.00")
        d = engine.evaluate(proposal(quantity=4, entry_price="100", stop_price="99"), s, SAFE)
        assert Reason.EXCEEDS_BOT_CAPITAL in d.reasons

    def test_margin_request_rejected(self, engine, cfg):
        d = ev(engine, cfg, quantity=1, use_margin=True)
        assert Reason.MARGIN_REQUIRED in d.reasons

    def test_margin_enabled_config_rejected(self, cfg):
        unsafe = make_cfg(MARGIN_ALLOWED=True)
        d = RiskEngine(unsafe).evaluate(proposal(), snapshot(unsafe), SAFE)
        assert Reason.CONFIG_UNSAFE in d.reasons

    def test_live_mode_rejected(self):
        unsafe = make_cfg(TRADING_MODE="LIVE", LIVE_TRADING_ALLOWED=True)
        d = RiskEngine(unsafe).evaluate(proposal(), snapshot(unsafe), SAFE)
        assert Reason.LIVE_TRADING_BLOCKED in d.reasons


# --------------------------------------------------------------------------
# Prohibited securities
# --------------------------------------------------------------------------
class TestSecurities:
    @pytest.mark.parametrize("sec_type",
                             ["OPT", "FUT", "FOP", "CASH", "CRYPTO", "CFD", "WAR",
                              "BOND", "IND", "BAG", "", "XYZ"])
    def test_prohibited_sec_types(self, engine, cfg, sec_type):
        d = ev(engine, cfg, sec_type=sec_type)
        assert Reason.PROHIBITED_SECURITY in d.reasons

    def test_stk_lowercase_allowed(self, engine, cfg):
        assert ev(engine, cfg, sec_type="stk").approved

    @pytest.mark.parametrize("currency", ["EUR", "CAD", "", None])
    def test_non_usd_rejected(self, engine, cfg, currency):
        assert Reason.PROHIBITED_SECURITY in ev(engine, cfg, currency=currency).reasons

    @pytest.mark.parametrize("exchange,primary", [
        ("LSE", ""), ("TSE", ""), ("SMART", "LSE"), ("SMART", ""), ("", ""),
        ("NYSE", "TSX"),
    ])
    def test_non_us_exchange_rejected(self, engine, cfg, exchange, primary):
        d = ev(engine, cfg, exchange=exchange, primary_exchange=primary)
        assert Reason.PROHIBITED_SECURITY in d.reasons

    @pytest.mark.parametrize("exchange,primary", [
        ("SMART", "NASDAQ"), ("SMART", "NYSE"), ("SMART", "ARCA"),
        ("NYSE", ""), ("ARCA", "ARCA"),
    ])
    def test_us_exchanges_allowed(self, engine, cfg, exchange, primary):
        assert ev(engine, cfg, exchange=exchange, primary_exchange=primary).approved

    def test_missing_symbol(self, engine, cfg):
        assert Reason.INVALID_SYMBOL in ev(engine, cfg, symbol="  ").reasons


# --------------------------------------------------------------------------
# Stop / entry / quantity validation
# --------------------------------------------------------------------------
class TestInputValidation:
    @pytest.mark.parametrize("stop", [None, "", "abc", "nan", "inf", 0, -1, True])
    def test_invalid_stop(self, engine, cfg, stop):
        d = ev(engine, cfg, stop_price=stop)
        assert Reason.INVALID_STOP in d.reasons
        assert not d.approved

    def test_stop_equal_to_entry_rejected(self, engine, cfg):
        d = ev(engine, cfg, entry_price="100", stop_price="100")
        assert Reason.INVALID_STOP in d.reasons

    def test_stop_above_entry_rejected_for_long(self, engine, cfg):
        d = ev(engine, cfg, entry_price="100", stop_price="101")
        assert Reason.INVALID_STOP in d.reasons

    @pytest.mark.parametrize("entry", [None, "x", 0, -5, float("nan")])
    def test_invalid_entry(self, engine, cfg, entry):
        assert Reason.INVALID_ENTRY in ev(engine, cfg, entry_price=entry).reasons

    @pytest.mark.parametrize("qty", [0, -1, 1.5, "3", None, True, Decimal("2.5")])
    def test_invalid_quantity(self, engine, cfg, qty):
        assert Reason.INVALID_QUANTITY in ev(engine, cfg, quantity=qty).reasons

    def test_whole_decimal_quantity_accepted(self, engine, cfg):
        assert ev(engine, cfg, quantity=Decimal("2")).approved

    @pytest.mark.parametrize("side", ["SELL", "SSHORT", "", None])
    def test_short_rejected(self, engine, cfg, side):
        assert Reason.SHORT_NOT_ALLOWED in ev(engine, cfg, side=side).reasons


# --------------------------------------------------------------------------
# Drawdown and system state
# --------------------------------------------------------------------------
class TestDrawdownAndState:
    def test_drawdown_at_exactly_15_percent_halts(self, engine, cfg):
        s = snapshot(cfg, cash="680.00", peak="800.00", realized="-120.00")
        assert s.bot_drawdown_pct == Decimal("0.15")
        d = engine.evaluate(proposal(quantity=1), s, SAFE)
        assert Reason.DRAWDOWN_HALT in d.reasons

    def test_drawdown_just_below_limit_allowed(self, engine, cfg):
        s = snapshot(cfg, cash="680.01", peak="800.00", realized="-119.99")
        assert engine.evaluate(proposal(quantity=1), s, SAFE).approved

    def test_drawdown_measured_from_peak(self, engine, cfg):
        # peak 1000 -> halt at 850 even though equity is above the initial 800
        s = snapshot(cfg, cash="850.00", peak="1000.00", realized="50.00")
        assert s.bot_drawdown_dollars == Decimal("150.00")
        assert s.max_drawdown_dollars == Decimal("150.00")
        d = engine.evaluate(proposal(quantity=1), s, SAFE)
        assert Reason.DRAWDOWN_HALT in d.reasons

    @pytest.mark.parametrize("state", [SystemState.SAFE_MODE, SystemState.INITIALIZING])
    def test_non_safe_state_rejects_everything(self, engine, cfg, state):
        d = ev(engine, cfg, state=state, quantity=1)
        assert d.reasons == [Reason.SAFE_MODE_ACTIVE]
        assert not d.approved

    def test_missing_ledger_rejected(self, engine, cfg):
        d = engine.evaluate(proposal(), None, SAFE)
        assert Reason.INVALID_LEDGER in d.reasons

    def test_all_reasons_reported(self, engine, cfg):
        d = ev(engine, cfg, state=SystemState.SAFE_MODE, sec_type="OPT",
               side="SELL", stop_price=None, use_margin=True)
        assert {Reason.SAFE_MODE_ACTIVE, Reason.PROHIBITED_SECURITY,
                Reason.SHORT_NOT_ALLOWED, Reason.INVALID_STOP,
                Reason.MARGIN_REQUIRED} <= set(d.reasons)

    def test_decision_serializable(self, engine, cfg):
        import json
        json.dumps(ev(engine, cfg).to_dict())


# --------------------------------------------------------------------------
# Market data gate (Phase 2)
# --------------------------------------------------------------------------
class TestMarketDataGate:
    def _md(self, **kw):
        from conftest import live_check
        return live_check("SPY", **kw)

    def test_fresh_live_data_allows(self, engine, cfg):
        assert ev(engine, cfg, market_data=self._md()).approved

    def test_missing_market_data_rejected(self, engine, cfg):
        d = ev(engine, cfg, market_data=None)
        assert d.reasons == [Reason.MARKET_DATA_UNAVAILABLE]

    @pytest.mark.parametrize("dtype", ["DELAYED", "DELAYED_FROZEN", "FROZEN"])
    def test_delayed_or_frozen_rejected(self, engine, cfg, dtype):
        from market_data.models import MarketDataType
        d = ev(engine, cfg, market_data=self._md(data_type=MarketDataType[dtype],
                                                 usable_for_trading=False))
        assert Reason.MARKET_DATA_DELAYED in d.reasons

    def test_unknown_data_type_rejected(self, engine, cfg):
        d = ev(engine, cfg, market_data=self._md(data_type=None))
        assert Reason.MARKET_DATA_UNAVAILABLE in d.reasons

    @pytest.mark.parametrize("kw", [dict(hist_stale=True), dict(quote_stale=True)])
    def test_stale_rejected(self, engine, cfg, kw):
        d = ev(engine, cfg, market_data=self._md(usable_for_trading=False, **kw))
        assert Reason.MARKET_DATA_STALE in d.reasons

    def test_old_check_rejected(self, engine, cfg):
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        d = ev(engine, cfg, market_data=self._md(checked_at=old))
        assert Reason.MARKET_DATA_STALE in d.reasons

    @pytest.mark.parametrize("kw", [dict(hist_valid=False), dict(quote_valid=False),
                                    dict(contract_ok=False)])
    def test_invalid_rejected(self, engine, cfg, kw):
        d = ev(engine, cfg, market_data=self._md(usable_for_trading=False, **kw))
        assert Reason.MARKET_DATA_INVALID in d.reasons

    def test_quote_unavailable_rejected(self, engine, cfg):
        d = ev(engine, cfg, market_data=self._md(quote_available=False, data_type=None,
                                                 usable_for_trading=False))
        assert Reason.MARKET_DATA_UNAVAILABLE in d.reasons
        assert "live quote unavailable" in d.rejections[0].message

    def test_valid_history_alone_never_authorizes(self, engine, cfg):
        # Market closed + no quote: candles fine for analysis, still no trade.
        d = ev(engine, cfg, market_data=self._md(
            market_open=False, quote_available=False, data_type=None,
            usable_for_trading=False,
            trading_blockers=["regular market closed", "live quote unavailable"]))
        assert not d.approved

    def test_other_symbol_rejected(self, engine, cfg):
        from conftest import live_check
        d = ev(engine, cfg, market_data=live_check("QQQ"))
        assert Reason.MARKET_DATA_INVALID in d.reasons

    def test_market_closed_rejected(self, engine, cfg):
        d = ev(engine, cfg, market_data=self._md(
            market_open=False, usable_for_trading=False,
            trading_blockers=["regular market closed"]))
        assert d.reasons == [Reason.MARKET_DATA_UNAVAILABLE]
        assert "regular market closed" in d.rejections[0].message