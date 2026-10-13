import json
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import make_bar, utc
from market_data.candle_store import (CandleStore, CandleStoreError,
                                      bar_is_complete, merge_bars)
from market_data.models import TIMEFRAMES, TradingSession

TF5 = TIMEFRAMES["5m"]
RTH = TradingSession.RTH
NOW = utc(2026, 10, 6, 15, 0)


def bars(n=5, last=utc(2026, 10, 6, 14, 55)):
    return [make_bar(last - timedelta(minutes=5 * (n - 1 - i)), 10, 11, 9, "10.25", v=100 + i)
            for i in range(n)]


@pytest.fixture
def store(tmp_path, cfg):
    return CandleStore(tmp_path, cfg)


def test_path_layout(store, tmp_path):
    csv_path, meta = store.paths("aapl", TF5, RTH)
    assert csv_path == tmp_path / "AAPL" / "5m.csv"
    assert store.paths("AAPL", TF5, TradingSession.EXTENDED)[0].name == "5m_ext.csv"


def test_roundtrip_drops_in_progress_bar(store, calendar):
    data = bars()
    store.save("AAPL", 265598, TF5, RTH, data, calendar, NOW)
    loaded = store.load("AAPL", 265598, TF5, RTH, NOW)
    # last bar (14:55) is still in progress at 15:00? 14:55+5m = 15:00 <= now -> complete
    assert loaded == data
    store.save("AAPL", 265598, TF5, RTH, data, calendar, utc(2026, 10, 6, 14, 57))
    assert store.load("AAPL", 265598, TF5, RTH, NOW) == data[:-1]


def test_decimal_precision_preserved(store, calendar):
    store.save("AAPL", 1, TF5, RTH, bars(), calendar, NOW)
    assert store.load("AAPL", 1, TF5, RTH, NOW)[0].close == Decimal("10.25")


def test_tampered_file_is_quarantined(store, calendar, tmp_path):
    store.save("AAPL", 1, TF5, RTH, bars(), calendar, NOW)
    csv_path, _ = store.paths("AAPL", TF5, RTH)
    csv_path.write_text(csv_path.read_text().replace("10.25", "99.99"))
    assert store.load("AAPL", 1, TF5, RTH, NOW) == []
    assert not csv_path.exists()
    assert any(".corrupt-" in p.name for p in (tmp_path / "AAPL").iterdir())


def test_conid_mismatch_discards(store, calendar):
    store.save("AAPL", 1, TF5, RTH, bars(), calendar, NOW)
    assert store.load("AAPL", 2, TF5, RTH, NOW) == []


def test_refuses_to_store_invalid_bars(store, calendar):
    bad = bars()
    bad[2] = make_bar(bad[2].timestamp, 10, 9, 11, 10)      # high < low
    with pytest.raises(CandleStoreError):
        store.save("AAPL", 1, TF5, RTH, bad, calendar, NOW)


def test_missing_cache(store):
    assert store.load("MSFT", 1, TF5, RTH, NOW) == []


def test_trimmed_to_cap(store, calendar, cfg):
    tf = TIMEFRAMES["5m"].__class__("5m", "5 mins", "10 D", 300, True, 3)
    store.save("AAPL", 1, tf, RTH, bars(5), calendar, NOW)
    meta = json.loads(store.paths("AAPL", tf, RTH)[1].read_text())
    assert meta["rows"] == 3


def test_merge_new_overrides_old():
    old = bars(3)
    updated = make_bar(old[-1].timestamp, 10, 12, 9, 11)
    merged = merge_bars(old, [updated, make_bar(old[-1].timestamp + timedelta(minutes=5),
                                                11, 12, 10, 11)])
    assert len(merged) == 4 and merged[2].high == Decimal(12)


def test_daily_completeness(calendar):
    tf = TIMEFRAMES["1d"]
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    today = datetime(2026, 10, 6, tzinfo=ZoneInfo("US/Eastern")).astimezone(timezone.utc)
    b = make_bar(today, 10, 11, 9, 10)
    assert not bar_is_complete(b, tf, calendar, utc(2026, 10, 6, 15))   # session open
    assert bar_is_complete(b, tf, calendar, utc(2026, 10, 7, 1))        # after close
