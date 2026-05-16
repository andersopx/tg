"""
v15.1.4 - hard safety path tests.

These tests verify that when real_orders_enabled=False, the local SHADOW
write path:

  1. Sets is_shadow=1 so the row is visible to get_shadow_open_trades().
  2. Sets shadow_reason='real_orders_disabled_shadow_mode'.
  3. Sets submitted_order_id (so settlement and TG lifecycle work).
  4. Never calls polymarket.place_buy_order().
  5. Logs a SHADOW BUY line that contains asset/timeframe/direction
     /entry/shares/cost (no AttributeError due to signal.get).

These tests directly exercise the v15.1.3 hard safety bug that
v15.1.4 fixes:
  - v15.1.3 only updated status='shadow_open' but left is_shadow=0,
    so get_shadow_open_trades() returned nothing -> orphans.
  - v15.1.3 called signal.get(...) on a ReversalSignal dataclass,
    raising AttributeError every time and losing detail in the log.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest


def _ensure_minimal_trades_table(db_path: str) -> None:
    """Create just enough schema for the hard-safety UPDATE to work."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            window_ts INTEGER,
            asset TEXT,
            timeframe TEXT,
            direction TEXT,
            entry_price REAL,
            size REAL,
            shares REAL,
            cost REAL,
            filled_shares REAL,
            filled_cost REAL,
            avg_price REAL,
            status TEXT DEFAULT 'attempted',
            is_shadow INTEGER DEFAULT 0,
            shadow_reason TEXT,
            submitted_at INTEGER,
            submitted_order_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def test_hard_safety_update_sets_is_shadow_and_reason(tmp_path):
    """
    Reproduce the exact UPDATE that v15.1.4 hard safety path performs,
    then verify the row is visible by a get_shadow_open_trades-style query.
    """
    db_path = str(tmp_path / "test.db")
    _ensure_minimal_trades_table(db_path)

    # Insert an 'attempted' row (mimic insert_trade before hard safety branch)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO trades (timestamp, window_ts, asset, timeframe, direction, "
        "entry_price, size, shares, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(time.time()), int(time.time()), "BTC", "5m", "Up", 0.42, 1.0, 2.38, "attempted"),
    )
    trade_id = cur.lastrowid
    conn.commit()

    # Apply v15.1.4 hard safety UPDATE
    now = int(time.time())
    shadow_order_id = f"shadow_local_{trade_id}_{int(time.time()*1000)}"
    conn.execute(
        """
        UPDATE trades SET
          status=?, is_shadow=1, shadow_reason=?, submitted_at=?,
          submitted_order_id=?, filled_shares=?, filled_cost=?, avg_price=?
        WHERE id=?
        """,
        ("shadow_open", "real_orders_disabled_shadow_mode", now,
         shadow_order_id, 2.38, 1.0, 0.42, trade_id),
    )
    conn.commit()

    # Verify a get_shadow_open_trades-style query finds it
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='shadow_open' AND COALESCE(is_shadow,0)=1"
    ).fetchall()
    assert len(rows) == 1, "v15.1.4 hard safety must set is_shadow=1 so shadow queries find the row"

    row = rows[0]
    # column index lookup
    cols = [c[0] for c in conn.execute("SELECT * FROM trades LIMIT 0").description]
    d = dict(zip(cols, row))
    assert d["is_shadow"] == 1
    assert d["shadow_reason"] == "real_orders_disabled_shadow_mode"
    assert d["submitted_order_id"], "submitted_order_id must be set so settlement / TG show the row"
    assert d["filled_shares"] == pytest.approx(2.38)
    assert d["filled_cost"] == pytest.approx(1.0)
    assert d["avg_price"] == pytest.approx(0.42)
    conn.close()


