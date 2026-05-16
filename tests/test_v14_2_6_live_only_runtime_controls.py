import tempfile

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
    assert "🎭 切到影子模式" in labels
    assert "🟢 切到真实下单" in labels
    assert "⚪ 切到观察/纸面" in labels


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


def test_trade_mode_buttons_persist_shadow_and_real_runtime_state():
    old_runtime = getattr(config, "_runtime_state", None)
    old = (
        config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED,
        config.OBSERVER_ONLY, config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED,
    )
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            bot.risk = None
            bot.polymarket = None

            TelegramBot._persist_trade_mode(bot, mode="small_live", dry_run=False, real_trading=False)
            assert config.MODE == "small_live"
            assert config.DRY_RUN is False
            assert config.REAL_TRADING_ENABLED is False
            assert config.real_orders_enabled is False
            assert db.get_state("mode") == "small_live"
            assert db.get_state("dry_run") is False
            assert db.get_state("real_trading_enabled") is False

            TelegramBot._persist_trade_mode(bot, mode="small_live", dry_run=False, real_trading=True, sig3_submit=True)
            assert config.REAL_TRADING_ENABLED is True
            assert config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED is True
            assert config.real_orders_enabled is True
            assert db.get_state("real_trading_enabled") is True
            assert db.get_state("clob_v2_sig3_real_submit_enabled") is True

            TelegramBot._persist_trade_mode(bot, mode="paper", dry_run=True, real_trading=False, sig3_submit=False)
            assert config.MODE == "paper"
            assert config.DRY_RUN is True
            assert config.REAL_TRADING_ENABLED is False
            assert config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED is False
            assert config.real_orders_enabled is False
            assert db.get_state("mode") == "paper"
            assert db.get_state("dry_run") is True
            assert db.get_state("real_trading_enabled") is False
            assert db.get_state("clob_v2_sig3_real_submit_enabled") is False
    finally:
        config._runtime_state = old_runtime
        (
            config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED,
            config.OBSERVER_ONLY, config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED,
        ) = old
