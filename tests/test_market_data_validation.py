from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import make_bar, make_info, utc
from market_data.contracts import ContractCheck
from market_data.models import (TIMEFRAMES, Bar, MarketDataType, Quote,
                                TradingSession)
from market_data.sessions import TradingCalendar
from market_data.validation import (bar_freshness, build_market_data_check,
                                    validate_bars, validate_quote)

D = Decimal
TF5 = TIMEFRAMES["5m"]
TF1D = TIMEFRAMES["1d"]
OPEN_NOW = utc(2026, 10, 6, 15, 0)        # Tue 11:00 ET, market open
CLOSED_NOW = utc(2026, 10, 7, 1, 0)       # Tue 21:00 ET, market closed


def five_min_bars(last_start, n=5):
    return [make_bar(last_start - timedelta(minutes=5 * (n - 1 - i)), 10, 11, 9, 10)
            for i in range(n)]


def daily_bar(y, m, d):
    from zoneinfo import ZoneInfo
    ts = datetime(y, m, d, tzinfo=ZoneInfo("US/Eastern")).astimezone(timezone.utc)
    return make_bar(ts, 10, 11, 9, 10)


# --------------------------------------------------------------------------
# Malformed bars
# --------------------------------------------------------------------------
class TestMalformedBars:
    good = make_bar(utc(2026, 10, 6, 14, 0), 10, 11, 9, 10)

    @pytest.mark.parametrize("field,value,msg", [
        ("open", None, "missing open"),
        ("high", None, "missing high"),
        ("low", None, "missing low"),
        ("close", None, "missing close"),
        ("close", D("0"), "invalid close"),
        ("open", D("-1"), "invalid open"),
        ("high", D("NaN"), "invalid high"),
        ("low", D("Infinity"), "invalid low"),
        ("volume", None, "missing volume"),
        ("volume", D("-1"), "invalid volume"),
    ])
    def test_bad_field_rejects_series(self, cfg, field, value, msg):
        bad = Bar(**{**self.good.__dict__, field: value})
        res = validate_bars([self.good, bad] if bad.timestamp != self.good.timestamp
                            else [bad], OPEN_NOW, cfg)
        assert not res.ok
        assert msg in " ".join(res.errors)

    def test_high_below_low(self, cfg):
        res = validate_bars([make_bar(utc(2026, 10, 6, 14), 10, 9, 11, 10)], OPEN_NOW, cfg)
        assert not res.ok and "high 9 < low 11" in res.errors[0]

    def test_high_below_close(self, cfg):
        res = validate_bars([make_bar(utc(2026, 10, 6, 14), 10, 11, 9, 12)], OPEN_NOW, cfg)
        assert "high below open/close" in res.errors[0]

    def test_low_above_open(self, cfg):
        res = validate_bars([make_bar(utc(2026, 10, 6, 14), 8, 11, 9, 10)], OPEN_NOW, cfg)
        assert "low above open/close" in res.errors[0]

    def test_naive_timestamp(self, cfg):
        res = validate_bars([make_bar(datetime(2026, 10, 6, 14), 10, 11, 9, 10)], OPEN_NOW, cfg)
        assert "invalid timestamp" in res.errors[0]

    def test_missing_timestamp(self, cfg):
        res = validate_bars([make_bar(None, 10, 11, 9, 10)], OPEN_NOW, cfg)
        assert "invalid timestamp" in res.errors[0]

    def test_future_timestamp(self, cfg):
        res = validate_bars([make_bar(OPEN_NOW + timedelta(hours=1), 10, 11, 9, 10)],
                            OPEN_NOW, cfg)
        assert "future" in res.errors[0]

    def test_implausible_year(self, cfg):
        res = validate_bars([make_bar(utc(1970, 1, 1), 10, 11, 9, 10)], OPEN_NOW, cfg)
        assert "implausible" in res.errors[0]

    def test_duplicate_timestamps(self, cfg):
        res = validate_bars([self.good, self.good], OPEN_NOW, cfg)
        assert "duplicate" in res.errors[0]

    def test_out_of_order(self, cfg):
        later = make_bar(utc(2026, 10, 6, 14, 5), 10, 11, 9, 10)
        res = validate_bars([later, self.good], OPEN_NOW, cfg)
        assert "out of order" in res.errors[0]

    def test_empty(self, cfg):
        assert not validate_bars([], OPEN_NOW, cfg).ok

    def test_valid_series(self, cfg):
        assert validate_bars(five_min_bars(utc(2026, 10, 6, 14, 55)), OPEN_NOW, cfg).ok

    def test_error_list_is_capped(self, cfg):
        bad = [make_bar(utc(2026, 10, 6, 13, 30) + timedelta(minutes=5 * i), None, 11, 9, 10)
               for i in range(20)]
        res = validate_bars(bad, OPEN_NOW, cfg)
        assert len(res.errors) == 6 and "15 more" in res.errors[-1]


