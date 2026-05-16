import tempfile

from config import config
from database import Database


def test_runtime_bet_size_overrides_env_for_small_live():
    old_mode = config.MODE
    old_state = getattr(config, "_runtime_state", None)
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as f:
            db = Database(f.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            db.set_state("test_bet_size", 1.75)
            assert abs(config.calc_bet_size(confidence=0.7, balance=100.0) - 1.75) < 1e-9
    finally:
        config.MODE = old_mode
        config._runtime_state = old_state


def test_runtime_stop_loss_and_daily_loss_aliases():
    old_mode = config.MODE
    old_state = getattr(config, "_runtime_state", None)
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as f:
            db = Database(f.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            db.set_state("stop_loss_balance", 88.5)
            db.set_state("loss_stop_usd", 3.25)
            assert abs(config.stop_loss_balance - 88.5) < 1e-9
            assert abs(config.effective_max_daily_loss_usd - 3.25) < 1e-9
    finally:
        config.MODE = old_mode
        config._runtime_state = old_state


def test_runtime_limits_aliases_are_effective():
    old_state = getattr(config, "_runtime_state", None)
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as f:
            db = Database(f.name)
            config.attach_runtime_state(db)
            db.set_state("MAX_TRADES_PER_DAY", 7)
            db.set_state("max_open_trades", 2)
            db.set_state("MAX_ORDER_ATTEMPTS_PER_DAY", 11)
            assert config.effective_max_trades_per_day == 7
            assert config.effective_max_open_trades == 2
            assert config.effective_max_order_attempts_per_day == 11
    finally:
        config._runtime_state = old_state
