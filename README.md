# IBKR Trading Bot — Phase 3 (read-only, advisory proposals, PAPER only)

**Phase 1** does the following:
- connects to TWS paper trading and verifies the account is a paper account
- reads the account summary, positions and open orders
- reconciles them against an internal **$800 BOT strategy ledger**
- prints a dashboard

**Phase 2** adds a read-only market-data layer:
- contract lookup and validation
- snapshot quotes
- historical candles
- deterministic price-action and volume features
- data validation
- `--analyze` and `--candles` commands

**Phase 3** adds deterministic market interpretation and an **advisory** trade-proposal engine (`--evaluate`). See [Phase 3](#phase-3-interpretation--advisory-proposals) below.

**Nothing places orders.** The code contains no `placeOrder()` call, and a test enforces that. Leave the TWS **Read-Only API** setting enabled. A proposal is advisory until the risk engine approves it, and even then no order is sent.

## Setup

1. Install ibapi 10.50.2 from the TWS API folder. The PyPI `ibapi` package is outdated.
   ```
   cd "C:\TWS API\source\pythonclient"
   python -m pip install .
   ```
2. Install the dependencies: `python -m pip install -r requirements.txt`. This installs `pytest` and `tzdata`. **`tzdata` is required on Windows** for exchange time zones.
3. In TWS (paper login), go to *Global Configuration → API → Settings*. Enable socket clients and set port **7497**.
4. **Market data for paper accounts:** the paper account shares the market data subscriptions of your **live** username. It receives **no data** while that live username is logged in somewhere else (IBKR Mobile, Client Portal, or another TWS). That shows up as IBKR error 10197 "competing live session" or 162 "connected from a different IP address". Log out of the other session first.

## Run (from this folder)

```
python main.py                                  # verification cycle + dashboard
python main.py --watch 30                       # refresh every 30s; Ctrl+C disconnects cleanly
python main.py --analyze AAPL                   # analysis report (daily bars)
python main.py --analyze SPY --timeframe 1h     # analysis on another bar size
python main.py --candles AAPL --timeframe 5m    # recent candles (default 20)
python main.py --candles AAPL --timeframe 1m --count 50
python main.py --analyze AAPL --data-type delayed   # live|frozen|delayed|delayed-frozen
python main.py --analyze AAPL --refresh         # bypass candle caches
python main.py --evaluate AAPL                  # Phase 3 advisory evaluation (never an order)
python -m pytest                                # test suite (no TWS needed)
```

Timeframes: `1m`, `5m`, `15m`, `1h`, `1d`.

Exit codes:
- `0`: SAFE, or the candles are usable for analysis
- `1`: SAFE_MODE, or not connected or verified
- `2`: unsafe config refused
- `3`: the candles are invalid, stale or unavailable for analysis
- `130`: Ctrl+C

`--analyze` and `--candles` first run the full Phase 1 check. They refuse to run unless the **paper** connection is verified. They are read-only, so other SAFE_MODE reasons don't block them, but those reasons are printed.

## Two separate money concepts

| | Source | Used for sizing? |
|---|---|---|
| **IBKR ACCOUNT** (net liquidation, cash, buying power) | TWS | **Never.** Display and reconciliation only. |
| **BOT STRATEGY** (equity, available capital, peak, drawdown) | `journal/data/bot_ledger.json` | **Yes.** It is the only input to the risk engine. |

- The BOT ledger starts at **$800.00**.
- Max risk per trade is 2% of BOT equity, which is **$16** initially.
- Max drawdown is 15% from **peak** BOT equity, which is **$120** initially.
- All money math uses `Decimal`.

## Market-data layer (Phase 2)

```
market_data/models.py       Bar, Quote, ContractInfo, Timeframe, MarketDataType, TradingSession
market_data/contracts.py    contract validation incl. leveraged/inverse/ETN detection
market_data/sessions.py     exchange calendar from IBKR liquidHours (time-zone aware)
market_data/validation.py   malformed-bar, stale-data and quote checks -> MarketDataCheck
market_data/features.py     candle geometry, % change, ATR, MA20/50, VWAP, RVOL (pure, Decimal)
market_data/candle_store.py CSV cache of validated candles (atomic, checksummed)
market_data/health.py       per-ticker vs widespread failure tracking, diagnostic hints
market_data/market_data.py  MarketDataService: IBKR requests, pacing, caching
analysis/market_report.py   descriptive trend + support/resistance ZONES (no signals)
analysis/formatting.py      --analyze / --candles output
```

**How the work is split:**
- `market_data/` fetches, validates and computes.
- `analysis/` describes.
- `strategy/` is still empty.
- Every feature (ATR, moving averages, VWAP, RVOL, candle geometry) is calculated **only** in `features.py`. A future AI layer must consume these values, never recompute them.

### Market data types

- **Requested type:** the default is `DELAYED_FROZEN`. IBKR upgrades it to LIVE when you're subscribed, gives frozen data when the market is closed, and delayed data otherwise.
- **Labelled type:** the type IBKR **actually delivered** is shown on every quote, report and check.
- **`ALLOW_DELAYED_DATA_FOR_TRADING = False` and `ALLOW_FROZEN_DATA_FOR_TRADING = False`:** delayed or frozen data can be used for analysis and display, but **never authorizes a trade**. Both settings are enforced in `validate_config()`.

### Validation

- **Bars:** a series is rejected if any bar has a missing, non-finite, zero or negative open/high/low/close; a high below the low; a high or low that doesn't contain the open and close; missing or negative volume; a missing, naive, future or implausible timestamp; or duplicate or out-of-order timestamps. Indicators are only ever computed on fully validated series.
- **Completed vs in-progress bars:** each series is split into **completed** bars (`BarSeries.bars`) and at most one **in-progress** bar (`BarSeries.in_progress`).
  - ATR, the moving averages, VWAP, relative volume, support/resistance and the previous close use completed bars only.
  - The forming bar is shown separately and never replaces the latest completed candle.
  - The split is redone at the current time, so a bar served from cache moves to completed once it finishes.
- **Staleness** is judged on completed bars against the exchange session calendar:
  - **Daily:** the latest completed candle must be the most recent **completed** regular session. While today's session is open, yesterday's candle is current, and today's forming candle is never required. For example, at 09:32 ET on 2026-10-08 the latest completed daily candle is 2026-10-07, and it is usable for analysis.
  - **Intraday, market open:** the last completed bar must be no older than 2 × the bar size + 120 s.
  - **Intraday, market closed:** the bars must reach the end of the last session.
  - **Quotes:** a live quote must be no more than 60 s old (25 min for delayed).
  - Trading authorization is judged separately and still requires live, current data.
- **IBKR error 2188:** without a live subscription, IBKR returns **no bars from the current session**. Intraday candles therefore stop at the previous session and are correctly marked stale while the market is open, with a hint explaining why. Daily analysis on completed sessions is unaffected.
- **Quotes:** a quote must have a current price, must not be crossed (bid above ask), and must report its market-data type.
- **Per-symbol verdict** (`MarketDataCheck`) has three **independent** parts. There is no combined "data valid" flag:
  - **`historical`**: whether each timeframe's candles are valid and current.
    - `usable_for_analysis` is true when the candles are valid and reach the expected session.
    - When the market is closed, that means the most recent completed regular session.
    - `latest_completed_session` is reported.
  - **`quote`**: `available`, `valid`, `stale` and `data_type`. These are reported separately. A missing quote or an UNKNOWN data type never invalidates valid candles.
  - **trading**: `usable_for_trading` plus `trading_blockers`. It requires all of the following:
    - a valid contract
    - valid, current candles
    - an available, valid, current **LIVE** quote
    - the regular market being open

    Example reason: `regular market closed / live quote unavailable`.
- **Risk engine:** it rejects any proposal without a recent check for the same symbol that passes every part. The rejection reasons are `MARKET_DATA_UNAVAILABLE`, `MARKET_DATA_INVALID`, `MARKET_DATA_STALE` and `MARKET_DATA_DELAYED`. Valid candles alone never authorize a trade.
- **Exit codes:** `--analyze` and `--candles` exit with `0` when the candles are usable for analysis, even if the quote is unavailable or the market is closed.

### Per-ticker vs global SAFE_MODE

- **Per-ticker:** a failure for one ticker (unknown symbol, no subscription, malformed bars, rejected contract) makes **only that ticker** unusable. IBKR errors tied to a market-data request are routed to that request and never trigger global SAFE_MODE.
- **Global SAFE_MODE** is reserved for system-level failures:
  - lost connection
  - failed account verification
  - corrupted ledger
  - uncertain execution or account state
  - **widespread** market-data failure: system-level failures (timeouts, data-service errors, competing session) for 3 or more distinct symbols within 5 minutes, with no success since

### Allowed instruments

- **Instrument policy:** the same rules apply in the risk engine and in contract validation (`risk/instrument_policy.py`): `STK` only, USD only, US listing venues.
- **IBKR `stockType`:** `COMMON`, `ETF`, `ADR` and `REIT` are allowed. `ETN`, preferred shares, warrants, closed-end funds, MLPs and similar are rejected.
- **Leveraged and inverse products are rejected.** Detection is layered:
  - `stockType`
  - an explicit deny list
  - long-name patterns (`2X`, `3X`, `ULTRA`, `ULTRASHORT`, `INVERSE`, `BULL`, `BEAR`, `PROSHARES SHORT`, `VIX`, …)
  - IBKR category, subcategory, industry and fund metadata

  The patterns deliberately avoid false positives such as "Ultra-Short Income" or "Low Volatility" funds.

### Candle storage

- **In memory:** a short-lived cache of active requests, refreshed within about 1 minute while the market is open and 15 minutes while it's closed.
- **On disk:** `market_data/cache/<SYMBOL>/<tf>.csv`, plus a `.meta.json` file with a SHA-256 checksum and the IBKR conId. Only validated bars are written, and writes are atomic. Each row records whether the bar was complete when fetched; in-progress bars are dropped on load so they are always downloaded again.
- **Market closed with a fresh cache:** no download at all.
- **Otherwise:** an **incremental** download covering the gap since the last cached bar, merged and re-validated. Anything else triggers a full reload.
- **Bad cache files:** a corrupt, tampered or mismatched cache is quarantined (`*.corrupt-<timestamp>`) and downloaded again.
- **IBKR pacing limits are respected:** under 60 requests per 10 min, no identical request within 15 s, and at most 5 requests per contract per 2 s.

### Regular trading hours

- `USE_RTH = True` is enforced in Phase 2.
- `TradingSession.EXTENDED` already exists in the interfaces (it adds a cache-file suffix and uses `tradingHours` instead of `liquidHours`), so extended hours can be added later without redesign.

## System states

`INITIALIZING` changes to `SAFE` only when one full verification cycle passes with no SAFE_MODE reasons. If any check fails, the state becomes **`SAFE_MODE`**, and the risk engine then rejects every trade.

SAFE_MODE triggers:
- connection loss or connectivity messages
- reconnect (a fresh verification cycle is required)
- account data failures
- paper verification failure
- an invalid or unreconciled ledger
- BOT drawdown of 15% or more
- any execution-related IBKR message
- widespread market-data failure

## Paper-account verification

All six checks must pass:
1. The config says paper.
2. The port is a paper port (7497 or 4002), not a live port.
3. There is exactly one managed account.
4. The account ID has a `DU` or `DF` prefix.
5. The account matches `EXPECTED_ACCOUNT_ID`, if set.
6. The account summary reports the same account.

## Ownership model and ledger

- **Ownership:**
  - Every future bot order will carry an `orderRef` starting with `TBOT-`.
  - Bot positions are keyed by conId in the ledger.
  - Anything else at IBKR is **EXTERNAL** and excluded from BOT equity.
- **Startup reconciliation:** any mismatch between the ledger and IBKR triggers SAFE_MODE.
- **Ledger file:** `journal/data/bot_ledger.json` is written atomically and protected by a checksum. **Never edit it by hand.**

## Phase 3: interpretation + advisory proposals

All of Phase 3 is deterministic Python. No language model is used. A future Claude reasoning layer (fundamentals, news, earnings, catalysts, macro) will interpret these values, but it will **never** override the risk rules.

```
analysis/market_structure.py      HH/HL/LH/LL, continuation, consolidation, compression,
                                  breakout/breakdown (+failed), distances in ATR, ATR-normalized moves
analysis/candle_interpretation.py shape detection, then CONTEXT scoring (zone, trend, RVOL, regime)
analysis/volume_analysis.py       buyer/seller pressure; breakout quality CONFIRMED/WEAK/FAILED/NOT_PRESENT
analysis/relative_strength.py     price RS vs SPY, QQQ (where relevant), sector ETF; 1/5/20 sessions
analysis/regime.py                RISK_ON / NEUTRAL / CHOPPY / DEFENSIVE / RISK_OFF from SPY+QQQ
analysis/setup_context.py         per-timeframe FRESH/STALE/UNAVAILABLE views -> SetupContext
analysis/evaluation_report.py     --evaluate output
strategy/models.py                TradeProposal (advisory), modes, setup types, policies
strategy/entry_logic.py           setup detection + entry / maximum entry (never chase)
strategy/stop_logic.py            technical invalidation + ATR sanity (0.5-3 ATR)
strategy/target_logic.py          next resistance / ATR projection / measured move (sources named)
strategy/no_trade_rules.py        analytical NO TRADE reasons vs execution blockers
strategy/opportunity_score.py     0-100 score, missing inputs = 0 points (never redistributed)
strategy/proposal_engine.py       orchestration; risk-engine hand-off for BUY only
```

**The two modes appear in every output:**
- **ANALYSIS_ONLY:** any execution blocker forces this mode. Blockers include:
  - a quote that is delayed, frozen, unavailable, stale or of unknown type
  - a market that is closed
  - a stale required timeframe
  - missing benchmark (SPY) data
  - a system that isn't SAFE

  In this mode the setup is still described and scored, but the action is capped at WATCH and the risk engine is **NOT EVALUATED**.
- **EXECUTION_READY:** there are no blockers at all. Only in this mode can the action become BUY.

**Required timeframes depend on the setup:**

| Horizon (setups) | Required | Preferred | Optional |
|---|---|---|---|
| SHORT_TERM (breakout, mean reversion) | 1d, 1h, 15m, 5m | | 1m |
| SWING (pullback, reversal) | 1d, 1h, 15m | | 5m |
| POSITION (trend continuation) | 1d, 1h | 15m | 5m |
| INTRADAY (reserved) | 1h, 15m, 5m | | 1m |

A missing **required** timeframe blocks execution. A missing **optional** or **preferred** timeframe only lowers confidence. Stale timeframes are described with a label but are never used for timing.

**Action decision:**
- **NO TRADE** if any analytical rule fires (for example R:R below **2.0** to Target 1, extended price, hostile regime, structure conflict, weak volume, too little data, an ambiguous setup, or a stop that's too wide) or the score is below 50.
- **WATCH** if the score is 50–74 (65–74 is classed as CONDITIONAL SETUP), the mode is ANALYSIS_ONLY, or the price is outside the entry range (WAIT FOR PULLBACK / AWAITING TRIGGER).
- **BUY** requires all of: a score of **75 or more**, EXECUTION_READY, no NO TRADE rule, and the price in the entry range. The risk engine then sizes the trade with the fixed formula at the **maximum entry** and decides. A rejection turns the proposal into NO TRADE (`RISK_VETO`).

**Risk engine:** it now also rejects any request not explicitly marked `EXECUTION_READY` (`NOT_EXECUTION_READY`). Its veto is final.

**Expensive stocks:** a price above BOT capital is **not** an analytical rule. An affordability warning is shown, and whole-share sizing is left to the risk engine, which rejects it. Fractional shares may be evaluated later.

Every evaluation is written to the journal (`journal/data/events.jsonl`, event `evaluation`).

## Phase 3.5: backtesting and strategy validation (simulation only)

The backtester runs the **unchanged** Phase 3 code. At each decision time T it builds the same inputs `--evaluate` builds, from data visible at T only, and calls `build_context` → `proposal_engine.evaluate` → `proposal_engine.risk_review` → `RiskEngine`. There is no separate backtest strategy, and a test confirms the functions are the same objects.

```
python main.py --backtest AAPL --start 2024-01-01 --end 2026-10-09
python main.py --backtest-universe backtest/universes/default.txt --start 2024-01-01
python main.py --backtest-universe backtest/universes/default.txt --tier 2 --start 2018-01-01
python main.py --backtest-universe backtest/universes/default.txt --download-only   # fetch history first
python main.py --backtest AAPL --no-download        # offline: cached data only
```

**Data:**
- History is downloaded once, read-only (TWS paper connection verified first), into `backtest/data/<SYM>/<tf>.csv`.
- It is validated and checksummed, and downloads are resumable.
- After that, backtests run **offline**.

**Tiers (never combined):**
- **Tier 1:** 2024 to today with 1d, 1h, 15m and 5m data, giving the exact Phase 3 inputs. Runs a live-policy portfolio plus shadow trades.
- **Tier 2:** 2018 to today with 1d and 1h data, giving research shadow trades only. With no 15m/5m data, intraday shares of the score are 0 (not redistributed), so Tier 2 scores are lower and not comparable to Tier 1.

**Runs reported separately:**
- Cadence 15m is the primary run and 1h is a sensitivity run.
- Exit **Variant A** is Target 1 + hard stop + dataset-end close.
- Exit **Variant B** adds a time exit: short-term after 5 sessions, swing after 15, position after 40 (8 weeks).
- Target 2 is informational only.
- Tier 1 therefore produces 4 runs (2 cadences × 2 exit variants) and Tier 2 produces 2.

**Look-ahead prevention:**
- `backtest/asof.py` is the only data path.
- An intraday bar is visible at T only if start + length ≤ T. A daily bar is visible only after its session closed, so today's forming candle is never visible.
- Windows are the same length a live `--evaluate` fetches.
- Phase 2 validation and freshness run with `now=T`.
- Entries can only fill on bars starting at or after T.
- Tests prove it in three ways:
  - all bars after T are replaced with extreme values, and the decision is byte-identical
  - history truncated at T gives identical decisions
  - every entry timestamp is at or after its signal timestamp

**Simulated quotes:**
- Backtest quotes are built from the last bar completed by T and carry `simulated=True`, `source=BACKTEST` and type `SIMULATED_BACKTEST`.
- Only a risk engine created with `Environment.BACKTEST` (see `environment.py`) accepts them.
- The real bot's risk engine defaults to `LIVE` and rejects them with `MARKET_DATA_SIMULATED`.
- The environment is chosen in code, not in config.

**Execution model:**
- **Entry:** a marketable limit at `maximum_entry`, valid until the next decision time.
- **Stop:** a hard stop. A gap through it fills at the open (worse).
- **Target 1:** a limit. A gap through it fills at the open (better).
- **Stop and target in the same bar:** finer bars are used if available; otherwise the **stop** is assumed.
- **Costs:** IBKR Fixed commission ($0.005/share, $1 minimum, 1% maximum), 2 bps slippage and a 1 bp spread, all configurable in `config.py`. Gross P/L, commission, spread, slippage and net P/L are reported separately.

**Portfolio:**
- $800, 2% risk, cash only, whole shares, one position per ticker.
- Candidates at the same T compete for the same cash. They are ranked by score, then R:R, then sector diversification, then ticker.
- Each candidate goes through the same `risk_review` used by `--evaluate`, with cash already reserved by earlier picks deducted.
- At 15% drawdown, the circuit breaker **permanently** stops new entries, matching the live bot.

**Shadow trades:**
- Every evaluated setup with a valid entry, stop and Target 1 is simulated, whatever its action, without touching capital.
- They are used to validate score bands, setup types and NO TRADE reasons.
- Shadow trades after the breaker are labelled `POST_CIRCUIT_BREAKER` and reported separately.

**Results:** written to `backtest/results/<timestamp>_<run_id>/` (`trades.csv`, `shadow_trades.csv`, `equity_curve.csv`, `metrics.json`, `run_manifest.json`). The run_id is a hash of all inputs.

**Survivorship bias:** the default universe is chosen with hindsight and dominated by recent winners. Long-only results are likely **optimistic**, and every report says so. INTC, PFE, BA, DIS and NKE are included as control names.