# --------------------------------------------------------------------------
# Stale data
# --------------------------------------------------------------------------
class TestStaleness:
    def test_intraday_fresh_while_open(self, cfg, calendar):
        bars = five_min_bars(utc(2026, 10, 6, 14, 50))      # 10 min old, limit 12
        assert not bar_freshness(bars, TF5, calendar, OPEN_NOW, cfg).stale

    def test_intraday_stale_while_open(self, cfg, calendar):
        bars = five_min_bars(utc(2026, 10, 6, 14, 40))      # 20 min old
        f = bar_freshness(bars, TF5, calendar, OPEN_NOW, cfg)
        assert f.stale and "older than 720s" in f.reasons[0]

    def test_intraday_after_close_reaches_session_end(self, cfg, calendar):
        bars = five_min_bars(utc(2026, 10, 6, 19, 55))      # 15:55 ET bar
        assert not bar_freshness(bars, TF5, calendar, CLOSED_NOW, cfg).stale

    def test_intraday_after_close_missing_end_of_session(self, cfg, calendar):
        bars = five_min_bars(utc(2026, 10, 6, 18, 0))
        assert bar_freshness(bars, TF5, calendar, CLOSED_NOW, cfg).stale

    def test_intraday_just_after_open_accepts_previous_session(self, cfg, calendar):
        now = utc(2026, 10, 6, 13, 31)                       # 09:31 ET
        bars = five_min_bars(utc(2026, 10, 5, 19, 55))
        assert not bar_freshness(bars, TF5, calendar, now, cfg).stale

    def test_daily_open_session_yesterday_is_current(self, cfg, calendar):
        # Tue 11:00 ET: latest COMPLETED session is Mon 10-05; today's
        # forming candle is not required.
        assert not bar_freshness([daily_bar(2026, 10, 5)], TF1D, calendar, OPEN_NOW, cfg).stale
        f = bar_freshness([daily_bar(2026, 10, 2)], TF1D, calendar, OPEN_NOW, cfg)
        assert f.stale and "expected the last completed session 2026-10-05" in f.reasons[0]

    def test_daily_after_close(self, cfg, calendar):
        assert not bar_freshness([daily_bar(2026, 10, 6)], TF1D, calendar, CLOSED_NOW, cfg).stale
        f = bar_freshness([daily_bar(2026, 10, 2)], TF1D, calendar, CLOSED_NOW, cfg)
        assert f.stale and "expected the last completed session 2026-10-06" in f.reasons[0]
        # after today's close, yesterday's candle is no longer enough
        assert bar_freshness([daily_bar(2026, 10, 5)], TF1D, calendar, CLOSED_NOW, cfg).stale

    def test_weekend_uses_friday(self, cfg, calendar):
        saturday = utc(2026, 10, 10, 16, 0)
        assert not bar_freshness([daily_bar(2026, 10, 9)], TF1D, calendar, saturday, cfg).stale
        assert bar_freshness([daily_bar(2026, 10, 8)], TF1D, calendar, saturday, cfg).stale

    def test_weekday_approximation_without_calendar_history(self, cfg):
        cal = TradingCalendar.from_contract(make_info(liquid="20261012:0930-20261012:1600"))
        monday_premarket = utc(2026, 10, 12, 12, 0)          # 08:00 ET
        f = bar_freshness([daily_bar(2026, 10, 9)], TF1D, cal, monday_premarket, cfg)
        assert not f.stale and "approximation" in f.warnings[0]
        assert bar_freshness([daily_bar(2026, 10, 8)], TF1D, cal, monday_premarket, cfg).stale


# --------------------------------------------------------------------------
# Quotes + delayed labelling
# --------------------------------------------------------------------------
def quote(dtype=MarketDataType.LIVE, last="100", bid="99.99", ask="100.01", ts=None,
          prev="98", now=OPEN_NOW):
    d = lambda x: None if x is None else D(x)
    return Quote("AAPL", 265598, dtype, d(last), d(bid), d(ask), d(prev), D(1000000),
                 ts if ts is not None else now - timedelta(seconds=5), now)


