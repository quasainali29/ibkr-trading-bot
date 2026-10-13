"""
Classification of IBKR error()/status messages.

IBKR routes informational notices, warnings and real errors through the same
error() callback. Each code maps to a log level and a safety impact:

* NONE        - logged only.
* REQUEST     - fails the single request it belongs to (e.g. no market-data
                subscription for one ticker). Never global SAFE_MODE.
* CONNECTION / ACCOUNT / DATA / EXECUTION - system-level: global SAFE_MODE.

Errors carrying the reqId of a pending market-data request are treated as
request-scoped by the connection manager (unless they are CONNECTION codes).
Unknown codes default to WARNING with no safety impact.
"""

import logging
from dataclasses import dataclass
from enum import Enum


class Impact(str, Enum):
    NONE = "NONE"
    REQUEST = "REQUEST"         # one request failed; caller decides
    CONNECTION = "CONNECTION"   # link to TWS / IBKR servers is unreliable
    ACCOUNT = "ACCOUNT"         # verified account state may be wrong
    DATA = "DATA"               # a required data request failed
    EXECUTION = "EXECUTION"     # anything touching order safety


@dataclass(frozen=True)
class MessageClass:
    level: int
    impact: Impact
    label: str
    restores_connection: bool = False
    fails_request: bool = False         # terminates the request it refers to
    farm_status: bool | None = None     # True=farm OK, False=farm broken


def _c(level, impact, label, restores=False, fails=None, farm=None):
    if fails is None:
        fails = impact in (Impact.REQUEST, Impact.DATA)
    return MessageClass(level, impact, label, restores, fails, farm)


INFO, WARNING, ERROR, CRITICAL = (logging.INFO, logging.WARNING,
                                  logging.ERROR, logging.CRITICAL)

