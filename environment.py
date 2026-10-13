"""
Execution environment.

LIVE      - the real bot. Execution-grade market data must be a genuine
            IBKR LIVE quote. Simulated quotes are always rejected.
BACKTEST  - historical simulation only. Simulated as-of-T quotes
            (MarketDataType.SIMULATED_BACKTEST, simulated=True,
            source=BACKTEST) are accepted in place of LIVE quotes so the
            unchanged strategy can be tested. Nothing in this environment can
            reach a broker: there is no order code in the project.

The environment is chosen in code when the risk engine / proposal engine is
constructed (default LIVE). It is not a config flag, so it cannot be switched
for the real bot by editing settings.
"""

from enum import Enum


class Environment(str, Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"
