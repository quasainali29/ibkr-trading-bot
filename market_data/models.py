"""
Market-data value objects. Prices/volumes are Decimal; timestamps are
timezone-aware UTC datetimes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, IntEnum


class MarketDataType(IntEnum):
    """IBKR market data types (reqMarketDataType / marketDataType callback)."""
    LIVE = 1
    FROZEN = 2
    DELAYED = 3
    DELAYED_FROZEN = 4
    # Not an IBKR type: an as-of-T quote synthesized by the backtester from
    # completed historical bars. Never sent to / received from IBKR and only
    # execution-grade inside Environment.BACKTEST.
    SIMULATED_BACKTEST = 101

    @property
    def is_delayed(self):
        return self in (MarketDataType.DELAYED, MarketDataType.DELAYED_FROZEN)

    @property
    def is_frozen(self):
        return self in (MarketDataType.FROZEN, MarketDataType.DELAYED_FROZEN)

    @property
    def is_simulated(self):
        return self is MarketDataType.SIMULATED_BACKTEST

    @property
    def label(self):
        return {
            MarketDataType.LIVE: "LIVE",
            MarketDataType.FROZEN: "FROZEN (last available values)",
            MarketDataType.DELAYED: "DELAYED (~15-20 min)",
            MarketDataType.DELAYED_FROZEN: "DELAYED-FROZEN (delayed, last available)",
            MarketDataType.SIMULATED_BACKTEST: "SIMULATED (BACKTEST as-of-T bar close)",
        }[self]

    @classmethod
    def parse(cls, value):
        """Accept 1-4, 'LIVE', 'delayed-frozen', 'DELAYED_FROZEN', ..."""
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper().replace("-", "_")]


class TradingSession(str, Enum):
    """Which hours a bar series covers. Only RTH is implemented in Phase 2;
    EXTENDED is part of the interface so it can be added without redesign."""
    RTH = "RTH"
    EXTENDED = "EXTENDED"

    @property
    def use_rth(self):
        return self is TradingSession.RTH

    @property
    def file_suffix(self):
        return "" if self is TradingSession.RTH else "_ext"


@dataclass(frozen=True)
class Timeframe:
    key: str                # CLI / cache name, e.g. "5m"
    ibkr_bar_size: str      # IBKR barSizeSetting
    full_duration: str      # IBKR durationStr for a full (re)load
    seconds: int
    intraday: bool
    max_cached_bars: int


TIMEFRAMES = {
    "1m": Timeframe("1m", "1 min", "5 D", 60, True, 20000),
    "5m": Timeframe("5m", "5 mins", "10 D", 300, True, 10000),
    "15m": Timeframe("15m", "15 mins", "20 D", 900, True, 6000),
    "1h": Timeframe("1h", "1 hour", "2 M", 3600, True, 3000),
    "1d": Timeframe("1d", "1 day", "1 Y", 86400, False, 1500),
}


def get_timeframe(key):
    try:
        return TIMEFRAMES[key]
    except KeyError:
        raise ValueError(f"unsupported timeframe {key!r}; "
                         f"use one of {', '.join(TIMEFRAMES)}") from None


@dataclass(frozen=True)
class Bar:
    timestamp: datetime          # bar START, aware UTC (daily: exchange-local midnight)
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    wap: Decimal | None = None   # IBKR volume-weighted average price of the bar
    bar_count: int | None = None


@dataclass(frozen=True)
class BarSeries:
    symbol: str
    con_id: int
    timeframe: Timeframe
    session: TradingSession
    bars: tuple                  # COMPLETED bars only - indicators use these
    fetched_at: datetime
    source: str                  # "ibkr", "cache", "cache+ibkr", "memory"
    in_progress: Bar | None = None   # newest bar if still forming (display only)

    @property
    def latest_completed(self):
        return self.bars[-1] if self.bars else None


@dataclass(frozen=True)
class ContractInfo:
    con_id: int
    symbol: str
    sec_type: str
    stock_type: str
    exchange: str
    primary_exchange: str
    currency: str
    long_name: str
    min_tick: Decimal | None
    time_zone_id: str
    liquid_hours: str
    trading_hours: str
    category: str = ""
    subcategory: str = ""
    industry: str = ""
    fund_type: str = ""
    fund_asset_type: str = ""


@dataclass(frozen=True)
class Quote:
    symbol: str
    con_id: int
    data_type: MarketDataType | None     # type actually delivered by IBKR
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    prev_close: Decimal | None
    day_volume: Decimal | None
    last_timestamp: datetime | None      # time of last trade, if reported
    received_at: datetime
    delayed_ticks_seen: bool = False
    simulated: bool = False                # True only for backtest quotes
    source: str = "IBKR"                   # "IBKR" or "BACKTEST"

    @property
    def current_price(self):
        """Last trade price; midpoint if no last; None otherwise."""
        if self.last is not None:
            return self.last
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None

    @property
    def price_source(self):
        if self.last is not None:
            return "last"
        if self.bid is not None and self.ask is not None:
            return "midpoint"
        return "none"

    @property
    def daily_change_pct(self):
        p = self.current_price
        if p is None or not self.prev_close:
            return None
        return (p - self.prev_close) / self.prev_close


@dataclass
class SymbolMarketData:
    """Everything fetched for one symbol, plus its validation result."""
    symbol: str
    contract_check: object                   # contracts.ContractCheck
    quote: Quote | None = None
    series: dict = field(default_factory=dict)       # timeframe key -> BarSeries
    errors: dict = field(default_factory=dict)       # section -> message
    check: object = None                     # validation.MarketDataCheck
    calendar: object = None                  # sessions.TradingCalendar
    quote_status: object = None              # validation.QuoteStatus
    timeframe_statuses: dict = field(default_factory=dict)   # key -> TimeframeStatus
