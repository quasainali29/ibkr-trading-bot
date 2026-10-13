"""
Order / fill simulation for backtests. SIMULATION ONLY - no broker access.

Entry  : marketable LIMIT at maximum_entry, valid until the next decision
         time. Eligible bars start at or after the signal time T (never the
         signal bar). Fills at the bar open when open <= limit (paying half
         the spread + slippage, capped at the limit), otherwise at the limit
         if the bar trades down to it.
Exits  : hard stop (stop-market): gap below the stop fills at the OPEN
         (worse); otherwise at the stop. Target 1 (limit): gap above fills at
         the open (a resting limit sell fills at the better price); otherwise
         exactly at Target 1. Time / dataset-end exits at the bar close.
Same bar: stop and Target 1 both touched and the order cannot be resolved
         from finer bars -> STOP (conservative). On the entry bar of a limit
         fill the bar's high may precede the fill, so Target 1 is ignored and
         the stop is checked conservatively.
Costs  : IBKR Fixed commission (per share, min, max % of value) per side;
         half-spread and slippage in basis points; tracked separately.
"""

from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from backtest.models import (EXIT_END, EXIT_STOP, EXIT_STOP_AMBIGUOUS, EXIT_STOP_GAP,
                             EXIT_TARGET, EXIT_TARGET_GAP, EXIT_TIME, Fill)
from market_data.sessions import bar_end

CENT = Decimal("0.01")
BPS = Decimal("10000")
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
def commission_for(qty, price, cm):
    """IBKR Fixed: per-share rate, minimum per order, capped at % of value."""
    if qty <= 0:
        return ZERO
    c = max(cm.commission_per_share * qty, cm.commission_min)
    c = min(c, cm.commission_max_pct * qty * price)
    return c.quantize(CENT, rounding=ROUND_HALF_UP)


def half_spread(price, cm):
    return max(price * cm.spread_bps / BPS / 2, cm.min_half_spread)


def slippage(price, cm):
    return price * cm.slippage_bps / BPS


# ---------------------------------------------------------------------------
# Compact bar storage (memory: ~48 bytes per bar)
# ---------------------------------------------------------------------------
SCALE = 10000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SimBar:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class CompactBars:
    """Chronological OHLC bars stored as integer arrays (price x 1e-4)."""

    def __init__(self, bars, bar_seconds, tz=None):
        self.bar_seconds = bar_seconds
        self.ts = array("q")
        self.ends = array("q")      # clock-aligned end (short 09:30 1h bar ends 10:00)
        self.o, self.h, self.l, self.c = array("q"), array("q"), array("q"), array("q")
        for b in bars:
            self.ts.append(int(b.timestamp.timestamp()))
            self.ends.append(int(bar_end(b.timestamp, bar_seconds, tz).timestamp()))
            for arr, v in ((self.o, b.open), (self.h, b.high), (self.l, b.low), (self.c, b.close)):
                arr.append(int((v * SCALE).to_integral_value(rounding=ROUND_HALF_UP)))

    def __len__(self):
        return len(self.ts)

    def bar(self, i):
        start = EPOCH + timedelta(seconds=self.ts[i])
        p = lambda a: Decimal(a[i]) / SCALE
        return SimBar(start, EPOCH + timedelta(seconds=self.ends[i]),
                      p(self.o), p(self.h), p(self.l), p(self.c))

    def end_epoch(self, i):
        return self.ends[i]

    def first_index_at_or_after(self, ts):
        return bisect_left(self.ts, int(ts.timestamp()))


# ---------------------------------------------------------------------------
# Orders and positions
# ---------------------------------------------------------------------------
@dataclass
class EntryOrder:
    symbol: str
    signal: object               # backtest.models.Signal
    qty: int
    limit: Decimal
    submitted: datetime          # decision time T
    expires: datetime            # next decision time


@dataclass
class StopLimitOrder(EntryOrder):
    """Buy-stop-limit: inactive until price trades at/above `trigger`, then a
    buy limit at `limit` (maximum entry). Never fills before the trigger."""
    trigger: Decimal = ZERO
    triggered: bool = False


@dataclass
class OpenPosition:
    symbol: str
    signal: object
    qty: int
    fill: Fill
    stop: Decimal
    target_1: Decimal
    target_2: Decimal | None
    entry_bar_start: datetime
    sessions: set = field(default_factory=set)
    max_high: Decimal = ZERO
    min_low: Decimal = ZERO
    t2_touched: bool = False

    @property
    def cost_basis(self):
        return self.fill.price * self.qty + self.fill.commission


def try_fill_entry(order, bar, cm):
    """Fill for a buy limit at order.limit on `bar`, or None."""
    if bar.start < order.submitted or bar.start >= order.expires:
        return None
    lim, q = order.limit, order.qty
    if bar.open <= lim:
        hs, sl = half_spread(bar.open, cm), slippage(bar.open, cm)
        px = min(bar.open + hs + sl, lim)
        spread_part = min(hs, px - bar.open)
        return Fill(bar.start, px, bar.open, spread_part * q, (px - bar.open - spread_part) * q,
                    commission_for(q, px, cm), "OPEN")
    if bar.low <= lim:
        return Fill(bar.start, lim, lim, ZERO, ZERO, commission_for(q, lim, cm), "LIMIT")
    return None