class TestQuotes:
    def test_live_fresh_quote(self, cfg, calendar):
        qv = validate_quote(quote(), calendar, OPEN_NOW, cfg)
        assert not qv.errors and not qv.stale and not qv.warnings

    def test_live_quote_stale(self, cfg, calendar):
        qv = validate_quote(quote(ts=OPEN_NOW - timedelta(seconds=120)), calendar, OPEN_NOW, cfg)
        assert qv.stale and "limit 60s" in qv.stale_reasons[0]

    def test_delayed_quote_uses_delayed_age_limit(self, cfg, calendar):
        q = quote(MarketDataType.DELAYED, ts=OPEN_NOW - timedelta(minutes=16))
        qv = validate_quote(q, calendar, OPEN_NOW, cfg)
        assert not qv.stale
        assert "DELAYED" in qv.warnings[0]

    def test_missing_timestamp_while_open_is_stale(self, cfg, calendar):
        q = Quote("AAPL", 1, MarketDataType.LIVE, D(1), None, None, None, None, None, OPEN_NOW)
        assert validate_quote(q, calendar, OPEN_NOW, cfg).stale

    @pytest.mark.parametrize("kw,msg", [
        (dict(last=None, bid=None, ask=None), "no valid current price"),
        (dict(bid="101", ask="100"), "crossed quote"),
    ])
    def test_invalid_quotes(self, cfg, calendar, kw, msg):
        qv = validate_quote(quote(**kw), calendar, OPEN_NOW, cfg)
        assert msg in " ".join(qv.errors)

    def test_unknown_type_is_warning_not_error(self, cfg, calendar):
        qv = validate_quote(quote(dtype=None), calendar, OPEN_NOW, cfg)
        assert not qv.errors
        assert "market data type not reported" in qv.warnings[0]

    def test_midpoint_when_no_last(self):
        q = quote(last=None, bid="99", ask="101")
        assert q.current_price == D(100) and q.price_source == "midpoint"

    def test_daily_change(self):
        assert quote(last="99", prev="90").daily_change_pct == D("0.1")

    def test_frozen_quote_after_close(self, cfg, calendar):
        q = quote(MarketDataType.FROZEN, ts=utc(2026, 10, 6, 19, 59), now=CLOSED_NOW)
        assert not validate_quote(q, calendar, CLOSED_NOW, cfg).stale
        old = quote(MarketDataType.FROZEN, ts=utc(2026, 10, 2, 19, 59), now=CLOSED_NOW)
        assert validate_quote(old, calendar, CLOSED_NOW, cfg).stale


class TestDelayedLabelling:
    @pytest.mark.parametrize("dtype,delayed,frozen,word", [
        (MarketDataType.LIVE, False, False, "LIVE"),
        (MarketDataType.FROZEN, False, True, "FROZEN"),
        (MarketDataType.DELAYED, True, False, "DELAYED"),
        (MarketDataType.DELAYED_FROZEN, True, True, "DELAYED-FROZEN"),
    ])
    def test_labels(self, dtype, delayed, frozen, word):
        assert dtype.is_delayed is delayed and dtype.is_frozen is frozen
        assert dtype.label.startswith(word)

    @pytest.mark.parametrize("text,expected", [
        ("delayed-frozen", MarketDataType.DELAYED_FROZEN), ("LIVE", MarketDataType.LIVE),
        (3, MarketDataType.DELAYED), ("frozen", MarketDataType.FROZEN),
    ])
    def test_parse(self, text, expected):
        assert MarketDataType.parse(text) is expected

    def _check(self, cfg, calendar, dtype, now=OPEN_NOW):
        return full_check(cfg, calendar, now, quote(dtype, now=now))

    def test_live_open_market_is_usable(self, cfg, calendar):
        chk = self._check(cfg, calendar, MarketDataType.LIVE)
        assert chk.usable_for_trading, chk.trading_blockers
        assert chk.historical.usable_for_analysis and chk.quote.available

    @pytest.mark.parametrize("dtype", [MarketDataType.DELAYED, MarketDataType.DELAYED_FROZEN,
                                       MarketDataType.FROZEN])
    def test_delayed_or_frozen_never_usable(self, cfg, calendar, dtype):
        chk = self._check(cfg, calendar, dtype)
        assert chk.historical.usable_for_analysis and chk.quote.valid  # fine for analysis
        assert not chk.usable_for_trading                              # never for trading
        assert any("cannot authorize trades" in b for b in chk.trading_blockers)
        assert chk.data_type_label.startswith(dtype.label.split()[0])

    def test_invalid_contract(self, cfg):
        cc = ContractCheck("XYZ", False, reasons=["no contract found at IBKR"])
        chk = build_market_data_check("XYZ", cc, None, {}, None, OPEN_NOW, cfg)
        assert not chk.contract_ok and not chk.usable_for_trading
        assert chk.contract_errors == ["no contract found at IBKR"]
        assert "contract invalid" in chk.trading_blockers


