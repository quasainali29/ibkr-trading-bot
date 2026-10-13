"""
Console dashboard.

IBKR ACCOUNT figures and BOT STRATEGY figures are shown in separate,
clearly labelled sections. Only BOT figures are ever used for sizing.
"""

from dataclasses import dataclass
from datetime import datetime

from money import fmt_pct, fmt_usd
from risk.system_state import SystemState

W = 64
LINE = "=" * W
THIN = "-" * W


@dataclass
class DashboardData:
    cfg: object
    connection_status: str
    managed_accounts: list
    server_version: object
    ibkr: object            # broker.account.IbkrAccountSnapshot | None
    bot: object             # portfolio_manager.BotStrategySnapshot | None
    ledger: object          # portfolio_manager.BotLedger | None
    recon: object           # portfolio_manager.ReconciliationResult | None
    state: SystemState
    reasons: dict
    md_requested_type: object = None    # market_data.models.MarketDataType
    farm_status: dict = None


def _kv(label, value):
    return f"{label}:\n  {value}"


def _qty(d):
    return f"{d.normalize():f}" if d is not None else "N/A"


def render(d):
    cfg = d.cfg
    out = [LINE, "IBKR AUTONOMOUS TRADING SYSTEM  -  PHASE 3 (READ-ONLY, ADVISORY ONLY)", LINE,
           f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    out.append(_kv("BROKER", "Interactive Brokers (TWS API)"))
    out.append(_kv("CONNECTION", f"{d.connection_status}  "
                   f"[{cfg.IBKR_HOST}:{cfg.IBKR_PORT} clientId={cfg.IBKR_CLIENT_ID}]"))

    ver = d.ibkr.verification if d.ibkr else None
    acct = ", ".join(d.managed_accounts) or "N/A"
    if ver is not None and ver.passed:
        out.append(_kv("ACCOUNT", f"PAPER / SIMULATED  (verified, {acct})"))
    else:
        out.append(_kv("ACCOUNT", f"UNVERIFIED  ({acct})"))
    out.append(_kv("TRADING MODE", cfg.TRADING_MODE))

    # ---- IBKR ACCOUNT ---------------------------------------------------
    s = d.ibkr.summary if d.ibkr else None
    out += ["", THIN, "IBKR ACCOUNT  (display only - NEVER used for sizing)", THIN]
    out.append(_kv("IBKR NET LIQUIDATION", fmt_usd(s.ibkr_net_liquidation) if s else "N/A"))
    out.append(_kv("IBKR CASH", fmt_usd(s.ibkr_cash) if s else "N/A"))
    if s and s.ibkr_buying_power is not None:
        out.append(_kv("IBKR BUYING POWER", f"{fmt_usd(s.ibkr_buying_power)}  (ignored)"))

    # ---- BOT STRATEGY ---------------------------------------------------
    b = d.bot
    out += ["", THIN, "BOT STRATEGY LEDGER  (the only capital used for sizing)", THIN]
    if b is None:
        out.append("  BOT ledger unavailable")
    else:
        out.append(_kv("BOT STRATEGY CAPITAL (initial)", fmt_usd(b.bot_initial_capital)))
        eq = fmt_usd(b.bot_strategy_equity)
        if b.marked_at_cost:
            eq += "  (positions marked at cost)"
        out.append(_kv("BOT STRATEGY EQUITY", eq))
        out.append(_kv("BOT AVAILABLE CAPITAL", fmt_usd(b.bot_available_capital)))
        out.append(_kv("BOT PEAK EQUITY", fmt_usd(b.bot_peak_equity)))
        out.append(_kv("BOT DRAWDOWN", f"{fmt_usd(b.bot_drawdown_dollars)} "
                       f"({fmt_pct(b.bot_drawdown_pct)})"))
        out.append(_kv("MAX RISK PER TRADE",
                       f"{fmt_usd(b.max_risk_per_trade_dollars)}  "
                       f"({fmt_pct(cfg.MAX_RISK_PER_TRADE)} of BOT equity)"))
        out.append(_kv("MAX PORTFOLIO DRAWDOWN",
                       f"{fmt_usd(b.max_drawdown_dollars)}  "
                       f"({fmt_pct(cfg.MAX_PORTFOLIO_DRAWDOWN)} of BOT peak; "
                       f"halt at {fmt_usd(b.drawdown_halt_equity)})"))

    out.append("")
    out.append(_kv("LIVE TRADING", "ENABLED" if cfg.LIVE_TRADING_ALLOWED else "DISABLED"))
    out.append(_kv("MARGIN", "ENABLED" if cfg.MARGIN_ALLOWED else "DISABLED"))
    out.append(_kv("ORDER PLACEMENT", "ENABLED" if cfg.ORDER_PLACEMENT_ENABLED
                   else "DISABLED (no order code exists; proposals are advisory)"))

    # ---- market data ----------------------------------------------------
    req = d.md_requested_type
    farms = d.farm_status or {}
    broken = sorted(k for k, ok in farms.items() if not ok)
    farm_txt = ("no farm status yet" if not farms else
                f"{len(farms) - len(broken)}/{len(farms)} farms OK"
                + (f" (broken: {', '.join(broken)})" if broken else ""))
    out.append(_kv("MARKET DATA",
                   f"requested {req.name if req else 'N/A'}; {farm_txt}; "
                   f"delayed/frozen data never authorizes trades"))

    # ---- positions ------------------------------------------------------
    out += ["", "POSITIONS:"]
    r = d.recon
    positions = d.ibkr.positions if d.ibkr else None
    if positions is None:
        out.append("  UNAVAILABLE")
    elif not positions:
        out.append("  (none)")
    else:
        bot_ids = d.ledger.bot_con_ids() if d.ledger else set()
        out.append(f"  {'OWNER':<9}{'SYMBOL':<8}{'TYPE':<6}{'QTY':>10}"
                   f"{'AVG COST':>14}  {'ACCOUNT'}")
        for p in positions:
            owner = "BOT" if p.con_id in bot_ids else "EXTERNAL"
            out.append(f"  {owner:<9}{p.symbol:<8}{p.sec_type:<6}{_qty(p.quantity):>10}"
                       f"{fmt_usd(p.avg_cost):>14}  {p.account}")
        out.append("  (EXTERNAL = manual/pre-existing; excluded from BOT equity)")

    # ---- open orders ----------------------------------------------------
    out += ["", "OPEN ORDERS:"]
    orders = d.ibkr.open_orders if d.ibkr else None
    if orders is None:
        out.append("  UNAVAILABLE")
    elif not orders:
        out.append("  (none)")
    else:
        prefix = cfg.BOT_ORDER_REF_PREFIX
        out.append(f"  {'OWNER':<9}{'SYMBOL':<8}{'SIDE':<6}{'QTY':>8} "
                   f"{'TYPE':<6}{'LMT':>11}{'STOP':>11}  STATUS")
        for o in orders:
            owner = "BOT" if o.order_ref.startswith(prefix) else "EXTERNAL"
            out.append(f"  {owner:<9}{o.symbol:<8}{o.action:<6}"
                       f"{_qty(o.total_quantity):>8} {o.order_type:<6}"
                       f"{fmt_usd(o.limit_price) if o.limit_price else '-':>11}"
                       f"{fmt_usd(o.aux_price) if o.aux_price else '-':>11}  {o.status}")

    # ---- reconciliation / safety ---------------------------------------
    out += ["", _kv("LEDGER RECONCILIATION",
                    "N/A" if r is None else ("OK" if r.ok else
                                            f"MISMATCH ({len(r.mismatches)})"))]
    if ver is not None:
        out.append("PAPER VERIFICATION CHECKS:")
        for c in ver.checks:
            out.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")

    out.append("")
    out.append(_kv("RISK ENGINE", "ACTIVE"))
    gate = ("risk gate open to valid trades" if d.state is SystemState.SAFE
            else "risk engine rejects ALL trades")
    out.append(_kv("SYSTEM STATUS", f"{d.state.value}   ({gate}; "
                   f"no orders are placed)"))
    if d.state is not SystemState.SAFE:
        if d.reasons:
            for code, msg in sorted(d.reasons.items()):
                out.append(f"  - [{code}] {msg}")
        else:
            out.append("  - verification pending")
    out.append(LINE)
    return "\n".join(out)
