"""
Backtest engine. SIMULATION ONLY - no order code exists in this project.

Pass A (signals, per symbol, portfolio-independent):
    at every decision time T: AsOfBuilder.context_at(T) -> the UNCHANGED
    strategy.proposal_engine.evaluate(..., now=T, environment=BACKTEST) with a
    fresh $800 ledger and SAFE state. Cached on disk; parallel per symbol.
Pass B (live-policy portfolio, Tier 1):
    one chronological loop over all symbols. Pre-risk BUY candidates at the
    same T compete for the same cash: ranked by score, reward/risk, sector
    diversification, ticker. Each candidate goes through the SAME
    proposal_engine.risk_review() hand-off (risk engine decides, with cash
    reduced by already-accepted pending orders). The 15% circuit breaker
    permanently stops NEW entries; open positions are still managed.
Shadow (research, Tier 1 and 2):
    every evaluated setup with a valid entry/stop/Target 1 is simulated
    without touching capital; labelled PRE_BREAKER / POST_CIRCUIT_BREAKER.
"""

import dataclasses
import hashlib
import heapq
import logging
import pickle
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from backtest.asof import AsOfBuilder, SymbolHistory
from backtest.execution_simulator import (CompactBars, EntryOrder, StopLimitOrder,
                                          check_exit, close_at, commission_for, fill_entry,
                                          half_spread, open_position, time_exit_due,
                                          try_fill_entry, update_excursions)
from backtest.models import (EXIT_END, EXIT_TIME, POST_BREAKER, PRE_BREAKER, REAL, SHADOW,
                             CostModel, EquityPoint, RunResult, RunSpec, Signal, TradeRecord)
from backtest.setup_instances import SetupInstanceTracker
from environment import Environment
from market_data.models import get_timeframe
from market_data.sessions import TradingCalendar, bar_end
from portfolio.portfolio_manager import BotPosition, compute_snapshot, new_ledger, update_peak
from risk.risk_engine import RiskEngine, calculate_max_quantity
from risk.system_state import SystemState
from strategy.proposal_engine import evaluate, risk_review

log = logging.getLogger(__name__)
TIER2_LABEL = "TIER2_RESEARCH"
SIGNAL_CACHE_VERSION = 1


# ---------------------------------------------------------------------------
# Decision times / calendar
# ---------------------------------------------------------------------------
def master_calendar(bars, tf_key, tz):
    return TradingCalendar.from_bars(bars, get_timeframe(tf_key).seconds, tz)


def decision_times(bars, tf_key, calendar, start, end):
    """End of every completed `tf_key` bar strictly inside a regular session
    (a decision at the close itself could never trade)."""
    secs = get_timeframe(tf_key).seconds
    out = set()
    for b in bars:
        d = calendar.local_date(b.timestamp)
        if not (start <= d <= end):
            continue
        s = calendar.session_for(b.timestamp)
        e = bar_end(b.timestamp, secs, calendar.tz)
        if s is not None and e < s.end:
            out.add(e)
    return sorted(out)


# ---------------------------------------------------------------------------
# Pass A - signals (the unchanged Phase 3 evaluation at every T)
# ---------------------------------------------------------------------------
def signal_from(p, ctx, t, cfg):
    from analysis.relative_strength import sector_etf_for
    contract = ctx.contract
    sector = (sector_etf_for(contract, cfg) or (contract.industry if contract else "")
              or ("ETF" if contract is not None and contract.stock_type == "ETF" else ""))
    pre_risk_buy = p.risk_status in ("APPROVED", "REJECTED")
    return Signal(
        symbol=p.ticker, ts=t, action=p.proposed_action.value, pre_risk_buy=pre_risk_buy,
        mode=p.analysis_mode.value, environment=p.environment, score=p.opportunity_score,
        band=p.setup_classification.value, setup_type=p.strategy_type.value,
        horizon=p.timeframe_policy.horizon if p.timeframe_policy else "",
        confidence=p.confidence, regime=ctx.regime.label, price=ctx.current_price,
        entry=p.entry, maximum_entry=p.maximum_entry, stop=p.stop, target_1=p.target_1,
        target_2=p.target_2, reward_risk=p.reward_risk,
        no_trade_codes=tuple(r.code for r in p.no_trade_reasons if r.code != "RISK_VETO"),
        blocker_codes=tuple(r.code for r in p.execution_blockers), sector=sector or "",
        md_check=p.market_data_check if pre_risk_buy else None,
        price_state=p.price_state.value, entry_zone_low=p.entry_zone_low,
        setup_anchor=p.setup_anchor)


