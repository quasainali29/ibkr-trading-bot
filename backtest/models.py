"""
Backtest data model. SIMULATION ONLY - nothing here can place an order.

Two kinds of trades are produced and NEVER mixed:
  REAL    - live-policy portfolio trades ($800, competition for cash, risk
            engine approval, 15% circuit breaker). Tier 1 only.
  SHADOW  - research trades for every evaluated setup with a valid entry /
            stop / Target 1, whatever the proposed action. They never touch
            portfolio cash or equity. Labelled PRE_BREAKER /
            POST_CIRCUIT_BREAKER relative to the run's breaker time.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

REAL, SHADOW = "REAL", "SHADOW"
PRE_BREAKER, POST_BREAKER = "PRE_BREAKER", "POST_CIRCUIT_BREAKER"

# Exit reasons
EXIT_STOP = "STOP"
EXIT_STOP_GAP = "STOP_GAP"
EXIT_STOP_AMBIGUOUS = "AMBIGUOUS_BAR_STOP"
EXIT_TARGET = "TARGET_1"
EXIT_TARGET_GAP = "TARGET_1_GAP"
EXIT_TIME = "TIME_EXIT"
EXIT_END = "END_OF_DATA"


@dataclass(frozen=True)
class CostModel:
    commission_per_share: Decimal
    commission_min: Decimal
    commission_max_pct: Decimal
    slippage_bps: Decimal
    spread_bps: Decimal
    min_half_spread: Decimal

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.BT_COMMISSION_PER_SHARE, cfg.BT_COMMISSION_MIN,
                   cfg.BT_COMMISSION_MAX_PCT, cfg.BT_SLIPPAGE_BPS, cfg.BT_SPREAD_BPS,
                   cfg.BT_MIN_HALF_SPREAD)


@dataclass(frozen=True)
class RunSpec:
    tier: int
    cadence: str                  # "15m" or "1h"
    variant: str                  # "A" (no time exit) or "B" (time exit)
    portfolio: bool               # live-policy portfolio simulated (Tier 1)


@dataclass
class Signal:
    """Pass-A result for one symbol at one decision time T (portfolio-
    independent: produced by the unchanged proposal engine with a fresh $800
    ledger and SAFE state)."""
    symbol: str
    ts: datetime
    action: str                   # final action with the neutral portfolio
    pre_risk_buy: bool            # proposal reached the risk engine
    mode: str
    environment: str
    score: int | None
    band: str
    setup_type: str
    horizon: str
    confidence: str
    regime: str
    price: Decimal | None
    entry: Decimal | None
    maximum_entry: Decimal | None
    stop: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    reward_risk: Decimal | None
    no_trade_codes: tuple = ()
    blocker_codes: tuple = ()
    sector: str = ""
    md_check: object = None       # exact MarketDataCheck, kept only for pre-risk BUYs
    price_state: str = ""         # IN_RANGE / WAIT_FOR_PULLBACK / AWAITING_TRIGGER / UNKNOWN
    entry_zone_low: Decimal | None = None   # trigger level for AWAITING_TRIGGER setups
    setup_anchor: tuple | None = None       # (kind, low, high): setup-instance identity

    @property
    def has_trade_plan(self):
        return (self.maximum_entry is not None and self.stop is not None
                and self.target_1 is not None and self.stop < self.maximum_entry)


@dataclass
class Fill:
    ts: datetime
    price: Decimal                # executed price (after spread / slippage)
    ref_price: Decimal            # reference price before costs
    spread_cost: Decimal
    slippage_cost: Decimal
    commission: Decimal
    mode: str                     # OPEN / LIMIT / STOP / GAP / CLOSE


@dataclass
class TradeRecord:
    kind: str                     # REAL / SHADOW
    tier: int
    cadence: str
    variant: str
    ticker: str
    signal_ts: datetime
    entry_ts: datetime
    setup_type: str
    horizon: str
    score: int | None
    score_band: str
    confidence: str
    market_regime: str
    sector: str
    mode: str
    action: str
    entry: Decimal
    planned_entry: Decimal
    quantity: int
    position_value: Decimal
    stop: Decimal
    target_1: Decimal
    target_2: Decimal | None
    initial_risk: Decimal          # PLANNED risk used for sizing: (maximum_entry - stop) x qty
    gross_pnl: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    spread_cost: Decimal = Decimal(0)
    slippage_cost: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)   # net
    return_pct: Decimal = Decimal(0)
    r_multiple: Decimal = Decimal(0)     # net P/L / initial risk
    gross_r: Decimal = Decimal(0)        # per-share move / per-share risk (no costs)
    mfe: Decimal = Decimal(0)            # max favorable excursion, $ per share
    mae: Decimal = Decimal(0)            # max adverse excursion, $ per share
    mfe_r: Decimal = Decimal(0)
    mae_r: Decimal = Decimal(0)
    exit_ts: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""
    holding_sessions: int = 0
    holding_hours: Decimal = Decimal(0)
    t2_touched: bool = False
    ambiguous_bar: bool = False
    gap_fill: bool = False
    label: str = PRE_BREAKER
    no_trade_codes: tuple = ()
    blocker_codes: tuple = ()
    unaffordable: bool = False           # shadow: whole-share sizing impossible
    fill_risk: Decimal = Decimal(0)      # diagnostic: (fill price - stop) x quantity
    fill_r_multiple: Decimal = Decimal(0)  # diagnostic: net P/L / fill_risk
    risk_invalid: bool = False           # planned risk <= 0 (R reported as 0)
    entry_order_type: str = "LIMIT"      # LIMIT or STOP_LIMIT
    setup_instance: str = ""             # shadow: unique setup instance this trade belongs to

    @property
    def win(self):
        return self.realized_pnl > 0

    @property
    def total_costs(self):
        return self.commission + self.spread_cost + self.slippage_cost


@dataclass
class EquityPoint:
    ts: datetime
    cash: Decimal
    equity: Decimal
    peak: Decimal
    drawdown_pct: Decimal
    open_positions: int


@dataclass
class RunResult:
    spec: RunSpec
    symbols: tuple
    start: date
    end: date
    starting_capital: Decimal
    trades: list = field(default_factory=list)          # REAL
    shadow: list = field(default_factory=list)          # SHADOW
    equity: list = field(default_factory=list)
    breaker_ts: datetime | None = None
    cancelled_entries: int = 0
    candidates: int = 0
    rejected_by_risk: int = 0
    skipped_in_position: int = 0
    decision_times: int = 0
    notes: list = field(default_factory=list)
    exposure_pct: Decimal = Decimal(0)
    risk_rejection_reasons: dict = field(default_factory=dict)
    signal_counts: dict = field(default_factory=dict)
    shadow_dedupe: dict = field(default_factory=dict)   # setup-instance accounting (shadow)
