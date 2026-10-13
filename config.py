"""
Hard safety configuration for the trading bot.

These values are deliberately plain module constants. They are NOT read from
environment variables, CLI flags, or files, so nothing at runtime (including
any future AI/strategy component) can loosen them. Changing them requires an
edit to this file, and validate_config() runs at startup to refuse any
unsafe combination.
"""

from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Hard safety settings
# ---------------------------------------------------------------------------
TRADING_MODE = "PAPER"
LIVE_TRADING_ALLOWED = False
MARGIN_ALLOWED = False
SHORT_SELLING_ALLOWED = False  # Long-only: shorting requires margin.

# Order placement is not implemented in Phase 1. The execution layer checks
# this flag in addition to having no placeOrder() call at all.
ORDER_PLACEMENT_ENABLED = False

# Strategy capital: the bot behaves as if this is ALL the money it has.
# The IBKR paper account balance (~$1M simulated) is never used for sizing.
BOT_CAPITAL = Decimal("800.00")

MAX_RISK_PER_TRADE = Decimal("0.02")        # 2% of BOT strategy equity
MAX_PORTFOLIO_DRAWDOWN = Decimal("0.15")    # 15% from PEAK BOT strategy equity

# ---------------------------------------------------------------------------
# IBKR connection
# ---------------------------------------------------------------------------
IBKR_HOST = "127.0.0.1"
IBKR_PORT = 7497          # TWS paper trading port
IBKR_CLIENT_ID = 1

# Known IBKR ports. 7497 = TWS paper, 4002 = Gateway paper,
# 7496 = TWS live, 4001 = Gateway live.
PAPER_PORTS = frozenset({7497, 4002})
LIVE_PORTS = frozenset({7496, 4001})

# Paper accounts at IBKR use these prefixes ("DU" individual, "DF" advisor).
PAPER_ACCOUNT_PREFIXES = ("DU", "DF")

# Optional: pin the exact paper account ID (e.g. "DU1234567"). When set, the
# connected account must match exactly or the bot enters SAFE_MODE.
EXPECTED_ACCOUNT_ID = None

CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 10.0

# ---------------------------------------------------------------------------
# Allowed / prohibited instruments
# ---------------------------------------------------------------------------
# IBKR models both US stocks and US-listed ETFs as secType "STK".
ALLOWED_SEC_TYPES = frozenset({"STK"})
ALLOWED_CURRENCIES = frozenset({"USD"})
PROHIBITED_SEC_TYPES = frozenset({
    "OPT", "FOP", "FUT", "CONTFUT", "CASH", "CRYPTO", "CFD", "WAR",
    "IOPT", "BOND", "FUND", "CMDTY", "BAG", "IND", "NEWS", "SSF",
})
# US listing venues accepted as exchange / primaryExchange.
US_EXCHANGES = frozenset({
    "NYSE", "NASDAQ", "ISLAND", "ARCA", "NYSEARCA", "AMEX", "NYSEAMERICAN",
    "BATS", "BYX", "EDGX", "EDGEA", "IEX", "CBOE", "PSX", "BEX", "LTSE",
    "MEMX", "PEARL",
})
SMART_EXCHANGE = "SMART"

# ---------------------------------------------------------------------------
# Ownership tagging
# ---------------------------------------------------------------------------
# Every order the bot ever creates will carry an IBKR orderRef starting with
# this prefix. Orders without it are treated as manual / external.
BOT_ORDER_REF_PREFIX = "TBOT-"

# IBKR stockType values for STK contracts.
ALLOWED_STOCK_TYPES = frozenset({"COMMON", "ETF", "ADR", "REIT"})
PROHIBITED_STOCK_TYPES = frozenset({
    "ETN", "WARRANT", "RIGHT", "UNIT", "PREFERRED", "CLOSED-END FUND", "MLP",
    "NY REG SHRS", "GDR", "OPEN-END FUND", "TRACKING STK", "LTD PART",
})

