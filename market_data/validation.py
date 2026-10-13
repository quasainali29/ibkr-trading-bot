"""
Market-data validation.

* Malformed bars (missing/invalid OHLC, impossible high/low, bad volume, bad
  or out-of-order timestamps) invalidate the whole series - indicators are
  only ever computed on fully validated Bar sequences.
* Staleness is judged against the exchange session calendar.
* Delayed / frozen data is labelled and never makes a symbol usable for
  trading (config forbids it).
* The result for one symbol is a MarketDataCheck with three INDEPENDENT parts:
    historical  - are the candles valid and current enough for ANALYSIS?
    quote       - is a snapshot quote available / valid / fresh, and what type?
    trading     - may this symbol be used for a NEW live trade right now?
  Quote problems never invalidate historical candles, and a closed market
  never invalidates analysis - it only blocks new trades. A failed check
  makes THAT symbol unusable for trading; it does not cause global SAFE_MODE.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from environment import Environment
from market_data.models import MarketDataType

_MIN_YEAR = 1990
_MAX_REPORTED = 5


@dataclass
class BarValidation:
    ok: bool
    errors: list = field(default_factory=list)


@dataclass
class Freshness:
    stale: bool
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class QuoteValidation:
    errors: list = field(default_factory=list)       # data unusable
    stale: bool = False
    stale_reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class TimeframeStatus:
    """Historical candles for one timeframe."""
    timeframe: str
    valid: bool                    # validated COMPLETED bars were obtained
    stale: bool                    # completed bars do not reach the expected session
    bar_count: int = 0             # completed bars
    last_bar: datetime | None = None          # latest COMPLETED bar
    in_progress_bar: datetime | None = None   # forming bar, excluded from indicators
    errors: list = field(default_factory=list)
    stale_reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def usable_for_analysis(self):
        return self.valid and not self.stale


@dataclass
class HistoricalStatus:
    timeframes: dict                          # key -> TimeframeStatus
    latest_completed_session: date | None
    session_date_approximated: bool = False
    required: tuple | None = None             # None = every fetched timeframe

    @property
    def required_keys(self):
        return tuple(self.required) if self.required is not None else tuple(self.timeframes)

    def is_required(self, key):
        return key in self.required_keys

    @property
    def valid(self):
        """All REQUIRED timeframes were obtained and validated."""
        keys = self.required_keys
        return bool(keys) and all(k in self.timeframes and self.timeframes[k].valid
                                  for k in keys)

    @property
    def stale(self):
        """Any REQUIRED timeframe is stale (optional ones never block)."""
        return any(self.timeframes[k].stale for k in self.required_keys
                   if k in self.timeframes)

    @property
    def usable_for_analysis(self):
        return self.valid and not self.stale

    @property
    def errors(self):
        return [f"{k}: {e}" for k, t in self.timeframes.items() for e in t.errors]


@dataclass
class QuoteStatus:
    available: bool                # IBKR returned a usable price
    valid: bool                    # price present, not crossed, sane timestamp
    data_type: MarketDataType | None
    stale: bool
    errors: list = field(default_factory=list)
    stale_reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    simulated: bool = False
    source: str = "IBKR"

    @property
    def data_type_label(self):
        return self.data_type.label if self.data_type else "UNKNOWN"


@dataclass
class MarketDataCheck:
    symbol: str
    checked_at: datetime
    market_open: bool
    contract_ok: bool
    historical: HistoricalStatus
    quote: QuoteStatus
    usable_for_trading: bool       # new LIVE trade allowed for this symbol now
    contract_errors: list = field(default_factory=list)
    trading_blockers: list = field(default_factory=list)
    environment: Environment = Environment.LIVE

    @property
    def data_type(self):
        return self.quote.data_type

    @property
    def data_type_label(self):
        return self.quote.data_type_label

    @property
    def all_warnings(self):
        return ([f"{k}: {w}" for k, t in self.historical.timeframes.items() for w in t.warnings]
                + list(self.quote.warnings))

    @property
    def all_errors(self):
        """Every error message (for diagnostics/journal only)."""
        return ([f"contract: {e}" for e in self.contract_errors]
                + [f"historical {e}" for e in self.historical.errors]
                + [f"quote: {e}" for e in self.quote.errors])


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------
def _valid_price(v):
    return v is not None and v.is_finite() and v > 0


def validate_bar(bar, now, cfg):
    errs = []
    ts = bar.timestamp
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        errs.append("invalid timestamp")
    else:
        if ts.year < _MIN_YEAR:
            errs.append(f"implausible timestamp {ts.isoformat()}")
        if ts > now + timedelta(seconds=cfg.FUTURE_TIMESTAMP_TOLERANCE_SECONDS):
            errs.append(f"timestamp in the future {ts.isoformat()}")
    prices_ok = True
    for name in ("open", "high", "low", "close"):
        v = getattr(bar, name)
        if v is None:
            errs.append(f"missing {name}")
            prices_ok = False
        elif not _valid_price(v):
            errs.append(f"invalid {name} {v}")
            prices_ok = False
    if prices_ok:
        if bar.high < bar.low:
            errs.append(f"high {bar.high} < low {bar.low}")
        if bar.high < max(bar.open, bar.close):
            errs.append("high below open/close")
        if bar.low > min(bar.open, bar.close):
            errs.append("low above open/close")
    if bar.volume is None:
        errs.append("missing volume")
    elif not bar.volume.is_finite() or bar.volume < 0:
        errs.append(f"invalid volume {bar.volume}")
    return errs


def validate_bars(bars, now, cfg):
    """Validate a full sequence. Any malformed bar rejects the series."""
    errors = []
    if not bars:
        return BarValidation(False, ["no bars returned"])
    bad = 0
    for i, bar in enumerate(bars):
        errs = validate_bar(bar, now, cfg)
        if errs:
            bad += 1
            if bad <= _MAX_REPORTED:
                ts = getattr(bar.timestamp, "isoformat", lambda: bar.timestamp)()
                errors.append(f"bar {i} ({ts}): {', '.join(errs)}")
    if bad > _MAX_REPORTED:
        errors.append(f"... {bad - _MAX_REPORTED} more malformed bars")
    for i in range(1, len(bars)):
        a, b = bars[i - 1].timestamp, bars[i].timestamp
        if isinstance(a, datetime) and isinstance(b, datetime) and a.tzinfo and b.tzinfo:
            if b == a:
                errors.append(f"duplicate timestamp {b.isoformat()}")
                break
            if b < a:
                errors.append(f"timestamps out of order at bar {i}")
                break
    return BarValidation(not errors, errors)


def bar_freshness(bars, timeframe, calendar, now, cfg):
    """Are the COMPLETED bars recent enough given the exchange session state?

    Pass completed bars only (see sessions.split_completed). Daily bars are
    current when the latest completed daily candle is the most recent
    COMPLETED regular session - while today's session is open that is
    yesterday's candle; today's forming candle is never required.
    """
    if not bars:
        return Freshness(True, ["no bars"])
    last = bars[-1].timestamp
    grace = timedelta(seconds=cfg.BAR_STALENESS_GRACE_SECONDS)
    session = calendar.current_session(now)
    prev_date, approx = calendar.previous_session_date(now)
    warnings = ["session calendar does not cover the previous session; "
                "weekday approximation used"] if approx and session is None else []
    last_date = calendar.local_date(last)

    if session is not None:
        # Just after the open the first bar of the session may not exist yet.
        early_window = timedelta(seconds=timeframe.seconds if timeframe.intraday else 0)
        early = now - session.start <= early_window + grace
        if timeframe.intraday:
            max_age = timedelta(seconds=2 * timeframe.seconds) + grace
            fresh = (now - last) <= max_age or (early and last_date >= prev_date)
            reason = (f"last {timeframe.key} bar {last.isoformat()} is older than "
                      f"{int(max_age.total_seconds())}s while the market is open")
        else:
            fresh = last_date >= prev_date
            reason = (f"latest completed daily bar is {last_date}, expected the last "
                      f"completed session {prev_date}")
    else:
        if timeframe.intraday:
            completed = calendar.last_completed_session(now)
            if completed is not None:
                cutoff = completed.end - timedelta(seconds=timeframe.seconds) - grace
                fresh = last >= cutoff
                reason = (f"last {timeframe.key} bar {last.isoformat()} does not reach "
                          f"the end of the last session ({completed.end.isoformat()})")
            else:
                fresh = last_date >= prev_date
                reason = f"last {timeframe.key} bar date {last_date} < last session {prev_date}"
        else:
            fresh = last_date >= prev_date
            reason = (f"latest completed daily bar is {last_date}, expected the last "
                      f"completed session {prev_date}")
    return Freshness(not fresh, [] if fresh else [reason], warnings)


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def validate_quote(quote, calendar, now, cfg):
    qv = QuoteValidation()
    if quote is None:
        qv.errors.append("no quote")
        qv.stale = True
        return qv
    if quote.data_type is None:
        qv.warnings.append("market data type not reported by IBKR")
    elif quote.data_type.is_delayed:
        qv.warnings.append(f"market data is {quote.data_type.label}")
    elif quote.data_type.is_frozen:
        qv.warnings.append(f"market data is {quote.data_type.label}")

    if quote.current_price is None:
        qv.errors.append("no valid current price (last/bid/ask missing or <= 0)")
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        qv.errors.append(f"crossed quote: bid {quote.bid} > ask {quote.ask}")
    if quote.prev_close is None:
        qv.warnings.append("previous close not available")

    ts = quote.last_timestamp
    if ts is not None and ts > now + timedelta(seconds=cfg.FUTURE_TIMESTAMP_TOLERANCE_SECONDS):
        qv.errors.append(f"quote timestamp in the future {ts.isoformat()}")
        return qv

    session = calendar.current_session(now)
    if session is not None:
        if ts is None:
            qv.stale, reason = True, "no last-trade timestamp while the market is open"
        else:
            delayed = quote.data_type is not None and quote.data_type.is_delayed
            limit = (cfg.DELAYED_QUOTE_MAX_AGE_SECONDS if delayed
                     else cfg.LIVE_QUOTE_MAX_AGE_SECONDS)
            age = (now - ts).total_seconds()
            qv.stale = age > limit
            reason = f"last trade {int(age)}s old (limit {limit}s)"
        if qv.stale:
            qv.stale_reasons.append(reason)
    elif ts is not None:
        prev_date, _ = calendar.previous_session_date(now)
        if calendar.local_date(ts) < prev_date:
            qv.stale = True
            qv.stale_reasons.append(
                f"last trade {calendar.local_date(ts)} predates the last session {prev_date}")
    return qv


# ---------------------------------------------------------------------------
# Per-symbol verdict
# ---------------------------------------------------------------------------
def timeframe_status(key, bars=None, validation=None, freshness=None, error=None,
                     in_progress=None):
    """Summarise one timeframe's fetch/validation/freshness results.
    `bars` are the COMPLETED bars; `in_progress` is the forming bar, if any."""
    if error or validation is None:
        return TimeframeStatus(key, False, False, errors=[error or "not fetched"])
    status = TimeframeStatus(key, validation.ok, bool(freshness and freshness.stale),
                             len(bars or ()), bars[-1].timestamp if bars else None,
                             in_progress.timestamp if in_progress is not None else None,
                             list(validation.errors))
    if freshness is not None:
        status.stale_reasons = list(freshness.reasons)
        status.warnings = list(freshness.warnings)
    return status


def quote_status(quote, quote_validation, fetch_error=None):
    if quote is None:
        return QuoteStatus(False, False, None, False,
                           errors=[fetch_error or "no quote received"])
    qv = quote_validation
    return QuoteStatus(
        available=quote.current_price is not None, valid=not qv.errors,
        data_type=quote.data_type, stale=qv.stale, errors=list(qv.errors),
        stale_reasons=list(qv.stale_reasons), warnings=list(qv.warnings),
        simulated=bool(getattr(quote, "simulated", False)),
        source=getattr(quote, "source", "IBKR"))


def quote_execution_blocker(quote_st, environment, cfg):
    """Why this quote's TYPE cannot authorize a trade in `environment`
    (None = acceptable). LIVE requires a genuine IBKR LIVE quote; a simulated
    quote is accepted only inside Environment.BACKTEST."""
    dt = quote_st.data_type
    simulated = quote_st.simulated or (dt is not None and dt.is_simulated)
    if dt is None:
        return "quote data type unknown"
    if simulated:
        if environment is Environment.BACKTEST and dt.is_simulated:
            return None
        return "SIMULATED quote cannot authorize a real trade (genuine LIVE data required)"
    if dt.is_delayed and not cfg.ALLOW_DELAYED_DATA_FOR_TRADING:
        return f"{dt.label} data cannot authorize trades"
    if dt.is_frozen and not cfg.ALLOW_FROZEN_DATA_FOR_TRADING:
        return f"{dt.label} data cannot authorize trades"
    return None


def build_market_data_check(symbol, contract_check, quote_st, timeframe_statuses,
                            calendar, now, cfg, required=None,
                            environment=Environment.LIVE):
    """Combine contract, historical and quote status into one verdict.

    The three parts stay separate; only `usable_for_trading` combines them,
    and it requires ALL of: valid contract, valid + fresh candles, an
    available + valid + fresh LIVE quote, and the regular market open.
    """
    contract_ok = bool(contract_check and contract_check.ok)
    contract_errors = [] if contract_ok else list(
        contract_check.reasons if contract_check else ["not resolved"])

    if calendar is not None:
        latest, approx = calendar.previous_session_date(now)
        market_open = calendar.is_open(now)
    else:
        latest, approx, market_open = None, False, False
    historical = HistoricalStatus(dict(timeframe_statuses), latest, approx,
                                  tuple(required) if required is not None else None)
    quote_st = quote_st or QuoteStatus(False, False, None, False, ["not requested"])

    blockers = []
    if not contract_ok:
        blockers.append("contract invalid")
    if not market_open:
        blockers.append("regular market closed")
    if not historical.valid:
        blockers.append("historical data invalid/unavailable")
    elif historical.stale:
        blockers.append("historical data stale")
    if not quote_st.available:
        blockers.append("live quote unavailable")
    else:
        if not quote_st.valid:
            blockers.append("quote invalid")
        if quote_st.stale:
            blockers.append("quote stale")
        type_problem = quote_execution_blocker(quote_st, environment, cfg)
        if type_problem:
            blockers.append(type_problem)

    return MarketDataCheck(
        symbol=symbol.upper(), checked_at=now, market_open=market_open,
        contract_ok=contract_ok, historical=historical, quote=quote_st,
        usable_for_trading=not blockers, contract_errors=contract_errors,
        trading_blockers=blockers, environment=environment)
