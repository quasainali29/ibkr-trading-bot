import json
from decimal import Decimal

import pytest

from broker.account import IbkrOpenOrder, IbkrPosition
from portfolio.portfolio_manager import (BotPendingOrder, BotPosition,
                                         LedgerError, LedgerStore,
                                         compute_snapshot, ledger_to_dict,
                                         new_ledger, reconcile, update_peak)

ACCT = "DU1234567"


def bot_position(con_id=756733, symbol="SPY", qty=2, cost="100.00"):
    return BotPosition(con_id=con_id, symbol=symbol, quantity=qty,
                       avg_cost=Decimal(cost), stop_price=Decimal("98.00"),
                       entry_order_ref="TBOT-0001", opened_at="2026-10-06T14:00:00+00:00")


def ib_position(con_id=756733, symbol="SPY", qty="2", account=ACCT):
    return IbkrPosition(account=account, con_id=con_id, symbol=symbol, sec_type="STK",
                        exchange="", primary_exchange="ARCA", currency="USD",
                        quantity=Decimal(qty), avg_cost=Decimal("100"))


def ib_order(ref="", symbol="AAPL", perm_id=1):
    return IbkrOpenOrder(perm_id=perm_id, order_id=perm_id, client_id=0, account=ACCT,
                         con_id=265598, symbol=symbol, sec_type="STK", action="BUY",
                         total_quantity=Decimal("1"), order_type="LMT",
                         limit_price=Decimal("150"), aux_price=None, order_ref=ref)


def ledger_with_position(cfg):
    led = new_ledger(ACCT, cfg.BOT_CAPITAL)
    led.positions = [bot_position()]
    led.cash = Decimal("600.00")  # 800 - 2 * 100
    return led


