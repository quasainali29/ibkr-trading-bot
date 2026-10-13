"""
Exchange session calendar built from IBKR contract-details hours strings.

IBKR formats (both supported):
    new: "20261006:0930-20261006:1600;20261007:CLOSED"
    old: "20261006:0930-1600;20261007:CLOSED"   (segments may be comma-separated)

liquidHours = regular trading hours (RTH); tradingHours = extended hours.
The calendar is built from whichever string the TradingSession requires.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ_ALIASES = {
    "EST": "America/New_York", "EDT": "America/New_York", "EST5EDT": "America/New_York",
    "CST": "America/Chicago", "CST6CDT": "America/Chicago",
    "MST": "America/Denver", "PST": "America/Los_Angeles", "PST8PDT": "America/Los_Angeles",
}
DEFAULT_RTH_CLOSE = time(16, 0)


class SessionError(ValueError):
    pass


def resolve_tz(tz_id):
    if not tz_id:
        raise SessionError("exchange time zone is missing")
    key = tz_id.split("(")[0].strip()
    # Aliases first: tzdata's "EST"/"MST" are fixed offsets WITHOUT daylight
    # saving, but IBKR uses them to mean the DST-observing US exchange zone.
    for candidate in (TZ_ALIASES.get(key), key):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    raise SessionError(f"unknown time zone {tz_id!r} (is the tzdata package installed?)")


@dataclass(frozen=True)
class Session:
    start: datetime   # aware UTC
    end: datetime     # aware UTC
    local_date: date

    def contains(self, ts):
        return self.start <= ts < self.end


def _point(text, tz, default_date=None):
    text = text.strip()
    if ":" in text:
        d_s, t_s = text.split(":", 1)
        d = datetime.strptime(d_s, "%Y%m%d").date()
    else:
        if default_date is None:
            raise SessionError(f"hours segment without a date: {text!r}")
        d, t_s = default_date, text
    if len(t_s) != 4 or not t_s.isdigit():
        raise SessionError(f"bad time {t_s!r}")
    hh, mm = int(t_s[:2]), int(t_s[2:])
    if hh == 24 and mm == 0:      # "2400" = end of day
        d, hh = d + timedelta(days=1), 0
    local = datetime.combine(d, time(hh, mm), tzinfo=tz)
    return local.astimezone(timezone.utc), d


def parse_hours(text, tz):
    """Parse an IBKR hours string into sorted Sessions."""
    sessions = []
    if not text:
        return sessions
    for day_part in text.split(";"):
        day_part = day_part.strip()
        if not day_part or day_part.upper().endswith("CLOSED"):
            continue
        day_date = None
        for segment in day_part.split(","):
            segment = segment.strip()
            if not segment:
                continue
            if "-" not in segment:
                raise SessionError(f"malformed hours segment {segment!r}")
            start_s, end_s = segment.split("-", 1)
            try:
                start, day_date = _point(start_s, tz, day_date)
                end, _ = _point(end_s, tz, day_date)
            except ValueError as exc:
                raise SessionError(f"malformed hours segment {segment!r}: {exc}") from None
            if end <= start:
                raise SessionError(f"session ends before it starts: {segment!r}")
            sessions.append(Session(start, end, day_date))
    return sorted(set(sessions), key=lambda s: s.start)


class TradingCalendar:
    def __init__(self, sessions, tz):
        # Sessions are sorted and non-overlapping, so starts and ends are both
        # sorted and lookups can use binary search (years of history in backtests).
        self.sessions = sorted(sessions, key=lambda s: s.start)
        self.tz = tz
        self._starts = [s.start for s in self.sessions]
        self._ends = [s.end for s in self.sessions]

    @classmethod
    def from_contract(cls, info, session_kind="RTH"):
        tz = resolve_tz(info.time_zone_id)
        text = info.liquid_hours if session_kind == "RTH" else info.trading_hours
        return cls(parse_hours(text, tz), tz)

    @classmethod
    def from_bars(cls, bars, bar_seconds, tz, close=DEFAULT_RTH_CLOSE):
        """Historical regular sessions derived from intraday RTH bars: one
        session per local date, from the first bar's start to the last bar's
        end (capped at the regular close). Exchange calendars - including
        early closes - are published in advance, so knowing a session's end
        time is not look-ahead."""
        by_date = {}
        for b in bars:
            d = b.timestamp.astimezone(tz).date()
            end = bar_end(b.timestamp, bar_seconds, tz)
            lo, hi = by_date.get(d, (b.timestamp, end))
            by_date[d] = (min(lo, b.timestamp), max(hi, end))
        sessions = []
        for d, (start, end) in by_date.items():
            cap = datetime.combine(d, close, tzinfo=tz).astimezone(timezone.utc)
            sessions.append(Session(start, min(end, cap), d))
        return cls(sessions, tz)

    def local_date(self, ts):
        return ts.astimezone(self.tz).date()

    def current_session(self, now):
        i = bisect_right(self._starts, now) - 1
        if i >= 0 and self.sessions[i].contains(now):
            return self.sessions[i]
        return None

    def is_open(self, now):
        return self.current_session(now) is not None

    def last_completed_session(self, now):
        i = bisect_right(self._ends, now) - 1
        return self.sessions[i] if i >= 0 else None

    def session_for(self, ts):
        return self.current_session(ts)

    def previous_session_date(self, now):
        """Local date of the most recent session that has ENDED.

        Uses the calendar when it covers the past; otherwise falls back to a
        weekday approximation (holidays unknown -> may report stale, which is
        the safe direction). Returns (date, approximated: bool).
        """
        s = self.last_completed_session(now)
        if s is not None:
            return s.local_date, False
        local = now.astimezone(self.tz)
        d = local.date()
        if local.time() < DEFAULT_RTH_CLOSE or d.weekday() >= 5:
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d, True


# ---------------------------------------------------------------------------
# Intraday bar duration (clock-aligned bars)
# ---------------------------------------------------------------------------
def bar_end(start, bar_seconds, tz):
    """End of the intraday bar starting at `start`.

    IBKR aligns intraday bars to the clock (multiples of the bar size from
    local midnight), so with a 09:30 open the first 1h bar is 09:30-10:00
    (30 minutes) and the next starts at 10:00. A bar that starts on a
    boundary lasts the full `bar_seconds`. Known from the start time alone
    (no look-ahead)."""
    if tz is None:
        return start + timedelta(seconds=bar_seconds)
    local = start.astimezone(tz)
    into = (local.hour * 3600 + local.minute * 60 + local.second) % bar_seconds
    return start + timedelta(seconds=bar_seconds - into)


def bar_span(start, bar_seconds, tz):
    """Fraction of the nominal bar length this bar covers (1 for a normal
    bar, 1/2 for the 09:30-10:00 opening 1h bar)."""
    secs = int((bar_end(start, bar_seconds, tz) - start).total_seconds())
    return Decimal(secs) / Decimal(bar_seconds)


# ---------------------------------------------------------------------------
# Completed vs in-progress bars
# ---------------------------------------------------------------------------
def bar_is_complete(bar, timeframe, calendar, as_of):
    """Was this bar finished at time `as_of`?

    Intraday: its interval has elapsed, or its session has ended.
    Daily:    its session date is not the session currently in progress.
    """
    if timeframe.intraday:
        if bar_end(bar.timestamp, timeframe.seconds, calendar.tz) <= as_of:
            return True
        sess = calendar.session_for(bar.timestamp)
        return sess is not None and sess.end <= as_of
    current = calendar.current_session(as_of)
    bar_date = calendar.local_date(bar.timestamp)
    if current is not None and current.local_date == bar_date:
        return False
    return bar_date <= calendar.local_date(as_of)


def split_completed(bars, timeframe, calendar, as_of):
    """Split a chronological bar sequence into (completed_bars, in_progress_bar).

    Only the newest bar can still be forming. Indicators (ATR, MAs, VWAP,
    RVOL, support/resistance) are computed on completed bars only; the
    in-progress bar is tracked separately for display.
    """
    bars = list(bars)
    if bars and not bar_is_complete(bars[-1], timeframe, calendar, as_of):
        return bars[:-1], bars[-1]
    return bars, None
