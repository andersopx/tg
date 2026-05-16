from pathlib import Path

from database import Database
from risk_manager import RiskManager


class DummyFeed:
    def get_price_change_pct(self, minutes):
        return 0.0


def test_v14231_small_balance_env_defaults_are_safe():
    cfg = Path('config.py').read_text()
    env = Path('.env.example').read_text()
    assert 'VERSION: str = "v14.2.36-audit-stable-schema"' in cfg
    assert 'BTC_BANKROLL_ALLOCATION: float = _env_float("BTC_BANKROLL_ALLOCATION", 1.0)' in cfg
    assert 'TEST_STOP_LOSS_BALANCE: float = _env_float("TEST_STOP_LOSS_BALANCE", 0.5)' in cfg
    assert 'AUTHORIZED_CAPITAL_USD=0' in env
    assert 'BTC_BANKROLL_ALLOCATION=1.0' in env
    assert 'ETH_BANKROLL_ALLOCATION=1.0' in env
    assert 'SOL_BANKROLL_ALLOCATION=1.0' in env
    assert 'XRP_BANKROLL_ALLOCATION=1.0' in env
    assert 'TEST_STOP_LOSS_BALANCE=0.5' in env


def test_v14236_install_uses_current_v36_directory():
    install = Path('install_v15_1_2.sh').read_text()
    assert '/root/btc_bot_v15_1_2' in install
    assert 'btc_bot_v15_1_2.tar.gz' in install
    assert '/root/btc_bot_v14_2_31_ready' not in install
    assert 'START_AFTER_INSTALL' in install


def test_capital_scope_disables_when_wallet_below_cap(tmp_path):
    import risk_manager as rm
    db = Database(str(tmp_path / 'bot.db'))
    db.set_state('authorized_capital_usd', 50.0)
    rm.config.attach_runtime_state(db)
    risk = RiskManager(db, DummyFeed())
    scope = risk.capital_scope(40.0)
    assert scope.enabled is False
    assert scope.trading_equity == 40.0
    assert scope.reserve_balance == 0.0
