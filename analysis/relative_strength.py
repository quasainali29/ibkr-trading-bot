"""
Price relative strength (NOT RSI): the stock's return minus a benchmark's
return over 1, 5 and 20 sessions, using COMPLETED daily closes aligned by
date. Composite = weighted excess return; +/-RS_THRESHOLD separates
OUTPERFORMING / NEUTRAL / UNDERPERFORMING.
"""

from dataclasses import dataclass, field
from decimal import Decimal

OUT, NEUTRAL, UNDER = "OUTPERFORMING", "NEUTRAL", "UNDERPERFORMING"
POINTS = {OUT: Decimal(10), NEUTRAL: Decimal(5), UNDER: Decimal(0)}


@dataclass
class RSComparison:
    benchmark: str
    excess: dict            # period -> Decimal excess return (fraction)
    composite: Decimal
    label: str


@dataclass
class RelativeStrength:
    comparisons: list = field(default_factory=list)
    missing: list = field(default_factory=list)      # benchmarks without data
    sector_etf: str | None = None
    notes: list = field(default_factory=list)

    def get(self, benchmark):
        return next((c for c in self.comparisons if c.benchmark == benchmark), None)

    @property
    def primary(self):
        return self.get("SPY")

    @property
    def label(self):
        p = self.primary
        return p.label if p else "UNKNOWN"


def _closes_by_date(bars, tz):
    return {b.timestamp.astimezone(tz).date(): b.close for b in bars}


def compare(stock_bars, bench_bars, benchmark, tz, cfg):
    """Excess return of the stock vs one benchmark (None if not enough data)."""
    s, b = _closes_by_date(stock_bars, tz), _closes_by_date(bench_bars, tz)
    dates = sorted(set(s) & set(b))
    longest = max(cfg.RS_PERIOD_WEIGHTS)
    if len(dates) <= longest:
        return None
    excess = {}
    for n in cfg.RS_PERIOD_WEIGHTS:
        d0, d1 = dates[-1 - n], dates[-1]
        excess[n] = (s[d1] / s[d0] - 1) - (b[d1] / b[d0] - 1)
    composite = sum((excess[n] * w for n, w in cfg.RS_PERIOD_WEIGHTS.items()), Decimal(0))
    label = OUT if composite > cfg.RS_THRESHOLD else UNDER if composite < -cfg.RS_THRESHOLD else NEUTRAL
    return RSComparison(benchmark, excess, composite, label)


def sector_etf_for(contract, cfg):
    """Map IBKR category/industry metadata to a SPDR sector ETF (or None)."""
    if contract is None or (contract.stock_type or "").upper() == "ETF":
        return None
    cat = (contract.category or "").upper()
    for key, etf in cfg.SECTOR_ETF_BY_CATEGORY.items():
        if key in cat:
            return etf
    return cfg.SECTOR_ETF_BY_INDUSTRY.get((contract.industry or "").upper())


def benchmarks_for(contract, cfg):
    """SPY always; QQQ for NASDAQ-listed or tech/communications names; the
    sector ETF when mapped. ETFs compare with SPY only."""
    symbol = (contract.symbol if contract else "").upper()
    out = [cfg.BENCHMARK_SPY]
    if contract is not None and (contract.stock_type or "").upper() != "ETF":
        sector = sector_etf_for(contract, cfg)
        if (contract.primary_exchange or "").upper() in ("NASDAQ", "ISLAND") \
                or sector in cfg.QQQ_RELEVANT_SECTORS:
            out.append(cfg.BENCHMARK_QQQ)
        if sector:
            out.append(sector)
    return [b for b in dict.fromkeys(out) if b != symbol]


def relative_strength(stock_bars, bench_bars_by_symbol, contract, tz, cfg):
    rs = RelativeStrength(sector_etf=sector_etf_for(contract, cfg))
    if contract is not None and contract.symbol.upper() == cfg.BENCHMARK_SPY:
        zero = {n: Decimal(0) for n in cfg.RS_PERIOD_WEIGHTS}
        rs.comparisons.append(RSComparison(cfg.BENCHMARK_SPY, zero, Decimal(0), NEUTRAL))
        rs.notes.append("SPY is the benchmark itself: relative strength NEUTRAL by definition")
        return rs
    for sym in benchmarks_for(contract, cfg):
        bars = bench_bars_by_symbol.get(sym)
        cmp_ = compare(stock_bars, bars, sym, tz, cfg) if bars else None
        if cmp_ is None:
            rs.missing.append(sym)
        else:
            rs.comparisons.append(cmp_)
    if rs.sector_etf is None:
        rs.notes.append("sector ETF: n/a")
    return rs


def rs_points(rs):
    """Score component: mean of available comparison points; SPY is required."""
    if rs is None or rs.primary is None:
        return Decimal(0)
    pts = [POINTS[c.label] for c in rs.comparisons]
    return sum(pts, Decimal(0)) / Decimal(len(pts))
