"""
IBKR TWS connection manager.

Wraps ibapi's EClient/EWrapper, runs the socket reader in a daemon thread,
classifies every error()/status message, and pushes the system into
SAFE_MODE whenever the connection cannot be trusted.

This module contains NO order placement code.
"""

import logging
import threading
import time
from enum import Enum

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from broker import messages
from broker.messages import Impact
from broker.requests import RequestRegistry

log = logging.getLogger(__name__)
ibkr_log = logging.getLogger("ibkr")

# SAFE_MODE reason codes owned by this module
R_NOT_CONNECTED = "IBKR_NOT_CONNECTED"
R_CONNECTIVITY = "IBKR_CONNECTIVITY"
R_REVERIFY = "IBKR_REVERIFY_REQUIRED"
R_EXECUTION_ALERT = "IBKR_EXECUTION_ALERT"
R_DATA_ERROR = "IBKR_DATA_ERROR"

# Callbacks forwarded from the wrapper to registered listeners
# (AccountService, MarketDataService). Read-only data callbacks only.
_FORWARDED = (
    "accountSummary", "accountSummaryEnd", "position", "positionEnd",
    "openOrder", "orderStatus", "openOrderEnd",
    "contractDetails", "contractDetailsEnd", "marketDataType",
    "tickPrice", "tickSize", "tickString", "tickGeneric", "tickSnapshotEnd",
    "historicalData", "historicalDataEnd",
)


