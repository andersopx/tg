import asyncio
import tempfile

from config import config
from database import Database
from risk_manager import RiskManager
from telegram_bot import TelegramBot


class DummyFeed:
    def get_price_change_pct(self, minutes):
        return 0.0


class DummyPoly:
    connected = True

    def __init__(self, balance):
        self._balance = balance

    def get_balance(self):
        return self._balance


class DummyQuery:
    def __init__(self):
        self.text = None
        self.reply_markup = None
        self.parse_mode = None

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.text = text
        self.reply_markup = reply_markup
        self.parse_mode = parse_mode


def _snapshot():
    return {
        "runtime": getattr(config, "_runtime_state", None),
        "mode": config.MODE,
        "authorized_capital": config.AUTHORIZED_CAPITAL_USD,
        "stop": config.TEST_STOP_LOSS_BALANCE,
    }


def _restore(old):
    config._runtime_state = old["runtime"]
    config.MODE = old["mode"]
    config.AUTHORIZED_CAPITAL_USD = old["authorized_capital"]
    config.TEST_STOP_LOSS_BALANCE = old["stop"]


def test_telegram_balance_shows_capital_isolation_lines():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.AUTHORIZED_CAPITAL_USD = 0.0
            config.TEST_STOP_LOSS_BALANCE = 0.0
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())

            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            bot.risk = risk
            bot.polymarket = DummyPoly(100.0)
            q = DummyQuery()

            asyncio.run(bot._show_balance(q))
            assert "可用本金上限" in q.text
            assert "保护储备" in q.text
            assert "可交易资产" in q.text
            assert "$50.00" in q.text
    finally:
        _restore(old)


def test_telegram_authorized_capital_button_writes_runtime_state():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.AUTHORIZED_CAPITAL_USD = 0.0

            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            q = DummyQuery()

            asyncio.run(bot._action_set_authorized_capital_value(q, "100"))
            assert db.get_state("authorized_capital_usd") == 100.0
            assert config.effective_authorized_capital_usd == 100.0
            assert "已设置可用本金上限" in q.text
    finally:
        _restore(old)


def test_capital_isolation_balance_page_does_not_show_wallet_pnl_as_profit():
    old = _snapshot()
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = Database(tmp.name)
            config.attach_runtime_state(db)
            config.MODE = "small_live"
            config.AUTHORIZED_CAPITAL_USD = 0.0
            config.TEST_STOP_LOSS_BALANCE = 75.0
            db.set_state("authorized_capital_usd", 50.0)
            risk = RiskManager(db, DummyFeed())

            bot = TelegramBot.__new__(TelegramBot)
            bot.db = db
            bot.risk = risk
            bot.polymarket = DummyPoly(102.51)
            q = DummyQuery()

            asyncio.run(bot._show_balance(q))
            assert "钱包余额" in q.text
            assert "可用本金上限" in q.text
            assert "保护储备" in q.text
            assert "$52.51" in q.text
            assert "可交易资产" in q.text
            assert "$50.00" in q.text
            assert "起始本金" not in q.text
            assert "总盈亏" not in q.text
            assert "+$2.51" not in q.text
            assert "停手线" not in q.text
            assert "触发停手余额" in q.text
    finally:
        _restore(old)
