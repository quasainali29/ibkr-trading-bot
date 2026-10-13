"""Console output for --analyze and --candles (display only)."""

from decimal import ROUND_HALF_UP, Decimal

from market_data.features import compute_features
from market_data.health import diagnose
from money import fmt_pct

W = 100
LINE = "=" * W
THIN = "-" * W


def fmt_price(d):
    if d is None:
        return "N/A"
    q = Decimal("0.0001") if abs(d) < 1 else Decimal("0.01")
    return f"${d.quantize(q, rounding=ROUND_HALF_UP):,}"


def fmt_num(d, places=2):
    if d is None:
        return "-"
    return f"{d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):,}"


def fmt_vol(d):
    return "N/A" if d is None else f"{int(d):,}"


def _zone(z):
    flag = "  <- price inside zone" if z.contains_price else ""
    return (f"{fmt_price(z.low)} - {fmt_price(z.high)}   "
            f"({z.touches} touch{'es' if z.touches != 1 else ''}, last "
            f"{z.last_touch.date()}){flag}")


def candle_table(feature_set, tz, count, intraday):
    rows = list(feature_set.rows)[-count:]
    head = (f"{'TIME (' + str(tz) + ')':<22}{'OPEN':>10}{'HIGH':>10}{'LOW':>10}"
            f"{'CLOSE':>10}{'VOLUME':>13}{'BODY':>8}{'UPWICK':>8}{'LOWICK':>8}"
            f"{'RANGE':>8}{'%CHG':>8}{'RVOL':>6}" + (f"{'VWAP':>10}" if intraday else ""))
    out = [head, "-" * len(head)]
    for r in rows:
        b, g = r.bar, r.geometry
        local = b.timestamp.astimezone(tz)
        ts = local.strftime("%Y-%m-%d %H:%M") if intraday else local.strftime("%Y-%m-%d")
        line = (f"{ts:<22}{fmt_num(b.open):>10}{fmt_num(b.high):>10}{fmt_num(b.low):>10}"
                f"{fmt_num(b.close):>10}{fmt_vol(b.volume):>13}{fmt_num(g.body):>8}"
                f"{fmt_num(g.upper_wick):>8}{fmt_num(g.lower_wick):>8}{fmt_num(g.range):>8}"
                f"{(fmt_pct(r.pct_change) if r.pct_change is not None else '-'):>8}"
                f"{fmt_num(r.rvol):>6}")
        if intraday:
            line += f"{fmt_num(r.vwap):>10}"
        out.append(line)
    return out


def _yn(flag):
    return "YES" if flag else "NO"


def _fmt_ts(ts, tz, daily):
    local = ts.astimezone(tz) if tz else ts
    return local.strftime("%Y-%m-%d") if daily else local.strftime("%Y-%m-%d %H:%M")


def in_progress_lines(bar, tz, intraday):
    """The forming bar, shown apart from the completed candles."""
    if bar is None:
        return []
    return [f"In-progress bar (NOT used in ATR/MA/VWAP/RVOL/support-resistance):",
            f"  {_fmt_ts(bar.timestamp, tz, not intraday)}   O {fmt_num(bar.open)}  "
            f"H {fmt_num(bar.high)}  L {fmt_num(bar.low)}  C {fmt_num(bar.close)}  "
            f"V {fmt_vol(bar.volume)}"]


def _check_lines(check, tz=None):
    """HISTORICAL DATA / QUOTE / TRADING sections - kept deliberately separate."""
    if check is None:
        return ["  (no validation result)"]
    out = []
    if not check.contract_ok:
        out.append("CONTRACT:")
        out.append("  Valid: NO")
        out += [f"  [ERROR] {e}" for e in check.contract_errors]
        out.append("")

    h = check.historical
    out.append("HISTORICAL DATA:")
    out.append(f"  Valid: {_yn(h.valid)}")
    if h.latest_completed_session is not None:
        approx = "  (weekday approximation)" if h.session_date_approximated else ""
        out.append(f"  Latest completed session: {h.latest_completed_session}{approx}")
    out.append(f"  Candles usable for analysis: {_yn(h.usable_for_analysis)}")
    for key, t in h.timeframes.items():
        if t.valid:
            daily = key == "1d"
            last = _fmt_ts(t.last_bar, tz, daily) if t.last_bar is not None else "-"
            label = "candle" if daily else "bar"
            out.append(f"  {key:>4}: {t.bar_count} completed bars, latest completed "
                       f"{label} {last}, current: {_yn(not t.stale)}")
            if t.in_progress_bar is not None:
                out.append(f"        in-progress {label} "
                           f"{_fmt_ts(t.in_progress_bar, tz, daily)} "
                           f"(tracked separately, excluded from indicators)")
        else:
            out.append(f"  {key:>4}: unavailable")
        out += [f"        [ERROR] {e}" for e in t.errors]
        out += [f"        [STALE] {r}" for r in t.stale_reasons]
        out += [f"        [WARN]  {w}" for w in t.warnings]

    q = check.quote
    out += ["", "QUOTE:", f"  Available: {_yn(q.available)}",
            f"  Data type: {q.data_type_label}"]
    if q.available:
        out.append(f"  Valid: {_yn(q.valid)}   Current: {_yn(not q.stale)}")
    out += [f"  [ERROR] {e}" for e in q.errors]
    out += [f"  [STALE] {r}" for r in q.stale_reasons]
    out += [f"  [WARN]  {w}" for w in q.warnings]

    out += ["", "TRADING:",
            f"  Market open: {_yn(check.market_open)}",
            f"  Usable for new trade: {_yn(check.usable_for_trading)}"]
    if check.trading_blockers:
        out.append(f"  Reason: {' / '.join(check.trading_blockers)}")
    for hint in diagnose(check.all_errors + check.all_warnings):
        out.append(f"  [HINT] {hint}")
    return out