def generate_signals(symbol, histories, calendar, times, cfg, timeframes, cost_model):
    """Pass A for one symbol (pure; deterministic)."""
    builder = AsOfBuilder(histories, calendar, cfg, lambda px: half_spread(px, cost_model))
    neutral = compute_snapshot(new_ledger("BACKTEST", cfg.BOT_CAPITAL), cfg)
    risk = RiskEngine(cfg, Environment.BACKTEST)
    cache, day, out = {}, None, []
    for t in times:
        if t.date() != day:
            cache.clear()
            day = t.date()
        ctx = builder.context_at(symbol, t, timeframes, cache)
        p = evaluate(ctx, cfg, SystemState.SAFE, neutral, risk, now=t,
                     environment=Environment.BACKTEST)
        out.append(signal_from(p, ctx, t, cfg))
    return out


def _load_histories(store, symbols_tfs, calendar, cfg, start, end):
    hist = {}
    for sym, tfs in symbols_tfs.items():
        contract = store.load_contract(sym)
        if contract is None:
            continue
        series = {}
        for tf in tfs:
            warm = start - timedelta(days=cfg.BACKTEST_WARMUP_DAYS[tf])
            series[tf] = store.load(sym, tf, warm, end)
        hist[sym] = SymbolHistory(sym, contract, series, calendar, cfg)
    return hist


def _signal_worker(args):
    """Process-pool entry point (top-level so it can be pickled on Windows)."""
    symbol, symbols_tfs, calendar, times, start, end, timeframes, data_dir = args
    import config as cfg
    from backtest.data_loader import HistoryStore
    store = HistoryStore(data_dir, cfg)
    hist = _load_histories(store, symbols_tfs, calendar, cfg, start, end)
    return symbol, generate_signals(symbol, hist, calendar, times, cfg, timeframes,
                                    CostModel.from_config(cfg))


# ---------------------------------------------------------------------------
# Trade records
# ---------------------------------------------------------------------------
def score_band(score):
    if score is None:
        return "NO SCORE"
    if score < 50:
        return "0-49"
    if score < 65:
        return "50-64"
    if score < 75:
        return "65-74"
    if score < 85:
        return "75-84"
    return "85-100"


