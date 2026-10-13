"""
Entry point: connect, verify, reconcile, display, analyze. NO trading.

    python main.py                          # verification cycle + dashboard
    python main.py --watch 30               # refresh every 30s until Ctrl+C
    python main.py --analyze AAPL           # analysis only (default 1d)
    python main.py --analyze AAPL --timeframe 1h
    python main.py --candles AAPL --timeframe 5m [--count 30]
    python main.py --evaluate AAPL          # advisory proposal (never an order)
    python main.py ... --data-type delayed  # live|frozen|delayed|delayed-frozen
    python main.py ... --refresh            # ignore candle caches
    python main.py -v                       # also show INFO logs on the console

Exit codes: 0 = SAFE / candles usable for analysis, 1 = SAFE_MODE or not
connected, 2 = config refused, 3 = candles invalid/stale for analysis,
130 = interrupted.
"""

import argparse
import logging
import os
import re
import signal
import sys
import threading
from datetime import date, datetime, timedelta, timezone

import config
import dashboard
from broker.account import AccountService
from broker.connection import (R_DATA_ERROR, R_REVERIFY, IBKRConnection,
                               wait_event)
from journal.trade_journal import TradeJournal
from log_setup import setup_logging
from portfolio.portfolio_manager import (LedgerError, LedgerStore,
                                         compute_snapshot, new_ledger,
                                         reconcile, update_peak)
from analysis.evaluation_report import render_evaluation
from analysis.formatting import render_analysis, render_candles
from analysis.market_report import build_report
from analysis.relative_strength import benchmarks_for
from analysis.setup_context import build_context
from strategy.proposal_engine import evaluate as evaluate_proposal
from market_data.candle_store import CandleStore
from market_data.health import MarketDataHealth
from market_data.market_data import MarketDataService
from market_data.models import TIMEFRAMES, MarketDataType, get_timeframe
from market_data.sessions import resolve_tz
from risk.risk_engine import RiskEngine
from risk.system_state import StateController, SystemState

log = logging.getLogger("main")

# SAFE_MODE reason codes owned by the verification cycle
R_DATA_INCOMPLETE = "IBKR_DATA_INCOMPLETE"
R_PAPER = "PAPER_VERIFICATION_FAILED"
R_LEDGER = "BOT_LEDGER_INVALID"
R_RECON = "BOT_LEDGER_RECONCILIATION"
R_DRAWDOWN = "BOT_DRAWDOWN_HALT"
R_INTERNAL = "INTERNAL_ERROR"


class TradingBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.journal = TradeJournal(cfg.JOURNAL_PATH)
        self.state = StateController(on_change=self._on_state_change)
        self.conn = IBKRConnection(self.state, cfg.IBKR_HOST, cfg.IBKR_PORT,
                                   cfg.IBKR_CLIENT_ID, cfg.CONNECT_TIMEOUT_SECONDS)
        self.account = AccountService(self.conn, cfg)
        self.store = LedgerStore(cfg.LEDGER_PATH, cfg)
        self.risk = RiskEngine(cfg)
        self.md_health = MarketDataHealth(self.state, cfg)
        self.market_data = MarketDataService(
            self.conn, cfg, CandleStore(cfg.CANDLE_CACHE_DIR, cfg), self.md_health)
        self.stop_event = threading.Event()

        self.ledger = None
        self.ibkr_snapshot = None
        self.bot_snapshot = None
        self.recon = None
        self._last_recon_issues = None

    # ------------------------------------------------------------------
    def _on_state_change(self, old, new, reasons):
        self.journal.record("state_change", old=old, new=new, reasons=reasons)

    def run_cycle(self):
        """One full verification pass. Never raises for data problems."""
        st = self.state
        try:
            if not self.conn.is_connected():
                if self.conn.connect():
                    self.market_data.set_market_data_type(self.market_data.requested_type)

            # 1. IBKR ACCOUNT data + paper verification
            snap = self.account.refresh()
            self.ibkr_snapshot = snap
            st.set_check(R_DATA_INCOMPLETE, snap.complete,
                         "; ".join(f"{k}: {v}" for k, v in snap.errors.items()))
            ver = snap.verification
            st.set_check(R_PAPER, ver.passed,
                         "; ".join(f"{c.name}: {c.detail}" for c in ver.failures))
            account_ok = snap.complete and ver.passed
            account_id = snap.summary.account_id if snap.summary else None

            # 2. BOT ledger: load (validated) or create on first run
            self._load_ledger(account_ok, account_id)

            # 3. Reconcile BOT ledger against IBKR positions/open orders
            if self.ledger is not None and account_ok:
                self.recon = reconcile(self.ledger, account_id, snap.positions,
                                       snap.open_orders, self.cfg)
                st.set_check(R_RECON, self.recon.ok, "; ".join(self.recon.mismatches))
                if self.recon.mismatches != self._last_recon_issues:
                    self._last_recon_issues = list(self.recon.mismatches)
                    self.journal.record(
                        "reconciliation", ok=self.recon.ok,
                        mismatches=self.recon.mismatches,
                        bot_positions=len(self.recon.bot_positions),
                        external_positions=len(self.recon.external_positions),
                        bot_orders=len(self.recon.bot_orders),
                        external_orders=len(self.recon.external_orders))
            else:
                self.recon = None
                st.set_check(R_RECON, False,
                             "cannot reconcile: IBKR account state or BOT ledger unverified")

            # 4. BOT strategy equity / drawdown (from peak)
            if self.ledger is not None:
                self.bot_snapshot = compute_snapshot(self.ledger, self.cfg)
                if update_peak(self.ledger, self.bot_snapshot):
                    self.store.save(self.ledger)
                st.set_check(
                    R_DRAWDOWN,
                    self.bot_snapshot.bot_drawdown_pct < self.cfg.MAX_PORTFOLIO_DRAWDOWN,
                    "BOT drawdown reached the maximum; trading halted")
            else:
                self.bot_snapshot = None

            # 5. Transient alarms are cleared only by a fully verified pass
            if account_ok:
                st.clear(R_DATA_ERROR)
                st.clear(R_REVERIFY)
            st.clear(R_INTERNAL)
            st.mark_verification_complete()
        except Exception as exc:
            log.exception("Verification cycle failed")
            st.enter_safe_mode(R_INTERNAL, f"verification cycle error: {exc}")

    def _load_ledger(self, account_ok, account_id):
        if self.ledger is not None:
            return
        try:
            if self.store.exists():
                self.ledger = self.store.load()
                log.info("BOT ledger loaded and validated")
                self.journal.record("ledger_loaded", account_id=self.ledger.account_id)
            elif account_ok:
                self.ledger = new_ledger(account_id, self.cfg.BOT_CAPITAL)
                self.store.save(self.ledger)
                log.warning("No BOT ledger found - created a new one with %s "
                            "for account %s", self.cfg.BOT_CAPITAL, account_id)
                self.journal.record("ledger_created", account_id=account_id,
                                    initial_capital=self.cfg.BOT_CAPITAL)
            else:
                self.state.enter_safe_mode(
                    R_LEDGER, "no BOT ledger yet; waiting for a verified paper account")
                return
            self.state.clear(R_LEDGER)
        except (LedgerError, OSError) as exc:
            self.ledger = None
            self.state.enter_safe_mode(R_LEDGER, str(exc))
            self.journal.record("ledger_invalid", error=str(exc))

    def render(self):
        return dashboard.render(dashboard.DashboardData(
            cfg=self.cfg,
            connection_status=self.conn.status.value,
            managed_accounts=self.conn.managed_accounts,
            server_version=self.conn.server_version,
            ibkr=self.ibkr_snapshot,
            bot=self.bot_snapshot,
            ledger=self.ledger,
            recon=self.recon,
            state=self.state.state,
            reasons=self.state.reasons,
            md_requested_type=self.market_data.requested_type,
            farm_status=dict(self.conn.farm_status),
        ))

    def _analysis_preflight(self):
        """Run the Phase 1 verification cycle; analysis requires a verified
        PAPER connection but is allowed (read-only) in other SAFE_MODE states."""
        self.run_cycle()
        snap = self.ibkr_snapshot
        verified = (self.conn.is_connected() and snap is not None
                    and snap.verification is not None and snap.verification.passed)
        state = self.state.state
        print(f"System: {state.value}   Connection: {self.conn.status.value}   "
              f"Paper verified: {'YES' if verified else 'NO'}", flush=True)
        if state is not SystemState.SAFE:
            for code, msg in sorted(self.state.reasons.items()):
                print(f"  - [{code}] {msg}")
        if not verified:
            print("Analysis refused: IBKR paper connection not verified.", file=sys.stderr)
        return verified

    def analyze(self, symbol, timeframe_key, count, refresh):
        if not self._analysis_preflight():
            return 1
        keys = list(dict.fromkeys([timeframe_key, "1d"]))
        smd = self.market_data.fetch_symbol(symbol, keys, refresh=refresh)
        report = build_report(smd, timeframe_key, self.cfg)
        tz = smd.calendar.tz if smd.calendar else resolve_tz("US/Eastern")
        print(render_analysis(report, tz, count), flush=True)
        chk = smd.check
        self.journal.record("analysis", symbol=smd.symbol, timeframe=timeframe_key,
                            historical_valid=chk.historical.valid,
                            usable_for_analysis=chk.historical.usable_for_analysis,
                            quote_available=chk.quote.available,
                            data_type=chk.data_type, market_open=chk.market_open,
                            usable_for_trading=chk.usable_for_trading,
                            blockers=chk.trading_blockers, errors=chk.all_errors)
        return 0 if chk.historical.usable_for_analysis else 3

    def candles(self, symbol, timeframe_key, count, refresh):
        if not self._analysis_preflight():
            return 1
        tf = get_timeframe(timeframe_key)
        smd = self.market_data.fetch_symbol(symbol, [tf.key], refresh=refresh)
        tz = smd.calendar.tz if smd.calendar else resolve_tz("US/Eastern")
        print(render_candles(smd, tf, tz, self.cfg, count), flush=True)
        return 0 if smd.check.historical.usable_for_analysis else 3

    def evaluate(self, symbol, refresh):
        """Phase 3: deterministic interpretation + ADVISORY proposal. No orders."""
        if not self._analysis_preflight():
            return 1
        cfg = self.cfg
        smd = self.market_data.fetch_symbol(symbol, list(cfg.EVALUATION_TIMEFRAMES),
                                            refresh=refresh)
        contract = smd.contract_check.contract if smd.contract_check else None
        # A benchmark evaluated itself (e.g. --evaluate SPY) supplies its own data.
        benchmarks = {smd.symbol: smd} if smd.symbol in (cfg.BENCHMARK_SPY,
                                                         cfg.BENCHMARK_QQQ) else {}
        for sym in benchmarks_for(contract, cfg) + [cfg.BENCHMARK_SPY, cfg.BENCHMARK_QQQ]:
            if sym not in benchmarks and sym != smd.symbol:
                benchmarks[sym] = self.market_data.fetch_symbol(sym, ["1d"], refresh=refresh,
                                                                quote=False)
        ctx = build_context(smd, benchmarks, cfg)
        bot_snapshot = compute_snapshot(self.ledger, cfg) if self.ledger is not None else None
        proposal = evaluate_proposal(ctx, cfg, self.state.state, bot_snapshot, self.risk)
        print(render_evaluation(ctx, proposal), flush=True)
        self.journal.record("evaluation", proposal=proposal.to_dict(),
                            system_state=self.state.state)
        daily = ctx.daily
        return 0 if daily is not None and daily.fresh else 3

    def backtest(self, args):
        """Phase 3.5: backtest the UNCHANGED Phase 3 logic. Simulation only."""
        from backtest.data_loader import (HistoryDownloader, HistoryError, HistoryStore,
                                          read_universe)
        from backtest.engine import Backtester, _code_fingerprint
        from backtest.models import CostModel
        from backtest.reports import export, render_cadence_comparison, render_run
        cfg = self.cfg
        tcfg = cfg.BACKTEST_TIERS[args.tier]
        symbols = ([args.backtest.upper()] if args.backtest
                   else read_universe(args.backtest_universe))
        start = date.fromisoformat(args.start or tcfg["default_start"])
        end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
        store = HistoryStore(cfg.BACKTEST_DATA_DIR, cfg)
        print(f"BACKTEST tier {args.tier}: {len(symbols)} symbol(s), {start} to {end} "
              f"(simulation only - no orders)", flush=True)

        def needs(sym, tfs):
            return any(store.missing_ranges(
                sym, tf, start - timedelta(days=cfg.BACKTEST_WARMUP_DAYS[tf]), end)
                for tf in tfs)

        bench_core = [cfg.BENCHMARK_SPY, cfg.BENCHMARK_QQQ]
        plan = {s: tcfg["timeframes"] for s in symbols}
        plan.setdefault(cfg.BENCHMARK_SPY, tcfg["timeframes"])   # session calendar
        plan.setdefault(cfg.BENCHMARK_QQQ, ("1d",))
        missing = [s for s, tfs in plan.items() if needs(s, tfs)]
        if missing or args.download_only:
            if args.no_download:
                print(f"Missing backtest data for: {', '.join(missing)} (run without "
                      f"--no-download to fetch from IBKR)", file=sys.stderr)
                return 3
            if not self._analysis_preflight():
                return 1
            dl = HistoryDownloader(self.market_data, store, cfg,
                                   progress=lambda m: print(m, flush=True))
            try:
                for sym, tfs in plan.items():
                    print(f"Downloading {sym} {', '.join(tfs)} ...", flush=True)
                    dl.ensure(sym, tfs, start, end)
                sectors = {b for s in symbols for b in benchmarks_for(store.load_contract(s), cfg)
                           if b not in plan}
                for etf in sorted(sectors):
                    print(f"Downloading sector benchmark {etf} 1d ...", flush=True)
                    dl.ensure(etf, ("1d",), start, end)
            except HistoryError as exc:
                print(f"Download failed: {exc}", file=sys.stderr)
                return 3
            if args.download_only:
                print("Download complete.")
                return 0

        bt = Backtester(cfg, store, args.tier, symbols, start, end, args.workers,
                        progress=lambda m: print(m, flush=True))
        results = bt.run()
        for r in results:
            print(render_run(r, cfg.BACKTEST_MIN_SAMPLE), flush=True)
        comparison = render_cadence_comparison(results)
        if comparison:
            print(comparison)
        data = {f"{s}:{tf}": store.checksum(s, tf) for s in sorted(set(plan) | set(bt.contracts))
                for tf in ("1d", "1h", "15m", "5m") if store.checksum(s, tf)}
        manifest = {"tier": args.tier, "symbols": symbols, "start": start, "end": end,
                    "cadences": tcfg["cadences"], "variants": cfg.BACKTEST_EXIT_VARIANTS,
                    "cost_model": CostModel.from_config(cfg).__dict__,
                    "code": _code_fingerprint(), "data": data,
                    "starting_capital": cfg.BOT_CAPITAL}
        out_dir, rid = export(results, manifest, cfg.BACKTEST_RESULTS_DIR, cfg.BACKTEST_MIN_SAMPLE)
        print(f"Results written to {out_dir} (run_id {rid})")
        self.journal.record("backtest", run_id=rid, tier=args.tier, symbols=symbols,
                            start=start, end=end, out_dir=str(out_dir))
        return 0

    def run(self, watch_seconds=None):
        first = True
        while not self.stop_event.is_set():
            self.run_cycle()
            if first:
                self.journal.record("startup_verification", state=self.state.state,
                                    reasons=self.state.reasons)
                first = False
            if watch_seconds:
                os.system("cls" if os.name == "nt" else "clear")
            print(self.render(), flush=True)
            if not watch_seconds:
                break
            print(f"Refreshing every {watch_seconds}s - press Ctrl+C to exit.",
                  flush=True)
            if wait_event(self.stop_event, watch_seconds):
                break
        return self.state.state

    def shutdown(self):
        self.stop_event.set()
        try:
            self.conn.disconnect()
        finally:
            self.journal.record("shutdown", state=self.state.state)
            log.info("Shutdown complete")


