import json
from database import Database


def test_trade_lifecycle_fields_insert_update(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    trade_id = db.insert_trade({
        "window_ts": 123456,
        "asset": "BTC",
        "timeframe": "5m",
        "market_slug": "btc-updown-5m-123456",
        "market_question": "BTC up or down",
        "token_id": "token-up",
        "direction": "UP",
        "entry_price": 0.79,
        "size": 1.0,
        "shares": 1.26,
        "cost": 1.0,
        "filled_shares": 0,
        "filled_cost": 0,
        "avg_price": 0,
        "model_probability": 0.957,
        "edge_after_fees": 0.114,
        "reference_price": 80000,
        "signal_data": {"pattern": "prob_edge_up"},
        "status": "attempted",
        "attempting_at": 111,
    })

    row = db.get_trade_by_id(trade_id)
    assert row["status"] == "attempted"
    assert row["attempting_at"] == 111

    db.update_trade_lifecycle(
        trade_id,
        status="submitted",
        submitted_at=112,
        submitted_order_id="0xabc123",
        submission_response_json={"success": True, "orderID": "0xabc123"},
        last_status_check_at=112,
    )
    row = db.get_trade_by_id(trade_id)
    assert row["status"] == "submitted"
    assert row["submitted_order_id"] == "0xabc123"
    assert json.loads(row["submission_response_json"])["success"] is True

    db.update_trade_lifecycle(
        trade_id,
        status="filled",
        filled_at=113,
        filled_shares=1.27,
        filled_cost=1.0,
        avg_price=0.787,
        first_fill_response_json={"filled_shares": 1.27},
    )
    row = db.get_trade_by_id(trade_id)
    assert row["status"] == "filled"
    assert row["filled_at"] == 113
    assert row["filled_shares"] == 1.27
    assert json.loads(row["first_fill_response_json"])["filled_shares"] == 1.27


def test_recent_lifecycle_and_submission_queries(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    tid = db.insert_trade({
        "window_ts": 1,
        "token_id": "token",
        "direction": "DOWN",
        "entry_price": 0.55,
        "size": 1,
        "shares": 1.8,
        "cost": 1,
        "status": "failed",
        "attempting_at": 10,
        "submission_error": "Tick size mismatch",
    })
    lifecycles = db.get_recent_trade_lifecycles(limit=5)
    submissions = db.get_recent_order_submission_details(limit=5)
    assert lifecycles[0]["id"] == tid
    assert submissions[0]["submission_error"] == "Tick size mismatch"