def make_record(kind, spec, pos, exit_fill, reason, ambiguous, gap, label, unaffordable=False):
    s, q, ef = pos.signal, pos.qty, pos.fill
    entry_px, exit_px = ef.price, exit_fill.price
    zero = Decimal(0)
    # PRIMARY R denominator: the planned risk the position was SIZED with
    # (maximum entry - stop) x quantity. Fill-to-stop risk is a diagnostic only:
    # a fill just above the stop would otherwise inflate |R| enormously.
    planned_ps = s.maximum_entry - s.stop
    risk_invalid = planned_ps <= 0 or q <= 0
    initial_risk = planned_ps * q if not risk_invalid else zero
    fill_risk = (entry_px - pos.stop) * q
    commission = ef.commission + exit_fill.commission
    net = (exit_px - entry_px) * q - commission
    gross = (exit_fill.ref_price - ef.ref_price) * q

    def per_r(x):
        return x / planned_ps if not risk_invalid else zero

    exit_ts = exit_fill.ts
    return TradeRecord(
        kind=kind, tier=spec.tier, cadence=spec.cadence, variant=spec.variant, ticker=pos.symbol,
        signal_ts=s.ts, entry_ts=pos.entry_bar_start, setup_type=s.setup_type, horizon=s.horizon,
        score=s.score, score_band=score_band(s.score), confidence=s.confidence,
        market_regime=s.regime, sector=s.sector, mode=s.mode, action=s.action,
        entry=entry_px, planned_entry=s.maximum_entry, quantity=q,
        position_value=entry_px * q, stop=pos.stop, target_1=pos.target_1,
        target_2=pos.target_2, initial_risk=initial_risk, gross_pnl=gross,
        commission=commission, spread_cost=ef.spread_cost + exit_fill.spread_cost,
        slippage_cost=ef.slippage_cost + exit_fill.slippage_cost, realized_pnl=net,
        return_pct=net / (entry_px * q) if q else Decimal(0),
        r_multiple=net / initial_risk if initial_risk > 0 else zero,
        gross_r=per_r(exit_fill.ref_price - ef.ref_price),
        mfe=max(pos.max_high - entry_px, zero), mae=max(entry_px - pos.min_low, zero),
        mfe_r=per_r(max(pos.max_high - entry_px, zero)),
        mae_r=per_r(max(entry_px - pos.min_low, zero)),
        exit_ts=exit_ts, exit_price=exit_px, exit_reason=reason,
        holding_sessions=len(pos.sessions),
        holding_hours=Decimal((exit_ts - pos.entry_bar_start).total_seconds()) / 3600,
        t2_touched=pos.t2_touched, ambiguous_bar=ambiguous, gap_fill=gap, label=label,
        no_trade_codes=s.no_trade_codes, blocker_codes=s.blocker_codes,
        unaffordable=unaffordable, fill_risk=fill_risk,
        fill_r_multiple=net / fill_risk if fill_risk > 0 else zero, risk_invalid=risk_invalid,
        entry_order_type="STOP_LIMIT" if isinstance(getattr(pos, "order", None), StopLimitOrder)
        else "LIMIT")


# ---------------------------------------------------------------------------
# Shared per-bar position logic
# ---------------------------------------------------------------------------
def _advance_position(pos, bar, session, cm, time_exit_n, entry_bar):
    """(exit_fill, reason, ambiguous, gap) or None."""
    if session is not None:
        pos.sessions.add(session.local_date)
    if not entry_bar:
        update_excursions(pos, bar)
    res = check_exit(pos, bar, cm, entry_bar=entry_bar)
    if res is not None:
        return res
    if time_exit_due(pos, time_exit_n, bar, session):
        fill, reason = close_at(pos, bar, cm, EXIT_TIME)
        return fill, reason, False, False
    return None


def _time_exit_n(spec, signal, cfg):
    if spec.variant != "B":
        return None
    return cfg.BACKTEST_TIME_EXIT_SESSIONS.get(signal.horizon)


def _bar_events(sym, cb):
    for i in range(len(cb)):
        yield cb.ts[i], sym, i


def _merged_bars(sim_bars):
    """Lazy chronological merge: yields (start_epoch, symbol, index).
    (A helper generator per symbol - a generator expression in the loop would
    bind `sym`/`cb` late and replay the last symbol's bars for everyone.)"""
    return heapq.merge(*(_bar_events(sym, sim_bars[sym]) for sym in sorted(sim_bars)))