def try_fill_stop_limit(order, bar, cm):
    """Fill for a buy-stop-limit on `bar`, or None. Conservative intrabar rules:
    a trigger crossed inside the bar fills AT the trigger (plus costs, capped
    at the limit) and the bar's earlier prices are unknown (mode TRIGGER)."""
    if bar.start < order.submitted or bar.start >= order.expires:
        return None
    if order.triggered:                       # already a resting limit
        return try_fill_entry(order, bar, cm)
    trig, lim, q = order.trigger, order.limit, order.qty
    if bar.open >= trig:                      # gap / open at or through the trigger
        order.triggered = True
        if bar.open <= lim:
            return try_fill_entry(order, bar, cm)            # fills at the open
        if bar.low <= lim:                    # opened above the limit, traded back down
            return Fill(bar.start, lim, lim, ZERO, ZERO, commission_for(q, lim, cm), "LIMIT")
        return None
    if bar.high >= trig:                      # triggered inside the bar
        order.triggered = True
        hs, sl = half_spread(trig, cm), slippage(trig, cm)
        px = min(trig + hs + sl, lim)
        spread_part = min(hs, px - trig)
        return Fill(bar.start, px, trig, spread_part * q, (px - trig - spread_part) * q,
                    commission_for(q, px, cm), "TRIGGER")
    return None


def fill_entry(order, bar, cm):
    """Dispatch on order type."""
    if isinstance(order, StopLimitOrder):
        return try_fill_stop_limit(order, bar, cm)
    return try_fill_entry(order, bar, cm)


def open_position(order, fill, bar, session_date):
    s = order.signal
    pos = OpenPosition(order.symbol, s, order.qty, fill, s.stop, s.target_1, s.target_2,
                       bar.start, {session_date})
    if fill.mode == "OPEN":
        pos.max_high, pos.min_low = bar.high, bar.low
    else:  # limit / trigger fill inside the bar: highs before the fill must not count
        pos.max_high, pos.min_low = max(fill.price, bar.close), bar.low
    return pos


def _sell_fill(bar_ts, ref, qty, cm, market):
    """Market-style sell: pays half spread + slippage. Limit sell: no costs."""
    if market:
        hs, sl = half_spread(ref, cm), slippage(ref, cm)
        px = ref - hs - sl
        return Fill(bar_ts, px, ref, hs * qty, sl * qty, commission_for(qty, px, cm), "MARKET")
    return Fill(bar_ts, ref, ref, ZERO, ZERO, commission_for(qty, ref, cm), "LIMIT")


def check_exit(pos, bar, cm, entry_bar=False, finer_bars=None):
    """(Fill, reason, ambiguous, gap) when the position exits on `bar`."""
    q, stop, t1 = pos.qty, pos.stop, pos.target_1
    limit_entry_bar = entry_bar and pos.fill.mode in ("LIMIT", "TRIGGER")
    if entry_bar and pos.fill.mode == "OPEN" and bar.open <= stop:
        # filled at an open that was already through the stop: stopped at once
        return _sell_fill(bar.end, bar.open, q, cm, True), EXIT_STOP_GAP, False, True
    if not entry_bar:
        if bar.open <= stop:
            return _sell_fill(bar.end, bar.open, q, cm, True), EXIT_STOP_GAP, False, True
        if bar.open >= t1:
            hs = half_spread(bar.open, cm)
            px = max(bar.open - hs, t1)
            f = Fill(bar.end, px, bar.open, (bar.open - px) * q, ZERO,
                     commission_for(q, px, cm), "GAP")
            return f, EXIT_TARGET_GAP, False, True
    stop_hit = bar.low <= stop
    target_hit = (not limit_entry_bar) and bar.high >= t1
    if stop_hit and target_hit:
        first = resolve_with_finer(finer_bars, stop, t1) if finer_bars else None
        if first == "TARGET":
            return _sell_fill(bar.end, t1, q, cm, False), EXIT_TARGET, False, False
        return _sell_fill(bar.end, stop, q, cm, True), EXIT_STOP_AMBIGUOUS, True, False
    if stop_hit:
        return _sell_fill(bar.end, stop, q, cm, True), EXIT_STOP, False, False
    if target_hit:
        return _sell_fill(bar.end, t1, q, cm, False), EXIT_TARGET, False, False
    return None


def resolve_with_finer(finer_bars, stop, t1):
    """Replay finer bars inside an ambiguous bar: which level was hit first?
    Returns "STOP", "TARGET" or None (still ambiguous -> caller uses STOP)."""
    for b in finer_bars:
        s, t = b.low <= stop, b.high >= t1
        if s and t:
            return None
        if s:
            return "STOP"
        if t:
            return "TARGET"
    return None


def close_at(pos, bar, cm, reason):
    """Exit at the bar close (time exit / dataset end)."""
    return _sell_fill(bar.end, bar.close, pos.qty, cm, True), reason


def update_excursions(pos, bar):
    pos.max_high = max(pos.max_high, bar.high)
    pos.min_low = min(pos.min_low, bar.low)
    if pos.target_2 is not None and bar.high >= pos.target_2:
        pos.t2_touched = True


def time_exit_due(pos, sessions_required, bar, session):
    """True at the close of the Nth session held (entry session counts as 1)."""
    return (sessions_required is not None and session is not None
            and bar.end >= session.end and len(pos.sessions) >= sessions_required)


__all__ = ["commission_for", "half_spread", "slippage", "CompactBars", "SimBar",
           "EntryOrder", "OpenPosition", "try_fill_entry", "open_position", "check_exit",
           "resolve_with_finer", "close_at", "update_excursions", "time_exit_due",
           "EXIT_END", "EXIT_TIME"]
