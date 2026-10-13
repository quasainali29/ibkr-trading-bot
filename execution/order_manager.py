"""
Order manager - PHASE 1 PLACEHOLDER.

Order placement is intentionally NOT implemented. This module contains no
call to placeOrder(), and submit() always raises. In a future phase every
bot order will:
  * pass RiskEngine.evaluate() with state SAFE,
  * carry an orderRef starting with config.BOT_ORDER_REF_PREFIX,
  * be written to the ledger's pending_orders BEFORE transmission.
"""


class TradingDisabledError(RuntimeError):
    pass


class OrderManager:
    def __init__(self, cfg):
        self.cfg = cfg

    def submit(self, *args, **kwargs):
        raise TradingDisabledError(
            "Phase 1: order placement is not implemented and is disabled.")

    def cancel(self, *args, **kwargs):
        raise TradingDisabledError(
            "Phase 1: order cancellation is not implemented and is disabled.")