# --------------------------------------------------------------------------
# Ledger persistence + validation
# --------------------------------------------------------------------------
class TestLedgerStore:
    def test_new_ledger_starts_at_800(self, cfg):
        led = new_ledger(ACCT, cfg.BOT_CAPITAL)
        assert led.cash == Decimal("800.00")
        assert led.peak_equity == Decimal("800.00")

    def test_roundtrip(self, cfg, tmp_path):
        store = LedgerStore(tmp_path / "ledger.json", cfg)
        led = ledger_with_position(cfg)
        led.pending_orders = [BotPendingOrder("TBOT-0002", 1, "QQQ", "BUY", 1, "t")]
        store.save(led)
        loaded = store.load()
        assert loaded.cash == Decimal("600.00")
        assert loaded.positions[0].avg_cost == Decimal("100.00")
        assert loaded.pending_orders[0].order_ref == "TBOT-0002"

    def test_atomic_write_leaves_no_temp_files(self, cfg, tmp_path):
        store = LedgerStore(tmp_path / "ledger.json", cfg)
        store.save(new_ledger(ACCT, cfg.BOT_CAPITAL))
        store.save(new_ledger(ACCT, cfg.BOT_CAPITAL))
        assert [p.name for p in tmp_path.iterdir()] == ["ledger.json"]

    def test_invalid_ledger_is_not_written(self, cfg, tmp_path):
        store = LedgerStore(tmp_path / "ledger.json", cfg)
        store.save(new_ledger(ACCT, cfg.BOT_CAPITAL))
        before = store.path.read_text()
        bad = new_ledger(ACCT, cfg.BOT_CAPITAL)
        bad.cash = Decimal("-1")
        with pytest.raises(LedgerError):
            store.save(bad)
        assert store.path.read_text() == before

    def test_corrupt_json_rejected(self, cfg, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text("{not json")
        with pytest.raises(LedgerError, match="not valid JSON"):
            LedgerStore(p, cfg).load()

    def test_hand_edit_detected_by_checksum(self, cfg, tmp_path):
        store = LedgerStore(tmp_path / "ledger.json", cfg)
        store.save(new_ledger(ACCT, cfg.BOT_CAPITAL))
        data = json.loads(store.path.read_text())
        data["cash"] = "5000.00"
        store.path.write_text(json.dumps(data))
        with pytest.raises(LedgerError, match="checksum"):
            store.load()

    def _write_resigned(self, store, cfg, mutate):
        """Mutate a valid payload and re-sign it, to test semantic checks."""
        payload = ledger_to_dict(ledger_with_position(cfg))
        mutate(payload)
        from portfolio.portfolio_manager import _checksum
        payload["checksum"] = _checksum(payload)
        store.path.write_text(json.dumps(payload))

    @pytest.mark.parametrize("mutate,match", [
        (lambda p: p.update(cash="700.00"), "accounting identity"),
        (lambda p: p.update(initial_capital="1000000.00"), "BOT_CAPITAL"),
        (lambda p: p.update(cash="NaN"), "not finite"),
        (lambda p: p.update(cash=600.0), "must be a string"),
        (lambda p: p.pop("peak_equity"), "missing 'peak_equity'"),
        (lambda p: p.update(schema_version=99), "schema_version"),
        (lambda p: p.update(peak_equity="700.00"), "peak_equity"),
        (lambda p: p["positions"][0].update(quantity=-2), "quantity must be positive"),
        (lambda p: p["positions"][0].update(entry_order_ref="MANUAL"), "bot prefix"),
        (lambda p: p.update(account_id=""), "account_id"),
    ])
    def test_semantic_validation(self, cfg, tmp_path, mutate, match):
        store = LedgerStore(tmp_path / "ledger.json", cfg)
        self._write_resigned(store, cfg, mutate)
        with pytest.raises(LedgerError, match=match):
            store.load()


# --------------------------------------------------------------------------
# BOT strategy accounting
# --------------------------------------------------------------------------
class TestSnapshot:
    def test_bot_equity_includes_bot_positions_at_cost(self, cfg):
        s = compute_snapshot(ledger_with_position(cfg), cfg)
        assert s.bot_strategy_equity == Decimal("800.00")
        assert s.bot_available_capital == Decimal("600.00")
        assert s.marked_at_cost

    def test_marks_and_drawdown_from_peak(self, cfg):
        led = ledger_with_position(cfg)
        led.peak_equity = Decimal("900.00")
        s = compute_snapshot(led, cfg, marks={756733: Decimal("95.00")})
        assert s.bot_strategy_equity == Decimal("790.00")
        assert s.bot_drawdown_dollars == Decimal("110.00")
        assert s.max_drawdown_dollars == Decimal("135.00")
        assert s.drawdown_halt_equity == Decimal("765.00")

    def test_update_peak(self, cfg):
        led = ledger_with_position(cfg)
        s = compute_snapshot(led, cfg, marks={756733: Decimal("150.00")})
        assert update_peak(led, s) is True
        assert led.peak_equity == Decimal("900.00")
        assert update_peak(led, compute_snapshot(led, cfg)) is False


# --------------------------------------------------------------------------
# Reconciliation + ownership
# --------------------------------------------------------------------------
class TestReconcile:
    def test_clean_with_external_positions_and_orders(self, cfg):
        led = new_ledger(ACCT, cfg.BOT_CAPITAL)
        r = reconcile(led, ACCT, [ib_position(symbol="MSFT", con_id=272093)],
                      [ib_order()], cfg)
        assert r.ok
        assert len(r.external_positions) == 1 and not r.bot_positions
        assert len(r.external_orders) == 1 and not r.bot_orders

    def test_matching_bot_position(self, cfg):
        r = reconcile(ledger_with_position(cfg), ACCT, [ib_position()], [], cfg)
        assert r.ok and len(r.bot_positions) == 1

    def test_account_mismatch(self, cfg):
        r = reconcile(new_ledger("DU9999999", cfg.BOT_CAPITAL), ACCT, [], [], cfg)
        assert not r.ok

    def test_bot_position_missing_at_ibkr(self, cfg):
        r = reconcile(ledger_with_position(cfg), ACCT, [], [], cfg)
        assert not r.ok and "IBKR qty 0" in r.mismatches[0]

    def test_manual_shares_added_to_bot_contract(self, cfg):
        r = reconcile(ledger_with_position(cfg), ACCT, [ib_position(qty="5")], [], cfg)
        assert not r.ok

    def test_position_on_other_account_ignored(self, cfg):
        r = reconcile(ledger_with_position(cfg), ACCT,
                      [ib_position(), ib_position(account="DU0000001")], [], cfg)
        assert r.ok

    def test_unknown_bot_tagged_order(self, cfg):
        r = reconcile(new_ledger(ACCT, cfg.BOT_CAPITAL), ACCT, [],
                      [ib_order(ref="TBOT-0099")], cfg)
        assert not r.ok and "not in the ledger" in r.mismatches[0]

    def test_pending_order_vanished(self, cfg):
        led = new_ledger(ACCT, cfg.BOT_CAPITAL)
        led.pending_orders = [BotPendingOrder("TBOT-0003", 1, "QQQ", "BUY", 1, "t")]
        r = reconcile(led, ACCT, [], [], cfg)
        assert not r.ok and "not open at IBKR" in r.mismatches[0]

    def test_pending_order_present(self, cfg):
        led = new_ledger(ACCT, cfg.BOT_CAPITAL)
        led.pending_orders = [BotPendingOrder("TBOT-0003", 1, "QQQ", "BUY", 1, "t")]
        r = reconcile(led, ACCT, [], [ib_order(ref="TBOT-0003")], cfg)
        assert r.ok and len(r.bot_orders) == 1
