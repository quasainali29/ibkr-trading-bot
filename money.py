"""
Safe monetary arithmetic helpers.

All capital, risk, P/L and drawdown math uses decimal.Decimal. Floats coming
from IBKR or callers are converted through str() so binary float artefacts
(e.g. 0.1 + 0.2) never enter the ledger. Rounding happens only for display
or when a value is persisted as cents.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0")

# ibapi uses these sentinels for "not set"
_UNSET_DOUBLE = 1.7976931348623157e308


class MoneyError(ValueError):
    pass


def to_decimal(value, field="value"):
    """Strictly convert int/float/str/Decimal to a finite Decimal.

    Raises MoneyError on None, bool, NaN, infinity or unparseable input.
    """
    if value is None:
        raise MoneyError(f"{field} is missing")
    if isinstance(value, bool):
        raise MoneyError(f"{field} must be numeric, got bool")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, float, str)):
        try:
            d = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise MoneyError(f"{field} is not a number: {value!r}") from None
    else:
        raise MoneyError(f"{field} has unsupported type {type(value).__name__}")
    if not d.is_finite():
        raise MoneyError(f"{field} is not finite: {value!r}")
    return d


def optional_price(value):
    """Convert an IBKR price that may be unset (UNSET_DOUBLE) to Decimal/None."""
    if value is None:
        return None
    try:
        if float(value) >= _UNSET_DOUBLE:
            return None
        return to_decimal(value)
    except (MoneyError, ValueError, TypeError, OverflowError):
        return None


def cents(d):
    """Round to cents (half-up) for display / persistence."""
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def floor_int(d):
    """Floor a Decimal to an int."""
    return int(d.to_integral_value(rounding=ROUND_FLOOR))


def fmt_usd(d):
    if d is None:
        return "N/A"
    d = cents(d)
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def fmt_pct(d):
    if d is None:
        return "N/A"
    return f"{(d * 100).quantize(CENT, rounding=ROUND_HALF_UP)}%"
