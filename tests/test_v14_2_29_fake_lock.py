import time

from database import Database


def test_submitting_without_order_id_does_not_lock_window(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now_window = int(time.time() // 300 * 300)
    db.mark_order_attempt(now_window, "sol-updown", status="submitting", reason="passed_filters", asset="SOL", timeframe="5m")
    assert db.has_recent_order_attempt(now_window, 45, asset="SOL", timeframe="5m") is False


def test_submitting_with_order_id_still_locks_window(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now_window = int(time.time() // 300 * 300)
    db.mark_order_attempt(now_window, "sol-updown", order_id="0xabc", status="submitting", reason="post_order_inflight", asset="SOL", timeframe="5m")
    assert db.has_recent_order_attempt(now_window, 45, asset="SOL", timeframe="5m") is True


def test_cleanup_stale_submitting_without_order_id(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now_window = int(time.time() // 300 * 300)
    db.mark_order_attempt(now_window, "xrp-updown", status="submitting", reason="passed_filters", asset="XRP", timeframe="5m")
    # Force it old enough to be stale.
    with db.conn() as c:
        c.execute("UPDATE order_attempts SET updated_at = ? WHERE asset='XRP'", (int(time.time()) - 60,))
    changed = db.cleanup_stale_pre_submit_attempts(stale_sec=15)
    assert changed == 1
    with db.conn() as c:
        row = c.execute("SELECT status, reason FROM order_attempts WHERE asset='XRP'").fetchone()
    assert row["status"] == "stale_pre_submit"
    assert row["reason"] == "stale_submitting_no_order_id"
    assert db.has_recent_order_attempt(now_window, 45, asset="XRP", timeframe="5m") is False
