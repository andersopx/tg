from database import Database
from risk_manager import RiskManager


class DummyFeed:
    def get_price_change_pct(self, minutes):
        return 0.0


def test_small_balance_config_defaults_are_safe():
    from config import Config
    cfg = Config()
    assert cfg.AUTHORIZED_CAPITAL_USD == 0.0
    assert cfg.BTC_BANKROLL_ALLOCATION == 1.0
    assert cfg.ETH_BANKROLL_ALLOCATION == 1.0
    assert cfg.SOL_BANKROLL_ALLOCATION == 1.0
    assert cfg.XRP_BANKROLL_ALLOCATION == 1.0
    assert cfg.TEST_STOP_LOSS_BALANCE == 0.5


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
