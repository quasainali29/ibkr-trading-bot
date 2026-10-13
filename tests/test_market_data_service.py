"""MarketDataService plumbing with a fake IBKR connection (no TWS needed)."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from ibapi.common import UNSET_DECIMAL

from broker.connection import IBKRConnection
from broker.requests import RequestRegistry
from conftest import make_info, utc
from market_data.health import MarketDataHealth, R_MD_WIDESPREAD, diagnose
from market_data.market_data import (HistoricalPacer, MarketDataService,
                                     MarketDataUnavailable, parse_ib_bar)
from market_data.models import TIMEFRAMES, MarketDataType
from risk.system_state import StateController, SystemState

ET = ZoneInfo("America/New_York")


def raw_bar(date, o=10, h=11, l=9, c=10, v=Decimal(100), wap=Decimal("10.1"), n=5):
    return SimpleNamespace(date=date, open=o, high=h, low=l, close=c, volume=v,
                           wap=wap, barCount=n)


# --------------------------------------------------------------------------
# Bar parsing
# --------------------------------------------------------------------------
class TestParse:
    def test_intraday_epoch(self):
        b = parse_ib_bar(raw_bar("1791293400"), ET)
        assert b.timestamp == utc(2026, 10, 6, 13, 30)
        assert b.close == Decimal(10) and b.wap == Decimal("10.1")

    def test_daily_date(self):
        b = parse_ib_bar(raw_bar("20261006"), ET)
        assert b.timestamp.astimezone(ET).date().isoformat() == "2026-10-06"

    def test_unset_volume_becomes_none(self):
        b = parse_ib_bar(raw_bar("20261006", v=UNSET_DECIMAL, wap=UNSET_DECIMAL), ET)
        assert b.volume is None and b.wap is None

    def test_garbage_date(self):
        assert parse_ib_bar(raw_bar("not a date"), ET).timestamp is None

    def test_float_artifacts_avoided(self):
        assert parse_ib_bar(raw_bar("20261006", c=0.1 + 0.2), ET).close == Decimal("0.30000000000000004")


# --------------------------------------------------------------------------
# Pacing + incremental refresh
# --------------------------------------------------------------------------
def test_pacer_blocks_identical_requests(cfg):
    t = [0.0]
    slept = []
    pacer = HistoricalPacer(cfg, clock=lambda: t[0], sleep=lambda s: (slept.append(s),
                                                                      t.__setitem__(0, t[0] + s)))
    pacer.acquire("k", 1)
    pacer.acquire("k", 1)
    assert slept and slept[0] == pytest.approx(cfg.HIST_IDENTICAL_MIN_INTERVAL_SECONDS)


def test_pacer_same_contract_burst(cfg):
    t = [0.0]
    slept = []
    pacer = HistoricalPacer(cfg, clock=lambda: t[0], sleep=lambda s: slept.append(s))
    for i in range(cfg.HIST_SAME_CONTRACT_MAX):
        pacer.acquire(f"k{i}", 1)
    pacer.acquire("other", 1)
    assert slept and slept[0] == pytest.approx(cfg.HIST_SAME_CONTRACT_WINDOW_SECONDS)


def test_pacer_refuses_long_wait(cfg):
    t = [0.0]
    pacer = HistoricalPacer(cfg, clock=lambda: t[0], sleep=lambda s: None)
    for i in range(cfg.HIST_PACING_MAX_REQUESTS):
        pacer._all.append(0.0)
    with pytest.raises(MarketDataUnavailable):
        pacer.acquire("x", 99)


@pytest.mark.parametrize("tf,gap_hours,expected", [
    ("5m", 1, "2 D"), ("5m", 30, "3 D"), ("5m", 24 * 20, None),
    ("1d", 24, "4 D"), ("1d", 24 * 400, None), ("1m", 0.1, "2 D"),
])
def test_incremental_duration(tf, gap_hours, expected):
    assert MarketDataService._incremental_duration(TIMEFRAMES[tf], gap_hours * 3600) == expected


# --------------------------------------------------------------------------
# Quotes via a fake connection: delayed labelling end-to-end
# --------------------------------------------------------------------------
class FakeApp:
    def __init__(self, svc_ref, script):
        self.svc_ref, self.script = svc_ref, script
        self.requested_types = []

    def reqMarketDataType(self, t):
        self.requested_types.append(t)

    def reqMktData(self, rid, contract, ticks, snapshot, regulatory, opts):
        svc = self.svc_ref[0]
        for name, *args in self.script:
            getattr(svc, name)(rid, *args)

    def cancelMktData(self, rid):
        pass


class FakeConn:
    def __init__(self, script):
        self.requests = RequestRegistry()
        self.listeners = []
        self.svc = [None]
        self.app = FakeApp(self.svc, script)

    def add_listener(self, x):
        self.listeners.append(x)
        self.svc[0] = x

    def is_connected(self):
        return True


def service(cfg, script, tmp_path):
    from market_data.candle_store import CandleStore
    conn = FakeConn(script)
    svc = MarketDataService(conn, cfg, CandleStore(tmp_path, cfg),
                            MarketDataHealth(StateController(), cfg))
    return svc


def test_delayed_ticks_without_type_callback_labelled_delayed(cfg, tmp_path):
    svc = service(cfg, [("tickPrice", 68, 101.5, None), ("tickPrice", 75, 100.0, None),
                        ("tickSize", 74, Decimal(5000)), ("tickString", 88, "1791300000"),
                        ("tickSnapshotEnd",)], tmp_path)
    q = svc.get_quote(make_info())
    assert q.data_type is MarketDataType.DELAYED and q.delayed_ticks_seen
    assert q.last == Decimal("101.5") and q.prev_close == Decimal(100)
    assert q.day_volume == Decimal(5000)


def test_reported_type_wins(cfg, tmp_path):
    svc = service(cfg, [("marketDataType", 4), ("tickPrice", 68, 101.5, None),
                        ("tickSnapshotEnd",)], tmp_path)
    assert svc.get_quote(make_info()).data_type is MarketDataType.DELAYED_FROZEN


def test_live_quote(cfg, tmp_path):
    svc = service(cfg, [("marketDataType", 1), ("tickPrice", 4, 101.5, None),
                        ("tickPrice", 1, 101.4, None), ("tickPrice", 2, 101.6, None),
                        ("tickSnapshotEnd",)], tmp_path)
    q = svc.get_quote(make_info())
    assert q.data_type is MarketDataType.LIVE and not q.delayed_ticks_seen


def test_invalid_tick_prices_ignored(cfg, tmp_path):
    svc = service(cfg, [("marketDataType", 1), ("tickPrice", 4, -1.0, None),
                        ("tickPrice", 1, 0.0, None), ("tickSnapshotEnd",)], tmp_path)
    with pytest.raises(MarketDataUnavailable):
        svc.get_quote(make_info())


# --------------------------------------------------------------------------
# Error routing: per-ticker vs global SAFE_MODE
# --------------------------------------------------------------------------
@pytest.fixture
def conn():
    state = StateController()
    state.mark_verification_complete()
    return IBKRConnection(state, "127.0.0.1", 7497, 1)


@pytest.mark.parametrize("code", [200, 354, 162, 10089, 10197, 321, 322])
def test_symbol_request_errors_do_not_trigger_global_safe_mode(conn, code):
    req = conn.requests.register("quote", "AAPL quote", scope="SYMBOL")
    conn._on_error(req.req_id, code, "some failure")
    assert req.error == (code, "some failure") and req.done.is_set()
    assert conn.state.state is SystemState.SAFE


def test_delayed_notice_does_not_fail_request(conn):
    req = conn.requests.register("quote", "AAPL quote", scope="SYMBOL")
    conn._on_error(req.req_id, 10167, "Displaying delayed market data")
    assert req.error is None and not req.done.is_set()


def test_connection_error_is_still_global(conn):
    req = conn.requests.register("quote", "AAPL quote", scope="SYMBOL")
    conn.status = conn.status.CONNECTED
    conn._on_error(req.req_id, 1100, "Connectivity lost")
    assert conn.state.state is SystemState.SAFE_MODE
    assert req.done.is_set()          # pending requests are failed


def test_system_request_error_is_global(conn):
    req = conn.requests.register("account_summary", "account summary")
    conn._on_error(req.req_id, 322, "Error processing request")
    assert conn.state.state is SystemState.SAFE_MODE


def test_farm_status_tracked(conn):
    conn._on_error(-1, 2104, "Market data farm connection is OK:usfarm")
    conn._on_error(-1, 2103, "Market data farm connection is broken:usfarm.nj")
    assert conn.farm_status == {"usfarm": True, "usfarm.nj": False}
    assert conn.state.state is SystemState.SAFE


# --------------------------------------------------------------------------
# Widespread market-data failure -> global SAFE_MODE
# --------------------------------------------------------------------------
def test_single_symbol_failure_is_not_global(cfg):
    state = StateController()
    state.mark_verification_complete()
    h = MarketDataHealth(state, cfg, clock=lambda: 0.0)
    h.record_failure("AAPL", "timeout", system_level=True)
    h.record_failure("ZZZZ", "no security definition", system_level=False)
    h.record_failure("YYYY", "no security definition", system_level=False)
    assert state.state is SystemState.SAFE


def test_widespread_failure_is_global_and_clears(cfg):
    state = StateController()
    state.mark_verification_complete()
    h = MarketDataHealth(state, cfg, clock=lambda: 0.0)
    for s in ("AAPL", "MSFT", "SPY"):
        h.record_failure(s, "timeout", system_level=True)
    assert R_MD_WIDESPREAD in state.reasons
    h.record_success("AAPL")
    assert state.state is SystemState.SAFE


def test_competing_session_hint():
    hints = diagnose(["IBKR 10197: No market data during competing live session"])
    assert hints and "logged in elsewhere" in hints[0]


def test_2188_is_non_fatal_notice_kept_with_request(conn):
    req = conn.requests.register("historical", "AAPL 5m bars", scope="SYMBOL")
    conn._on_error(req.req_id, 2188, "Up-to-the-second historical data requires "
                   "additional subscription for the API.")
    assert req.error is None and not req.done.is_set()
    assert req.meta["warnings"][0].startswith("IBKR 2188")
    assert conn.state.state is SystemState.SAFE


def test_2188_hint_is_specific():
    hints = diagnose(["5m: IBKR 2188: Up-to-the-second historical data requires "
                      "additional subscription for the API."])
    assert len(hints) == 1 and "CURRENT session" in hints[0]
