"""Config validation, message classification, system state, paper verification."""

import logging
from decimal import Decimal

import pytest

import config
from broker import messages
from broker.account import IbkrAccountSummary, verify_paper_account
from broker.messages import Impact
from conftest import make_cfg
from execution.order_manager import OrderManager, TradingDisabledError
from risk.system_state import StateController, SystemState


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class TestConfig:
    def test_shipped_config_is_safe(self):
        config.validate_config()
        assert config.BOT_CAPITAL == Decimal("800.00")
        assert config.IBKR_PORT == 7497

    @pytest.mark.parametrize("override", [
        {"TRADING_MODE": "LIVE"},
        {"LIVE_TRADING_ALLOWED": True},
        {"MARGIN_ALLOWED": True},
        {"SHORT_SELLING_ALLOWED": True},
        {"ORDER_PLACEMENT_ENABLED": True},
        {"IBKR_PORT": 7496},
        {"IBKR_PORT": 4001},
        {"IBKR_PORT": 1234},
        {"IBKR_HOST": "10.0.0.5"},
        {"BOT_CAPITAL": 800.0},
        {"MAX_RISK_PER_TRADE": Decimal("0.05")},
        {"MAX_PORTFOLIO_DRAWDOWN": Decimal("0.5")},
        {"ALLOW_DELAYED_DATA_FOR_TRADING": True},
        {"ALLOW_FROZEN_DATA_FOR_TRADING": True},
        {"USE_RTH": False},
        {"MARKET_DATA_TYPE": "REALTIME_PLEASE"},
        {"HIST_PACING_MAX_REQUESTS": 60},
        {"ALLOWED_STOCK_TYPES": frozenset({"COMMON", "ETN"})},
    ])
    def test_unsafe_config_refused(self, override):
        with pytest.raises(config.ConfigError):
            config.validate_config(make_cfg(**override))


# --------------------------------------------------------------------------
# IBKR message classification
# --------------------------------------------------------------------------
class TestMessages:
    @pytest.mark.parametrize("code", [2104, 2106, 2158, 2107, 2108, 2119])
    def test_farm_ok_messages_are_info(self, code):
        mc = messages.classify(code)
        assert mc.level == logging.INFO and mc.impact is Impact.NONE

    def test_unknown_code_is_warning_not_fatal(self):
        mc = messages.classify(987654)
        assert mc.level == logging.WARNING and mc.impact is Impact.NONE

    def test_garbage_code(self):
        assert messages.classify("abc").impact is Impact.NONE

    @pytest.mark.parametrize("code", [1100, 502, 504, 326, 2110])
    def test_connectivity_failures_have_impact(self, code):
        assert messages.classify(code).impact is Impact.CONNECTION

    @pytest.mark.parametrize("code", [1101, 1102])
    def test_restored_requires_reverification(self, code):
        assert messages.classify(code).restores_connection

    @pytest.mark.parametrize("code", [162, 200, 354, 366, 10089, 10168, 10197])
    def test_market_data_failures_are_request_scoped(self, code):
        mc = messages.classify(code)
        assert mc.impact is Impact.REQUEST and mc.fails_request

    def test_unknown_error_range_fails_its_request(self):
        assert messages.classify(999).fails_request
        assert not messages.classify(2199).fails_request

    def test_farm_name_parsing(self):
        assert messages.farm_name("Market data farm connection is OK:usfarm") == "usfarm"

    def test_farm_broken_is_warning_only(self):
        assert messages.classify(2103).level == logging.WARNING
        assert messages.classify(2103).impact is Impact.NONE


# --------------------------------------------------------------------------
# System state controller
# --------------------------------------------------------------------------
class TestSystemState:
    def test_starts_initializing(self):
        s = StateController()
        assert s.state is SystemState.INITIALIZING
        assert not s.trading_permitted()

    def test_safe_only_after_verification_without_reasons(self):
        s = StateController()
        s.mark_verification_complete()
        assert s.state is SystemState.SAFE and s.trading_permitted()

    def test_any_reason_forces_safe_mode(self):
        s = StateController()
        s.mark_verification_complete()
        s.enter_safe_mode("X", "boom")
        assert s.state is SystemState.SAFE_MODE
        s.clear("X")
        assert s.state is SystemState.SAFE

    def test_invalidate_requires_new_cycle(self):
        s = StateController()
        s.mark_verification_complete()
        s.invalidate_verification()
        assert s.state is SystemState.SAFE_MODE
        s.mark_verification_complete()
        assert s.state is SystemState.SAFE

    def test_listener_errors_do_not_break_state(self):
        def bad(*_):
            raise RuntimeError
        s = StateController(on_change=bad)
        s.enter_safe_mode("X", "y")
        assert s.state is SystemState.SAFE_MODE


# --------------------------------------------------------------------------
# Paper account verification
# --------------------------------------------------------------------------
def summary(acct="DU1234567", seen=None):
    return IbkrAccountSummary(acct, "INDIVIDUAL", Decimal("1000000"), Decimal("1000000"),
                              None, None, "USD", tuple(seen or [acct]))


class TestPaperVerification:
    def test_all_checks_pass(self, cfg):
        v = verify_paper_account(cfg, 7497, ["DU1234567"], summary())
        assert v.passed, v.failures
        assert len(v.checks) >= 5

    @pytest.mark.parametrize("port,accounts,summ,failed", [
        (7496, ["DU1234567"], summary(), "paper_port"),
        (7497, ["U1234567"], summary("U1234567"), "paper_account_prefix"),
        (7497, ["DU1", "DU2"], summary("DU1", ["DU1", "DU2"]), "single_managed_account"),
        (7497, [], None, "single_managed_account"),
        (7497, ["DU1234567"], None, "summary_account_match"),
        (7497, ["DU1234567"], summary("DU7654321"), "summary_account_match"),
    ])
    def test_failures(self, cfg, port, accounts, summ, failed):
        v = verify_paper_account(cfg, port, accounts, summ)
        assert not v.passed
        assert failed in {c.name for c in v.failures}

    def test_prefix_alone_is_not_enough(self, cfg):
        # DU account but on the live port -> must fail
        assert not verify_paper_account(cfg, 7496, ["DU1234567"], summary()).passed

    def test_pinned_account(self):
        cfg = make_cfg(EXPECTED_ACCOUNT_ID="DU0000001")
        v = verify_paper_account(cfg, 7497, ["DU1234567"], summary())
        assert "expected_account_id" in {c.name for c in v.failures}


def test_order_manager_cannot_place_orders(cfg):
    with pytest.raises(TradingDisabledError):
        OrderManager(cfg).submit("SPY", 1)


def test_no_place_order_call_in_codebase():
    import re
    from pathlib import Path
    root = Path(config.BASE_DIR)
    call = re.compile(r"\.\s*placeOrder\s*\(")   # method calls, not prose
    offenders = [p for p in root.rglob("*.py")
                 if "tests" not in p.parts and call.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
