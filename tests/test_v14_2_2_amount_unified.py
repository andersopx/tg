import tempfile

from config import config
from database import Database
from trader import Trader


def test_per_order_amount_alias_matches_effective_test_bet_size():
    old_db = getattr(config, "_runtime_state", None)
    old_test_bet_size = config.TEST_BET_SIZE
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.TEST_BET_SIZE = 1.0
            db.set_state("test_bet_size", 2.25)
            assert config.effective_test_bet_size == 2.25
            assert config.effective_per_order_amount == 2.25
    finally:
        config._runtime_state = old_db
        config.TEST_BET_SIZE = old_test_bet_size


def test_calc_bet_amount_uses_telegram_runtime_amount_not_env_constant():
    old_db = getattr(config, "_runtime_state", None)
    old_mode = config.MODE
    old_test_bet_size = config.TEST_BET_SIZE
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.TEST_BET_SIZE = 9.99
            db.set_state("test_bet_size", 1.00)
            trader = Trader.__new__(Trader)
            assert trader.calc_bet_amount(None, 100.0) == 1.00
    finally:
        config._runtime_state = old_db
        config.MODE = old_mode
        config.TEST_BET_SIZE = old_test_bet_size
