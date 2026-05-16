from config import Config
from pathlib import Path


def test_barrier_reclaim_config_and_strategy_enabled():
    config = Path('config.py').read_text()
    strat = Path('strategy.py').read_text()
    assert 'BARRIER_RECLAIM_ENABLED' in config
    assert '_signal_barrier_reclaim' in strat
    assert '先跌破目标线，正在向上回收' in strat
    assert '先涨破目标线，正在向下回收' in strat


def test_barrier_reclaim_reuses_single_order_path_only():
    trader = Path('trader.py').read_text()
    strat = Path('strategy.py').read_text()
    assert 'polymarket.place_buy_order' in trader
    assert 'batch' not in strat.lower()
    assert 'two_leg' not in strat.lower()


def test_deep_ambush_and_custom_gate_present():
    config = Path('config.py').read_text()
    trader = Path('trader.py').read_text()
    telegram = Path('telegram_bot.py').read_text()
    cfg = Config()
    assert 'BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE' in config
    assert cfg.BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE == 0.05
    assert cfg.BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE is True
    assert 'barrier_reclaim_custom_gate_pass' in trader
    assert 'barrier_reclaim_price_impact_high' in trader
    assert '目标线回收专属门槛通过' in telegram


def test_quality_funnel_has_callback_error_fallback():
    telegram = Path('telegram_bot.py').read_text()
    assert 'quality_funnel' in telegram
    assert '按钮处理失败' in telegram
    assert 'Telegram callback failed' in telegram
