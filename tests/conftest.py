import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import config
from market_data.models import Bar, MarketDataType
from market_data.validation import MarketDataCheck
from portfolio.portfolio_manager import compute_snapshot, new_ledger
from risk.risk_engine import TradeProposal

UTC = timezone.utc


def make_cfg(**overrides):
    """A copy of the real config module's constants with optional overrides."""
    values = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    values.update(overrides)
    return types.SimpleNamespace(**values)


@pytest.fixture
def cfg():
    return make_cfg()


def snapshot(cfg, cash="800.00", peak="800.00", positions=None, realized=None):
    """BOT strategy snapshot from a ledger with the given cash/peak."""
    led = new_ledger("DU1234567", cfg.BOT_CAPITAL)
    led.cash = Decimal(cash)
    led.peak_equity = Decimal(peak)
    led.positions = positions or []
    if realized is not None:
        led.realized_pnl = Decimal(realized)
    return compute_snapshot(led, cfg)


def live_check(symbol="SPY", data_type=MarketDataType.LIVE, market_open=True,
               contract_ok=True, hist_valid=True, hist_stale=False,
               quote_available=True, quote_valid=True, quote_stale=False,
               checked_at=None, usable_for_trading=True, trading_blockers=None):
    """A MarketDataCheck; defaults = fresh, valid, LIVE (what a tradable symbol needs)."""
    from market_data.validation import HistoricalStatus, QuoteStatus, TimeframeStatus
    tf = TimeframeStatus("1d", hist_valid, hist_stale, 250 if hist_valid else 0,
                         errors=[] if hist_valid else ["bad bars"])
    hist = HistoricalStatus({"1d": tf}, None)
    quote = QuoteStatus(quote_available, quote_valid, data_type, quote_stale,
                        errors=[] if quote_valid else ["crossed quote"])
    return MarketDataCheck(symbol=symbol, checked_at=checked_at or datetime.now(UTC),
                           market_open=market_open, contract_ok=contract_ok,
                           historical=hist, quote=quote,
                           usable_for_trading=usable_for_trading,
                           trading_blockers=trading_blockers or [])


def proposal(**kw):
    base = dict(symbol="SPY", sec_type="STK", currency="USD", exchange="SMART",
                primary_exchange="ARCA", side="BUY", quantity=1,
                entry_price="100.00", stop_price="98.00", analysis_mode="EXECUTION_READY")
    base.update(kw)
    if "market_data" not in kw:
        base["market_data"] = live_check(str(base["symbol"]).strip() or "SPY")
    return TradeProposal(**base)


def make_bar(ts, o, h, l, c, v=1000, wap=None):
    d = lambda x: None if x is None else Decimal(str(x))
    return Bar(ts, d(o), d(h), d(l), d(c), d(v), d(wap))


def daily_bars(closes, start=datetime(2026, 1, 5, 5, tzinfo=UTC), spread=1, volume=1000):
    """Daily bars (one per calendar day) with high/low = close +/- spread."""
    return [make_bar(start + timedelta(days=i), c, c + spread, c - spread, c, volume)
            for i, c in enumerate(closes)]


# --------------------------------------------------------------------------
# Market-data fixtures: week of 2026-10-05 (Mon) .. 2026-10-09 (Fri), EDT.
# 09:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC.
# --------------------------------------------------------------------------
LIQUID_HOURS = ";".join(
    [f"202610{d:02d}:0930-202610{d:02d}:1600" for d in range(5, 10)]
    + ["20261010:CLOSED", "20261011:CLOSED"])


def make_info(symbol="AAPL", stock_type="COMMON", long_name="APPLE INC", con_id=265598,
              sec_type="STK", currency="USD", primary="NASDAQ", tz="US/Eastern",
              liquid=LIQUID_HOURS, **kw):
    from market_data.models import ContractInfo
    base = dict(con_id=con_id, symbol=symbol, sec_type=sec_type, stock_type=stock_type,
                exchange="SMART", primary_exchange=primary, currency=currency,
                long_name=long_name, min_tick=Decimal("0.01"), time_zone_id=tz,
                liquid_hours=liquid, trading_hours=liquid)
    base.update(kw)
    return ContractInfo(**base)


@pytest.fixture
def calendar():
    from market_data.sessions import TradingCalendar
    return TradingCalendar.from_contract(make_info())


def utc(*args):
    return datetime(*args, tzinfo=UTC)