# Leveraged / inverse product detection (layered: stockType, deny list,
# long-name keywords, IBKR category/subcategory/fund metadata).
LEVERAGED_DENY_LIST = frozenset({
    "TQQQ", "SQQQ", "QLD", "QID", "PSQ", "SSO", "SDS", "UPRO", "SPXU", "SPXL",
    "SPXS", "SH", "SPDN", "DDM", "DXD", "UDOW", "SDOW", "DOG", "MVV", "MZZ",
    "UWM", "TWM", "TNA", "TZA", "URTY", "SRTY", "RWM", "SOXL", "SOXS", "TECL",
    "TECS", "FAS", "FAZ", "LABU", "LABD", "ERX", "ERY", "GUSH", "DRIP", "NUGT",
    "DUST", "JNUG", "JDST", "YINN", "YANG", "FNGU", "FNGD", "BULZ", "BERZ",
    "WEBL", "WEBS", "DPST", "CURE", "TMF", "TMV", "TBT", "UBT", "BOIL", "KOLD",
    "UCO", "SCO", "AGQ", "ZSL", "UGL", "GLL", "UVXY", "SVXY", "UVIX", "SVIX",
    "VXX", "VIXY", "VIXM", "TSLL", "TSLQ", "TSLS", "NVDL", "NVDU", "NVDQ",
    "NVDD", "CONL", "MSTU", "MSTX", "MSTZ", "BITX", "BITI", "SBIT", "ETHU",
    "AAPU", "AAPD", "AMZU", "AMZD", "GGLL", "METU", "MSFU",
})
# Regexes matched (case-insensitive) against long name and IBKR metadata.
# "ULTRA" excludes "Ultra-Short" (short-duration bond funds, not leveraged).
LEVERAGED_NAME_PATTERNS = (
    r"\b\d+(\.\d+)?\s*X\b",          # 2X, 3x, 1.5X, -1X
    r"\bULTRA\b(?!-)",               # ProShares Ultra ...
    r"\bULTRAPRO\b",
    r"\bULTRASHORT\b",
    r"\bINVERSE\b",
    r"\bLEVERAGED?\b",
    r"\bBULL\b",
    r"\bBEAR\b",
    r"\bPROSHARES SHORT\b",
    r"\bDAILY TARGET\b",
    r"\bDIREXION DAILY\b",
    r"\bVIX\b",                      # VIX-futures products (not "Low Volatility")
)

# ---------------------------------------------------------------------------
# Market data (Phase 2) - read-only, never authorizes a trade on its own
# ---------------------------------------------------------------------------
# Requested IBKR market data type. DELAYED_FROZEN returns LIVE data when
# subscribed, frozen when the market is closed, delayed otherwise. The type
# actually delivered is always reported and labelled.
MARKET_DATA_TYPE = "DELAYED_FROZEN"   # LIVE | FROZEN | DELAYED | DELAYED_FROZEN
ALLOW_DELAYED_DATA_FOR_TRADING = False
ALLOW_FROZEN_DATA_FOR_TRADING = False
USE_RTH = True                        # Phase 2: regular trading hours only

ATR_PERIOD = 14
MA_FAST_PERIOD = 20
MA_SLOW_PERIOD = 50
TREND_SLOPE_LOOKBACK = 5
RVOL_DAILY_LOOKBACK = 20              # daily: vs previous 20 sessions
RVOL_MIN_SESSIONS = 5                 # intraday: same time-of-day samples
RVOL_MAX_SESSIONS = 20
SWING_WINDOW = 3                      # bars each side for swing high/low
SR_ZONE_ATR_MULT = Decimal("0.5")     # cluster pivots within 0.5 x ATR
SR_MIN_ZONE_ATR_MULT = Decimal("0.25")
SR_LOOKBACK_BARS = 120
SR_MAX_ZONES = 3

LIVE_QUOTE_MAX_AGE_SECONDS = 60
DELAYED_QUOTE_MAX_AGE_SECONDS = 25 * 60
BAR_STALENESS_GRACE_SECONDS = 120
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 120
MARKET_DATA_CHECK_MAX_AGE_SECONDS = 60   # risk engine: check must be recent

