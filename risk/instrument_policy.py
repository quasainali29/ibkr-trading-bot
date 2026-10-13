"""
Allowed-instrument policy - single source of truth.

Used by the risk engine (trade proposals) and by market-data contract
validation, so both apply exactly the same rules:
US stocks and US-listed ETFs only (secType STK, USD, US listing venue).
"""


def check_instrument(cfg, sec_type, currency, exchange, primary_exchange):
    """Return a list of violation messages (empty list = allowed)."""
    problems = []
    sec_type = str(sec_type or "").upper()
    currency = str(currency or "").upper()
    exchange = str(exchange or "").upper()
    primary = str(primary_exchange or "").upper()

    if sec_type in cfg.PROHIBITED_SEC_TYPES or sec_type not in cfg.ALLOWED_SEC_TYPES:
        problems.append(f"security type {sec_type or '<missing>'} is not allowed "
                        f"(US stocks/ETFs only)")
    if currency not in cfg.ALLOWED_CURRENCIES:
        problems.append(f"currency {currency or '<missing>'} is not allowed (USD only)")
    if exchange == cfg.SMART_EXCHANGE:
        if primary not in cfg.US_EXCHANGES:
            problems.append(f"SMART-routed order needs a US primary exchange, got "
                            f"{primary or '<missing>'}")
    elif exchange not in cfg.US_EXCHANGES:
        problems.append(f"exchange {exchange or '<missing>'} is not a US exchange")
    elif primary and primary not in cfg.US_EXCHANGES:
        problems.append(f"primary exchange {primary} is not a US exchange")
    return problems