# ---------------------------------------------------------------------------
# Pass B - live-policy portfolio
# ---------------------------------------------------------------------------
def simulate_portfolio(spec, signals_by_symbol, sim_bars, contracts, calendar, times, cfg,
                       cm, start, end, capital):
    res = RunResult(spec, tuple(sorted(signals_by_symbol)), start, end, capital,
                    decision_times=len(times))
    ledger = new_ledger("BACKTEST", capital)
    risk = RiskEngine(cfg, Environment.BACKTEST)
    candidates_at = defaultdict(list)
    times_set = set(times)
    for sym, sigs in signals_by_symbol.items():
        for s in sigs:
            if s.pre_risk_buy and s.ts in times_set:
                candidates_at[s.ts].append(s)
    orders, positions, last_close, reserved = {}, {}, {}, {}
    entry_count = 0
    merged = _merged_bars(sim_bars)
    pending = next(merged, None)
    exposure_hits = 0
    risk_reasons = defaultdict(int)

    def handle_bar(sym, i):
        nonlocal entry_count
        bar = sim_bars[sym].bar(i)
        last_close[sym] = bar.close
        session = calendar.session_for(bar.start)
        pos = positions.get(sym)
        if pos is not None:
            out = _advance_position(pos, bar, session, cm, _time_exit_n(spec, pos.signal, cfg), False)
            if out is not None:
                _close(sym, pos, *out)
            return
        order = orders.get(sym)
        if order is None:
            return
        if bar.start >= order.expires:
            del orders[sym]
            reserved.pop(sym, None)
            res.cancelled_entries += 1
            return
        fill = try_fill_entry(order, bar, cm)
        if fill is None:
            return
        del orders[sym]
        reserved.pop(sym, None)
        pos = open_position(order, fill, bar, session.local_date if session else bar.start.date())
        positions[sym] = pos
        entry_count += 1
        ledger.cash -= pos.cost_basis
        ledger.positions.append(BotPosition(
            contracts[sym].con_id, sym, pos.qty, pos.cost_basis / pos.qty, pos.stop,
            f"TBOT-BT-{entry_count:06d}", fill.ts.isoformat()))
        out = _advance_position(pos, bar, session, cm, _time_exit_n(spec, pos.signal, cfg), True)
        if out is not None:
            _close(sym, pos, *out)

    def _close(sym, pos, fill, reason, ambiguous, gap):
        del positions[sym]
        proceeds = fill.price * pos.qty - fill.commission
        ledger.cash += proceeds
        ledger.realized_pnl += proceeds - pos.cost_basis
        ledger.positions = [p for p in ledger.positions if p.symbol != sym]
        res.trades.append(make_record(REAL, spec, pos, fill, reason, ambiguous, gap, PRE_BREAKER))

    def advance_until(t):
        nonlocal pending
        while pending is not None:
            start_epoch, sym, i = pending
            if sim_bars[sym].end_epoch(i) > int(t.timestamp()):
                break
            handle_bar(sym, i)
            pending = next(merged, None)

    for idx, t in enumerate(times):
        advance_until(t)
        for sym in [s for s, o in orders.items() if o.expires <= t]:
            del orders[sym]
            reserved.pop(sym, None)
            res.cancelled_entries += 1
        marks = {contracts[s].con_id: last_close[s] for s in positions if s in last_close}
        snap = compute_snapshot(ledger, cfg, marks)
        update_peak(ledger, snap)
        snap = compute_snapshot(ledger, cfg, marks)
        if positions:
            exposure_hits += 1
        res.equity.append(EquityPoint(t, ledger.cash, snap.bot_strategy_equity,
                                      snap.bot_peak_equity, snap.bot_drawdown_pct,
                                      len(positions)))
        if res.breaker_ts is None and snap.bot_drawdown_pct >= cfg.MAX_PORTFOLIO_DRAWDOWN:
            res.breaker_ts = t
            res.notes.append(f"15% circuit breaker tripped at {t.isoformat()}: no new entries "
                             f"for the rest of the run (live policy)")
        cands = candidates_at.get(t, [])
        res.candidates += len(cands)
        if not cands or res.breaker_ts is not None:
            continue
        state = SystemState.SAFE
        t_next = times[idx + 1] if idx + 1 < len(times) else t + timedelta(days=7)
        held = {positions[s].signal.sector for s in positions} | \
               {orders[s].signal.sector for s in orders}
        remaining = list(cands)
        while remaining:
            remaining.sort(key=lambda s: (-(s.score or 0), -(s.reward_risk or 0),
                                          1 if s.sector and s.sector in held else 0, s.symbol))
            s = remaining.pop(0)
            if s.symbol in positions or s.symbol in orders:
                res.skipped_in_position += 1
                continue
            avail = ledger.cash - sum(reserved.values(), Decimal(0))
            snap_avail = dataclasses.replace(snap, bot_cash=avail)
            decision, qty = risk_review(s.symbol, contracts[s.symbol], s.maximum_entry, s.stop,
                                        s.md_check, snap_avail, state, risk, t, cfg)
            if not decision.approved or qty < 1:
                res.rejected_by_risk += 1
                for r in decision.rejections:
                    risk_reasons[r.reason.value] += 1
                continue
            orders[s.symbol] = EntryOrder(s.symbol, s, qty, s.maximum_entry, t, t_next)
            reserved[s.symbol] = qty * s.maximum_entry + commission_for(qty, s.maximum_entry, cm)
            held.add(s.sector)

    advance_until(datetime.max.replace(tzinfo=timezone.utc))
    res.cancelled_entries += len(orders)
    orders.clear()
    for sym in sorted(positions):
        pos = positions[sym]
        cb = sim_bars[sym]
        fill, reason = close_at(pos, cb.bar(len(cb) - 1), cm, EXIT_END)
        _close(sym, pos, fill, reason, False, False)
    if res.equity:
        snap = compute_snapshot(ledger, cfg, {})
        res.equity.append(EquityPoint(res.equity[-1].ts + timedelta(seconds=1), ledger.cash,
                                      snap.bot_strategy_equity, snap.bot_peak_equity,
                                      snap.bot_drawdown_pct, 0))
    res.exposure_pct = (Decimal(exposure_hits) / Decimal(len(times))) if times else Decimal(0)
    res.risk_rejection_reasons = dict(risk_reasons)
    return res