def parse_args(argv):
    p = argparse.ArgumentParser(description="IBKR trading bot - read-only (no trading)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--watch", type=int, metavar="N",
                      help="refresh the dashboard every N seconds (min 5)")
    mode.add_argument("--analyze", metavar="SYMBOL",
                      help="analysis-only market report for a US stock/ETF")
    mode.add_argument("--candles", metavar="SYMBOL", help="print recent candles")
    mode.add_argument("--evaluate", metavar="SYMBOL",
                      help="advisory trade evaluation (ANALYSIS_ONLY / EXECUTION_READY); "
                           "never places orders")
    mode.add_argument("--backtest", metavar="SYMBOL",
                      help="backtest the unchanged Phase 3 logic on one symbol (simulation)")
    mode.add_argument("--backtest-universe", metavar="FILE",
                      help="backtest every symbol listed in FILE (shared $800 portfolio)")
    p.add_argument("--start", help="backtest start date YYYY-MM-DD (default per tier)")
    p.add_argument("--end", help="backtest end date YYYY-MM-DD (default: today)")
    p.add_argument("--tier", type=int, choices=[1, 2], default=1,
                   help="1 = 2024+ full Phase 3 data (default); 2 = 2018+ daily/hourly research")
    p.add_argument("--download-only", action="store_true",
                   help="download/refresh backtest history and exit")
    p.add_argument("--no-download", action="store_true",
                   help="never connect to IBKR; use cached backtest data only")
    p.add_argument("--workers", type=int, default=None,
                   help="parallel worker processes for signal generation")
    p.add_argument("--timeframe", choices=list(TIMEFRAMES), default=None,
                   help="bar size (default: 1d for --analyze, 5m for --candles)")
    p.add_argument("--count", type=int, default=None,
                   help="number of candles to print (default 10 / 20)")
    p.add_argument("--data-type", choices=["live", "frozen", "delayed", "delayed-frozen"],
                   help="IBKR market data type to request (default from config)")
    p.add_argument("--refresh", action="store_true",
                   help="ignore memory/CSV candle caches and re-download")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show INFO logs on the console")
    args = p.parse_args(argv)
    if args.watch is not None and args.watch < 5:
        p.error("--watch must be at least 5 seconds")
    if args.count is not None and not 1 <= args.count <= 500:
        p.error("--count must be between 1 and 500")
    backtesting = bool(args.backtest or args.backtest_universe)
    if (args.start or args.end or args.download_only or args.no_download or args.workers) \
            and not backtesting:
        p.error("--start/--end/--download-only/--no-download/--workers require a backtest")
    if args.tier != 1 and not backtesting:
        p.error("--tier requires a backtest")
    for d in (args.start, args.end):
        if d is not None:
            try:
                date.fromisoformat(d)
            except ValueError:
                p.error(f"invalid date {d!r} (use YYYY-MM-DD)")
    if args.download_only and args.no_download:
        p.error("--download-only and --no-download are mutually exclusive")
    for sym in (args.analyze, args.candles, args.evaluate, args.backtest):
        if sym is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{0,9}( [A-Za-z])?", sym):
            p.error(f"invalid ticker symbol {sym!r}")
    if (args.timeframe or args.count) and not (args.analyze or args.candles):
        p.error("--timeframe/--count require --analyze or --candles")
    if args.refresh and not (args.analyze or args.candles or args.evaluate):
        p.error("--refresh requires --analyze, --candles or --evaluate")
    return args


