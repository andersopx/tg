import tempfile

from config import config
from database import Database
from risk_manager import RiskManager
from trader import Trader


class DummyFeed:
    def get_price_change_pct(self, minutes):
        return 0.0


def _restore(old):
    config._runtime_state = old["runtime"]
    config.MODE = old["mode"]
    config.TEST_BET_SIZE = old["test_bet_size"]
    config.AUTHORIZED_CAPITAL_USD = old["authorized_capital"]
    config.TEST_STOP_LOSS_BALANCE = old["stop"]
    config.MAX_DAILY_LOSS_USD = old["daily_loss"]
    config.MAX_DAILY_LOSS_PCT = old["daily_loss_pct"]


def _snapshot():
    return {
        "runtime": getattr(config, "_runtime_state", None),
        "mode": config.MODE,
        "test_bet_size": config.TEST_BET_SIZE,
        "authorized_capital": config.AUTHORIZED_CAPITAL_USD,
        "stop": config.TEST_STOP_LOSS_BALANCE,
        "daily_loss": config.MAX_DAILY_LOSS_USD,
        "daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
    }


def test_capital_isolation_anchors_reserve_and_allows_profit_growth():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.AUTHORIZED_CAPITAL_USD = 0.0
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())

            scope = risk.capital_scope(100.0)
            assert scope.enabled is True
            assert scope.reserve_balance == 50.0
            assert scope.trading_equity == 50.0

            # Profit is not capped: the protected reserve stays 50, so a 110
            # wallet exposes 60 as trading equity.
            scope2 = risk.capital_scope(110.0)
            assert scope2.reserve_balance == 50.0
            assert scope2.trading_equity == 60.0
    finally:
        _restore(old)


def test_capital_isolation_halts_when_authorized_principal_is_exhausted():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.TEST_STOP_LOSS_BALANCE = 0.0
            config.MAX_DAILY_LOSS_USD = 0.0
            config.MAX_DAILY_LOSS_PCT = 0.0
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())
            risk.capital_scope(100.0)  # reserve anchored at 50

            check = risk.can_trade(50.0)
            assert check.can_trade is False
            assert "Authorized capital exhausted" in check.reason
            assert db.get_state("halted") is True
    finally:
        _restore(old)


def test_per_order_amount_is_capped_by_authorized_trading_equity():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.TEST_BET_SIZE = 1.0
            db.set_state("test_bet_size", 1.0)
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())
            risk.capital_scope(100.0)  # reserve anchored at 50

            assert risk.calc_max_bet(100.0) == 1.0
            assert abs(risk.calc_max_bet(50.40) - 0.40) < 1e-9
            ok, reason = risk.validate_order(1.0, 50.40)
            assert ok is False
            assert "authorized trading equity" in reason
    finally:
        _restore(old)


def test_trader_calc_bet_amount_uses_isolated_equity_not_full_wallet():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            db.set_state("test_bet_size", 1.0)
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())
            risk.capital_scope(100.0)  # reserve anchored at 50

            trader = Trader.__new__(Trader)
            trader.risk = risk
            assert trader.calc_bet_amount(None, 100.0) == 1.0
            assert abs(trader.calc_bet_amount(None, 50.25) - 0.25) < 1e-9
    finally:
        _restore(old)


def test_capital_isolation_uses_protected_reserve_instead_of_legacy_wallet_stop():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.TEST_STOP_LOSS_BALANCE = 75.0
            config.MAX_DAILY_LOSS_USD = 0.0
            config.MAX_DAILY_LOSS_PCT = 0.0
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())
            risk.capital_scope(100.0)  # reserve anchored at 50

            check = risk.can_trade(74.0)
            assert check.can_trade is True
            assert db.get_state("halted") is not True

            exhausted = risk.can_trade(50.0)
            assert exhausted.can_trade is False
            assert "Authorized capital exhausted" in exhausted.reason
    finally:
        _restore(old)
