from pathlib import Path

from telegram_bot import TelegramBot


def test_direct_command_handlers_added_to_source():
    source = Path(__file__).resolve().parents[1].joinpath("telegram_bot.py").read_text()
    for name in ["start", "help", "status", "balance", "settings", "today"]:
        assert f'CommandHandler("{name}"' in source


def test_command_render_helpers_exist():
    bot = TelegramBot.__new__(TelegramBot)
    assert hasattr(TelegramBot, "cmd_status")
    assert hasattr(TelegramBot, "cmd_balance")
    assert hasattr(TelegramBot, "cmd_settings")
    assert hasattr(TelegramBot, "cmd_today")
    assert hasattr(TelegramBot, "_build_balance_text")
    assert hasattr(TelegramBot, "_build_settings_text")
    assert hasattr(TelegramBot, "_build_today_text")
