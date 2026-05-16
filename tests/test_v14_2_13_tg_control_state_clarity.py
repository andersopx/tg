from pathlib import Path


def test_product_doctor_and_ws_health_use_contextual_run_pause_controls():
    src = Path('telegram_bot.py').read_text()
    product_section = src[src.index('async def _show_product_doctor'):src.index('async def _show_ws_health')]
    ws_section = src[src.index('async def _show_ws_health'):src.index('async def _show_bet_size_picker')]

    assert 'self._run_pause_buttons("product_doctor")' in product_section
    assert 'self._run_pause_buttons("ws_health")' in ws_section
    assert '机器人交易：' in product_section
    assert '不代表机器人停止' in ws_section


def test_run_pause_callbacks_can_return_to_current_pages():
    src = Path('telegram_bot.py').read_text()
    assert 'data.startswith("run:")' in src
    assert 'data.startswith("pause:")' in src
    assert 'return_to == "product_doctor"' in src
    assert 'return_to == "ws_health"' in src
