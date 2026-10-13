"""
Risk engine: hard, deterministic rejection rules and position sizing.

The engine is pure: no IBKR calls, no I/O. It evaluates a proposed LONG
entry against the BOT strategy ledger (never the IBKR account balance) and
the system safety state. Every rule is evaluated so a rejection lists all
reasons, not just the first.

Position sizing (fixed - no caller, strategy or AI component can override):

    risk_dollars   = bot_strategy_equity * MAX_RISK_PER_TRADE
    risk_per_share = abs(entry_price - stop_price)
    max_quantity   = floor(risk_dollars / risk_per_share)

additionally capped at floor(bot_available_capital / entry_price) so a trade
can never require margin. The engine may only reject or report a smaller
maximum; it never increases a requested quantity.

Market data (Phase 2): every proposal must carry a recent MarketDataCheck
for the same symbol that is valid, not stale, and LIVE. Delayed or frozen
data never authorizes a trade. A missing or failed check rejects only that
proposal - per-ticker data problems do not cause global SAFE_MODE.

Phase 3: every request must be explicitly marked EXECUTION_READY by the
proposal engine; anything else (including ANALYSIS_ONLY or unmarked
requests) is rejected with NOT_EXECUTION_READY. The engine's veto is final.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from environment import Environment
from money import MoneyError, floor_int, to_decimal
from risk.instrument_policy import check_instrument
from risk.system_state import SystemState


class Reason(str, Enum):
    SAFE_MODE_ACTIVE = "SAFE_MODE_ACTIVE"
    LIVE_TRADING_BLOCKED = "LIVE_TRADING_BLOCKED"
    CONFIG_UNSAFE = "CONFIG_UNSAFE"
    PROHIBITED_SECURITY = "PROHIBITED_SECURITY"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    INVALID_ENTRY = "INVALID_ENTRY"
    INVALID_STOP = "INVALID_STOP"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    SHORT_NOT_ALLOWED = "SHORT_NOT_ALLOWED"
    MARGIN_REQUIRED = "MARGIN_REQUIRED"
    EXCEEDS_BOT_CAPITAL = "EXCEEDS_BOT_CAPITAL"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    QUANTITY_EXCEEDS_MAX = "QUANTITY_EXCEEDS_MAX"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    INVALID_LEDGER = "INVALID_LEDGER"
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    MARKET_DATA_INVALID = "MARKET_DATA_INVALID"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_DELAYED = "MARKET_DATA_DELAYED"
    NOT_EXECUTION_READY = "NOT_EXECUTION_READY"
    MARKET_DATA_SIMULATED = "MARKET_DATA_SIMULATED"


EXECUTION_READY = "EXECUTION_READY"


@dataclass(frozen=True)
class RiskRequest:
    """A concrete long entry submitted to the risk engine for authorization.
    (Not the advisory strategy.models.TradeProposal.)"""
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    primary_exchange: str
    side: str                 # "BUY" (long entry). Anything else is rejected.
    quantity: object          # int expected; validated strictly
    entry_price: object       # Decimal/str/float; converted via str()
    stop_price: object        # required protective stop
    use_margin: bool = False  # any request for margin/leverage is rejected
    market_data: object = None  # market_data.validation.MarketDataCheck
    analysis_mode: str | None = None  # must be "EXECUTION_READY" or the request is rejected


# Backward-compatible name used by Phase 1/2 code and tests.
TradeProposal = RiskRequest


@dataclass(frozen=True)
class Rejection:
    reason: Reason
    message: str


@dataclass
class RiskDecision:
    approved: bool
    rejections: list = field(default_factory=list)
    risk_budget: Decimal | None = None       # max $ at risk for this trade
    risk_per_share: Decimal | None = None
    planned_risk: Decimal | None = None      # quantity * risk_per_share
    notional: Decimal | None = None          # quantity * entry_price
    max_quantity_by_risk: int | None = None
    max_quantity_by_capital: int | None = None
    max_quantity: int | None = None

    @property
    def reasons(self):
        return [r.reason for r in self.rejections]

    def to_dict(self):
        return {
            "approved": self.approved,
            "rejections": [{"reason": r.reason.value, "message": r.message}
                           for r in self.rejections],
            "risk_budget": _s(self.risk_budget),
            "risk_per_share": _s(self.risk_per_share),
            "planned_risk": _s(self.planned_risk),
            "notional": _s(self.notional),
            "max_quantity_by_risk": self.max_quantity_by_risk,
            "max_quantity_by_capital": self.max_quantity_by_capital,
            "max_quantity": self.max_quantity,
        }


def _s(d):
    return None if d is None else str(d)


def calculate_max_quantity(bot_strategy_equity, bot_available_capital,
                           entry_price, stop_price, max_risk_fraction):
    """Fixed sizing formula. Returns (risk_dollars, risk_per_share,
    qty_by_risk, qty_by_capital, max_quantity). All inputs are Decimals."""
    risk_dollars = bot_strategy_equity * max_risk_fraction
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0 or entry_price <= 0 or risk_dollars <= 0:
        return risk_dollars, risk_per_share, 0, 0, 0

    qty_risk = max(floor_int(risk_dollars / risk_per_share), 0)
    # Guard against Decimal context rounding at the integer boundary.
    while qty_risk > 0 and qty_risk * risk_per_share > risk_dollars:
        qty_risk -= 1

    qty_cap = max(floor_int(bot_available_capital / entry_price), 0)
    while qty_cap > 0 and qty_cap * entry_price > bot_available_capital:
        qty_cap -= 1

    return risk_dollars, risk_per_share, qty_risk, qty_cap, min(qty_risk, qty_cap)


class RiskEngine:
    def __init__(self, cfg, environment=Environment.LIVE):
        """environment defaults to LIVE: genuine IBKR LIVE quotes required.
        Only the backtester constructs Environment.BACKTEST."""
        self.cfg = cfg
        self.environment = Environment(environment)

    def evaluate(self, proposal, bot_snapshot, system_state, now=None):
        """Evaluate a proposed long entry.

        bot_snapshot: portfolio_manager.BotStrategySnapshot (BOT ledger only).
        system_state: risk.system_state.SystemState.
        now: aware UTC datetime (injectable for tests).
        """
        cfg = self.cfg
        now = now or datetime.now(timezone.utc)
        rej = []
        d = RiskDecision(approved=False, rejections=rej)

        def reject(reason, msg):
            rej.append(Rejection(reason, msg))

        # --- system / configuration gates ---------------------------------
        mode = getattr(getattr(proposal, "analysis_mode", None), "value",
                       getattr(proposal, "analysis_mode", None))
        if mode != EXECUTION_READY:
            reject(Reason.NOT_EXECUTION_READY,
                   f"request is {mode or 'not marked'}; only EXECUTION_READY "
                   f"requests can be authorized")
        if system_state is not SystemState.SAFE:
            state_name = getattr(system_state, "value", system_state)
            reject(Reason.SAFE_MODE_ACTIVE,
                   f"system state is {state_name}; trading not permitted")
        if cfg.TRADING_MODE != "PAPER" or cfg.LIVE_TRADING_ALLOWED is not False:
            reject(Reason.LIVE_TRADING_BLOCKED, "only PAPER trading is permitted")
        if cfg.MARGIN_ALLOWED is not False or cfg.SHORT_SELLING_ALLOWED is not False:
            reject(Reason.CONFIG_UNSAFE, "margin/short selling must be disabled")

        # --- instrument -----------------------------------------------------
        symbol = proposal.symbol if isinstance(proposal.symbol, str) else ""
        if not symbol.strip():
            reject(Reason.INVALID_SYMBOL, "symbol is missing")
        for msg in check_instrument(cfg, proposal.sec_type, proposal.currency,
                                    proposal.exchange, proposal.primary_exchange):
            reject(Reason.PROHIBITED_SECURITY, msg)

        # --- market data ----------------------------------------------------
        self._check_market_data(proposal.market_data, symbol, now, reject)

        # --- direction ------------------------------------------------------
        side = str(proposal.side or "").upper()
        if side != "BUY":
            reject(Reason.SHORT_NOT_ALLOWED,
                   f"side {side or '<missing>'}: only long (BUY) entries are allowed")

        if proposal.use_margin is not False:
            reject(Reason.MARGIN_REQUIRED, "margin/leverage is not allowed")

        # --- numeric inputs -------------------------------------------------
        entry = stop = None
        try:
            entry = to_decimal(proposal.entry_price, "entry_price")
            if entry <= 0:
                raise MoneyError("entry_price must be positive")
        except MoneyError as exc:
            entry = None
            reject(Reason.INVALID_ENTRY, str(exc))
        try:
            stop = to_decimal(proposal.stop_price, "stop_price")
            if stop <= 0:
                raise MoneyError("stop_price must be positive")
        except MoneyError as exc:
            stop = None
            reject(Reason.INVALID_STOP, str(exc))
        if entry is not None and stop is not None and stop >= entry:
            stop = None
            reject(Reason.INVALID_STOP,
                   "stop_price must be below entry_price for a long trade")

        qty = proposal.quantity
        if isinstance(qty, Decimal) and qty.is_finite() and qty == qty.to_integral_value():
            qty = int(qty)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            reject(Reason.INVALID_QUANTITY,
                   f"quantity must be a positive whole number, got {proposal.quantity!r}")
            qty = None

        # --- BOT ledger state ---------------------------------------------
        snap = bot_snapshot
        if snap is None:
            reject(Reason.INVALID_LEDGER, "BOT strategy ledger unavailable")
            return d
        equity = snap.bot_strategy_equity
        available = snap.bot_available_capital
        if equity <= 0 or available < 0:
            reject(Reason.INVALID_LEDGER, "BOT strategy equity/capital invalid")
            return d

        if snap.bot_drawdown_pct >= cfg.MAX_PORTFOLIO_DRAWDOWN:
            reject(Reason.DRAWDOWN_HALT,
                   f"BOT drawdown {snap.bot_drawdown_pct:.2%} reached the "
                   f"{cfg.MAX_PORTFOLIO_DRAWDOWN:.0%} limit")

        # --- sizing ---------------------------------------------------------
        if entry is None or stop is None:
            return d
        (d.risk_budget, d.risk_per_share, d.max_quantity_by_risk,
         d.max_quantity_by_capital, d.max_quantity) = calculate_max_quantity(
            equity, available, entry, stop, cfg.MAX_RISK_PER_TRADE)

        if qty is not None:
            d.planned_risk = qty * d.risk_per_share
            d.notional = qty * entry
            # Exactly 2.00% is permitted; only strictly greater is rejected.
            if d.planned_risk > d.risk_budget:
                reject(Reason.RISK_TOO_HIGH,
                       f"planned risk ${d.planned_risk} exceeds "
                       f"{cfg.MAX_RISK_PER_TRADE:.0%} of BOT equity (${d.risk_budget})")
            if d.notional > available:
                reject(Reason.EXCEEDS_BOT_CAPITAL,
                       f"cost ${d.notional} exceeds BOT available capital "
                       f"${available} (would require margin)")
            if qty > d.max_quantity:
                reject(Reason.QUANTITY_EXCEEDS_MAX,
                       f"quantity {qty} exceeds max allowed {d.max_quantity}")

        d.approved = not rej
        return d

    def _check_market_data(self, md, symbol, now, reject):
        cfg = self.cfg
        if md is None:
            reject(Reason.MARKET_DATA_UNAVAILABLE,
                   f"no validated market data supplied for {symbol or '<missing>'}")
            return
        found = []

        def add(reason, msg):
            found.append(reason)
            reject(reason, msg)

        # Fail closed: any missing attribute counts as a failure.
        hist = getattr(md, "historical", None)
        quote = getattr(md, "quote", None)

        if str(getattr(md, "symbol", "")).upper() != symbol.strip().upper():
            add(Reason.MARKET_DATA_INVALID,
                f"market data is for {getattr(md, 'symbol', None)!r}, not {symbol!r}")
        if not getattr(md, "contract_ok", False):
            add(Reason.MARKET_DATA_INVALID, "contract failed validation")
        if hist is None or not hist.valid:
            add(Reason.MARKET_DATA_INVALID, "historical candles invalid/unavailable: "
                + "; ".join(getattr(hist, "errors", None) or ["missing"]))
        elif hist.stale:
            add(Reason.MARKET_DATA_STALE, "historical candles are stale")
        if quote is None or not quote.available:
            add(Reason.MARKET_DATA_UNAVAILABLE, "live quote unavailable")
        else:
            if not quote.valid:
                add(Reason.MARKET_DATA_INVALID, "quote invalid: " + "; ".join(quote.errors))
            if quote.stale:
                add(Reason.MARKET_DATA_STALE, "quote is stale")
            dt = quote.data_type
            simulated = bool(getattr(quote, "simulated", False)) or (
                dt is not None and dt.is_simulated)
            if simulated and not (self.environment is Environment.BACKTEST
                                  and dt is not None and dt.is_simulated):
                add(Reason.MARKET_DATA_SIMULATED,
                    "simulated quote cannot authorize a real trade "
                    "(genuine IBKR LIVE data required)")
            elif dt is None:
                add(Reason.MARKET_DATA_UNAVAILABLE, "quote data type not reported by IBKR")
            elif dt.is_delayed and not cfg.ALLOW_DELAYED_DATA_FOR_TRADING:
                add(Reason.MARKET_DATA_DELAYED, f"{dt.label} data cannot authorize a trade")
            elif dt.is_frozen and not cfg.ALLOW_FROZEN_DATA_FOR_TRADING:
                add(Reason.MARKET_DATA_DELAYED, f"{dt.label} data cannot authorize a trade")
        checked_at = getattr(md, "checked_at", None)
        if checked_at is None or (now - checked_at).total_seconds() > cfg.MARKET_DATA_CHECK_MAX_AGE_SECONDS:
            add(Reason.MARKET_DATA_STALE,
                f"market data check is older than {cfg.MARKET_DATA_CHECK_MAX_AGE_SECONDS}s")
        if not getattr(md, "usable_for_trading", False) and not found:
            add(Reason.MARKET_DATA_UNAVAILABLE,
                "market data not usable for trading: "
                + "; ".join(getattr(md, "trading_blockers", []) or ["unspecified"]))
