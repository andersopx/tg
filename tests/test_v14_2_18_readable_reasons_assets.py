from pathlib import Path


def test_tg_reasons_are_plain_chinese_and_include_hints():
    s = Path("telegram_bot.py").read_text()
    assert '"orderbook_missing": "盘口暂时没有可买价"' in s
    assert '"token_price_outside_range": "价格太贵/太接近1，不追"' in s
    assert '"insufficient_window_klines": "K线样本不足，等待数据"' in s
    assert "例如 $0.99 的票" in s
    assert "各币种记录" in s


def test_version_v14218_or_later():
    s = Path("config.py").read_text()
    assert 'v14.2.21-live-execution-diagnostics-token-range' in s