CONTRACT_TIMEOUT_SECONDS = 10.0
QUOTE_TIMEOUT_SECONDS = 15.0
HISTORICAL_TIMEOUT_SECONDS = 60.0

# IBKR historical pacing: 60 req / 10 min, no identical req within 15 s,
# max 6 req per contract per 2 s. We stay under each limit.
HIST_PACING_WINDOW_SECONDS = 600
HIST_PACING_MAX_REQUESTS = 50
HIST_IDENTICAL_MIN_INTERVAL_SECONDS = 16
HIST_SAME_CONTRACT_WINDOW_SECONDS = 2
HIST_SAME_CONTRACT_MAX = 5
HIST_MAX_PACING_WAIT_SECONDS = 90

# Global SAFE_MODE for *widespread* market-data failure: this many distinct
# symbols failing for system-level reasons within the window, with no
# successful request since.
MD_WIDESPREAD_MIN_SYMBOLS = 3
MD_WIDESPREAD_WINDOW_SECONDS = 300

# ---------------------------------------------------------------------------
# Phase 3 - deterministic interpretation + advisory trade proposals
# (no orders; a proposal is advisory until the risk engine approves it)
# ---------------------------------------------------------------------------
EVALUATION_TIMEFRAMES = ("1d", "1h", "15m", "5m", "1m")
BENCHMARK_SPY = "SPY"
BENCHMARK_QQQ = "QQQ"

# Required / preferred / optional FRESH timeframes per setup horizon.
# Missing REQUIRED -> execution blocked (ANALYSIS_ONLY).
# Missing PREFERRED/OPTIONAL -> lower confidence, setup stays valid.
TIMEFRAME_POLICY = {
    "SHORT_TERM": {"required": ("1d", "1h", "15m", "5m"), "preferred": (), "optional": ("1m",)},
    "SWING":      {"required": ("1d", "1h", "15m"), "preferred": (), "optional": ("5m",)},
    "POSITION":   {"required": ("1d", "1h"), "preferred": ("15m",), "optional": ("5m",)},
    "INTRADAY":   {"required": ("1h", "15m", "5m"), "preferred": (), "optional": ("1m",)},
}
# Setup type -> horizon
SETUP_HORIZON = {
    "BREAKOUT": "SHORT_TERM",
    "PULLBACK_TO_SUPPORT": "SWING",
    "TREND_CONTINUATION": "POSITION",
    "REVERSAL_FROM_SUPPORT": "SWING",
    "MEAN_REVERSION": "SHORT_TERM",
}
HOLDING_PERIOD = {
    "SHORT_TERM": "1-5 trading days",
    "SWING": "3-15 trading days",
    "POSITION": "2-8 weeks",
    "INTRADAY": "intraday to 2 days",
}

# Higher timeframes carry more weight in multi-timeframe alignment.
TIMEFRAME_WEIGHTS = {"1d": Decimal("0.40"), "1h": Decimal("0.25"), "15m": Decimal("0.15"),
                     "5m": Decimal("0.12"), "1m": Decimal("0.08")}

# Market structure
STRUCTURE_LOOKBACK_BARS = 120
CONSOLIDATION_BARS = 10
CONSOLIDATION_MAX_ATR = Decimal("2.5")
COMPRESSION_RECENT_BARS = 5
COMPRESSION_BASE_BARS = 20
COMPRESSION_RATIO = Decimal("0.7")
BREAK_LOOKBACK_BARS = 5           # breakout / failed-breakout window
ZONE_PROXIMITY_ATR = Decimal("0.5")

