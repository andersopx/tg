import sys
import tempfile
import types

if "telegram" not in sys.modules:
    telegram_stub = types.ModuleType("telegram")
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data
    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard
    telegram_stub.Update = object
    telegram_stub.InlineKeyboardButton = InlineKeyboardButton
    telegram_stub.InlineKeyboardMarkup = InlineKeyboardMarkup
    sys.modules["telegram"] = telegram_stub

if "telegram.ext" not in sys.modules:
    ext_stub = types.ModuleType("telegram.ext")
    class _Builder:
        def token(self, token):
            return self
        def build(self):
            class App:
                def add_handler(self, *a, **k):
                    return None
            return App()
    class Application:
        @classmethod
        def builder(cls):
            return _Builder()
    ext_stub.Application = Application
    ext_stub.CommandHandler = object
    ext_stub.CallbackQueryHandler = object
    ext_stub.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram.ext"] = ext_stub

from config import config
from database import Database
from telegram_bot import TelegramBot


def _flatten_buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_settings_keyboard_is_live_only_and_has_runtime_money_controls():
    bot = TelegramBot.__new__(TelegramBot)
    labels = _flatten_buttons(TelegramBot.settings_keyboard(bot))
    assert "💵 设置每笔金额" in labels
    assert "💰 设置可用本金" in labels
    assert "🔐 设置交易密钥" in labels
    assert not any("模拟" in x or "DRY" in x or "切回" in x or "开启小额实盘" in x for x in labels)


def test_bet_size_runtime_write_updates_effective_amount():
    old_runtime = getattr(config, "_runtime_state", None)
    old_bet = config.TEST_BET_SIZE
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.TEST_BET_SIZE = 1.0
            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            ok, msg = TelegramBot._save_runtime_field(bot, "test_bet_size", "2.5")
            assert ok, msg
            assert db.get_state("test_bet_size") == 2.5
            assert config.effective_per_order_amount == 2.5
    finally:
        config._runtime_state = old_runtime
        config.TEST_BET_SIZE = old_bet


def test_tg_status_and_settings_show_actual_safety_state():
    old = (config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY)
    try:
        config.MODE = "paper"
        config.DRY_RUN = True
        config.REAL_TRADING_ENABLED = False
        config.OBSERVER_ONLY = False
        bot = TelegramBot.__new__(TelegramBot)
        bot.risk = None
        bot.learner = None
        bot.feed = None
        settings = TelegramBot._build_settings_text(bot)
        status = TelegramBot._render_status(bot)
        assert "不会真实下单" in settings
        assert "不会真实下单" in status
        assert "风控真实下单" not in settings
        assert "实盘专用" not in status
    finally:
        config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY = old