MESSAGE_CODES = {
    # --- Informational: farm connection OK notices etc. ---
    2104: _c(INFO, Impact.NONE, "Market data farm connection OK", farm=True),
    2106: _c(INFO, Impact.NONE, "HMDS data farm connection OK", farm=True),
    2158: _c(INFO, Impact.NONE, "Sec-def data farm connection OK", farm=True),
    2107: _c(INFO, Impact.NONE, "HMDS data farm inactive but available", farm=True),
    2108: _c(INFO, Impact.NONE, "Market data farm inactive but available", farm=True),
    2119: _c(INFO, Impact.NONE, "Market data farm connecting"),
    2100: _c(INFO, Impact.NONE, "Account data subscription unsubscribed"),
    2150: _c(INFO, Impact.NONE, "Invalid position trade derived value"),
    165: _c(INFO, Impact.NONE, "Historical market data service query message"),

    # --- Warnings: market-data farms (per-ticker data health, not global) ---
    2103: _c(WARNING, Impact.NONE, "Market data farm connection broken", farm=False),
    2105: _c(WARNING, Impact.NONE, "HMDS data farm connection broken", farm=False),
    2157: _c(WARNING, Impact.NONE, "Sec-def data farm connection broken", farm=False),
    2109: _c(WARNING, Impact.NONE, "Outside RTH attribute ignored"),
    2137: _c(WARNING, Impact.NONE, "Cross-side warning"),
    2176: _c(WARNING, Impact.NONE, "Fractional share warning"),
    10167: _c(WARNING, Impact.NONE, "Displaying delayed market data"),
    2188: _c(WARNING, Impact.NONE,
             "Up-to-the-second historical data requires a live subscription"),

    # --- Request-scoped market-data failures (one ticker / one request) ---
    101: _c(WARNING, Impact.REQUEST, "Max number of tickers reached"),
    162: _c(WARNING, Impact.REQUEST, "Historical market data service error / pacing"),
    166: _c(WARNING, Impact.REQUEST, "HMDS expired contract violation"),
    200: _c(WARNING, Impact.REQUEST, "No security definition found"),
    300: _c(WARNING, Impact.NONE, "Can't find EId with ticker id"),
    354: _c(WARNING, Impact.REQUEST, "Requested market data is not subscribed"),
    366: _c(WARNING, Impact.REQUEST, "No historical data query found"),
    420: _c(WARNING, Impact.REQUEST, "Invalid real-time query / pacing"),
    10089: _c(WARNING, Impact.REQUEST, "Market data requires additional subscription"),
    10090: _c(WARNING, Impact.REQUEST, "Part of market data requires subscription"),
    10091: _c(WARNING, Impact.REQUEST, "Part of market data requires subscription"),
    10168: _c(WARNING, Impact.REQUEST, "Market data not subscribed; delayed not enabled"),
    10186: _c(WARNING, Impact.REQUEST, "Requested market data is not subscribed"),
    10197: _c(WARNING, Impact.REQUEST, "No market data during competing live session"),
    10314: _c(WARNING, Impact.REQUEST, "End date/time invalid"),

    # --- Connectivity ---
    1100: _c(ERROR, Impact.CONNECTION, "Connectivity between IBKR and TWS lost"),
    1101: _c(WARNING, Impact.CONNECTION,
             "Connectivity restored - data lost, re-verification required",
             restores=True),
    1102: _c(WARNING, Impact.CONNECTION,
             "Connectivity restored - data maintained, re-verification required",
             restores=True),
    1300: _c(ERROR, Impact.CONNECTION, "TWS socket port has been reset"),
    2110: _c(ERROR, Impact.CONNECTION, "Connectivity between TWS and server broken"),
    502: _c(CRITICAL, Impact.CONNECTION, "Couldn't connect to TWS"),
    503: _c(CRITICAL, Impact.CONNECTION, "TWS version not supported"),
    504: _c(ERROR, Impact.CONNECTION, "Not connected"),
    507: _c(ERROR, Impact.CONNECTION, "Bad message length / socket error"),
    509: _c(ERROR, Impact.CONNECTION, "Exception while processing message"),
    326: _c(CRITICAL, Impact.CONNECTION, "Client ID already in use"),
    10225: _c(ERROR, Impact.CONNECTION, "Bust event: connection reset"),

    # --- Account / data-request failures ---
    321: _c(ERROR, Impact.EXECUTION, "Error validating request (e.g. Read-Only API)",
            fails=True),
    322: _c(ERROR, Impact.DATA, "Error processing request"),
    320: _c(ERROR, Impact.DATA, "Error reading request"),

    # --- Execution (should never occur - no orders are placed) ---
    103: _c(ERROR, Impact.EXECUTION, "Duplicate order id"),
    104: _c(ERROR, Impact.EXECUTION, "Can't modify a filled order"),
    105: _c(ERROR, Impact.EXECUTION, "Order modification mismatch"),
    110: _c(ERROR, Impact.EXECUTION, "Price does not conform to min tick"),
    201: _c(ERROR, Impact.EXECUTION, "Order rejected"),
    202: _c(WARNING, Impact.EXECUTION, "Order cancelled"),
    399: _c(WARNING, Impact.EXECUTION, "Order message warning"),
    10147: _c(WARNING, Impact.EXECUTION, "Order to cancel not found"),
    10148: _c(WARNING, Impact.EXECUTION, "Order cannot be cancelled"),
}


def _unknown(code):
    # Codes below 2100 and 10000+ are errors in IBKR's numbering; when they
    # refer to a pending request they terminate it. 2100-2199 are warnings.
    fails = code < 2100 or code >= 10000
    return MessageClass(WARNING, Impact.NONE, "Unclassified IBKR message",
                        fails_request=fails)


UNKNOWN = MessageClass(WARNING, Impact.NONE, "Unclassified IBKR message")


def classify(code):
    """Return the MessageClass for an IBKR error/status code."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return UNKNOWN
    return MESSAGE_CODES.get(code) or _unknown(code)


def farm_name(text):
    """Extract the farm name from e.g. 'Market data farm connection is OK:usfarm'."""
    if ":" in text:
        return text.rsplit(":", 1)[1].strip() or None
    return None


def level_name(msg_class):
    return logging.getLevelName(msg_class.level)
