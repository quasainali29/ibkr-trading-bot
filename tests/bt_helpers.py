"""Deterministic synthetic market history for backtest tests (no IBKR)."""

import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from conftest import make_cfg, make_info
from market_data.models import Bar

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
D = Decimal
CENT = D("0.01")


def sessions(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _q(x):
    return D(str(x)).quantize(CENT, ROUND_HALF_UP)


def synth_5m(start, end, seed=1, price=100.0, drift=0.0004, vol=0.0025, base_volume=10000):
    """78 x 5m RTH bars per weekday; seeded random walk."""
    rng = random.Random(seed)
    bars, p = [], price
    for d in sessions(start, end):
        t0 = datetime.combine(d, time(9, 30), tzinfo=ET).astimezone(UTC)
        for k in range(78):
            o = p
            path = [o]
            for _ in range(4):
                p = max(1.0, p * (1 + drift / 78 + rng.gauss(0, vol)))
                path.append(p)
            c = path[-1]
            hi, lo = max(path) * (1 + abs(rng.gauss(0, vol / 3))), min(path) * (1 - abs(rng.gauss(0, vol / 3)))
            v = int(base_volume * (1.5 if k < 6 or k > 71 else 1.0) * (0.5 + rng.random()))
            bars.append(Bar(t0 + timedelta(minutes=5 * k), _q(o), _q(hi), _q(lo), _q(c), D(v)))
    return bars


def aggregate(bars5, minutes, daily=False):
    """Aggregate 5m bars to N-minute bars or daily bars. Like IBKR, intraday
    bars are CLOCK-aligned: with a 09:30 open the first 1h bar is 09:30-10:00."""
    groups = {}
    for b in bars5:
        local = b.timestamp.astimezone(ET)
        if daily:
            key = datetime.combine(local.date(), time(0), tzinfo=ET).astimezone(UTC)
        else:
            mins = local.hour * 60 + local.minute
            slot_start = max((mins // minutes) * minutes, 9 * 60 + 30)
            key = (datetime.combine(local.date(), time(0), tzinfo=ET)
                   + timedelta(minutes=slot_start)).astimezone(UTC)
        groups.setdefault(key, []).append(b)
    out = []
    for k in sorted(groups):
        g = groups[k]
        out.append(Bar(k, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                       sum((x.volume for x in g), D(0))))
    return out


def build_store(tmp_path, symbols, start, end, cfg=None, seeds=None, drifts=None,
                timeframes=("1d", "1h", "15m", "5m")):
    """Populate a HistoryStore with synthetic data + contracts."""
    from backtest.data_loader import HistoryStore
    cfg = cfg or make_cfg()
    store = HistoryStore(tmp_path / "data", cfg)
    warm_start = start - timedelta(days=max(cfg.BACKTEST_WARMUP_DAYS.values()))
    for i, sym in enumerate(symbols):
        etf = sym in ("SPY", "QQQ", "XLK", "XLF", "XLV", "XLI")
        info = make_info(symbol=sym, con_id=1000 + i, stock_type="ETF" if etf else "COMMON",
                         long_name=f"{sym} TEST", primary="ARCA" if etf else "NASDAQ",
                         industry="" if etf else "Technology")
        store.save_contract(info)
        b5 = synth_5m(warm_start, end, seed=(seeds or {}).get(sym, 7 + i),
                      drift=(drifts or {}).get(sym, 0.0004))
        series = {"5m": b5, "15m": aggregate(b5, 15), "1h": aggregate(b5, 60),
                  "1d": aggregate(b5, 0, daily=True)}
        for tf in timeframes:
            store.save(sym, info.con_id, tf, series[tf], warm_start, end)
    return store
