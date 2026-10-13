from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from analysis.market_report import (classify_trend, cluster_zones, find_swings,
                                    support_resistance)
from conftest import daily_bars, make_bar, utc
from market_data.features import compute_features
from market_data.models import TIMEFRAMES

D = Decimal
ET = ZoneInfo("America/New_York")


def bars_from_hl(pairs):
    t = utc(2026, 1, 5, 5)
    return [make_bar(t + timedelta(days=i), (h + l) / 2, h, l, (h + l) / 2)
            for i, (h, l) in enumerate(pairs)]


def test_find_swings():
    #             0       1       2       3        4       5       6
    bars = bars_from_hl([(10, 8), (11, 9), (12, 10), (15, 11), (12, 10), (11, 9), (10, 5)])
    pts = find_swings(bars, 2)
    assert [(p[1], p[2]) for p in pts] == [(D(15), "H")]


def test_cluster_zones_groups_within_tolerance():
    t = utc(2026, 1, 5)
    pts = [(t, D("100.0"), "L"), (t, D("100.4"), "L"), (t, D("101.0"), "H"),
           (t, D("110"), "H")]
    zones = cluster_zones(pts, tolerance=D("1.0"), min_width=D("0.2"))
    assert len(zones) == 2
    assert (zones[0].low, zones[0].high, zones[0].touches) == (D("100.0"), D("101.0"), 3)
    # single-point zone is widened to the minimum width - a zone, not a price
    assert zones[1].low == D("109.9") and zones[1].high == D("110.1")


def test_support_below_resistance_above(cfg):
    # oscillating series: lows near 90, highs near 110
    pairs = []
    for i in range(60):
        phase = i % 8
        mid = 100 + (10 if phase == 4 else -10 if phase == 0 else 0)
        pairs.append((mid + 1, mid - 1))
    bars = bars_from_hl(pairs)
    sup, res = support_resistance(bars, D(2), D(100), cfg)
    assert sup and res
    assert all(z.high < 100 for z in sup)
    assert all(z.low > 100 for z in res)
    assert sup[0].touches >= 3 and res[0].touches >= 3


def test_no_zones_without_atr(cfg):
    assert support_resistance(daily_bars([100] * 30), None, D(100), cfg) == ([], [])


def _trend(cfg, closes):
    fs = compute_features(daily_bars(closes), TIMEFRAMES["1d"], ET, cfg)
    return classify_trend(fs, cfg)


def test_uptrend(cfg):
    assert _trend(cfg, [100 + i for i in range(80)]).label == "UPTREND"


def test_downtrend(cfg):
    assert _trend(cfg, [200 - i for i in range(80)]).label == "DOWNTREND"


def test_sideways(cfg):
    assert _trend(cfg, [100 + (i % 2) for i in range(80)]).label == "SIDEWAYS / MIXED"


def test_insufficient_data(cfg):
    assert _trend(cfg, [100 + i for i in range(40)]).label == "INSUFFICIENT DATA"
