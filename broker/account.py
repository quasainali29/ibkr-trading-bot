"""
Read-only IBKR account access: account summary, positions, open orders and
paper-account verification.

Every value read here describes the IBKR ACCOUNT. None of it is used for
strategy sizing; sizing uses only the internal BOT strategy ledger.
All requests are read-only and work with TWS "Read-Only API" enabled.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from broker.connection import wait_event
from money import MoneyError, optional_price, to_decimal

log = logging.getLogger(__name__)

SUMMARY_TAGS = "AccountType,NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower"
REQUIRED_SUMMARY_TAGS = ("NetLiquidation", "TotalCashValue")


class DataUnavailable(Exception):
    """A required IBKR data request failed or timed out."""


# ---------------------------------------------------------------------------
# Records (IBKR ACCOUNT data - display/reconciliation only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IbkrAccountSummary:
    account_id: str
    account_type: str
    ibkr_net_liquidation: Decimal
    ibkr_cash: Decimal
    ibkr_available_funds: Decimal | None
    ibkr_buying_power: Decimal | None
    currency: str
    accounts_seen: tuple


@dataclass(frozen=True)
class IbkrPosition:
    account: str
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    quantity: Decimal
    avg_cost: Decimal


@dataclass
class IbkrOpenOrder:
    perm_id: int
    order_id: int
    client_id: int
    account: str
    con_id: int
    symbol: str
    sec_type: str
    action: str
    total_quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    aux_price: Decimal | None
    order_ref: str
    status: str = ""
    filled: Decimal = Decimal("0")
    remaining: Decimal | None = None


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PaperVerification:
    checks: tuple

    @property
    def passed(self):
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failures(self):
        return [c for c in self.checks if not c.passed]


@dataclass
class IbkrAccountSnapshot:
    taken_at: datetime
    summary: IbkrAccountSummary | None = None
    positions: list | None = None
    open_orders: list | None = None
    verification: PaperVerification | None = None
    errors: dict = field(default_factory=dict)   # section -> message

    @property
    def complete(self):
        return (self.summary is not None and self.positions is not None
                and self.open_orders is not None and not self.errors)


# ---------------------------------------------------------------------------
# Paper account verification (pure function - unit tested)
# ---------------------------------------------------------------------------
def verify_paper_account(cfg, connected_port, managed_accounts, summary):
    """Run independent checks that the connection is a PAPER account.

    The IBKR API has no explicit "is paper" flag, so no single signal is
    trusted on its own; every check must pass.
    """
    checks = []

    def add(name, ok, detail):
        checks.append(VerificationCheck(name, bool(ok), detail))

    add("config_paper_mode",
        cfg.TRADING_MODE == "PAPER" and cfg.LIVE_TRADING_ALLOWED is False,
        f"TRADING_MODE={cfg.TRADING_MODE}, LIVE_TRADING_ALLOWED={cfg.LIVE_TRADING_ALLOWED}")

    add("paper_port",
        connected_port in cfg.PAPER_PORTS and connected_port not in cfg.LIVE_PORTS,
        f"connected port {connected_port}")

    accounts = list(managed_accounts or [])
    add("single_managed_account", len(accounts) == 1,
        f"managed accounts: {accounts or 'none'}")

    acct = accounts[0] if len(accounts) == 1 else None
    add("paper_account_prefix",
        acct is not None and acct.upper().startswith(cfg.PAPER_ACCOUNT_PREFIXES),
        f"account {acct!r} vs prefixes {cfg.PAPER_ACCOUNT_PREFIXES}")

    if cfg.EXPECTED_ACCOUNT_ID:
        add("expected_account_id", acct == cfg.EXPECTED_ACCOUNT_ID,
            f"expected {cfg.EXPECTED_ACCOUNT_ID!r}, got {acct!r}")
    else:
        add("expected_account_id", True, "not pinned (EXPECTED_ACCOUNT_ID=None)")

    if summary is None:
        add("summary_account_match", False, "account summary unavailable")
    else:
        add("summary_account_match",
            acct is not None and summary.account_id == acct
            and set(summary.accounts_seen) == {acct},
            f"summary accounts {list(summary.accounts_seen)} vs managed {acct!r}")

    return PaperVerification(tuple(checks))


# ---------------------------------------------------------------------------
# Account service
# ---------------------------------------------------------------------------
class _Pending:
    def __init__(self, req_id=None):
        self.req_id = req_id
        self.done = threading.Event()
        self.error = None


class AccountService:
    """Issues read-only IBKR requests and collects their callbacks."""

    def __init__(self, connection, cfg):
        self.conn = connection
        self.cfg = cfg
        self.timeout = cfg.REQUEST_TIMEOUT_SECONDS
        self._fetch_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._summary_pending = None
        self._summary_rows = {}
        self._positions_pending = None
        self._positions = []
        self._orders_pending = None
        self._orders = {}
        connection.add_listener(self)

    # --- high level -----------------------------------------------------
    def refresh(self):
        """Fetch summary, positions and open orders and verify the account.

        Never raises for data problems; failures are recorded in
        snapshot.errors so the caller can enter SAFE_MODE.
        """
        snap = IbkrAccountSnapshot(taken_at=datetime.now(timezone.utc))
        if not self.conn.is_connected():
            snap.errors["connection"] = "Not connected to IBKR"
        else:
            for section, fn in (("summary", self.fetch_account_summary),
                                ("positions", self.fetch_positions),
                                ("open_orders", self.fetch_open_orders)):
                try:
                    setattr(snap, section, fn())
                except DataUnavailable as exc:
                    snap.errors[section] = str(exc)
                    log.error("IBKR %s unavailable: %s", section, exc)

        snap.verification = verify_paper_account(
            self.cfg, self.conn.port, self.conn.managed_accounts, snap.summary)
        for c in snap.verification.failures:
            log.error("Paper verification FAILED [%s]: %s", c.name, c.detail)
        return snap

    # --- requests -------------------------------------------------------
    def fetch_account_summary(self):
        with self._fetch_lock:
            pending = self.conn.requests.register("account_summary", "account summary")
            req_id = pending.req_id
            with self._data_lock:
                self._summary_rows = {}
                self._summary_pending = pending
            try:
                self.conn.app.reqAccountSummary(req_id, "All", SUMMARY_TAGS)
                self._await(pending, "account summary")
            finally:
                self._safe_call(self.conn.app.cancelAccountSummary, req_id)
                self.conn.requests.remove(req_id)
            with self._data_lock:
                rows = dict(self._summary_rows)
            return self._build_summary(rows)

    def fetch_positions(self):
        with self._fetch_lock:
            with self._data_lock:
                self._positions = []
                self._positions_pending = _Pending()
                pending = self._positions_pending
            self.conn.app.reqPositions()
            try:
                self._await(pending, "positions")
            finally:
                self._safe_call(self.conn.app.cancelPositions)
            with self._data_lock:
                return [p for p in self._positions if p.quantity != 0]

    def fetch_open_orders(self):
        with self._fetch_lock:
            with self._data_lock:
                self._orders = {}
                self._orders_pending = _Pending()
                pending = self._orders_pending
            # All clients' orders (incl. manual TWS orders), read-only.
            self.conn.app.reqAllOpenOrders()
            self._await(pending, "open orders")
            with self._data_lock:
                return sorted(self._orders.values(), key=lambda o: o.perm_id)

    def _await(self, pending, what):
        if not wait_event(pending.done, self.timeout):
            raise DataUnavailable(f"timed out waiting for {what}")
        if pending.error:
            raise DataUnavailable(f"{what} request failed: {pending.error}")
        if not self.conn.is_connected():
            raise DataUnavailable(f"connection lost while reading {what}")

    @staticmethod
    def _safe_call(fn, *args):
        try:
            fn(*args)
        except Exception:
            log.debug("cancel call failed", exc_info=True)

    def _build_summary(self, rows):
        accounts = tuple(sorted(rows))
        if not accounts:
            raise DataUnavailable("account summary returned no accounts")
        account_id = accounts[0]
        tags = rows[account_id]
        missing = [t for t in REQUIRED_SUMMARY_TAGS if t not in tags]
        if missing:
            raise DataUnavailable(f"account summary missing {missing}")
        try:
            netliq = to_decimal(tags["NetLiquidation"][0], "NetLiquidation")
            cash = to_decimal(tags["TotalCashValue"][0], "TotalCashValue")
        except MoneyError as exc:
            raise DataUnavailable(str(exc)) from None

        def opt(tag):
            try:
                return to_decimal(tags[tag][0], tag) if tag in tags else None
            except MoneyError:
                return None

        return IbkrAccountSummary(
            account_id=account_id,
            account_type=tags.get("AccountType", ("", ""))[0],
            ibkr_net_liquidation=netliq,
            ibkr_cash=cash,
            ibkr_available_funds=opt("AvailableFunds"),
            ibkr_buying_power=opt("BuyingPower"),
            currency=tags["NetLiquidation"][1] or "USD",
            accounts_seen=accounts,
        )

    # --- forwarded ibapi callbacks (reader thread) -------------------------
    def accountSummary(self, reqId, account, tag, value, currency):
        with self._data_lock:
            p = self._summary_pending
            if p and p.req_id == reqId:
                self._summary_rows.setdefault(account, {})[tag] = (value, currency)

    def accountSummaryEnd(self, reqId):
        with self._data_lock:
            p = self._summary_pending
            if p and p.req_id == reqId:
                p.done.set()

    def position(self, account, contract, position, avgCost):
        try:
            rec = IbkrPosition(
                account=account,
                con_id=int(contract.conId),
                symbol=contract.symbol,
                sec_type=contract.secType,
                exchange=contract.exchange or "",
                primary_exchange=contract.primaryExchange or "",
                currency=contract.currency,
                quantity=to_decimal(position, "position"),
                avg_cost=to_decimal(avgCost, "avgCost"),
            )
        except MoneyError as exc:
            log.error("Unparseable position for %s: %s", contract.symbol, exc)
            with self._data_lock:
                if self._positions_pending:
                    self._positions_pending.error = f"unparseable position {contract.symbol}"
            return
        with self._data_lock:
            self._positions.append(rec)

    def positionEnd(self):
        with self._data_lock:
            if self._positions_pending:
                self._positions_pending.done.set()

    def openOrder(self, orderId, contract, order, orderState):
        key = order.permId or orderId
        try:
            qty = to_decimal(order.totalQuantity, "totalQuantity")
        except MoneyError:
            qty = Decimal("0")
        rec = IbkrOpenOrder(
            perm_id=int(order.permId),
            order_id=int(orderId),
            client_id=int(order.clientId),
            account=order.account,
            con_id=int(contract.conId),
            symbol=contract.symbol,
            sec_type=contract.secType,
            action=order.action,
            total_quantity=qty,
            order_type=order.orderType,
            limit_price=optional_price(order.lmtPrice),
            aux_price=optional_price(order.auxPrice),
            order_ref=order.orderRef or "",
            status=orderState.status or "",
        )
        with self._data_lock:
            prev = self._orders.get(key)
            if prev is not None:
                rec.filled, rec.remaining = prev.filled, prev.remaining
                rec.status = rec.status or prev.status
            self._orders[key] = rec

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld,
                    mktCapPrice):
        key = permId or orderId
        with self._data_lock:
            rec = self._orders.get(key)
            if rec is None:
                return
            rec.status = status
            try:
                rec.filled = to_decimal(filled, "filled")
                rec.remaining = to_decimal(remaining, "remaining")
            except MoneyError:
                pass

    def openOrderEnd(self):
        with self._data_lock:
            if self._orders_pending:
                self._orders_pending.done.set()