# --------------------------------------------------------------------------
# Historical analysis validity vs quote vs live-trading usability
# --------------------------------------------------------------------------
def daily_series(last_day, n=60):
    from zoneinfo import ZoneInfo
    days, d = [], last_day
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    ts = [datetime(x.year, x.month, x.day, tzinfo=ZoneInfo("US/Eastern")).astimezone(timezone.utc)
          for x in reversed(days)]
    return [make_bar(t, 10, 11, 9, 10) for t in ts]


def full_check(cfg, calendar, now, q=None, quote_error=None, bars=None, tf=TF5):
    from market_data.validation import quote_status, timeframe_status
    cc = ContractCheck("AAPL", True, make_info())
    if bars is None:
        bars = five_min_bars(now - timedelta(minutes=5))
    st = timeframe_status(tf.key, bars, validate_bars(bars, now, cfg),
                          bar_freshness(bars, tf, calendar, now, cfg))
    qs = quote_status(q, validate_quote(q, calendar, now, cfg) if q else None, quote_error)
    return build_market_data_check("AAPL", cc, qs, {tf.key: st}, calendar, now, cfg)


class TestAnalysisVsTradingSeparation:
    # Wednesday 2026-10-07 21:00 ET: market closed, last completed session 10-07
    AFTER_CLOSE = utc(2026, 10, 8, 1, 0)

    def _closed_no_quote(self, cfg, calendar):
        from datetime import date
        return full_check(cfg, calendar, self.AFTER_CLOSE, q=None,
                          quote_error="no quote ticks received",
                          bars=daily_series(date(2026, 10, 7)), tf=TF1D)

    def test_closed_market_history_valid_for_analysis(self, cfg, calendar):
        chk = self._closed_no_quote(cfg, calendar)
        assert chk.historical.valid
        assert chk.historical.usable_for_analysis
        assert str(chk.historical.latest_completed_session) == "2026-10-07"

    def test_missing_quote_reported_separately(self, cfg, calendar):
        chk = self._closed_no_quote(cfg, calendar)
        assert not chk.quote.available
        assert chk.quote.data_type is None and chk.data_type_label == "UNKNOWN"
        assert chk.quote.errors == ["no quote ticks received"]
        assert chk.historical.errors == []          # quote error not merged in

    def test_closed_market_not_usable_for_trading(self, cfg, calendar):
        chk = self._closed_no_quote(cfg, calendar)
        assert not chk.market_open and not chk.usable_for_trading
        assert chk.trading_blockers == ["regular market closed", "live quote unavailable"]

    def test_unknown_quote_type_does_not_invalidate_history(self, cfg, calendar):
        q = quote(dtype=None, ts=utc(2026, 10, 7, 19, 59), now=self.AFTER_CLOSE)
        from datetime import date
        chk = full_check(cfg, calendar, self.AFTER_CLOSE, q=q,
                         bars=daily_series(date(2026, 10, 7)), tf=TF1D)
        assert chk.quote.available and chk.quote.valid
        assert chk.historical.usable_for_analysis
        assert "quote data type unknown" in chk.trading_blockers

    def test_history_missing_previous_session_is_stale(self, cfg, calendar):
        from datetime import date
        chk = full_check(cfg, calendar, self.AFTER_CLOSE, q=None,
                         bars=daily_series(date(2026, 10, 6)), tf=TF1D)
        assert chk.historical.valid and chk.historical.stale
        assert not chk.historical.usable_for_analysis

    def test_bad_bars_do_not_affect_quote_status(self, cfg, calendar):
        bad = five_min_bars(OPEN_NOW - timedelta(minutes=5))
        bad[1] = make_bar(bad[1].timestamp, 10, 9, 11, 10)
        chk = full_check(cfg, calendar, OPEN_NOW, q=quote(), bars=bad)
        assert not chk.historical.valid and chk.quote.valid and chk.quote.available
        assert not chk.usable_for_trading

    def test_fetch_failure_for_bars(self, cfg, calendar):
        from market_data.validation import quote_status, timeframe_status
        cc = ContractCheck("AAPL", True, make_info())
        q = quote()
        chk = build_market_data_check(
            "AAPL", cc, quote_status(q, validate_quote(q, calendar, OPEN_NOW, cfg)),
            {"5m": timeframe_status("5m", error="IBKR 162: no data")}, calendar, OPEN_NOW, cfg)
        assert not chk.historical.valid and chk.quote.available
        assert chk.historical.errors == ["5m: IBKR 162: no data"]

