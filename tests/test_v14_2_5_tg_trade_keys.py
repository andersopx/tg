import tempfile

from config import config
from database import Database
from telegram_bot import TelegramBot


def test_runtime_polymarket_creds_win_over_env():
    old_runtime = getattr(config, "_runtime_state", None)
    old_pk = config.POLYMARKET_PRIVATE_KEY
    old_funder = config.POLYMARKET_FUNDER
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            db.set_state("polymarket_private_key", "0x" + "1" * 64)
            db.set_state("polymarket_funder", "0x" + "2" * 40)
            db.set_state("polymarket_signature_type", 3)
            assert config.effective_polymarket_private_key.endswith("111111")
            assert config.effective_polymarket_funder.endswith("222222")
            assert config.effective_polymarket_signature_type == 3
            assert config.has_polymarket_creds is True
    finally:
        config._runtime_state = old_runtime
        config.POLYMARKET_PRIVATE_KEY = old_pk
        config.POLYMARKET_FUNDER = old_funder


def test_telegram_save_trade_key_field_validates_and_stores():
    old_runtime = getattr(config, "_runtime_state", None)
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            ok, msg = TelegramBot._save_trade_key_field(bot, "private_key", "0x" + "a" * 64)
            assert ok, msg
            ok, msg = TelegramBot._save_trade_key_field(bot, "funder", "0x" + "b" * 40)
            assert ok, msg
            assert db.get_state("polymarket_private_key").endswith("aaaaaa")
            assert db.get_state("polymarket_funder").endswith("bbbbbb")
    finally:
        config._runtime_state = old_runtime