# Candles / volume / breakouts
REJECTION_WICK_BODY_MULT = Decimal("2")
REJECTION_WICK_RANGE_FRAC = Decimal("0.4")
MOMENTUM_BODY_RANGE_FRAC = Decimal("0.7")
MOMENTUM_RANGE_ATR = Decimal("1.2")
INDECISION_BODY_RANGE_FRAC = Decimal("0.1")
EXHAUSTION_RANGE_ATR = Decimal("1.5")
EXHAUSTION_EXTENSION_ATR = Decimal("2")
EXHAUSTION_RVOL = Decimal("2.5")
CANDLE_CONTEXT_RVOL = Decimal("1.2")
PRESSURE_LOOKBACK_BARS = 5
PRESSURE_STRONG = Decimal("0.6")
PRESSURE_MODERATE = Decimal("0.3")
BREAKOUT_CLOSE_BEYOND_ATR = Decimal("0.25")
BREAKOUT_MIN_RVOL = Decimal("1.5")
BREAKOUT_MIN_RANGE_ATR = Decimal("1.0")
WEAK_VOLUME_RVOL = Decimal("0.8")

# Relative strength (price, not RSI): excess return vs benchmark
RS_PERIOD_WEIGHTS = {1: Decimal("0.2"), 5: Decimal("0.3"), 20: Decimal("0.5")}
RS_THRESHOLD = Decimal("0.01")    # +/-1% weighted excess return
# Sector ETFs from IBKR contract metadata (category checked before industry)
SECTOR_ETF_BY_CATEGORY = {
    "REIT": "XLRE", "PHARMACEUTICAL": "XLV", "HEALTHCARE": "XLV", "HEALTH CARE": "XLV",
    "BIOTECHNOLOGY": "XLV", "MEDICAL": "XLV",
}
SECTOR_ETF_BY_INDUSTRY = {
    "TECHNOLOGY": "XLK", "FINANCIAL": "XLF", "ENERGY": "XLE", "INDUSTRIAL": "XLI",
    "CONSUMER, CYCLICAL": "XLY", "CONSUMER, NON-CYCLICAL": "XLP", "UTILITIES": "XLU",
    "BASIC MATERIALS": "XLB", "COMMUNICATIONS": "XLC",
}
QQQ_RELEVANT_SECTORS = frozenset({"XLK", "XLC"})

# Market regime (SPY/QQQ daily)
REGIME_CHOP_LOOKBACK = 20
REGIME_CHOP_CROSSES = 4
REGIME_QQQ_LAG = Decimal("-0.03")  # QQQ 20d return - SPY 20d return

# Entry / stop / target
ENTRY_BUFFER_ATR = Decimal("0.1")
MAX_ENTRY_ATR = Decimal("0.25")       # never chase beyond entry + 0.25 ATR
STOP_BUFFER_ATR = Decimal("0.1")
STOP_MIN_ATR = Decimal("0.5")
STOP_MAX_ATR = Decimal("3.0")
TARGET_BUFFER_ATR = Decimal("0.05")
TARGET_ATR_PROJECTION = Decimal("2.0")
EXTENDED_FROM_MA20_ATR = Decimal("2.0")
MEAN_REVERSION_MIN_ATR_BELOW_MA20 = Decimal("2.5")
STRONG_SUPPORT_TOUCHES = 3

MIN_REWARD_RISK = Decimal("2.0")      # measured to Target 1; below = NO TRADE

# Opportunity score (0-100)
SCORE_WEIGHTS = {
    "technical_structure": 25, "volume_confirmation": 15, "multi_timeframe_alignment": 15,
    "support_resistance_quality": 10, "relative_strength": 10,
    "market_regime_alignment": 10, "reward_risk": 15,
}
SCORE_WATCH_MIN = 50
SCORE_CONDITIONAL_MIN = 65
SCORE_STRONG_MIN = 75
SCORE_EXCEPTIONAL_MIN = 85
BUY_MIN_SCORE = 75                    # BUY also needs EXECUTION_READY + risk approval

