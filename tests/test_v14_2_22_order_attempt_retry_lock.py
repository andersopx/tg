import time

from database import Database


def test_failed_order_without_order_id_does_not_lock_window(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now_window = int(time.time() // 300 * 300)
    db.mark_order_attempt(now_window, "btc-updown", status="failed", reason="order_placement_failed", asset="BTC", timeframe="5m")
    assert db.has_recent_order_attempt(now_window, 45, asset="BTC", timeframe="5m") is False


def test_submitted_order_locks_window_briefly(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now_window = int(time.time() // 300 * 300)
    db.mark_order_attempt(now_window, "btc-updown", order_id="0xabc", status="submitted", reason="post_order_response", asset="BTC", timeframe="5m")
    assert db.has_recent_order_attempt(now_window, 45, asset="BTC", timeframe="5m") is True
