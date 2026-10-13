"""
Contract resolution and validation (pure validation lives here; the IBKR
request is issued by MarketDataService).

A contract is accepted only if it is an allowed US stock / US-listed ETF
(shared instrument policy), has an allowed IBKR stockType, and is not a
leveraged or inverse product. Leveraged/inverse detection is layered:
    1. IBKR stockType (ETN etc. rejected)
    2. explicit deny list of tickers
    3. long-name keywords (2X, 3X, ULTRA, INVERSE, BULL, BEAR, ...)
    4. IBKR metadata: category, subcategory, industry, fund type/asset type
"""

import re
from dataclasses import dataclass, field

from risk.instrument_policy import check_instrument


@dataclass
class ContractCheck:
    symbol: str
    ok: bool
    contract: object = None                     # ContractInfo when resolved
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def detect_leveraged_or_inverse(info, cfg):
    """Return reasons the contract looks leveraged/inverse (empty = clean)."""
    reasons = []
    symbol = (info.symbol or "").upper()
    if symbol in cfg.LEVERAGED_DENY_LIST:
        reasons.append(f"{symbol} is on the leveraged/inverse deny list")

    fields = {
        "long name": info.long_name,
        "category": info.category,
        "subcategory": info.subcategory,
        "industry": info.industry,
        "fund type": info.fund_type,
        "fund asset type": info.fund_asset_type,
    }
    for label, text in fields.items():
        if not text:
            continue
        for pattern in cfg.LEVERAGED_NAME_PATTERNS:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                reasons.append(f"{label} {text!r} matches leveraged/inverse "
                               f"pattern {m.group(0)!r}")
                break
    return reasons


def validate_contract(symbol, candidates, cfg):
    """Validate IBKR contract-details results for a requested symbol."""
    symbol = (symbol or "").strip().upper()
    check = ContractCheck(symbol=symbol, ok=False)
    if not symbol:
        check.reasons.append("symbol is empty")
        return check
    if not candidates:
        check.reasons.append("no contract found at IBKR")
        return check

    # Keep only exact-symbol, US-listed USD stock matches.
    matches = [c for c in candidates if (c.symbol or "").upper() == symbol
               and not check_instrument(cfg, c.sec_type, c.currency,
                                        cfg.SMART_EXCHANGE, c.primary_exchange)]
    by_con = {c.con_id: c for c in matches}
    if not by_con:
        first = candidates[0]
        problems = check_instrument(cfg, first.sec_type, first.currency,
                                    cfg.SMART_EXCHANGE, first.primary_exchange)
        check.reasons += problems or [f"IBKR returned {first.symbol!r}, not {symbol!r}"]
        return check
    if len(by_con) > 1:
        listing = ", ".join(f"{c.con_id}@{c.primary_exchange}" for c in by_con.values())
        check.reasons.append(f"ambiguous contract: {len(by_con)} matches ({listing})")
        return check

    info = next(iter(by_con.values()))
    check.contract = info
    if not info.con_id:
        check.reasons.append("contract has no conId")

    stock_type = (info.stock_type or "").upper()
    if not stock_type:
        check.reasons.append("IBKR stockType missing; cannot confirm stock/ETF")
    elif stock_type in cfg.PROHIBITED_STOCK_TYPES:
        check.reasons.append(f"stockType {stock_type} is prohibited")
    elif stock_type not in cfg.ALLOWED_STOCK_TYPES:
        check.reasons.append(f"stockType {stock_type} is not in the allowed list")

    check.reasons += detect_leveraged_or_inverse(info, cfg)

    if not info.time_zone_id:
        check.reasons.append("exchange time zone missing")
    if not info.liquid_hours:
        check.reasons.append("regular trading hours (liquidHours) missing")

    check.ok = not check.reasons
    return check
