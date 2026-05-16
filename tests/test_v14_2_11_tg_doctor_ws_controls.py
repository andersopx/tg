from pathlib import Path


def test_product_doctor_and_ws_health_pages_have_run_pause_controls():
    src = Path('telegram_bot.py').read_text()
    product_section = src[src.index('async def _show_product_doctor'):src.index('async def _show_ws_health')]
    ws_section = src[src.index('async def _show_ws_health'):src.index('async def _show_bet_size_picker')]

    assert 'self._run_pause_buttons("product_doctor")' in product_section
    assert 'self._run_pause_buttons("ws_health")' in ws_section
