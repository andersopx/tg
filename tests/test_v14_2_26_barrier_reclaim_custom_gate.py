from pathlib import Path


def env_text():
    p = Path(".env")
    if p.exists():
        return p.read_text()
    return Path(".env.example").read_text()


def test_v14225_barrier_reclaim_config_and_strategy_enabled():
    config = Path('config.py').read_text()
    env = env_text()
    strat = Path('strategy.py').read_text()
    assert 'VERSION: str = "v14.2.27"' in config
    assert 'BARRIER_RECLAIM_ENABLED' in config
    assert 'barrier_reclaim' in env
    assert '_signal_barrier_reclaim' in strat
    assert '先跌破目标线，正在向上回收' in strat
    assert '先涨破目标线，正在向下回收' in strat


def test_v14225_barrier_reclaim_reuses_single_order_path_only():
    trader = Path('trader.py').read_text()
    strat = Path('strategy.py').read_text()
    assert 'polymarket.place_buy_order' in trader
    assert 'batch' not in strat.lower()
    assert 'two_leg' not in strat.lower()


def test_v14225_short_package_install_names():
    install = Path('install_v15_1_2.sh').read_text()
    assert '/root/btc_bot_v15_1_2' in install
    assert '/root/btc_bot_v15_1_2.tar.gz' in install
    assert 'v15.1.2' in install
    assert '.env.example' in install


def test_v14226_deep_ambush_and_custom_gate_present():
    config = Path('config.py').read_text()
    env = env_text()
    trader = Path('trader.py').read_text()
    telegram = Path('telegram_bot.py').read_text()
    assert 'BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE' in config
    assert 'BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE=0.05' in env
    assert 'BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE=true' in env
    assert 'barrier_reclaim_custom_gate_pass' in trader
    assert 'barrier_reclaim_price_impact_high' in trader
    assert '目标线回收专属门槛通过' in telegram


def test_v14226_quality_funnel_has_callback_error_fallback():
    telegram = Path('telegram_bot.py').read_text()
    assert 'quality_funnel' in telegram
    assert '按钮处理失败' in telegram
    assert 'Telegram callback failed' in telegram
