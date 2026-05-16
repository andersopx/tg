import sqlite3

from profit_rule_engine import ensure_schema, extract_features, profit_rule_decision, signature_exact


class DbRef:
    def __init__(self, path):
        self.db_path = str(path)


def _adaptive(prob=0.84):
    return {
        "decision": "SHADOW",
        "expected_value": 0.18,
        "quality_score": 0.72,
        "calibrated_probability": prob,
        "raw_settlement_probability": prob,
    }


def _signal(prob=0.84, gap=0.04, adaptive=True):
    indicators = {}
    if adaptive:
        indicators["adaptive_edge"] = _adaptive(prob)
    return {
        "strategy_name": "barrier_reclaim",
        "model_probability": prob,
        "model_market_gap": gap,
        "market_mid_probability": prob - gap,
        "seconds_left": 45,
        "indicators": indicators,
    }


def _safe_env(monkeypatch):
    monkeypatch.setenv("PROFIT_RULE_LIVE_REQUIRE_PROMOTE", "true")
    monkeypatch.setenv("PROFIT_RULE_MIN_ENTRY_PRICE", "0.10")
    monkeypatch.setenv("PROFIT_RULE_MAX_ENTRY_PRICE", "0.50")
    monkeypatch.setenv("PROFIT_RULE_MAX_MODEL_GAP", "0.15")
    monkeypatch.setenv("PROFIT_RULE_DEFAULT_HIGH_PROB", "0.75")
    monkeypatch.setenv("PROFIT_RULE_NEW_RULE_REQUIRES_ADAPTIVE_EDGE", "true")
    monkeypatch.setenv("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_EV", "0.08")
    monkeypatch.setenv("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_QUALITY", "0.56")


def test_live_mode_blocks_new_bucket_even_when_adaptive_edge_is_good(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    db_path = tmp_path / "bot.db"
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)

    allowed, reason, info = profit_rule_decision(
        DbRef(db_path), _signal(prob=0.84), "BTC", "5m", "Up", 0.30, mode="live"
    )

    assert allowed is False
    assert reason == "profit_rule_not_promoted_for_live"
    assert info["decision"] == "SHADOW_ONLY"
    assert info["rule_rating"] == "NEW"


def test_shadow_mode_allows_new_bucket_only_with_adaptive_edge_for_learning(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    db_path = tmp_path / "bot.db"
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)

    allowed, reason, info = profit_rule_decision(
        DbRef(db_path), _signal(prob=0.84, adaptive=True), "BTC", "5m", "Up", 0.30, mode="shadow"
    )

    assert allowed is True
    assert reason == "profit_rule_new_adaptive_edge_shadow"
    assert info["decision"] == "SHADOW"


def test_shadow_mode_rejects_new_bucket_without_adaptive_edge_when_required(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    db_path = tmp_path / "bot.db"
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)

    allowed, reason, info = profit_rule_decision(
        DbRef(db_path), _signal(prob=0.84, adaptive=False), "BTC", "5m", "Up", 0.30, mode="shadow"
    )

    assert allowed is False
    assert reason == "profit_rule_observe_only"
    assert info["decision"] == "OBSERVE"


def test_live_mode_allows_promoted_exact_bucket(tmp_path, monkeypatch):
    _safe_env(monkeypatch)
    db_path = tmp_path / "bot.db"
    sig_payload = _signal(prob=0.84)
    x = extract_features(sig_payload, asset="BTC", timeframe="5m", direction="Up", entry_price=0.30)
    sig = signature_exact(x)

    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO profit_rules (
                signature, rule_type, strategy_name, asset, timeframe, direction,
                entry_bucket, prob_bucket, gap_bucket, remaining_bucket,
                attempts, wins, losses, win_rate, total_pnl, avg_pnl,
                avg_entry, avg_multiplier, max_multiplier, loss_streak,
                recent_attempts, recent_wins, recent_pnl, rating, reason,
                frozen_until, last_trade_id, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sig, "exact", x["strategy_name"], x["asset"], x["timeframe"], x["direction"],
                x["entry_bucket"], x["prob_bucket"], x["gap_bucket"], x["remaining_bucket"],
                20, 16, 4, 0.80, 12.0, 0.60,
                0.30, 3.33, 4.0, 0,
                10, 8, 6.0, "PROMOTE", "test_promote",
                0, 1, 1,
            ),
        )

    allowed, reason, info = profit_rule_decision(
        DbRef(db_path), sig_payload, "BTC", "5m", "Up", 0.30, mode="live"
    )

    assert allowed is True
    assert reason == "profit_rule_promote"
    assert info["decision"] == "PROMOTE"


def test_live_mode_blocks_when_db_unavailable(monkeypatch):
    _safe_env(monkeypatch)
    allowed, reason, info = profit_rule_decision(
        object(), _signal(prob=0.84), "BTC", "5m", "Up", 0.30, mode="live"
    )

    assert allowed is False
    assert reason == "profit_rule_db_unavailable_live_block"
    assert info["decision"] == "SHADOW_ONLY"
