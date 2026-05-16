from pathlib import Path


def _section(src: str, start: str, end: str) -> str:
    a = src.index(start)
    b = src.index(end, a)
    return src[a:b]


def test_main_menu_is_compact_and_grouped():
    src = Path("telegram_bot.py").read_text()
    menu = _section(src, "def main_menu_keyboard", "def _runtime_state_label")
    assert 'callback_data="review_menu"' in menu
    assert 'callback_data="skip_analysis_menu"' in menu
    assert 'callback_data="system_health_menu"' in menu
    assert 'callback_data="settings"' in menu

    # Raw diagnostic pages should no longer clutter the main menu directly.
    for raw in [
        'callback_data="recent_decisions"',
        'callback_data="skip_reasons"',
        'callback_data="quality_funnel"',
        'callback_data="quality_gate"',
        'callback_data="product_doctor"',
        'callback_data="ws_health"',
        'callback_data="asset_stats"',
        'callback_data="strategy_review"',
    ]:
        assert raw not in menu


def test_settings_menu_is_compact_and_advanced_keeps_old_callbacks():
    src = Path("telegram_bot.py").read_text()
    settings = _section(src, "def settings_keyboard", "def review_menu_keyboard")
    assert 'callback_data="advanced_menu"' in settings
    assert 'callback_data="recent_decisions"' not in settings
    assert 'callback_data="skip_reasons"' not in settings
    assert 'callback_data="product_doctor"' not in settings
    assert 'callback_data="ws_health"' not in settings

    advanced = _section(src, "def advanced_keyboard", "# ============ Command handlers")
    assert 'callback_data="reset_daily"' in advanced
    assert 'callback_data="order_submission_details"' in advanced
    assert 'callback_data="signal_stats"' in advanced
    assert 'callback_data="cleanup_db"' in advanced


def test_group_pages_preserve_existing_callback_data_values():
    src = Path("telegram_bot.py").read_text()
    assert '"review_menu": self._show_review_menu' in src
    assert '"skip_analysis_menu": self._show_skip_analysis_menu' in src
    assert '"system_health_menu": self._show_system_health_menu' in src
    assert '"advanced_menu": self._show_advanced_menu' in src

    for existing in [
        'callback_data="asset_stats"',
        'callback_data="strategy_review"',
        'callback_data="signal_stats"',
        'callback_data="recent_decisions"',
        'callback_data="skip_reasons"',
        'callback_data="quality_funnel"',
        'callback_data="quality_gate"',
        'callback_data="product_doctor"',
        'callback_data="ws_health"',
        'callback_data="order_submission_details"',
    ]:
        assert existing in src
