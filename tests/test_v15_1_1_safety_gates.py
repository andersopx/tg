import importlib
import sys


def load_config_with_env(monkeypatch, **env):
    keys = {
        "MODE", "DRY_RUN", "OBSERVER_ONLY", "REAL_TRADING_ENABLED",
        "POLYMARKET_REPLAY_ON_START",
    }
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    sys.modules.pop("config", None)
    mod = importlib.import_module("config")
    return mod.Config()


def test_real_trading_enabled_is_required_even_when_dry_run_false(monkeypatch):
    cfg = load_config_with_env(
        monkeypatch,
        MODE="small_live",
        DRY_RUN="false",
        REAL_TRADING_ENABLED="false",
        OBSERVER_ONLY="false",
    )
    assert cfg.real_orders_enabled is False


def test_real_orders_require_all_live_arming_switches(monkeypatch):
    cfg = load_config_with_env(
        monkeypatch,
        MODE="small_live",
        DRY_RUN="false",
        REAL_TRADING_ENABLED="true",
        OBSERVER_ONLY="false",
    )
    assert cfg.real_orders_enabled is True


def test_paper_mode_forces_real_trading_disabled(monkeypatch):
    cfg = load_config_with_env(
        monkeypatch,
        MODE="paper",
        DRY_RUN="false",
        REAL_TRADING_ENABLED="true",
        OBSERVER_ONLY="false",
    )
    assert cfg.DRY_RUN is True
    assert cfg.REAL_TRADING_ENABLED is False
    assert cfg.real_orders_enabled is False


def test_polymarket_replay_default_is_false(monkeypatch):
    cfg = load_config_with_env(monkeypatch)
    assert cfg.POLYMARKET_REPLAY_ON_START is False
