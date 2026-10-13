"""
Descriptive market analysis - NO trading decisions, NO signals.

Consumes validated Bar sequences and the deterministic features computed in
market_data.features. Produces a trend label and support/resistance ZONES
(price bands, not single prices) built by clustering swing highs/lows that
lie within SR_ZONE_ATR_MULT x ATR of each other.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from market_data.features import compute_features

TWO = Decimal(2)


@dataclass(frozen=True)
class Zone:
    low: Decimal
    high: Decimal
    touches: int
    last_touch: datetime
    contains_price: bool = False

    @property
    def mid(self):
        return (self.low + self.high) / TWO


@dataclass(frozen=True)
class TrendResult:
    label: str          # UPTREND / DOWNTREND / SIDEWAYS / INSUFFICIENT DATA
    detail: str


# ---------------------------------------------------------------------------
# Swings and zones
# ---------------------------------------------------------------------------
def find_swings(bars, window):
    """Swing highs/lows: a bar whose high (low) is strictly above (below)
    the `window` bars before it and at least as extreme as the `window` bars
    after it. Returns [(timestamp, price, "H"|"L")]."""
    points = []
    for i in range(window, len(bars) - window):
        left, right = bars[i - window:i], bars[i + 1:i + 1 + window]
        b = bars[i]
        if b.high > max(x.high for x in left) and b.high >= max(x.high for x in right):
            points.append((b.timestamp, b.high, "H"))
        if b.low < min(x.low for x in left) and b.low <= min(x.low for x in right):
            points.append((b.timestamp, b.low, "L"))
    return points


def cluster_zones(points, tolerance, min_width):
    """Greedy clustering of swing prices: a cluster grows while the next
    price is within `tolerance` of the cluster's lowest price."""
    zones = []
    ordered = sorted(points, key=lambda p: p[1])
    cluster = []
    for p in ordered:
        if cluster and p[1] - cluster[0][1] > tolerance:
            zones.append(_make_zone(cluster, min_width))
            cluster = []
        cluster.append(p)
    if cluster:
        zones.append(_make_zone(cluster, min_width))
    return zones


def _make_zone(cluster, min_width):
    lo = min(p[1] for p in cluster)
    hi = max(p[1] for p in cluster)
    if hi - lo < min_width:
        mid = (lo + hi) / TWO
        lo, hi = mid - min_width / TWO, mid + min_width / TWO
    cent = Decimal("0.01")
    return Zone(lo.quantize(cent, ROUND_FLOOR), hi.quantize(cent, ROUND_CEILING),
                len(cluster), max(p[0] for p in cluster))


def support_resistance(bars, atr_value, current_price, cfg):
    """Return (supports, resistances): nearest zones below / above price."""
    if atr_value is None or atr_value <= 0 or current_price is None or not bars:
        return [], []
    recent = list(bars)[-cfg.SR_LOOKBACK_BARS:]
    zones = cluster_zones(find_swings(recent, cfg.SWING_WINDOW),
                          atr_value * cfg.SR_ZONE_ATR_MULT,
                          atr_value * cfg.SR_MIN_ZONE_ATR_MULT)
    supports, resistances = [], []
    for z in zones:
        inside = z.low <= current_price <= z.high
        z = Zone(z.low, z.high, z.touches, z.last_touch, inside)
        (supports if z.mid <= current_price else resistances).append(z)
    supports.sort(key=lambda z: current_price - z.mid)
    resistances.sort(key=lambda z: z.mid - current_price)
    return supports[:cfg.SR_MAX_ZONES], resistances[:cfg.SR_MAX_ZONES]


# ---------------------------------------------------------------------------
# Trend (descriptive)
# ---------------------------------------------------------------------------
def classify_trend(features, cfg):
    rows = features.rows
    need = cfg.MA_SLOW_PERIOD + cfg.TREND_SLOPE_LOOKBACK
    if len(rows) < need or rows[-1].sma_slow is None:
        return TrendResult("INSUFFICIENT DATA",
                           f"need at least {need} bars, have {len(rows)}")
    last = rows[-1]
    prior = rows[-1 - cfg.TREND_SLOPE_LOOKBACK].sma_fast
    close, fast, slow = last.bar.close, last.sma_fast, last.sma_slow
    if prior is None:
        return TrendResult("INSUFFICIENT DATA", "MA slope unavailable")
    rising, falling = fast > prior, fast < prior
    detail = (f"close {close} | MA{cfg.MA_FAST_PERIOD} {fast:.2f} "
              f"({'rising' if rising else 'falling' if falling else 'flat'}) | "
              f"MA{cfg.MA_SLOW_PERIOD} {slow:.2f}")
    if close > fast > slow and rising:
        return TrendResult("UPTREND", detail)
    if close < fast < slow and falling:
        return TrendResult("DOWNTREND", detail)
    return TrendResult("SIDEWAYS / MIXED", detail)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass
class MarketReport:
    symbol: str
    contract: object
    timeframe_key: str
    check: object
    quote: object
    current_price: Decimal | None
    price_source: str
    prev_close: Decimal | None
    daily_change_pct: Decimal | None
    day_volume: Decimal | None
    features: object = None
    atr: Decimal | None = None
    ma_fast: Decimal | None = None
    ma_slow: Decimal | None = None
    trend: TrendResult | None = None
    supports: list = field(default_factory=list)
    resistances: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    in_progress: object = None      # forming bar of the analysis timeframe


def build_report(smd, timeframe_key, cfg):
    """Build a descriptive report from fetched SymbolMarketData."""
    cc = smd.contract_check
    q = smd.quote
    report = MarketReport(
        symbol=smd.symbol, contract=cc.contract if cc else None,
        timeframe_key=timeframe_key, check=smd.check, quote=q,
        current_price=q.current_price if q else None,
        price_source=q.price_source if q else "none",
        prev_close=q.prev_close if q else None,
        daily_change_pct=q.daily_change_pct if q else None,
        day_volume=q.day_volume if q else None,
        errors=dict(smd.errors))

    series = smd.series.get(timeframe_key)
    if series is None or smd.calendar is None:
        return report
    feats = compute_features(series.bars, series.timeframe, smd.calendar.tz, cfg)
    last = feats.latest
    report.features = feats
    report.atr, report.ma_fast, report.ma_slow = last.atr, last.sma_fast, last.sma_slow
    report.trend = classify_trend(feats, cfg)

    # series.bars holds COMPLETED bars only; the forming bar is display-only.
    report.in_progress = series.in_progress
    if report.current_price is None:
        if series.in_progress is not None:
            report.current_price = series.in_progress.close
            report.price_source = f"in-progress {timeframe_key} bar close (no quote)"
        else:
            report.current_price = last.bar.close
            report.price_source = f"last completed {timeframe_key} close (no quote)"
    daily = smd.series.get("1d")
    if daily is not None and daily.bars:
        if report.prev_close is None:
            report.prev_close = daily.latest_completed.close
            report.notes.append("previous close = latest completed daily candle")
        if report.daily_change_pct is None and report.prev_close:
            report.daily_change_pct = (report.current_price - report.prev_close) / report.prev_close
    report.supports, report.resistances = support_resistance(
        series.bars, report.atr, report.current_price, cfg)
    return report
