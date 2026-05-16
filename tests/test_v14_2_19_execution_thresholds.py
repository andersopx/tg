from pathlib import Path


def env_text():
    p = Path(".env")
    if p.exists():
        return p.read_text()
    return Path(".env.example").read_text()


def test_v14219_execution_threshold_defaults_and_env():
    config = Path("config.py").read_text()
    env = env_text()
    assert 'v14.2.21-live-execution-diagnostics-token-range' in config
    assert 'MARKET_BUY_SLIPPAGE=0.035' in env
    assert 'EXECUTION_GUARD_SLIPPAGE_CAP=0.035' in env
    assert 'QUALITY_MAX_PRICE_IMPACT=0.035' in env
    assert 'EXECUTION_GUARD_PRICE_JUMP_CAP=0.025' in env
    assert 'TARGET_SPREAD_MAX=0.06' in env


def test_v14219_guard_uses_explicit_slippage_cap():
    guard = Path("execution_quality_guard.py").read_text()
    trader = Path("trader.py").read_text()
    assert 'config.EXECUTION_GUARD_SLIPPAGE_CAP' in guard
    assert 'config.EXECUTION_GUARD_SLIPPAGE_CAP' in trader
    assert 'TARGET_SPREAD_MAX' in trader


def test_v14219_tg_reasons_cover_execution_blocks():
    tg = Path("telegram_bot.py").read_text()
    assert '滑点超过上限' in tg
    assert '盘口冲击太大' in tg
    assert '买卖价差太大' in tg
    assert '执行容忍' in tg
