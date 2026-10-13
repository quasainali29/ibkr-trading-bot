from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from conftest import daily_bars, make_bar, utc
from market_data import features as F
from market_data.models import TIMEFRAMES

D = Decimal
ET = ZoneInfo("America/New_York")


def close_enough(a, b, eps=D("1e-20")):
    return abs(a - b) < eps


# --------------------------------------------------------------------------
# Candle geometry
# --------------------------------------------------------------------------
class TestCandleGeometry:
    def test_bullish_candle(self):
        g = F.candle_geometry(make_bar(utc(2026, 10, 6), 10, 12, 9, 11))
        assert (g.body, g.upper_wick, g.lower_wick, g.range) == (D(1), D(1), D(1), D(3))
        assert g.direction == "UP"

    def test_bearish_candle(self):
        g = F.candle_geometry(make_bar(utc(2026, 10, 6), "11.50", "11.75", "10.00", "10.25"))
        assert g.body == D("1.25")
        assert g.upper_wick == D("0.25")
        assert g.lower_wick == D("0.25")
        assert g.range == D("1.75")
        assert g.direction == "DOWN"

    def test_doji(self):
        g = F.candle_geometry(make_bar(utc(2026, 10, 6), 10, 11, 9, 10))
        assert g.body == 0 and g.direction == "DOJI"
        assert g.upper_wick == g.lower_wick == D(1)

    def test_geometry_sums_to_range(self):
        b = make_bar(utc(2026, 10, 6), "100.10", "101.37", "99.02", "100.95")
        g = F.candle_geometry(b)
        assert g.body + g.upper_wick + g.lower_wick == g.range

    def test_pct_change(self):
        bars = daily_bars([100, 110, 99])
        out = F.pct_changes(bars)
        assert out[0] is None
        assert out[1] == D("0.1")
        assert out[2] == D("-0.1")


# --------------------------------------------------------------------------
# ATR
# --------------------------------------------------------------------------
class TestATR:
    bars = [
        make_bar(utc(2026, 1, 5), 9, 10, 8, 9),        # TR 2 (h-l)
        make_bar(utc(2026, 1, 6), 9, 11, 9, 10),       # TR 2
        make_bar(utc(2026, 1, 7), 10, 12, 9, 11),      # TR 3
        make_bar(utc(2026, 1, 8), 11, 13, 11, 12),     # TR 2
        make_bar(utc(2026, 1, 9), 15, 16, 15, "15.5"),  # gap: TR = |16-12| = 4
    ]

    def test_true_range_uses_previous_close(self):
        assert F.true_ranges(self.bars) == [D(2), D(2), D(3), D(2), D(4)]

    def test_wilder_atr_hand_calculated(self):
        out = F.atr(self.bars, period=3)
        assert out[:2] == [None, None]
        assert close_enough(out[2], D(7) / D(3))
        assert close_enough(out[3], D(20) / D(9))
        assert close_enough(out[4], D(76) / D(27))

    def test_insufficient_bars(self):
        assert F.atr(self.bars[:2], period=3) == [None, None]

    def test_constant_range_atr(self):
        bars = daily_bars([100] * 30, spread=1)    # every TR = 2
        out = F.atr(bars, 14)
        assert out[12] is None and out[13] == D(2) and out[-1] == D(2)

    def test_invalid_period(self):
        with pytest.raises(ValueError):
            F.atr(self.bars, 0)


# --------------------------------------------------------------------------
# Moving averages
# --------------------------------------------------------------------------
class TestSMA:
    def test_basic(self):
        assert F.sma([D(1), D(2), D(3), D(4), D(5)], 3) == [None, None, D(2), D(3), D(4)]

    def test_ma20_ma50(self):
        closes = [D(i) for i in range(1, 61)]
        ma20, ma50 = F.sma(closes, 20), F.sma(closes, 50)
        assert ma20[18] is None and ma20[19] == D("10.5")
        assert ma50[48] is None and ma50[49] == D("25.5")
        assert ma50[-1] == D("35.5")       # mean of 11..60

    def test_never_partial(self):
        assert F.sma([D(1)] * 10, 20) == [None] * 10

    def test_none_in_window(self):
        assert F.sma([D(1), None, D(3), D(4)], 2) == [None, None, None, D("3.5")]


