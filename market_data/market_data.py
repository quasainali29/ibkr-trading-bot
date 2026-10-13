"""
MarketDataService - read-only IBKR market data.

Responsibilities: contract details, snapshot quotes, historical bars,
market-data type selection, historical pacing, in-memory + CSV caching,
and producing a validated per-symbol MarketDataCheck.

It contains NO strategy logic and NO order code. Failures for one symbol
are reported in that symbol's result; only widespread system-level failures
reach global SAFE_MODE (via MarketDataHealth).
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone

from ibapi.common import UNSET_DECIMAL
from ibapi.contract import Contract

from broker.connection import wait_event
from market_data.candle_store import CandleStoreError, merge_bars
from market_data.contracts import ContractCheck, validate_contract
from market_data.models import (Bar, BarSeries, ContractInfo, MarketDataType,
                                Quote, SymbolMarketData, TradingSession,
                                get_timeframe)
from market_data.sessions import SessionError, TradingCalendar, split_completed
from market_data.validation import (bar_freshness, build_market_data_check,
                                    quote_status, timeframe_status,
                                    validate_bars, validate_quote)
from money import MoneyError, optional_price, to_decimal

log = logging.getLogger(__name__)

# IBKR tick type IDs (ibapi.ticktype.TickTypeEnum)
_PRICE_TICKS = {1: "bid", 2: "ask", 4: "last", 9: "close",
                66: "bid", 67: "ask", 68: "last", 75: "close"}
_SIZE_TICKS = {8: "volume", 74: "volume"}
_TIME_TICKS = {45, 88}
_DELAYED_TICKS = {66, 67, 68, 72, 73, 74, 75, 76, 88}


class MarketDataUnavailable(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _utcnow():
    return datetime.now(timezone.utc)


def _duration_seconds(duration):
    n, unit = duration.split()
    return int(n) * {"S": 1, "D": 86400, "W": 7 * 86400, "M": 31 * 86400,
                     "Y": 366 * 86400}[unit]


def parse_ib_bar(raw, tz):
    """Convert an ibapi BarData into a Bar. Unparseable fields become None
    (or invalid values) so validation rejects them explicitly."""
    ts = None
    text = str(raw.date).strip()
    try:
        if text.isdigit() and len(text) == 8:            # daily: YYYYMMDD
            d = date(int(text[:4]), int(text[4:6]), int(text[6:]))
            ts = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
        elif text.isdigit():                             # intraday: epoch seconds
            ts = datetime.fromtimestamp(int(text), tz=timezone.utc)
        else:                                            # "YYYYMMDD HH:MM:SS[ tz]"
            parts = text.split()
            local = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S")
            ts = local.replace(tzinfo=tz).astimezone(timezone.utc)
    except (ValueError, IndexError, OverflowError):
        ts = None

    def price(v):
        try:
            return to_decimal(v)
        except MoneyError:
            return None

    vol = raw.volume
    volume = None if vol is None or vol == UNSET_DECIMAL else price(vol)
    wap = raw.wap
    wap = None if wap is None or wap == UNSET_DECIMAL else price(wap)
    if wap is not None and wap <= 0:
        wap = None
    return Bar(ts, price(raw.open), price(raw.high), price(raw.low), price(raw.close),
               volume, wap, int(raw.barCount) if raw.barCount and raw.barCount > 0 else None)


class HistoricalPacer:
    """Keeps historical requests under IBKR pacing limits."""

    def __init__(self, cfg, clock=time.monotonic, sleep=None):
        self.cfg = cfg
        self.clock = clock
        self._sleep = sleep or self._sleep_slices
        self._all = deque()
        self._by_contract = {}
        self._last_identical = {}
        self._lock = threading.Lock()

    @staticmethod
    def _sleep_slices(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(0.2, end - time.monotonic()))

    def wait_time(self, key, con_id):
        cfg, now = self.cfg, self.clock()
        waits = [0.0]
        while self._all and self._all[0] <= now - cfg.HIST_PACING_WINDOW_SECONDS:
            self._all.popleft()
        if len(self._all) >= cfg.HIST_PACING_MAX_REQUESTS:
            waits.append(self._all[0] + cfg.HIST_PACING_WINDOW_SECONDS - now)
        recent = self._by_contract.setdefault(con_id, deque())
        while recent and recent[0] <= now - cfg.HIST_SAME_CONTRACT_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= cfg.HIST_SAME_CONTRACT_MAX:
            waits.append(recent[0] + cfg.HIST_SAME_CONTRACT_WINDOW_SECONDS - now)
        last = self._last_identical.get(key)
        if last is not None:
            waits.append(last + cfg.HIST_IDENTICAL_MIN_INTERVAL_SECONDS - now)
        return max(waits)

    def acquire(self, key, con_id):
        with self._lock:
            wait = self.wait_time(key, con_id)
            if wait > self.cfg.HIST_MAX_PACING_WAIT_SECONDS:
                raise MarketDataUnavailable(
                    f"historical pacing limit: would need to wait {int(wait)}s", 162)
            if wait > 0:
                log.info("Historical pacing: waiting %.1fs", wait)
                self._sleep(wait)
            now = self.clock()
            self._all.append(now)
            self._by_contract.setdefault(con_id, deque()).append(now)
            self._last_identical[key] = now


class MarketDataService:
    def __init__(self, conn, cfg, store, health, clock=_utcnow):
        self.conn = conn
        self.cfg = cfg
        self.store = store
        self.health = health
        self.clock = clock
        self.pacer = HistoricalPacer(cfg)
        self.requested_type = MarketDataType.parse(cfg.MARKET_DATA_TYPE)
        self._contracts = {}          # symbol -> ContractCheck
        self._memory = {}             # (con_id, tf, session) -> ((bars, fetched_at), monotonic t)
        self._ibkr_notices = {}       # (con_id, tf key) -> IBKR warnings from last fetch
        conn.add_listener(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_market_data_type(self, data_type):
        self.requested_type = MarketDataType.parse(data_type)
        if self.conn.is_connected():
            self.conn.app.reqMarketDataType(int(self.requested_type))

    def resolve_contract(self, symbol):
        symbol = symbol.strip().upper()
        if symbol in self._contracts:
            return self._contracts[symbol]
        c = Contract()
        c.symbol, c.secType, c.exchange, c.currency = symbol, "STK", "SMART", "USD"
        items = self._request("contract", f"{symbol} contract details",
                              lambda rid: self.conn.app.reqContractDetails(rid, c),
                              self.cfg.CONTRACT_TIMEOUT_SECONDS,
                              allow_empty=True).items
        check = validate_contract(symbol, items, self.cfg)
        if check.ok:
            self._contracts[symbol] = check
        return check

    def get_quote(self, info):
        self._ensure_connected()
        self.conn.app.reqMarketDataType(int(self.requested_type))
        c = self._ib_contract(info)
        req = self._request(
            "quote", f"{info.symbol} quote",
            lambda rid: self.conn.app.reqMktData(rid, c, "", True, False, []),
            self.cfg.QUOTE_TIMEOUT_SECONDS, cancel=self.conn.app.cancelMktData,
            allow_partial=True)
        m = req.meta
        if not any(k in m for k in ("last", "bid", "ask", "close")):
            code, text = req.error or (None, "no quote ticks received")
            raise MarketDataUnavailable(f"quote unavailable: {text}", code)
        dtype = m.get("data_type")
        if dtype is None and m.get("delayed_ticks"):
            dtype = MarketDataType.DELAYED   # only delayed tick IDs were received
        return Quote(
            symbol=info.symbol, con_id=info.con_id, data_type=dtype,
            last=m.get("last"), bid=m.get("bid"), ask=m.get("ask"),
            prev_close=m.get("close"), day_volume=m.get("volume"),
            last_timestamp=m.get("last_timestamp"), received_at=self.clock(),
            delayed_ticks_seen=bool(m.get("delayed_ticks")))

    def get_bars(self, info, timeframe, calendar, session=TradingSession.RTH,
                 refresh=False):
        """Return a validated BarSeries, using memory/CSV caches where valid.

        Raises MarketDataUnavailable if valid bars cannot be obtained.
        """
        if session is not TradingSession.RTH:
            raise NotImplementedError("Phase 2 supports regular trading hours only")
        now = self.clock()
        key = (info.con_id, timeframe.key, session)
        ttl = self._memory_ttl(timeframe, calendar, now)
        hit = self._memory.get(key)
        if hit and not refresh and time.monotonic() - hit[1] < ttl:
            all_bars, fetched_at = hit[0]
            return self._series(info, timeframe, session, all_bars, calendar,
                                fetched_at, "memory")

        cached = [] if refresh else self.store.load(info.symbol, info.con_id,
                                                    timeframe, session, now)
        bars, source = None, "ibkr"
        min_bars = self.cfg.MA_SLOW_PERIOD + self.cfg.ATR_PERIOD
        if len(cached) >= min_bars:
            fresh = not bar_freshness(cached, timeframe, calendar, now, self.cfg).stale
            if fresh and not calendar.is_open(now):
                bars, source = cached, "cache"          # closed market: nothing new
            else:
                gap = (now - cached[-1].timestamp).total_seconds()
                duration = self._incremental_duration(timeframe, gap)
                if duration is not None:
                    new = self._fetch_bars(info, timeframe, session, duration, calendar)
                    merged = merge_bars(cached, new)
                    if validate_bars(merged, now, self.cfg).ok:
                        bars, source = merged, "cache+ibkr"
                    else:
                        log.warning("Merged %s %s bars invalid; full reload",
                                    info.symbol, timeframe.key)
        if bars is None:
            bars = self._fetch_bars(info, timeframe, session,
                                    timeframe.full_duration, calendar)

        result = validate_bars(bars, now, self.cfg)
        if not result.ok:
            raise MarketDataUnavailable("bars failed validation: " + "; ".join(result.errors))
        if source != "cache":
            try:
                self.store.save(info.symbol, info.con_id, timeframe, session, bars,
                                calendar, now)
            except (CandleStoreError, OSError) as exc:
                log.warning("Could not store %s %s candles: %s",
                            info.symbol, timeframe.key, exc)
        bars = tuple(bars[-timeframe.max_cached_bars:])
        self._memory[key] = ((bars, now), time.monotonic())
        return self._series(info, timeframe, session, bars, calendar, now, source)

    def _series(self, info, timeframe, session, all_bars, calendar, fetched_at, source):
        """Split at the CURRENT time so a bar that has since completed moves
        from in-progress to completed even when served from memory."""
        completed, in_progress = split_completed(all_bars, timeframe, calendar, self.clock())
        return BarSeries(info.symbol, info.con_id, timeframe, session, tuple(completed),
                         fetched_at, source, in_progress)

    def fetch_symbol(self, symbol, timeframe_keys, refresh=False, quote=True,
                     required=None):
        """Fetch contract, quote and bars for one symbol and validate them.

        Never raises for data problems: they are recorded on the result and
        make only this symbol unusable for trading.
        """
        symbol = symbol.strip().upper()
        now = self.clock()
        system_failure = False
        try:
            cc = self.resolve_contract(symbol)
        except MarketDataUnavailable as exc:
            system_failure = self.health.is_system_level(exc.code)
            cc = ContractCheck(symbol, False, reasons=[str(exc)])
        smd = SymbolMarketData(symbol, cc)
        calendar = None
        if cc.ok:
            try:
                calendar = TradingCalendar.from_contract(cc.contract)
            except SessionError as exc:
                cc.ok = False
                cc.reasons.append(f"trading hours unusable: {exc}")
        if not cc.ok:
            self.health.record_failure(symbol, "; ".join(cc.reasons), system_failure)
            smd.check = build_market_data_check(symbol, cc, None, {}, calendar, now, self.cfg)
            return smd

        info = cc.contract
        smd.calendar = calendar

        # Quote and historical candles are fetched and judged independently:
        # a missing quote never invalidates valid candles, and vice versa.
        if not quote:
            q_status = quote_status(None, None, "quote not requested")
        else:
            try:
                smd.quote = self.get_quote(info)
                q_status = quote_status(smd.quote, validate_quote(
                    smd.quote, calendar, self.clock(), self.cfg))
            except MarketDataUnavailable as exc:
                smd.errors["quote"] = str(exc)
                system_failure |= self.health.is_system_level(exc.code)
                q_status = quote_status(None, None, str(exc))

        tf_statuses = {}
        for key in timeframe_keys:
            tf = get_timeframe(key)
            try:
                series = self.get_bars(info, tf, calendar, refresh=refresh)
                smd.series[key] = series
                now_ = self.clock()
                tf_statuses[key] = timeframe_status(
                    key, series.bars, validate_bars(series.bars, now_, self.cfg),
                    bar_freshness(series.bars, tf, calendar, now_, self.cfg),
                    in_progress=series.in_progress)
                tf_statuses[key].warnings += self._ibkr_notices.get((info.con_id, key), [])
            except MarketDataUnavailable as exc:
                smd.errors[f"{key} bars"] = str(exc)
                system_failure |= self.health.is_system_level(exc.code)
                tf_statuses[key] = timeframe_status(key, error=str(exc))

        smd.quote_status, smd.timeframe_statuses = q_status, tf_statuses
        smd.check = build_market_data_check(symbol, cc, q_status, tf_statuses,
                                            calendar, self.clock(), self.cfg, required)
        # Health: IBKR delivered SOMETHING usable -> the data service works.
        if q_status.available or any(t.valid for t in tf_statuses.values()):
            self.health.record_success(symbol)
        else:
            self.health.record_failure(symbol, "; ".join(smd.check.all_errors),
                                       system_failure)
        return smd

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _ensure_connected(self):
        if not self.conn.is_connected():
            raise MarketDataUnavailable("not connected to IBKR", -1)

    @staticmethod
    def _ib_contract(info):
        c = Contract()
        c.conId, c.symbol, c.secType = info.con_id, info.symbol, info.sec_type
        c.exchange, c.primaryExchange, c.currency = "SMART", info.primary_exchange, info.currency
        return c

    def _request(self, kind, label, send, timeout, cancel=None,
                 allow_empty=False, allow_partial=False):
        self._ensure_connected()
        req = self.conn.requests.register(kind, label, scope="SYMBOL")
        try:
            send(req.req_id)
            finished = wait_event(req.done, timeout)
            if not finished:
                if cancel:
                    try:
                        cancel(req.req_id)
                    except Exception:
                        log.debug("cancel failed", exc_info=True)
                if not allow_partial:
                    raise MarketDataUnavailable(f"{label}: timed out after {timeout:.0f}s", -1)
            if req.error and not allow_partial:
                code, text = req.error
                if not (allow_empty and code == 200):
                    raise MarketDataUnavailable(f"{label}: IBKR {code}: {text}", code)
            if not self.conn.is_connected():
                raise MarketDataUnavailable(f"{label}: connection lost", -1)
            return req
        finally:
            self.conn.requests.remove(req.req_id)

    def _fetch_bars(self, info, timeframe, session, duration, calendar, end_datetime=""):
        """end_datetime "" = now (live). Backtest downloads pass an explicit
        end ("YYYYMMDD-HH:MM:SS", UTC) to page backwards through history."""
        self.pacer.acquire((info.con_id, timeframe.key, session.value, duration), info.con_id)
        c = self._ib_contract(info)
        req = self._request(
            "historical", f"{info.symbol} {timeframe.key} bars ({duration})",
            lambda rid: self.conn.app.reqHistoricalData(
                rid, c, end_datetime, duration, timeframe.ibkr_bar_size, "TRADES",
                1 if session.use_rth else 0, 2, False, []),
            self.cfg.HISTORICAL_TIMEOUT_SECONDS,
            cancel=self.conn.app.cancelHistoricalData)
        self._ibkr_notices[(info.con_id, timeframe.key)] = list(req.meta.get("warnings", []))
        return [parse_ib_bar(raw, calendar.tz) for raw in req.items]

    @staticmethod
    def _incremental_duration(timeframe, gap_seconds):
        """IBKR duration covering the gap since the last cached bar (plus
        overlap so the last partial bar is replaced). None = full reload."""
        days = max(1, math.ceil(gap_seconds / 86400) + (1 if timeframe.intraday else 3))
        if days * 86400 >= _duration_seconds(timeframe.full_duration):
            return None
        return f"{days} D"

    @staticmethod
    def _memory_ttl(timeframe, calendar, now):
        if calendar.is_open(now):
            return min(timeframe.seconds, 60)
        return 900

    # ------------------------------------------------------------------
    # ibapi callbacks (reader thread)
    # ------------------------------------------------------------------
    def _req(self, req_id):
        return self.conn.requests.get(req_id)

    def contractDetails(self, reqId, cd):
        req = self._req(reqId)
        if req is None or req.kind != "contract":
            return
        c = cd.contract
        req.items.append(ContractInfo(
            con_id=int(c.conId), symbol=c.symbol, sec_type=c.secType,
            stock_type=cd.stockType or "", exchange=c.exchange or "",
            primary_exchange=c.primaryExchange or "", currency=c.currency,
            long_name=cd.longName or "", min_tick=optional_price(cd.minTick),
            time_zone_id=cd.timeZoneId or "", liquid_hours=cd.liquidHours or "",
            trading_hours=cd.tradingHours or "", category=cd.category or "",
            subcategory=cd.subcategory or "", industry=cd.industry or "",
            fund_type=str(getattr(cd, "fundType", "") or ""),
            fund_asset_type=str(getattr(cd, "fundAssetType", "") or "")))

    def contractDetailsEnd(self, reqId):
        req = self._req(reqId)
        if req is not None:
            req.done.set()

    def marketDataType(self, reqId, marketDataType):
        req = self._req(reqId)
        if req is not None and req.kind == "quote":
            try:
                req.meta["data_type"] = MarketDataType(int(marketDataType))
            except ValueError:
                log.warning("Unknown market data type %s", marketDataType)

    def tickPrice(self, reqId, tickType, price, attrib):
        req = self._req(reqId)
        if req is None or req.kind != "quote" or tickType not in _PRICE_TICKS:
            return
        if tickType in _DELAYED_TICKS:
            req.meta["delayed_ticks"] = True
        value = optional_price(price)
        if value is not None and value > 0:
            req.meta[_PRICE_TICKS[tickType]] = value

    def tickSize(self, reqId, tickType, size):
        req = self._req(reqId)
        if req is None or req.kind != "quote" or tickType not in _SIZE_TICKS:
            return
        if tickType in _DELAYED_TICKS:
            req.meta["delayed_ticks"] = True
        if size is not None and size != UNSET_DECIMAL:
            try:
                v = to_decimal(size)
                if v >= 0:
                    req.meta["volume"] = v
            except MoneyError:
                pass

    def tickString(self, reqId, tickType, value):
        req = self._req(reqId)
        if req is None or req.kind != "quote" or tickType not in _TIME_TICKS:
            return
        try:
            req.meta["last_timestamp"] = datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            log.warning("Unparseable last-trade timestamp %r", value)

    def tickSnapshotEnd(self, reqId):
        req = self._req(reqId)
        if req is not None:
            req.done.set()

    def historicalData(self, reqId, bar):
        req = self._req(reqId)
        if req is not None and req.kind == "historical":
            req.items.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        req = self._req(reqId)
        if req is not None:
            req.done.set()
