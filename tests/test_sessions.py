from zoneinfo import ZoneInfo

import pytest

from conftest import utc
from market_data.sessions import (SessionError, TradingCalendar, parse_hours,
                                  resolve_tz)

ET = ZoneInfo("America/New_York")


def test_new_format_with_closed_days():
    s = parse_hours("20261006:0930-20261006:1600;20261010:CLOSED", ET)
    assert len(s) == 1
    assert s[0].start == utc(2026, 10, 6, 13, 30) and s[0].end == utc(2026, 10, 6, 20, 0)


def test_old_format_and_comma_segments():
    s = parse_hours("20261006:0930-1200,1300-1600;20261007:CLOSED", ET)
    assert [x.start.hour for x in s] == [13, 17]


def test_winter_time_offset():
    s = parse_hours("20260115:0930-20260115:1600", ET)
    assert s[0].start == utc(2026, 1, 15, 14, 30)        # EST = UTC-5


def test_half_day():
    s = parse_hours("20261127:0930-20261127:1300", ET)
    assert s[0].end == utc(2026, 11, 27, 18, 0)


@pytest.mark.parametrize("bad", ["20261006:0930", "20261006:09-1600", "garbage-x",
                                 "20261006:1600-20261006:0930"])
def test_malformed_raises(bad):
    with pytest.raises(SessionError):
        parse_hours(bad, ET)


def test_tz_resolution():
    assert resolve_tz("US/Eastern") is not None
    assert str(resolve_tz("EST")) == "America/New_York"
    with pytest.raises(SessionError):
        resolve_tz("Mars/Olympus")
    with pytest.raises(SessionError):
        resolve_tz("")


def test_calendar_queries(calendar):
    assert calendar.is_open(utc(2026, 10, 6, 15))
    assert not calendar.is_open(utc(2026, 10, 6, 21))
    assert not calendar.is_open(utc(2026, 10, 10, 15))           # Saturday
    last = calendar.last_completed_session(utc(2026, 10, 7, 1))
    assert last.local_date.isoformat() == "2026-10-06"
    d, approx = calendar.previous_session_date(utc(2026, 10, 7, 12))
    assert d.isoformat() == "2026-10-06" and not approx