# --------------------------------------------------------------------------
# Daily candles: completed vs in-progress
# --------------------------------------------------------------------------
class TestInProgressDailyBar:
    AT_0932 = utc(2026, 10, 8, 13, 32)      # Thu 2026-10-08 09:32 ET, session open

    def _status(self, cfg, calendar, bars, now):
        from market_data.sessions import split_completed
        from market_data.validation import timeframe_status
        completed, forming = split_completed(bars, TF1D, calendar, now)
        return timeframe_status("1d", completed, validate_bars(completed, now, cfg),
                                bar_freshness(completed, TF1D, calendar, now, cfg),
                                in_progress=forming), completed, forming

    def test_example_0932_with_forming_bar(self, cfg, calendar):
        from datetime import date
        bars = daily_series(date(2026, 10, 8))           # includes today's partial bar
        st, completed, forming = self._status(cfg, calendar, bars, self.AT_0932)
        assert calendar.local_date(forming.timestamp) == date(2026, 10, 8)
        assert calendar.local_date(completed[-1].timestamp) == date(2026, 10, 7)
        assert calendar.local_date(st.last_bar) == date(2026, 10, 7)
        assert st.in_progress_bar == forming.timestamp
        assert st.usable_for_analysis

    def test_example_0932_without_forming_bar(self, cfg, calendar):
        from datetime import date
        st, _, forming = self._status(cfg, calendar, daily_series(date(2026, 10, 7)),
                                      self.AT_0932)
        assert forming is None and st.usable_for_analysis

    def test_missing_yesterday_is_stale_while_open(self, cfg, calendar):
        from datetime import date
        st, _, _ = self._status(cfg, calendar, daily_series(date(2026, 10, 6)), self.AT_0932)
        assert st.valid and st.stale and not st.usable_for_analysis

    def test_trading_still_needs_live_data(self, cfg, calendar):
        from datetime import date
        from market_data.sessions import split_completed
        completed, _ = split_completed(daily_series(date(2026, 10, 8)), TF1D, calendar,
                                       self.AT_0932)
        chk = full_check(cfg, calendar, self.AT_0932, q=None, quote_error="no ticks",
                         bars=completed, tf=TF1D)
        assert chk.market_open and chk.historical.usable_for_analysis
        assert not chk.usable_for_trading
        assert chk.trading_blockers == ["live quote unavailable"]

    def test_todays_bar_complete_after_close(self, cfg, calendar):
        from datetime import date
        from market_data.sessions import split_completed
        bars = daily_series(date(2026, 10, 8))
        completed, forming = split_completed(bars, TF1D, calendar, utc(2026, 10, 9, 1, 0))
        assert forming is None and completed == bars

    def test_intraday_forming_bar_split(self, calendar):
        from market_data.sessions import split_completed
        bars = five_min_bars(utc(2026, 10, 8, 14, 0))   # last bar 10:00-10:05 ET
        completed, forming = split_completed(bars, TF5, calendar, utc(2026, 10, 8, 14, 2))
        assert forming is bars[-1] and completed == bars[:-1]
        completed, forming = split_completed(bars, TF5, calendar, utc(2026, 10, 8, 14, 5))
        assert forming is None


def test_indicators_ignore_in_progress_bar(cfg, calendar):
    """ATR / MAs / support-resistance / previous close use completed bars only."""
    from datetime import date
    from analysis.market_report import build_report
    from market_data.features import compute_features
    from market_data.models import BarSeries, SymbolMarketData
    from market_data.sessions import split_completed
    from zoneinfo import ZoneInfo

    now = utc(2026, 10, 8, 13, 32)
    bars = daily_series(date(2026, 10, 8))
    wild = bars[-1]
    bars[-1] = make_bar(wild.timestamp, 10, 500, 1, 400)   # absurd forming candle
    completed, forming = split_completed(bars, TF1D, calendar, now)
    series = BarSeries("AAPL", 265598, TF1D, TradingSession.RTH, tuple(completed), now,
                       "test", forming)
    smd = SymbolMarketData("AAPL", ContractCheck("AAPL", True, make_info()),
                           series={"1d": series}, calendar=calendar)
    smd.check = full_check(cfg, calendar, now, q=None, quote_error="none",
                           bars=completed, tf=TF1D)
    report = build_report(smd, "1d", cfg)

    expected = compute_features(completed, TF1D, ZoneInfo("US/Eastern"), cfg).latest
    assert report.atr == expected.atr and report.ma_fast == expected.sma_fast
    assert report.prev_close == completed[-1].close
    assert report.in_progress is forming
    assert report.current_price == D(400)                   # display only
    assert all(z.high < 100 for z in report.supports + report.resistances)
