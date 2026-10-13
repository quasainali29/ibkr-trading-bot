"""
SetupContext: everything the strategy layer may read, assembled from
validated market data. Strategy code never touches IBKR or Phase 2 internals.

Per timeframe the view is FRESH, STALE or UNAVAILABLE. STALE timeframes are
still described (clearly labelled) but are never used for timing or
multi-timeframe alignment - nothing is fabricated for missing data.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from analysis.candle_interpretation import interpret_recent
from analysis.market_structure import analyze_structure
from analysis.regime import UNKNOWN, RegimeResult, classify_regime
from analysis.relative_strength import relative_strength
from analysis.volume_analysis import breakout_quality, classify_pressure
from market_data.features import cached_features
from market_data.models import get_timeframe

FRESH, STALE, UNAVAILABLE = "FRESH", "STALE", "UNAVAILABLE"


@dataclass
class TimeframeView:
    key: str
    status: str                  # FRESH / STALE / UNAVAILABLE
    structure: object = None     # StructureResult
    features: object = None      # FeatureSet (completed bars)
    candles: list = field(default_factory=list)   # recent CandleReadings
    pressure: object = None      # PressureResult
    breakout: object = None      # BreakoutQuality
    last_bar: datetime | None = None
    in_progress: object = None   # forming bar (display only)
    note: str = ""

    @property
    def fresh(self):
        return self.status == FRESH

    @property
    def last_row(self):
        return self.features.rows[-1] if self.features and self.features.rows else None


@dataclass
class SetupContext:
    symbol: str
    contract: object
    quote: object
    quote_status: object
    current_price: Decimal | None
    price_source: str
    market_open: bool
    as_of: datetime
    timeframes: dict             # key -> TimeframeView
    regime: RegimeResult
    relative_strength: object
    tz: object
    smd: object = None           # SymbolMarketData (for setup-specific data checks)
    notes: list = field(default_factory=list)

    @property
    def daily(self):
        return self.timeframes.get("1d")

    @property
    def atr(self):
        row = self.daily.last_row if self.daily else None
        return row.atr if row else None


def _view(key, smd, regime_label, cfg, current_price, feature_cache=None):
    status_obj = smd.timeframe_statuses.get(key)
    series = smd.series.get(key)
    if status_obj is None or not status_obj.valid or series is None or not series.bars:
        err = "; ".join(status_obj.errors) if status_obj else "not fetched"
        return TimeframeView(key, UNAVAILABLE, note=err or "no validated bars")
    status = STALE if status_obj.stale else FRESH
    note = ("; ".join(status_obj.stale_reasons) or "stale") if status == STALE else ""
    return view_from_bars(key, series.bars, status, smd.calendar.tz, regime_label, cfg,
                          current_price if key == "1d" else None, series.in_progress, note,
                          feature_cache, smd.symbol)


def view_from_bars(key, bars, status, tz, regime_label, cfg, current_price=None,
                   in_progress=None, note="", feature_cache=None, cache_prefix=""):
    """Interpret one timeframe's COMPLETED bars."""
    feats = cached_features(bars, get_timeframe(key), tz, cfg, feature_cache, cache_prefix)
    structure = analyze_structure(bars, feats, key, cfg, current_price)
    return TimeframeView(key, status, structure, feats,
                         interpret_recent(feats.rows, structure, regime_label, cfg),
                         classify_pressure(feats.rows, structure, cfg),
                         breakout_quality(feats.rows, structure, cfg),
                         bars[-1].timestamp, in_progress, note)


def build_context(smd, benchmark_data, cfg, now=None, feature_cache=None):
    """Build a SetupContext from SymbolMarketData for the target and
    {symbol: SymbolMarketData} for benchmarks (SPY, QQQ, sector ETF)."""
    now = now or datetime.now(timezone.utc)
    tz = smd.calendar.tz if smd.calendar else None
    quote = smd.quote
    qs = smd.quote_status

    def bench_bars(sym):
        b = benchmark_data.get(sym)
        if b is None:
            return None
        st = b.timeframe_statuses.get("1d")
        s = b.series.get("1d")
        return list(s.bars) if (st is not None and st.usable_for_analysis and s) else None

    if tz is None:
        regime = RegimeResult(UNKNOWN, details=["target contract/calendar unavailable"])
    else:
        regime = classify_regime(bench_bars(cfg.BENCHMARK_SPY), bench_bars(cfg.BENCHMARK_QQQ),
                                 tz, cfg, feature_cache)

    daily_series = smd.series.get("1d")
    price, source = None, "none"
    if quote is not None and quote.current_price is not None:
        price = quote.current_price
        source = f"quote ({quote.data_type.label if quote.data_type else 'type UNKNOWN'})"
    elif daily_series is not None and daily_series.bars:
        price = daily_series.bars[-1].close
        source = "latest completed daily close (no quote)"

    views = {}
    for key in cfg.EVALUATION_TIMEFRAMES:
        if smd.calendar is None:
            views[key] = TimeframeView(key, UNAVAILABLE, note="contract unavailable")
        else:
            views[key] = _view(key, smd, regime.label, cfg, price, feature_cache)

    rs = None
    if tz is not None and daily_series is not None and daily_series.bars:
        benches = {sym: bench_bars(sym) for sym in benchmark_data}
        rs = relative_strength(list(daily_series.bars),
                               {k: v for k, v in benches.items() if v},
                               smd.contract_check.contract, tz, cfg)

    return SetupContext(
        symbol=smd.symbol, contract=smd.contract_check.contract if smd.contract_check else None,
        quote=quote, quote_status=qs, current_price=price, price_source=source,
        market_open=bool(smd.check and smd.check.market_open), as_of=now,
        timeframes=views, regime=regime, relative_strength=rs, tz=tz, smd=smd)
