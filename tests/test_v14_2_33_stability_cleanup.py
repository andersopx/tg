import asyncio
import sqlite3
import time

from polymarket_ws import PolymarketMarketWebSocket
from risk_manager import RiskManager
from database import Database
from config import config


def test_ws_desired_assets_is_lru_capped(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_ORDERBOOK_MAX_SUBSCRIPTIONS", 3, raising=False)
    ws = PolymarketMarketWebSocket()

    asyncio.run(ws.subscribe_assets(["a", "b", "c", "d"]))

    assert len(ws.desired_assets) == 3
    assert list(ws.desired_assets.keys()) == ["b", "c", "d"]


def test_ws_cleanup_expired_subscriptions_keeps_active_tokens(monkeypatch):
    monkeypatch.setattr(config, "REALTIME_ORDERBOOK_MAX_SUBSCRIPTIONS", 10, raising=False)
    ws = PolymarketMarketWebSocket()

    asyncio.run(ws.subscribe_assets(["old-a", "active", "old-b"]))
    removed = asyncio.run(ws.cleanup_expired_subscriptions({"active"}))

    assert removed == 2
    assert set(ws.desired_assets.keys()) == {"active"}


def test_risk_manager_clears_stale_halt_state_on_start(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    db.set_state("halted", True)
    db.set_state("halt_reason", "old stop loss")
    db.set_state("last_halt_notify", "old notify")

    class Feed:
        def get_price_change_pct(self, minutes):
            return 0.0

    risk = RiskManager(db, Feed())

    assert risk.is_halted is False
    assert db.get_state("halted", None) is False
    assert db.get_state("halt_reason", None) == ""
    assert db.get_state("last_halt_notify", None) == ""


def test_database_active_market_tokens_only_open_statuses(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now = int(time.time())
    with db.conn() as c:
        c.execute("""
            INSERT INTO trades (timestamp, window_ts, token_id, status)
            VALUES (?, ?, ?, ?)
        """, (now, now, "token-open", "open"))
        c.execute("""
            INSERT INTO trades (timestamp, window_ts, token_id, status)
            VALUES (?, ?, ?, ?)
        """, (now, now, "token-resolved", "resolved"))

    assert db.get_active_market_tokens() == {"token-open"}


def test_prune_public_data_respects_decision_and_execution_flags(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "bot.db"))
    old = int(time.time()) - 10 * 86400
    with db.conn() as c:
        c.execute("INSERT INTO decision_logs (timestamp, reason) VALUES (?, ?)", (old, "old"))
        c.execute("INSERT INTO execution_events (timestamp, reason) VALUES (?, ?)", (old, "old"))

    monkeypatch.setattr(config, "PRUNE_DECISION_LOGS", True, raising=False)
    monkeypatch.setattr(config, "PRUNE_EXECUTION_EVENTS", True, raising=False)
    db.prune_public_data(keep_days=3)

    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM decision_logs").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0] == 0