# --------------------------------------------------------------------------
# VWAP / relative volume
# --------------------------------------------------------------------------
class TestVWAP:
    def test_resets_each_session(self):
        day1 = utc(2026, 10, 5, 13, 30)
        day2 = utc(2026, 10, 6, 13, 30)
        bars = [
            make_bar(day1, 10, 10, 10, 10, v=100),
            make_bar(day1 + timedelta(minutes=5), 20, 20, 20, 20, v=300),
            make_bar(day2, 50, 50, 50, 50, v=10),
        ]
        out = F.session_vwap(bars, lambda ts: ts.astimezone(ET).date())
        assert out[0] == D(10)
        assert out[1] == D("17.5")            # (10*100 + 20*300) / 400
        assert out[2] == D(50)                # reset

    def test_uses_ibkr_wap_when_present(self):
        b = make_bar(utc(2026, 10, 5, 13, 30), 10, 12, 8, 11, v=100, wap="10.4")
        assert F.session_vwap([b], lambda ts: 1)[0] == D("10.4")

    def test_zero_volume(self):
        b = make_bar(utc(2026, 10, 5, 13, 30), 10, 12, 8, 11, v=0)
        assert F.session_vwap([b], lambda ts: 1)[0] is None


class TestRelativeVolume:
    def test_daily(self):
        bars = daily_bars([100] * 20 + [100], volume=1000)
        bars[-1] = make_bar(bars[-1].timestamp, 100, 101, 99, 100, v=2000)
        out = F.relative_volume_daily(bars, 20)
        assert out[19] is None
        assert out[20] == D(2)

    def _intraday(self, sessions, last_volume):
        bars = []
        for d in range(sessions):
            t = utc(2026, 9, 1 + d, 13, 30)
            vol = last_volume if d == sessions - 1 else 100
            bars.append(make_bar(t, 10, 11, 9, 10, v=vol))
            bars.append(make_bar(t + timedelta(minutes=5), 10, 11, 9, 10, v=50))
        return bars

    def _rvol(self, bars, min_sessions=5):
        sess = lambda ts: ts.astimezone(ET).date()
        slot = lambda ts: ts.astimezone(ET).strftime("%H:%M")
        return F.relative_volume_intraday(bars, sess, slot, min_sessions, 20)

    def test_same_time_of_day(self):
        out = self._rvol(self._intraday(6, 300))
        assert out[-2] == D(3)               # 300 vs avg 100 of the 09:30 slot
        assert out[-1] == D(1)               # 09:35 slot: 50 vs 50

    def test_not_enough_sessions(self):
        out = self._rvol(self._intraday(5, 300))   # only 4 prior sessions
        assert out[-2] is None and out[-1] is None


def test_compute_features_daily_has_no_vwap(cfg):
    bars = daily_bars([100 + i for i in range(60)])
    fs = F.compute_features(bars, TIMEFRAMES["1d"], ET, cfg)
    last = fs.latest
    assert last.vwap is None
    assert last.sma_fast == D("149.5") and last.sma_slow == D("134.5")
    assert last.atr is not None and last.rvol == D(1)
    assert len(fs.rows) == 60


def test_compute_features_intraday_has_vwap(cfg):
    t = utc(2026, 10, 6, 13, 30)
    bars = [make_bar(t + timedelta(minutes=5 * i), 10, 11, 9, 10, v=100) for i in range(10)]
    fs = F.compute_features(bars, TIMEFRAMES["5m"], ET, cfg)
    assert fs.latest.vwap == D(10)
    assert fs.latest.sma_fast is None      # only 10 bars


def test_sma_running_sum_identical_to_window_sum():
    """Optimised SMA must equal the naive per-window sum exactly."""
    import random
    rng = random.Random(42)
    values = [D(str(round(rng.uniform(1, 2000), rng.choice([2, 4])))) for _ in range(400)]
    values[37] = None
    values[200] = None
    for period in (1, 5, 20, 50):
        naive = [None] * len(values)
        for i in range(period - 1, len(values)):
            w = values[i - period + 1:i + 1]
            if all(v is not None for v in w):
                naive[i] = sum(w, D(0)) / D(period)
        assert F.sma(values, period) == naive
