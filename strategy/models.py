"""
Phase 3 strategy models. A TradeProposal is ADVISORY ONLY: it carries no
authority until the deterministic risk engine approves it, and the risk
engine's veto is final. Long-only.
"""

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum


class AnalysisMode(str, Enum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    EXECUTION_READY = "EXECUTION_READY"


class ProposedAction(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"


class SetupType(str, Enum):
    BREAKOUT = "BREAKOUT"
    PULLBACK_TO_SUPPORT = "PULLBACK_TO_SUPPORT"
    TREND_CONTINUATION = "TREND_CONTINUATION"
    REVERSAL_FROM_SUPPORT = "REVERSAL_FROM_SUPPORT"
    MEAN_REVERSION = "MEAN_REVERSION"
    NONE = "NONE"


class SetupClass(str, Enum):
    NO_TRADE = "NO TRADE"
    WATCH = "WATCH"
    CONDITIONAL_SETUP = "CONDITIONAL SETUP"
    STRONG_SETUP = "STRONG SETUP"
    EXCEPTIONAL_SETUP = "EXCEPTIONAL SETUP"


class PriceState(str, Enum):
    IN_RANGE = "IN_RANGE"                    # entry zone low <= price <= maximum entry
    WAIT_FOR_PULLBACK = "WAIT_FOR_PULLBACK"  # price above maximum entry: do not chase
    AWAITING_TRIGGER = "AWAITING_TRIGGER"    # price below the entry trigger
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TimeframePolicy:
    horizon: str
    required: tuple
    preferred: tuple
    optional: tuple

    @property
    def all(self):
        return self.required + self.preferred + self.optional


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass
class Setup:
    setup_type: SetupType
    horizon: str
    trigger_timeframe: str
    entry: Decimal
    entry_zone_low: Decimal          # lowest acceptable price for IN_RANGE
    maximum_entry: Decimal
    reference_zone: object = None    # Zone the setup is built on
    signal_bar: object = None
    facts: list = field(default_factory=list)


@dataclass
class StopPlan:
    stop: Decimal | None
    stop_type: str
    invalidation: str
    distance: Decimal | None = None
    distance_atr: Decimal | None = None
    adjusted: bool = False
    valid: bool = True
    problem: str = ""


@dataclass
class TargetPlan:
    target_1: Decimal | None
    target_1_source: str
    target_2: Decimal | None
    target_2_source: str
    reward_risk: Decimal | None


@dataclass
class ScoreComponent:
    name: str
    points: Decimal
    max_points: int
    reasons: list = field(default_factory=list)
    missing_required: list = field(default_factory=list)
    missing_optional: list = field(default_factory=list)


@dataclass
class ScoreBreakdown:
    components: list

    @property
    def total(self):
        return int(sum((c.points for c in self.components), Decimal(0))
                   .quantize(Decimal(1), rounding="ROUND_HALF_UP"))

    def get(self, name):
        return next(c for c in self.components if c.name == name)


@dataclass
class TradeProposal:
    """Advisory trade proposal (long-only). Never an order."""
    ticker: str
    analysis_mode: AnalysisMode
    strategy_type: SetupType
    direction: str = "LONG"
    entry: Decimal | None = None
    maximum_entry: Decimal | None = None
    stop: Decimal | None = None
    stop_type: str = ""
    target_1: Decimal | None = None
    target_2: Decimal | None = None
    expected_holding_period: str = ""
    technical_thesis: str = ""
    market_structure: str = ""
    candle_context: str = ""
    volume_context: str = ""
    relative_strength: str = ""
    market_regime: str = ""
    risk_factors: list = field(default_factory=list)
    invalidation: str = ""
    confidence: str = "LOW"
    opportunity_score: int | None = None
    reward_risk: Decimal | None = None
    market_data_check: object = None
    execution_eligible: bool = False
    # --- engine bookkeeping ---
    proposed_action: ProposedAction = ProposedAction.NO_TRADE
    setup_classification: SetupClass = SetupClass.NO_TRADE
    price_state: PriceState = PriceState.UNKNOWN
    no_trade_reasons: list = field(default_factory=list)      # analytical
    execution_blockers: list = field(default_factory=list)    # force ANALYSIS_ONLY
    score_breakdown: ScoreBreakdown | None = None
    timeframe_policy: TimeframePolicy | None = None
    missing_optional: list = field(default_factory=list)
    target_1_source: str = ""
    target_2_source: str = ""
    risk_status: str = "NOT EVALUATED"                        # APPROVED / REJECTED / NOT EVALUATED
    risk_decision: object = None
    risk_quantity: int | None = None
    affordability_note: str = ""
    notes: list = field(default_factory=list)
    environment: str = "LIVE"                                 # LIVE or BACKTEST
    entry_zone_low: Decimal | None = None   # lowest IN_RANGE price (= trigger for breakouts)
    setup_anchor: tuple | None = None       # informational: (kind, low, high) of the setup structure

    def to_dict(self):
        d = asdict(self)
        d["market_data_check"] = None
        d["risk_decision"] = self.risk_decision.to_dict() if self.risk_decision else None
        return d