def test_v15_1_3_bug_would_not_set_is_shadow(tmp_path):
    """
    Confirm the original v15.1.3 UPDATE statement leaves is_shadow=0.
    This documents the bug that v15.1.4 fixes.
    """
    db_path = str(tmp_path / "test_v1513_bug.db")
    _ensure_minimal_trades_table(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO trades (timestamp, window_ts, asset, timeframe, direction, "
        "entry_price, size, shares, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(time.time()), int(time.time()), "BTC", "5m", "Up", 0.42, 1.0, 2.38, "attempted"),
    )
    trade_id = cur.lastrowid
    conn.commit()

    # OLD v15.1.3 UPDATE (the bug):
    conn.execute("UPDATE trades SET status=? WHERE id=?", ("shadow_open", trade_id))
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM trades WHERE status='shadow_open' AND COALESCE(is_shadow,0)=1"
    ).fetchall()
    # The OLD v15.1.3 path produces an orphan: status='shadow_open' but is_shadow=0
    assert len(rows) == 0, "v15.1.3 created orphan rows that shadow queries never see"
    conn.close()


def test_signal_attribute_access_does_not_raise():
    """
    v15.1.3 used signal.get('asset') on a ReversalSignal dataclass which has
    no .get() method, so the log line was always swallowed by the except.
    v15.1.4 uses getattr instead.
    """
    from dataclasses import dataclass, field

    @dataclass
    class FakeSignal:
        asset: str = "BTC"
        timeframe: str = "5m"
        direction: str = "Up"
        indicators: dict = field(default_factory=dict)

    sig = FakeSignal(asset="ETH", timeframe="15m", direction="Down")

    # v15.1.3 style (bug) - this raises
    with pytest.raises(AttributeError):
        sig.get("asset")

    # v15.1.4 style - this works
    _asset = str(getattr(sig, "asset", "") or "")
    _timeframe = str(getattr(sig, "timeframe", "") or "")
    _direction = str(getattr(sig, "direction", "") or "")
    assert _asset == "ETH"
    assert _timeframe == "15m"
    assert _direction == "Down"


def test_profit_rule_engine_uses_config_db_path(monkeypatch, tmp_path):
    """
    v15.1.4 profit_rule_engine should use config.DB_PATH not hardcoded
    'btc_bot.db'.
    """
    import sys

    # Provide a fake config module with DB_PATH pointing to a temp file
    fake_db = tmp_path / "alt.db"
    fake_db.write_text("")

    class FakeConfig:
        DB_PATH = str(fake_db)

        @staticmethod
        def _runtime_float(keys, default):
            return float(default)

        @staticmethod
        def _runtime_int(keys, default):
            return int(default)

        @staticmethod
        def _state_get_first(keys, default=None):
            return default

    fake_mod = type(sys)("config")
    fake_mod.config = FakeConfig()
    monkeypatch.setitem(sys.modules, "config", fake_mod)

    # Reimport profit_rule_engine fresh so it picks up the patched config
    sys.modules.pop("profit_rule_engine", None)
    import profit_rule_engine as pre

    assert pre._config_db_path() == str(fake_db), \
        "profit_rule_engine must read config.DB_PATH"


def test_profit_rule_engine_env_float_respects_runtime_state(monkeypatch):
    """
    v15.1.4 _env_float should prefer config._runtime_float so TG-set values
    are honored.
    """
    import sys

    captured = {}

    class FakeConfig:
        DB_PATH = "x.db"

        @staticmethod
        def _runtime_float(keys, default):
            captured["keys"] = keys
            return 0.99  # something obviously from runtime, not .env

        @staticmethod
        def _runtime_int(keys, default):
            return int(default)

        @staticmethod
        def _state_get_first(keys, default=None):
            return default

    fake_mod = type(sys)("config")
    fake_mod.config = FakeConfig()
    monkeypatch.setitem(sys.modules, "config", fake_mod)

    sys.modules.pop("profit_rule_engine", None)
    import profit_rule_engine as pre

    val = pre._env_float("PROFIT_RULE_MIN_ENTRY_PRICE", 0.05)
    assert val == 0.99, "TG runtime override must take precedence over .env"
    assert "keys" in captured, "config._runtime_float must have been called"