# ---------------------------------------------------------------------------
# Phase 3.5 - backtesting (simulation only; never places orders)
# ---------------------------------------------------------------------------
# Tier 1: exact Phase 3 data (2024+). Tier 2: daily + hourly research (2018+),
# shadow trades only. Tier statistics are NEVER combined.
BACKTEST_TIERS = {
    1: {"timeframes": ("1d", "1h", "15m", "5m"), "default_start": "2024-01-01",
        "cadences": ("15m", "1h"), "portfolio": True, "sim_timeframes": ("5m", "15m", "1h")},
    2: {"timeframes": ("1d", "1h"), "default_start": "2018-01-01",
        "cadences": ("1h",), "portfolio": False, "sim_timeframes": ("1h",)},
}
# As-of-T window per timeframe, matching what a live --evaluate fetches.
BACKTEST_WINDOW_BARS = {"1d": 252, "1h": 294, "15m": 520, "5m": 780}
# Calendar days of history loaded before the start date (indicator warm-up).
BACKTEST_WARMUP_DAYS = {"1d": 400, "1h": 70, "15m": 35, "5m": 20}
# IBKR durations tried per download request (largest first, then fallback).
BACKTEST_DOWNLOAD_CHUNKS = {"1d": ("10 Y", "1 Y"), "1h": ("1 Y", "1 M"),
                            "15m": ("2 M", "2 W"), "5m": ("1 M", "1 W")}
BACKTEST_EXIT_VARIANTS = ("A", "B")   # A: T1/stop/data-end; B: + time exit
BACKTEST_TIME_EXIT_SESSIONS = {"SHORT_TERM": 5, "SWING": 15, "POSITION": 40, "INTRADAY": 1}
BACKTEST_WORKERS = 4
BACKTEST_MIN_SAMPLE = 30              # smaller groups flagged INSUFFICIENT SAMPLE

# Cost model (IBKR Fixed assumption) - configurable
BT_COMMISSION_PER_SHARE = Decimal("0.005")
BT_COMMISSION_MIN = Decimal("1.00")
BT_COMMISSION_MAX_PCT = Decimal("0.01")
BT_SLIPPAGE_BPS = Decimal("2")
BT_SPREAD_BPS = Decimal("1")
BT_MIN_HALF_SPREAD = Decimal("0.005")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
JOURNAL_DIR = BASE_DIR / "journal" / "data"
LEDGER_PATH = JOURNAL_DIR / "bot_ledger.json"
JOURNAL_PATH = JOURNAL_DIR / "events.jsonl"
CANDLE_CACHE_DIR = BASE_DIR / "market_data" / "cache"
BACKTEST_DIR = BASE_DIR / "backtest"
BACKTEST_DATA_DIR = BACKTEST_DIR / "data"
BACKTEST_RESULTS_DIR = BACKTEST_DIR / "results"
BACKTEST_SIGNAL_CACHE_DIR = BACKTEST_DIR / "cache"
BACKTEST_DEFAULT_UNIVERSE = BACKTEST_DIR / "universes" / "default.txt"


class ConfigError(Exception):
    """Raised when the configuration violates a hard safety rule."""