class ConnectionStatus(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    LOST = "LOST"


def wait_event(event, timeout, step=0.2):
    """Wait for a threading.Event in short slices so Ctrl+C stays responsive
    on Windows. Returns True if the event was set."""
    deadline = time.monotonic() + timeout
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        event.wait(min(step, remaining))
    return True


class IBKRApp(EWrapper, EClient):
    """Thin ibapi adapter. All logic lives in IBKRConnection."""

    def __init__(self, conn):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._conn = conn

    # --- connection lifecycle -------------------------------------------
    def nextValidId(self, orderId):
        self._conn._on_next_valid_id(orderId)

    def managedAccounts(self, accountsList):
        self._conn._on_managed_accounts(accountsList)

    def connectionClosed(self):
        self._conn._on_connection_closed()

    def error(self, reqId, errorTime, errorCode, errorString,
              advancedOrderRejectJson=""):
        self._conn._on_error(reqId, errorCode, errorString)


def _make_forwarder(name):
    def forward(self, *args):
        conn = self._conn
        for listener in list(conn.listeners):
            handler = getattr(listener, name, None)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception as exc:
                log.exception("Listener %s failed", name)
                # A failure while handling one symbol's request fails only
                # that request; anything else is a system-level data error.
                req = (conn.requests.get(args[0])
                       if args and isinstance(args[0], int) else None)
                if req is not None and req.scope == "SYMBOL":
                    req.fail(-1, f"internal error in {name}: {exc}")
                else:
                    conn.state.enter_safe_mode(
                        R_DATA_ERROR, f"Internal error while processing IBKR {name}")
    forward.__name__ = name
    return forward


for _name in _FORWARDED:
    setattr(IBKRApp, _name, _make_forwarder(_name))


class IBKRConnection:
    def __init__(self, state, host, port, client_id, connect_timeout=10.0):
        self.state = state
        self.host = host
        self.port = port
        self.client_id = client_id
        self.connect_timeout = connect_timeout
        self.listeners = []             # receive forwarded data callbacks
        self.requests = RequestRegistry()
        self.farm_status = {}           # farm name -> True (OK) / False (broken)

        self.app = None
        self._thread = None
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._accounts_received = threading.Event()
        self._shutting_down = False

        self.status = ConnectionStatus.DISCONNECTED
        self.managed_accounts = []
        self.server_version = None
        self.connection_time = None
        self.last_error = None

    # --- public API -----------------------------------------------------
    def connect(self):
        """Connect and wait for the handshake. Returns True on success."""
        with self._lock:
            if self.is_connected():
                return True
            self._teardown_app()
            self._shutting_down = False
            self._ready.clear()
            self._accounts_received.clear()
            self.managed_accounts = []
            self.status = ConnectionStatus.CONNECTING
            log.info("Connecting to IBKR TWS at %s:%s (clientId=%s)",
                     self.host, self.port, self.client_id)

            self.app = IBKRApp(self)
            try:
                self.app.connect(self.host, self.port, self.client_id)
            except Exception as exc:
                log.error("Socket connect raised: %s", exc)

            if not self.app.isConnected():
                return self._connect_failed("TWS socket connection failed "
                                            "(is TWS running with API enabled?)")

            self._thread = threading.Thread(
                target=self._run_reader, name="ibkr-reader", daemon=True)
            self._thread.start()

        ok = (wait_event(self._ready, self.connect_timeout)
              and wait_event(self._accounts_received, self.connect_timeout))
        with self._lock:
            if not ok or not self.app or not self.app.isConnected():
                self._teardown_app()
                return self._connect_failed(
                    "Handshake timeout: no nextValidId/managedAccounts")

            self.status = ConnectionStatus.CONNECTED
            self.server_version = self.app.serverVersion()
            self.connection_time = self.app.twsConnectionTime()
            # A fresh connection supersedes any earlier connectivity alarm,
            # but the data must be re-verified before SAFE is possible.
            self.state.clear(R_NOT_CONNECTED)
            self.state.clear(R_CONNECTIVITY)
            log.info("Connected. serverVersion=%s accounts=%s",
                     self.server_version, ",".join(self.managed_accounts))
            return True

    def disconnect(self):
        with self._lock:
            self._shutting_down = True
            if self.app is not None:
                log.info("Disconnecting from IBKR")
            self._teardown_app()
            self.status = ConnectionStatus.DISCONNECTED

    def add_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def is_connected(self):
        return (self.app is not None and self.app.isConnected()
                and self.status is ConnectionStatus.CONNECTED)

    # --- internals ------------------------------------------------------
    def _run_reader(self):
        try:
            self.app.run()
        except Exception:
            log.exception("IBKR reader thread crashed")
            self.state.enter_safe_mode(R_NOT_CONNECTED, "IBKR reader thread crashed")
            self.status = ConnectionStatus.LOST

    def _teardown_app(self):
        app, thread = self.app, self._thread
        self.app, self._thread = None, None
        if app is not None:
            try:
                app.disconnect()
            except Exception:
                log.debug("Error during disconnect", exc_info=True)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def _connect_failed(self, message):
        self.status = ConnectionStatus.DISCONNECTED
        self.state.enter_safe_mode(R_NOT_CONNECTED, message)
        log.error(message)
        return False

    def _on_next_valid_id(self, order_id):
        # Recorded only as a handshake signal; Phase 1 never uses order IDs.
        log.debug("nextValidId=%s", order_id)
        self._ready.set()

    def _on_managed_accounts(self, accounts_list):
        self.managed_accounts = [a.strip() for a in accounts_list.split(",")
                                 if a.strip()]
        self._accounts_received.set()

    def _on_connection_closed(self):
        if self._shutting_down or self.status is ConnectionStatus.CONNECTING:
            log.info("IBKR connection closed")  # connect() reports its own failure
            return
        self.status = ConnectionStatus.LOST
        log.error("IBKR connection closed unexpectedly")
        self.state.enter_safe_mode(R_NOT_CONNECTED, "IBKR connection closed")
        self.requests.fail_all(-1, "connection closed")

    def _on_error(self, req_id, code, text):
        mc = messages.classify(code)
        req = self.requests.get(req_id) if req_id is not None and req_id >= 0 else None
        ibkr_log.log(mc.level, "IBKR %s [%s] reqId=%s%s: %s",
                     messages.level_name(mc), code, req_id,
                     f" ({req.label})" if req else "", text)
        if mc.level >= logging.ERROR:
            self.last_error = f"{code}: {text}"

        if mc.farm_status is not None:
            name = messages.farm_name(text)
            if name:
                self.farm_status[name] = mc.farm_status

        # Errors tied to a pending request are routed to that request.
        if req is not None and mc.impact is not Impact.CONNECTION:
            if mc.fails_request or mc.impact is not Impact.NONE:
                req.fail(code, text)
            else:   # non-fatal notice: keep it with the request's result
                req.meta.setdefault("warnings", []).append(f"IBKR {code}: {text}")
            if req.scope == "SYMBOL":
                return          # per-ticker problem: never global SAFE_MODE
            # SYSTEM-scope requests (account data) fall through below.

        if mc.impact in (Impact.NONE, Impact.REQUEST):
            return
        if mc.restores_connection:
            self.state.clear(R_CONNECTIVITY)
            self.state.invalidate_verification()
            self.state.enter_safe_mode(R_REVERIFY, f"{code}: {mc.label}")
        elif mc.impact is Impact.CONNECTION:
            if self.status is ConnectionStatus.CONNECTING:
                return  # connect() reports the failure itself
            self.state.enter_safe_mode(R_CONNECTIVITY, f"{code}: {text}")
            self.requests.fail_all(code, text)
        elif mc.impact is Impact.EXECUTION:
            # Latched until restart: no orders are ever sent, so any
            # execution-related message is unexplained.
            self.state.enter_safe_mode(R_EXECUTION_ALERT, f"{code}: {text}")
        else:  # ACCOUNT / DATA
            self.state.enter_safe_mode(R_DATA_ERROR, f"{code}: {text}")