"""Synthetic bar builders and context fixtures for Phase 3 tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from conftest import make_bar, make_cfg

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
D = Decimal


def bars_from_closes(closes, start=datetime(2026, 1, 5, 5, tzinfo=UTC), step=timedelta(days=1),
                     spread=D("0.5"), volume=1000, volumes=None):
    """Bars whose open = previous close, high/low = max/min(open, close) +/- spread."""
    out, prev = [], D(str(closes[0]))
    for i, c in enumerate(closes):
        c = D(str(c))
        o = prev
        v = volumes[i] if volumes else volume
        out.append(make_bar(start + step * i, o, max(o, c) + spread, min(o, c) - spread, c, v))
        prev = c
    return out


def zigzag(start, legs, up_len=5, down_len=4, up_step=D(1), down_step=D(1)):
    """Closes alternating up-legs and down-legs (ends after the last leg)."""
    closes, p = [], D(str(start))
    for i in range(legs):
        n, s = (up_len, up_step) if i % 2 == 0 else (down_len, -down_step)
        for _ in range(n):
            p += s
            closes.append(p)
    return closes


def range_then(base_low, base_high, base_legs, tail):
    """Oscillation between base_low/base_high (forming zones), then `tail` closes."""
    closes = []
    for i in range(base_legs):
        target = base_high if i % 2 == 0 else base_low
        start = closes[-1] if closes else (base_low + base_high) / 2
        for k in range(1, 6):
            closes.append(start + (target - start) * k / 5)
    return closes + list(tail)


# --------------------------------------------------------------------------
# Full SymbolMarketData / context fixtures (no IBKR)
# --------------------------------------------------------------------------
NOW = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)        # Wed 11:00 ET, market open
INTRADAY_STEP = {"1h": timedelta(hours=1), "15m": timedelta(minutes=15),
                 "5m": timedelta(minutes=5), "1m": timedelta(minutes=1)}


def daily_ending(closes, last_day=datetime(2026, 10, 6, 4, tzinfo=UTC), **kw):
    start = last_day - timedelta(days=len(closes) - 1)
    return bars_from_closes(closes, start=start, **kw)


def intraday_ending(key, closes, end=NOW):
    step = INTRADAY_STEP[key]
    start = end - step * (len(closes) + 1)
    return bars_from_closes(closes, start=start, step=step, spread=D("0.1"))


def make_smd(daily_closes, statuses=None, quote_type="LIVE", quote_available=True,
             symbol="AAPL", now=NOW, intraday_closes=None, **info_kw):
    """statuses: {tf: "FRESH" | "STALE" | "UNAVAILABLE"} for intraday timeframes."""
    from conftest import LIQUID_HOURS, make_info
    from market_data.contracts import ContractCheck
    from market_data.models import (TIMEFRAMES, BarSeries, MarketDataType, Quote,
                                    SymbolMarketData, TradingSession)
    from market_data.sessions import TradingCalendar
    from market_data.validation import (QuoteStatus, TimeframeStatus,
                                        build_market_data_check)
    cfg = make_cfg()
    info_kw.setdefault("industry", "Technology")
    info = make_info(symbol=symbol, **info_kw)
    cal = TradingCalendar.from_contract(info)
    statuses = dict(statuses or {})
    statuses.setdefault("1d", "FRESH")
    for k in ("1h", "15m", "5m", "1m"):
        statuses.setdefault(k, "FRESH")
    smd = SymbolMarketData(symbol, ContractCheck(symbol, True, info))
    smd.calendar = cal
    for key, st in statuses.items():
        if st == "UNAVAILABLE":
            smd.timeframe_statuses[key] = TimeframeStatus(key, False, False, errors=["no data"])
            continue
        bars = (daily_ending(daily_closes) if key == "1d"
                else intraday_ending(key, (intraday_closes or zigzag(100, 10))))
        smd.series[key] = BarSeries(symbol, info.con_id, TIMEFRAMES[key], TradingSession.RTH,
                                    tuple(bars), now, "test")
        smd.timeframe_statuses[key] = TimeframeStatus(
            key, True, st == "STALE", len(bars), bars[-1].timestamp,
            stale_reasons=["stale for test"] if st == "STALE" else [])
    price = D(str(daily_closes[-1]))
    dtype = MarketDataType[quote_type] if quote_type else None
    if quote_available:
        smd.quote = Quote(symbol, info.con_id, dtype, price, price - D("0.01"),
                          price + D("0.01"), price, D(1000000), now - timedelta(seconds=5), now)
        smd.quote_status = QuoteStatus(True, True, dtype, False)
    else:
        smd.quote_status = QuoteStatus(False, False, None, False, ["no quote"])
    smd.check = build_market_data_check(symbol, smd.contract_check, smd.quote_status,
                                        smd.timeframe_statuses, cal, now, cfg)
    return smd


def bench(symbol, closes):
    return make_smd(closes, statuses={k: "UNAVAILABLE" for k in ("1h", "15m", "5m", "1m")},
                    symbol=symbol, quote_available=False, stock_type="ETF",
                    long_name=f"{symbol} ETF", primary="ARCA", industry="")
