from pathlib import Path


def env_text():
    p = Path(".env")
    if p.exists():
        return p.read_text()
    return Path(".env.example").read_text()


def test_v14220_high_confidence_override_present():
    config = Path("config.py").read_text()
    trader = Path("trader.py").read_text()
    env = env_text()
    assert "HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED" in config
    assert "token_range_override_high_confidence" in trader
    assert "REALTIME_ORDERBOOK_ENABLED=true" in env


def test_v14220_diagnostic_script_present():
    s = Path("diagnose_live_state.py").read_text()
    assert "token_price_outside_range" in s
    assert "orderbook_missing" in s
    assert "最近1小时跳过原因" in s