def main(argv=None):
    args = parse_args(argv)
    log_path = setup_logging(config.LOG_DIR, args.verbose)
    log.info("Starting Phase 1 (read-only). Log file: %s", log_path)

    try:
        config.validate_config()
    except config.ConfigError as exc:
        log.critical("Unsafe configuration - refusing to start: %s", exc)
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return 2

    # Ctrl+Break on Windows behaves like Ctrl+C.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    bot = TradingBot(config)
    bot.journal.record("startup", trading_mode=config.TRADING_MODE,
                       bot_capital=config.BOT_CAPITAL, port=config.IBKR_PORT)
    if args.data_type:
        bot.market_data.requested_type = MarketDataType.parse(args.data_type)
    try:
        if args.analyze:
            return bot.analyze(args.analyze, args.timeframe or "1d", args.count or 10,
                               args.refresh)
        if args.candles:
            return bot.candles(args.candles, args.timeframe or "5m", args.count or 20,
                               args.refresh)
        if args.evaluate:
            return bot.evaluate(args.evaluate, args.refresh)
        if args.backtest or args.backtest_universe:
            return bot.backtest(args)
        final = bot.run(args.watch)
        return 0 if final is SystemState.SAFE else 1
    except KeyboardInterrupt:
        print("\nCtrl+C received - disconnecting safely...", flush=True)
        log.warning("Interrupted by user (Ctrl+C)")
        return 130
    finally:
        try:
            bot.shutdown()
        except KeyboardInterrupt:
            log.warning("Second interrupt during shutdown; exiting")


if __name__ == "__main__":
    sys.exit(main())