def render_analysis(report, tz, count=10):
    c, chk = report.contract, report.check
    out = [LINE, f"MARKET ANALYSIS - {report.symbol}   (ANALYSIS ONLY - NO TRADING DECISION)", LINE]
    if c is None:
        out.append("Contract could not be resolved/validated.")
        out += [""] + _check_lines(chk, tz)
        out.append(LINE)
        return "\n".join(out)

    dtype = chk.data_type_label if chk else "UNKNOWN"
    if chk and not chk.quote.available:
        dtype += "   (quote unavailable)"
    warn = "" if (chk and chk.data_type and not chk.data_type.is_delayed
                  and not chk.data_type.is_frozen) else "   <-- cannot authorize trades"
    out += [
        f"Ticker:            {c.symbol}   ({c.long_name})",
        f"Security type:     {c.sec_type} / {c.stock_type}",
        f"Exchange:          SMART (primary {c.primary_exchange})",
        f"Currency:          {c.currency}",
        f"Quote data type:   {dtype}{warn}",
        "",
        f"Current price:     {fmt_price(report.current_price)}   (source: {report.price_source})",
        f"Previous close:    {fmt_price(report.prev_close)}",
        f"Daily change:      {fmt_pct(report.daily_change_pct)}",
        f"Daily volume:      {fmt_vol(report.day_volume)}",
        "",
        f"Analysis timeframe: {report.timeframe_key}"
        + (f"   ({len(report.features.rows)} validated bars)" if report.features else ""),
        f"ATR({report.features.atr_period if report.features else '-'}):           {fmt_price(report.atr)}",
        f"MA20:              {fmt_price(report.ma_fast)}",
        f"MA50:              {fmt_price(report.ma_slow)}",
        f"Trend:             {report.trend.label if report.trend else 'N/A'}"
        + (f"   [{report.trend.detail}]" if report.trend else ""),
        "",
        "Recent support zones (nearest first):",
    ]
    out += [f"  {_zone(z)}" for z in report.supports] or ["  none identified"]
    out.append("Recent resistance zones (nearest first):")
    out += [f"  {_zone(z)}" for z in report.resistances] or ["  none identified"]
    for n in report.notes:
        out.append(f"Note: {n}")

    if report.features:
        intraday = report.features.timeframe_key != "1d"
        out += ["", f"Last {count} completed candles ({report.timeframe_key}):"]
        out += candle_table(report.features, tz, count, intraday)
        out += in_progress_lines(report.in_progress, tz, intraday)
    out += ["", "-" * W] + _check_lines(chk, tz)
    out.append(LINE)
    return "\n".join(out)


def render_candles(smd, timeframe, tz, cfg, count=20):
    out = [LINE, f"CANDLES - {smd.symbol} {timeframe.key}   (display only)", LINE]
    chk = smd.check
    out.append(f"Quote data type: {chk.data_type_label if chk else 'UNKNOWN'}")
    series = smd.series.get(timeframe.key)
    if series is None:
        out.append(f"No validated {timeframe.key} bars: "
                   f"{smd.errors.get(timeframe.key + ' bars', 'unavailable')}")
    else:
        out.append(f"Source: {series.source}   completed bars: {len(series.bars)}   "
                   f"session: {series.session.value}   fetched: "
                   f"{series.fetched_at.astimezone(tz):%Y-%m-%d %H:%M:%S %Z}")
        feats = compute_features(series.bars, timeframe, tz, cfg)
        out.append("")
        out += candle_table(feats, tz, count, timeframe.intraday)
        out += in_progress_lines(series.in_progress, tz, timeframe.intraday)
    out += ["", "-" * W] + _check_lines(chk, tz)
    out.append(LINE)
    return "\n".join(out)
