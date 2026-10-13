"""
As-of-T views: the ONLY path from historical data to the strategy.

At decision time T a bar is visible only if it had COMPLETED by T:
  intraday bar : its clock-aligned end <= T (the 09:30-10:00 opening 1h
                 bar is visible at 10:00, never earlier)
  daily bar    : its regular session had closed by T (today's forming daily
                 candle is never visible - same as live split_completed)
Windows have the same length a live --evaluate would fetch. Phase 2
validation / freshness run with now=T. The quote is SIMULATED from the last
bar completed by T and is explicitly labelled (simulated=True,
source=BACKTEST, type SIMULATED_BACKTEST) - never a genuine LIVE quote.
"""

from bisect import bisect_right
from datetime import datetime, timedelta, timezone

from analysis.relative_strength import benchmarks_for
from analysis.setup_context import build_context
from environment import Environment
from market_data.contracts import validate_contract
from market_data.models import (BarSeries, MarketDataType, Quote, SymbolMarketData,
                                TradingSession, get_timeframe)
from market_data.sessions import DEFAULT_RTH_CLOSE, bar_end
from market_data.validation import (TimeframeStatus, bar_freshness, build_market_data_check,
                                    quote_status, validate_quote)

QUOTE_TIMEFRAMES = ("5m", "15m", "1h")      # finest available supplies the quote


class SymbolHistory:
    def __init__(self, symbol, contract, series, calendar, cfg):
        self.symbol = symbol
        self.contract = contract
        self.series = {k: list(v) for k, v in series.items() if v}
        self.calendar = calendar
        by_date = {s.local_date: s for s in calendar.sessions}
        self.ends = {}
        for key, bars in self.series.items():
            tf = get_timeframe(key)
            if tf.intraday:
                self.ends[key] = [bar_end(b.timestamp, tf.seconds, calendar.tz) for b in bars]
            else:
                ends = []
                for b in bars:
                    d = b.timestamp.astimezone(calendar.tz).date()
                    s = by_date.get(d)
                    ends.append(s.end if s is not None else datetime.combine(
                        d, DEFAULT_RTH_CLOSE, tzinfo=calendar.tz).astimezone(timezone.utc))
                self.ends[key] = ends
        self.contract_check = validate_contract(symbol, [contract], cfg)

    def completed(self, key, t, window):
        """Bars of `key` completed by t (at most `window` of them)."""
        bars = self.series.get(key)
        if not bars:
            return []
        i = bisect_right(self.ends[key], t)
        return bars[max(0, i - window):i]



class AsOfBuilder:
    def __init__(self, histories, calendar, cfg, half_spread_fn):
        self.h = histories
        self.calendar = calendar
        self.cfg = cfg
        self.half_spread = half_spread_fn

    def smd_at(self, symbol, t, timeframes, quote=True):
        cfg, cal, h = self.cfg, self.calendar, self.h[symbol]
        smd = SymbolMarketData(symbol, h.contract_check)
        smd.calendar = cal
        for key in timeframes:
            window = h.completed(key, t, cfg.BACKTEST_WINDOW_BARS[key])
            if not window:
                smd.timeframe_statuses[key] = TimeframeStatus(key, False, False,
                                                              errors=["no history as of T"])
                continue
            tf = get_timeframe(key)
            fr = bar_freshness(window, tf, cal, t, cfg)
            smd.series[key] = BarSeries(symbol, h.contract.con_id, tf, TradingSession.RTH,
                                        tuple(window), t, "backtest")
            smd.timeframe_statuses[key] = TimeframeStatus(
                key, True, fr.stale, len(window), window[-1].timestamp,
                stale_reasons=list(fr.reasons), warnings=list(fr.warnings))

        q = self._simulated_quote(symbol, h, t, timeframes) if quote else None
        if q is None:
            smd.quote_status = quote_status(None, None,
                                            "quote not requested" if not quote else
                                            "no completed bar for a simulated quote")
        else:
            smd.quote = q
            smd.quote_status = quote_status(q, validate_quote(q, cal, t, cfg))
        smd.check = build_market_data_check(symbol, h.contract_check, smd.quote_status,
                                            smd.timeframe_statuses, cal, t, cfg,
                                            environment=Environment.BACKTEST)
        return smd

    def _simulated_quote(self, symbol, h, t, timeframes):
        key = next((k for k in QUOTE_TIMEFRAMES if k in timeframes and h.series.get(k)), None)
        if key is None:
            return None
        window = h.completed(key, t, self.cfg.BACKTEST_WINDOW_BARS[key])
        if not window:
            return None
        last = window[-1]
        tf = get_timeframe(key)
        last_end = bar_end(last.timestamp, tf.seconds, self.calendar.tz)
        hs = self.half_spread(last.close)
        today = self.calendar.local_date(t)
        day_volume = sum((b.volume for b in window
                          if self.calendar.local_date(b.timestamp) == today), last.volume * 0)
        daily = h.completed("1d", t, 1)
        prev_close = daily[-1].close if daily else None
        return Quote(symbol, h.contract.con_id, MarketDataType.SIMULATED_BACKTEST,
                     last.close, last.close - hs, last.close + hs, prev_close, day_volume,
                     last_end, t, simulated=True, source="BACKTEST")

    def benchmark_symbols(self, symbol):
        cfg = self.cfg
        contract = self.h[symbol].contract
        out = []
        for sym in benchmarks_for(contract, cfg) + [cfg.BENCHMARK_SPY, cfg.BENCHMARK_QQQ]:
            if sym not in out and sym != symbol and sym in self.h:
                out.append(sym)
        return out

    def context_at(self, symbol, t, timeframes, feature_cache=None):
        """Exactly what main.py --evaluate builds, but as of T."""
        cfg = self.cfg
        smd = self.smd_at(symbol, t, timeframes)
        benches = {symbol: smd} if symbol in (cfg.BENCHMARK_SPY, cfg.BENCHMARK_QQQ) else {}
        for sym in self.benchmark_symbols(symbol):
            benches[sym] = self.smd_at(sym, t, ("1d",), quote=False)
        return build_context(smd, benches, cfg, t, feature_cache)