# ---------------------------------------------------------------------------
# Shadow research trades (never touch capital)
# ---------------------------------------------------------------------------
def simulate_shadow(spec, symbol, signals, bars, calendar, times, cfg, cm, capital,
                    breaker_ts=None, stats=None):
    """One shadow trade per unique setup instance (backtest.setup_instances);
    at most one pending shadow order per (symbol, setup type). `stats`, when
    given, accumulates the setup-instance counts."""
    out = []
    times_set = set(times)
    by_t = defaultdict(list)
    present = defaultdict(set)          # decision time -> setup types present
    for s in signals:
        if s.ts in times_set and s.setup_type != "NONE":
            present[s.ts].add(s.setup_type)
            if s.has_trade_plan:
                by_t[s.ts].append(s)
    tracker = SetupInstanceTracker(symbol)
    active_orders, active_pos = {}, {}
    i, n = 0, len(bars)

    def label(s):
        if spec.tier == 2:
            return TIER2_LABEL
        return POST_BREAKER if breaker_ts is not None and s.ts >= breaker_ts else PRE_BREAKER

    def process(bar):
        session = calendar.session_for(bar.start)
        for key in list(active_pos):
            pos = active_pos[key]
            r = _advance_position(pos, bar, session, cm, _time_exit_n(spec, pos.signal, cfg), False)
            if r is not None:
                del active_pos[key]
                out.append(_shadow_record(spec, pos, r, label(pos.signal)))
        for key in list(active_orders):
            order = active_orders[key]
            if bar.start >= order.expires:
                del active_orders[key]
                continue
            fill = fill_entry(order, bar, cm)
            if fill is None:
                continue
            del active_orders[key]
            day = session.local_date if session else bar.start.date()
            pos = open_position(order, fill, bar, day)
            pos.order = order
            pos.unaffordable = getattr(order, "unaffordable", False)
            pos.instance = tracker.consume(order.signal, day)
            r = _advance_position(pos, bar, session, cm, _time_exit_n(spec, pos.signal, cfg), True)
            if r is not None:
                out.append(_shadow_record(spec, pos, r, label(pos.signal)))
            else:
                active_pos[pos.instance] = pos

    for idx, t in enumerate(times):
        te = int(t.timestamp())
        while i < n and bars.end_epoch(i) <= te:
            process(bars.bar(i))
            i += 1
        for key in [k for k, o in active_orders.items() if o.expires <= t]:
            del active_orders[key]
        tracker.observe(calendar.local_date(t), present.get(t, ()))
        t_next = times[idx + 1] if idx + 1 < len(times) else t + timedelta(days=7)
        for s in by_t.get(t, []):
            key = s.setup_type
            if tracker.is_consumed(s):
                # same setup instance as an earlier shadow trade: no re-entry
                tracker.suppress()
                active_orders.pop(key, None)
                continue
            *_, qty = calculate_max_quantity(capital, capital, s.maximum_entry, s.stop,
                                             cfg.MAX_RISK_PER_TRADE)
            if (s.price_state == "AWAITING_TRIGGER" and s.entry_zone_low is not None
                    and s.entry_zone_low <= s.maximum_entry):
                # Not triggered yet: buy-stop-limit, never filled below the trigger.
                order = StopLimitOrder(symbol, s, max(qty, 1), s.maximum_entry, t, t_next,
                                       trigger=s.entry_zone_low)
            else:
                order = EntryOrder(symbol, s, max(qty, 1), s.maximum_entry, t, t_next)
            order.unaffordable = qty < 1
            active_orders[key] = order
    while i < n:
        process(bars.bar(i))
        i += 1
    if n:
        last = bars.bar(n - 1)
        for key in sorted(active_pos):
            pos = active_pos[key]
            fill, reason = close_at(pos, last, cm, EXIT_END)
            out.append(_shadow_record(spec, pos, (fill, reason, False, False),
                                      label(pos.signal)))
    if stats is not None:
        for k, v in tracker.stats.items():
            stats[k] = stats.get(k, 0) + v
    return out


