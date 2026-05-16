from pathlib import Path


def test_product_doctor_page_has_raw_code_guard():
    s = Path('telegram_bot.py').read_text()
    assert 'raw_markers' in s
    assert 'candidate_stream_crowded' in s  # guarded, not shown raw
    assert '交易密钥还没完整认证' in s


def test_recent_decisions_hides_observe_noise():
    s = Path('telegram_bot.py').read_text()
    assert '普通观察已合并' in s
    assert '认证/风控拦截已合并' in s
    assert '显示最近1小时候选、执行拦截、下单尝试和成交' in s
    assert 'dry-run' not in s[s.find('async def _show_recent_decisions'):s.find('async def _show_skip_reasons')]