def validate_config(cfg=None):
    """Refuse to start if any hard safety setting is unsafe.

    `cfg` defaults to this module; tests pass a namespace with overrides.
    Returns None on success, raises ConfigError listing every violation.
    """
    import sys
    cfg = cfg or sys.modules[__name__]
    problems = []

    if cfg.TRADING_MODE != "PAPER":
        problems.append(f"TRADING_MODE must be 'PAPER', got {cfg.TRADING_MODE!r}")
    if cfg.LIVE_TRADING_ALLOWED is not False:
        problems.append("LIVE_TRADING_ALLOWED must be False")
    if cfg.MARGIN_ALLOWED is not False:
        problems.append("MARGIN_ALLOWED must be False")
    if cfg.SHORT_SELLING_ALLOWED is not False:
        problems.append("SHORT_SELLING_ALLOWED must be False (long-only)")
    if cfg.ORDER_PLACEMENT_ENABLED is not False:
        problems.append("ORDER_PLACEMENT_ENABLED must be False in Phase 1")
    if cfg.IBKR_PORT in cfg.LIVE_PORTS:
        problems.append(f"IBKR_PORT {cfg.IBKR_PORT} is a LIVE trading port")
    if cfg.IBKR_PORT not in cfg.PAPER_PORTS:
        problems.append(f"IBKR_PORT {cfg.IBKR_PORT} is not a known paper port")
    if cfg.IBKR_HOST not in ("127.0.0.1", "localhost"):
        problems.append(f"IBKR_HOST must be local, got {cfg.IBKR_HOST!r}")

    for name in ("BOT_CAPITAL", "MAX_RISK_PER_TRADE", "MAX_PORTFOLIO_DRAWDOWN"):
        if not isinstance(getattr(cfg, name), Decimal):
            problems.append(f"{name} must be a Decimal")
    if not problems:
        if not cfg.BOT_CAPITAL > 0:
            problems.append("BOT_CAPITAL must be positive")
        if not Decimal("0") < cfg.MAX_RISK_PER_TRADE <= Decimal("0.02"):
            problems.append("MAX_RISK_PER_TRADE must be in (0, 0.02]")
        if not Decimal("0") < cfg.MAX_PORTFOLIO_DRAWDOWN <= Decimal("0.15"):
            problems.append("MAX_PORTFOLIO_DRAWDOWN must be in (0, 0.15]")

    if not cfg.ALLOWED_SEC_TYPES.isdisjoint(cfg.PROHIBITED_SEC_TYPES):
        problems.append("ALLOWED_SEC_TYPES overlaps PROHIBITED_SEC_TYPES")
    if not cfg.BOT_ORDER_REF_PREFIX:
        problems.append("BOT_ORDER_REF_PREFIX must be non-empty")

    # --- market data (Phase 2) ---
    if cfg.MARKET_DATA_TYPE not in ("LIVE", "FROZEN", "DELAYED", "DELAYED_FROZEN"):
        problems.append(f"MARKET_DATA_TYPE invalid: {cfg.MARKET_DATA_TYPE!r}")
    if cfg.ALLOW_DELAYED_DATA_FOR_TRADING is not False:
        problems.append("ALLOW_DELAYED_DATA_FOR_TRADING must be False")
    if cfg.ALLOW_FROZEN_DATA_FOR_TRADING is not False:
        problems.append("ALLOW_FROZEN_DATA_FOR_TRADING must be False")
    if cfg.USE_RTH is not True:
        problems.append("USE_RTH must be True in Phase 2 (extended hours not implemented)")
    if not cfg.ALLOWED_STOCK_TYPES.isdisjoint(cfg.PROHIBITED_STOCK_TYPES):
        problems.append("ALLOWED_STOCK_TYPES overlaps PROHIBITED_STOCK_TYPES")
    if cfg.HIST_PACING_MAX_REQUESTS >= 60 or cfg.HIST_SAME_CONTRACT_MAX >= 6:
        problems.append("historical pacing limits must stay below IBKR limits")
    for name in ("ATR_PERIOD", "MA_FAST_PERIOD", "MA_SLOW_PERIOD", "SWING_WINDOW"):
        if not (isinstance(getattr(cfg, name), int) and getattr(cfg, name) > 0):
            problems.append(f"{name} must be a positive integer")

    # --- Phase 3 ---
    if sum(cfg.SCORE_WEIGHTS.values()) != 100:
        problems.append("SCORE_WEIGHTS must sum to 100")
    if cfg.MIN_REWARD_RISK < Decimal("2.0"):
        problems.append("MIN_REWARD_RISK must be at least 2.0")
    if cfg.BUY_MIN_SCORE < 75:
        problems.append("BUY_MIN_SCORE must be at least 75")
    for setup, horizon in cfg.SETUP_HORIZON.items():
        if horizon not in cfg.TIMEFRAME_POLICY:
            problems.append(f"setup {setup} has unknown horizon {horizon}")
    for horizon, pol in cfg.TIMEFRAME_POLICY.items():
        if not pol["required"]:
            problems.append(f"horizon {horizon} must require at least one timeframe")
        for tf in pol["required"] + pol["preferred"] + pol["optional"]:
            if tf not in cfg.TIMEFRAME_WEIGHTS:
                problems.append(f"horizon {horizon} uses unknown timeframe {tf}")

    if problems:
        raise ConfigError("; ".join(problems))
