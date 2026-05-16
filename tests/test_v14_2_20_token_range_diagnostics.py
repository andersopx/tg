from config import Config
from pathlib import Path


def test_high_confidence_override_present():
    config = Path("config.py").read_text()
    trader = Path("trader.py").read_text()
    cfg = Config()
    assert "HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED" in config
    assert "token_range_override_high_confidence" in trader
    assert cfg.REALTIME_ORDERBOOK_ENABLED is True


def test_v14220_diagnostic_script_present():
    s = Path("diagnose_live_state.py").read_text()
    assert "token_price_outside_range" in s
    assert "orderbook_missing" in s
    assert "最近1小时跳过原因" in s
