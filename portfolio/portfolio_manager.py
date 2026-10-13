"""
BOT strategy ledger.

The bot's capital is tracked in an internal ledger that starts at
config.BOT_CAPITAL ($800). It is completely separate from the IBKR ACCOUNT
(net liquidation, cash, buying power), which is never used for sizing.

Ownership model
---------------
* Every order the bot creates (future phases) carries an IBKR orderRef
  starting with config.BOT_ORDER_REF_PREFIX. Until it fills it is recorded
  in `pending_orders`.
* Filled bot orders become `positions`, keyed by IBKR conId.
* Any IBKR position/order NOT recorded here is EXTERNAL (manual or
  pre-existing) and is never counted in BOT strategy equity.

Ledger file integrity
---------------------
* Writes are atomic: temp file in the same directory -> fsync -> os.replace.
* Every load is validated (schema, types, finite Decimals, accounting
  identity, SHA-256 checksum). Any failure raises LedgerError; the caller
  enters SAFE_MODE. The ledger is never silently reset.

Accounting identity (always holds for a valid ledger):
    cash + sum(quantity * avg_cost) == initial_capital + realized_pnl
where avg_cost includes buy commissions and realized_pnl is net of fees.
"""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from money import ZERO, MoneyError, to_decimal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class LedgerError(Exception):
    """Ledger file is missing required data, corrupt, or inconsistent."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class BotPosition:
    con_id: int
    symbol: str
    quantity: int
    avg_cost: Decimal           # per share, including buy commission
    stop_price: Decimal
    entry_order_ref: str
    opened_at: str

    @property
    def cost_basis(self):
        return self.avg_cost * self.quantity


@dataclass
class BotPendingOrder:
    order_ref: str
    con_id: int
    symbol: str
    action: str
    quantity: int
    created_at: str


@dataclass
class BotLedger:
    account_id: str
    initial_capital: Decimal
    cash: Decimal
    realized_pnl: Decimal
    peak_equity: Decimal
    created_at: str
    updated_at: str
    positions: list = field(default_factory=list)
    pending_orders: list = field(default_factory=list)

    def bot_con_ids(self):
        return {p.con_id for p in self.positions}

    def bot_order_refs(self):
        return {o.order_ref for o in self.pending_orders}


@dataclass(frozen=True)
class BotStrategySnapshot:
    """Read-only view of BOT strategy capital used by the risk engine."""
    bot_initial_capital: Decimal
    bot_cash: Decimal                     # = BOT available capital (no margin)
    bot_positions_value: Decimal
    bot_strategy_equity: Decimal
    bot_peak_equity: Decimal
    bot_drawdown_dollars: Decimal
    bot_drawdown_pct: Decimal
    bot_realized_pnl: Decimal
    max_risk_per_trade_dollars: Decimal
    max_drawdown_dollars: Decimal
    drawdown_halt_equity: Decimal
    marked_at_cost: bool

    @property
    def bot_available_capital(self):
        return self.bot_cash


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_ledger(account_id, initial_capital):
    ts = _now()
    return BotLedger(
        account_id=account_id,
        initial_capital=initial_capital,
        cash=initial_capital,
        realized_pnl=ZERO,
        peak_equity=initial_capital,
        created_at=ts,
        updated_at=ts,
    )


# ---------------------------------------------------------------------------
# Serialization + validation
# ---------------------------------------------------------------------------
def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _checksum(payload):
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def ledger_to_dict(ledger):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "account_id": ledger.account_id,
        "initial_capital": str(ledger.initial_capital),
        "cash": str(ledger.cash),
        "realized_pnl": str(ledger.realized_pnl),
        "peak_equity": str(ledger.peak_equity),
        "created_at": ledger.created_at,
        "updated_at": ledger.updated_at,
        "positions": [{
            "con_id": p.con_id, "symbol": p.symbol, "quantity": p.quantity,
            "avg_cost": str(p.avg_cost), "stop_price": str(p.stop_price),
            "entry_order_ref": p.entry_order_ref, "opened_at": p.opened_at,
        } for p in ledger.positions],
        "pending_orders": [{
            "order_ref": o.order_ref, "con_id": o.con_id, "symbol": o.symbol,
            "action": o.action, "quantity": o.quantity, "created_at": o.created_at,
        } for o in ledger.pending_orders],
    }
    payload["checksum"] = _checksum(payload)
    return payload


def _req(d, key, typ, where):
    if key not in d:
        raise LedgerError(f"{where}: missing '{key}'")
    v = d[key]
    if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
        raise LedgerError(f"{where}: '{key}' must be an integer")
    if typ is str and not isinstance(v, str):
        raise LedgerError(f"{where}: '{key}' must be a string")
    return v


def _dec(d, key, where):
    raw = _req(d, key, str, where)   # Decimals are always persisted as strings
    try:
        return to_decimal(raw, f"{where}.{key}")
    except MoneyError as exc:
        raise LedgerError(str(exc)) from None


def ledger_from_dict(payload, cfg):
    """Validate a parsed ledger payload and build a BotLedger."""
    if not isinstance(payload, dict):
        raise LedgerError("ledger root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(f"unsupported schema_version {payload.get('schema_version')!r}")
    stored = payload.get("checksum")
    if not isinstance(stored, str) or stored != _checksum(payload):
        raise LedgerError("checksum mismatch (file corrupt or edited by hand)")

    w = "ledger"
    account_id = _req(payload, "account_id", str, w)
    if not account_id:
        raise LedgerError("ledger: account_id is empty")
    initial = _dec(payload, "initial_capital", w)
    cash = _dec(payload, "cash", w)
    realized = _dec(payload, "realized_pnl", w)
    peak = _dec(payload, "peak_equity", w)
    created = _req(payload, "created_at", str, w)
    updated = _req(payload, "updated_at", str, w)

    if initial != cfg.BOT_CAPITAL:
        raise LedgerError(f"ledger initial_capital {initial} != config BOT_CAPITAL "
                          f"{cfg.BOT_CAPITAL}")
    if cash < 0:
        raise LedgerError("ledger cash is negative (margin is not allowed)")

    positions, seen_ids = [], set()
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, list):
        raise LedgerError("ledger: 'positions' must be a list")
    for i, rp in enumerate(raw_positions):
        pw = f"positions[{i}]"
        if not isinstance(rp, dict):
            raise LedgerError(f"{pw} must be an object")
        p = BotPosition(
            con_id=_req(rp, "con_id", int, pw),
            symbol=_req(rp, "symbol", str, pw),
            quantity=_req(rp, "quantity", int, pw),
            avg_cost=_dec(rp, "avg_cost", pw),
            stop_price=_dec(rp, "stop_price", pw),
            entry_order_ref=_req(rp, "entry_order_ref", str, pw),
            opened_at=_req(rp, "opened_at", str, pw),
        )
        if p.quantity <= 0:
            raise LedgerError(f"{pw}: quantity must be positive (long-only)")
        # A stop may later trail above avg_cost, so only positivity is enforced.
        if p.avg_cost <= 0 or p.stop_price <= 0:
            raise LedgerError(f"{pw}: avg_cost and stop_price must be positive")
        if p.con_id in seen_ids:
            raise LedgerError(f"{pw}: duplicate con_id {p.con_id}")
        if not p.entry_order_ref.startswith(cfg.BOT_ORDER_REF_PREFIX):
            raise LedgerError(f"{pw}: entry_order_ref lacks bot prefix")
        seen_ids.add(p.con_id)
        positions.append(p)

    pending, seen_refs = [], set()
    raw_pending = payload.get("pending_orders")
    if not isinstance(raw_pending, list):
        raise LedgerError("ledger: 'pending_orders' must be a list")
    for i, ro in enumerate(raw_pending):
        ow = f"pending_orders[{i}]"
        if not isinstance(ro, dict):
            raise LedgerError(f"{ow} must be an object")
        o = BotPendingOrder(
            order_ref=_req(ro, "order_ref", str, ow),
            con_id=_req(ro, "con_id", int, ow),
            symbol=_req(ro, "symbol", str, ow),
            action=_req(ro, "action", str, ow),
            quantity=_req(ro, "quantity", int, ow),
            created_at=_req(ro, "created_at", str, ow),
        )
        if not o.order_ref.startswith(cfg.BOT_ORDER_REF_PREFIX):
            raise LedgerError(f"{ow}: order_ref lacks bot prefix")
        if o.order_ref in seen_refs:
            raise LedgerError(f"{ow}: duplicate order_ref")
        if o.action not in ("BUY", "SELL") or o.quantity <= 0:
            raise LedgerError(f"{ow}: invalid action/quantity")
        seen_refs.add(o.order_ref)
        pending.append(o)

    cost_basis = sum((p.cost_basis for p in positions), ZERO)
    if cash + cost_basis != initial + realized:
        raise LedgerError(
            f"accounting identity broken: cash {cash} + cost basis {cost_basis} "
            f"!= initial {initial} + realized P/L {realized}")
    if peak < initial or peak < cash + cost_basis:
        raise LedgerError("peak_equity is below initial capital or current equity")

    return BotLedger(account_id, initial, cash, realized, peak, created, updated,
                     positions, pending)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
class LedgerStore:
    def __init__(self, path, cfg):
        self.path = Path(path)
        self.cfg = cfg

    def exists(self):
        return self.path.exists()

    def load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError(f"cannot read ledger {self.path}: {exc}") from None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger is not valid JSON: {exc}") from None
        return ledger_from_dict(payload, self.cfg)

    def save(self, ledger):
        """Atomically persist the ledger after validating what will be written."""
        ledger.updated_at = _now()
        payload = ledger_to_dict(ledger)
        ledger_from_dict(json.loads(_canonical(payload)), self.cfg)  # self-check

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        log.info("BOT ledger saved (%s)", self.path.name)


# ---------------------------------------------------------------------------
# Strategy accounting
# ---------------------------------------------------------------------------
def compute_snapshot(ledger, cfg, marks=None):
    """BOT strategy equity, drawdown (from PEAK equity) and risk budgets.

    `marks` maps con_id -> Decimal price. Phase 1 has no market data, so
    positions are marked at cost when no mark is supplied.
    """
    marks = marks or {}
    marked_at_cost = False
    positions_value = ZERO
    for p in ledger.positions:
        price = marks.get(p.con_id)
        if price is None:
            price, marked_at_cost = p.avg_cost, True
        positions_value += price * p.quantity

    equity = ledger.cash + positions_value
    peak = max(ledger.peak_equity, equity)
    dd_dollars = max(peak - equity, ZERO)
    dd_pct = (dd_dollars / peak) if peak > 0 else Decimal("1")
    return BotStrategySnapshot(
        bot_initial_capital=ledger.initial_capital,
        bot_cash=ledger.cash,
        bot_positions_value=positions_value,
        bot_strategy_equity=equity,
        bot_peak_equity=peak,
        bot_drawdown_dollars=dd_dollars,
        bot_drawdown_pct=dd_pct,
        bot_realized_pnl=ledger.realized_pnl,
        max_risk_per_trade_dollars=equity * cfg.MAX_RISK_PER_TRADE,
        max_drawdown_dollars=peak * cfg.MAX_PORTFOLIO_DRAWDOWN,
        drawdown_halt_equity=peak * (1 - cfg.MAX_PORTFOLIO_DRAWDOWN),
        marked_at_cost=marked_at_cost and bool(ledger.positions),
    )


def update_peak(ledger, snapshot):
    """Raise the stored high-water mark if equity made a new peak.
    Returns True if the ledger changed and should be saved."""
    if snapshot.bot_strategy_equity > ledger.peak_equity:
        ledger.peak_equity = snapshot.bot_strategy_equity
        return True
    return False


# ---------------------------------------------------------------------------
# Startup reconciliation: BOT ledger vs IBKR ACCOUNT
# ---------------------------------------------------------------------------
@dataclass
class ReconciliationResult:
    mismatches: list = field(default_factory=list)
    bot_positions: list = field(default_factory=list)       # IbkrPosition owned by bot
    external_positions: list = field(default_factory=list)  # manual / pre-existing
    bot_orders: list = field(default_factory=list)
    external_orders: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.mismatches


def reconcile(ledger, account_id, ibkr_positions, ibkr_orders, cfg):
    """Compare the BOT ledger with what IBKR reports.

    Rules (any violation is an unexplained mismatch -> SAFE_MODE):
      1. The ledger must belong to the verified IBKR account.
      2. Each bot position must exist at IBKR with EXACTLY the same quantity.
         Extra manual shares in a bot-held contract make ownership ambiguous.
      3. Each IBKR open order tagged with the bot prefix must be in the ledger.
      4. Each pending bot order in the ledger must still be open at IBKR
         (a vanished order may have filled while the bot was offline).
    IBKR positions/orders with no bot ownership are EXTERNAL and allowed.
    """
    res = ReconciliationResult()
    prefix = cfg.BOT_ORDER_REF_PREFIX

    if ledger.account_id != account_id:
        res.mismatches.append(
            f"ledger belongs to account {ledger.account_id!r}, "
            f"connected account is {account_id!r}")

    acct_positions = [p for p in ibkr_positions if p.account == account_id]
    by_con = {}
    for p in acct_positions:
        by_con[p.con_id] = by_con.get(p.con_id, ZERO) + p.quantity

    bot_ids = ledger.bot_con_ids()
    for bp in ledger.positions:
        ib_qty = by_con.get(bp.con_id, ZERO)
        if ib_qty != bp.quantity:
            res.mismatches.append(
                f"bot position {bp.symbol} (conId {bp.con_id}): ledger qty "
                f"{bp.quantity}, IBKR qty {ib_qty}")
    for p in acct_positions:
        (res.bot_positions if p.con_id in bot_ids else res.external_positions).append(p)

    ledger_refs = ledger.bot_order_refs()
    open_bot_refs = set()
    for o in ibkr_orders:
        if o.order_ref.startswith(prefix):
            res.bot_orders.append(o)
            open_bot_refs.add(o.order_ref)
            if o.order_ref not in ledger_refs:
                res.mismatches.append(
                    f"IBKR open order {o.order_ref} ({o.symbol}) is tagged as a "
                    f"bot order but is not in the ledger")
            if o.account and o.account != account_id:
                res.mismatches.append(
                    f"bot order {o.order_ref} is on unexpected account {o.account}")
        else:
            res.external_orders.append(o)
    for po in ledger.pending_orders:
        if po.order_ref not in open_bot_refs:
            res.mismatches.append(
                f"ledger pending order {po.order_ref} ({po.symbol}) is not open "
                f"at IBKR (filled/cancelled while offline?)")
    return res