def _shadow_record(spec, pos, exit_info, label):
    rec = make_record(SHADOW, spec, pos, *exit_info, label, getattr(pos, "unaffordable", False))
    rec.setup_instance = getattr(pos, "instance", "")
    return rec


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _code_fingerprint():
    root = Path(__file__).resolve().parent.parent
    h = hashlib.sha256()
    for pkg in ("analysis", "strategy", "market_data", "risk", "backtest"):
        for f in sorted((root / pkg).glob("*.py")):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    for f in ("config.py", "money.py", "environment.py"):
        h.update((root / f).read_bytes())
    return h.hexdigest()[:16]


class Backtester:
    def __init__(self, cfg, store, tier, symbols, start, end, workers=None, progress=print,
                 cache_dir=None, cost_model=None):
        self.cfg = cfg
        self.store = store
        self.tier = tier
        self.tier_cfg = cfg.BACKTEST_TIERS[tier]
        self.timeframes = self.tier_cfg["timeframes"]
        self.symbols = list(symbols)
        self.start, self.end = start, end
        self.workers = workers or cfg.BACKTEST_WORKERS
        self.progress = progress
        self.cache_dir = Path(cache_dir) if cache_dir else Path(cfg.BACKTEST_SIGNAL_CACHE_DIR)
        self.cm = cost_model or CostModel.from_config(cfg)
        self.capital = cfg.BOT_CAPITAL
        cal_tf = "15m" if "15m" in self.timeframes else "1h"
        spy_bars = store.load(cfg.BENCHMARK_SPY, cal_tf,
                              start - timedelta(days=cfg.BACKTEST_WARMUP_DAYS[cal_tf]), end)
        if not spy_bars:
            raise RuntimeError(f"SPY {cal_tf} history is required for the session calendar")
        from market_data.sessions import resolve_tz
        contract = store.load_contract(cfg.BENCHMARK_SPY)
        self.calendar = master_calendar(spy_bars, cal_tf, resolve_tz(contract.time_zone_id))
        self.times = {c: decision_times(spy_bars if c == cal_tf else
                                        store.load(cfg.BENCHMARK_SPY, c, start, end),
                                        c, self.calendar, start, end)
                      for c in self.tier_cfg["cadences"]}
        self.signal_times = sorted(set().union(*self.times.values()))
        self.contracts = {s: store.load_contract(s) for s in self.symbols}
        missing = [s for s, c in self.contracts.items() if c is None]
        if missing:
            raise RuntimeError(f"no downloaded data/contract for: {', '.join(missing)}")

    # --- pass A -----------------------------------------------------------
    def _bench_symbols(self, symbol):
        from analysis.relative_strength import benchmarks_for
        cfg = self.cfg
        out = []
        for b in benchmarks_for(self.contracts[symbol], cfg) + [cfg.BENCHMARK_SPY,
                                                                cfg.BENCHMARK_QQQ]:
            if b != symbol and b not in out and self.store.load_contract(b) is not None:
                out.append(b)
        return out

    def _fingerprint(self, symbol):
        h = hashlib.sha256()
        h.update(f"{SIGNAL_CACHE_VERSION}|{self.tier}|{self.start}|{self.end}|"
                 f"{len(self.signal_times)}|{_code_fingerprint()}".encode())
        for sym, tfs in self._symbols_tfs(symbol).items():
            for tf in tfs:
                h.update(f"{sym}:{tf}:{self.store.checksum(sym, tf)}".encode())
        return h.hexdigest()[:20]

    def _symbols_tfs(self, symbol):
        d = {symbol: self.timeframes}
        for b in self._bench_symbols(symbol):
            d.setdefault(b, ("1d",))
        return d

    def signals(self):
        out, todo = {}, []
        for sym in self.symbols:
            path = self.cache_dir / f"tier{self.tier}" / f"{sym}-{self._fingerprint(sym)}.pkl"
            if path.exists():
                out[sym] = pickle.loads(path.read_bytes())
                self.progress(f"  signals {sym}: cached ({len(out[sym])} evaluations)")
            else:
                todo.append((sym, path))
        args = [(sym, self._symbols_tfs(sym), self.calendar, self.signal_times, self.start,
                 self.end, self.timeframes, str(self.store.root)) for sym, _ in todo]
        paths = dict(todo)
        if args:
            self.progress(f"  evaluating {len(args)} symbol(s) x {len(self.signal_times)} "
                          f"decision times with {min(self.workers, len(args))} worker(s)...")
            if self.workers <= 1 or len(args) == 1:
                results = map(_signal_worker, args)
            else:
                pool = ProcessPoolExecutor(max_workers=min(self.workers, len(args)))
                results = pool.map(_signal_worker, args)
            for sym, sigs in results:
                out[sym] = sigs
                paths[sym].parent.mkdir(parents=True, exist_ok=True)
                paths[sym].write_bytes(pickle.dumps(sigs))
                self.progress(f"  signals {sym}: {len(sigs)} evaluations")
            if not (self.workers <= 1 or len(args) == 1):
                pool.shutdown()
        return out

    # --- simulation bars --------------------------------------------------
    def sim_bars(self):
        out = {}
        for sym in self.symbols:
            for tf in self.tier_cfg["sim_timeframes"]:
                if tf in self.timeframes:
                    bars = self.store.load(sym, tf, self.start, self.end)
                    if bars:
                        out[sym] = CompactBars(bars, get_timeframe(tf).seconds,
                                               self.calendar.tz)
                        break
        return out

    def run(self):
        sigs = self.signals()
        bars = self.sim_bars()
        results = []
        for cadence in self.tier_cfg["cadences"]:
            times = self.times[cadence]
            for variant in self.cfg.BACKTEST_EXIT_VARIANTS:
                spec = RunSpec(self.tier, cadence, variant, self.tier_cfg["portfolio"])
                self.progress(f"  simulating tier {self.tier} cadence {cadence} variant {variant}")
                if spec.portfolio:
                    r = simulate_portfolio(spec, {s: sigs[s] for s in bars}, bars,
                                           self.contracts, self.calendar, times, self.cfg,
                                           self.cm, self.start, self.end, self.capital)
                else:
                    r = RunResult(spec, tuple(self.symbols), self.start, self.end, self.capital,
                                  decision_times=len(times))
                    r.exposure_pct, r.risk_rejection_reasons = Decimal(0), {}
                    r.notes.append("Tier 2: research / shadow trades only (no portfolio)")
                for sym in sorted(bars):
                    r.shadow.extend(simulate_shadow(spec, sym, sigs.get(sym, []), bars[sym],
                                                    self.calendar, times, self.cfg, self.cm,
                                                    self.capital, r.breaker_ts,
                                                    r.shadow_dedupe))
                r.signal_counts = _signal_counts(sigs, times)
                results.append(r)
        return results


def _signal_counts(sigs, times):
    ts = set(times)
    counts = defaultdict(int)
    for lst in sigs.values():
        for s in lst:
            if s.ts in ts:
                counts["evaluations"] += 1
                counts[f"mode:{s.mode}"] += 1
                counts[f"action:{s.action}"] += 1
                if s.pre_risk_buy:
                    counts["pre_risk_buy"] += 1
    return dict(counts)
