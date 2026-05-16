"""
SQLite database for trades, daily stats, bot state, and learning data.
Persists across restarts.

V6 adds a public-data layer: market snapshots, historical price/trade samples,
and conservative probability calibration.
"""
import sqlite3
import json


# v15.1.3 safety:
# Prevent json.dumps from crashing when signal_data/details contain circular references.
def _json_safe(obj, _seen=None, _depth=0):
    if _seen is None:
        _seen = set()
    if _depth > 12:
        return "<max_depth>"
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    oid = id(obj)
    if isinstance(obj, dict):
        if oid in _seen:
            return "<circular_ref>"
        _seen.add(oid)
        out = {}
        for k, v in obj.items():
            try:
                key = str(k)
            except Exception:
                key = "<bad_key>"
            if key in {"self", "parent", "__dict__", "__weakref__"}:
                continue
            out[key] = _json_safe(v, _seen, _depth + 1)
        _seen.discard(oid)
        return out
    if isinstance(obj, (list, tuple, set)):
        if oid in _seen:
            return "<circular_ref>"
        _seen.add(oid)
        out = [_json_safe(v, _seen, _depth + 1) for v in list(obj)[:300]]
        _seen.discard(oid)
        return out
    try:
        return str(obj)
    except Exception:
        return f"<unserializable:{type(obj).__name__}>"

def _safe_json_dumps(obj):
    return json.dumps(_json_safe(obj), ensure_ascii=False)

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from config import config


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()
        self._migrate_schema()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init_schema(self):
        with self.conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                window_ts INTEGER NOT NULL,
                market_slug TEXT,
                timeframe TEXT DEFAULT '5m',
                token_id TEXT,
                direction TEXT,
                entry_price REAL,
                size REAL,
                shares REAL,
                cost REAL,
                signal_data TEXT,
                status TEXT DEFAULT 'open',
                outcome TEXT,
                payout REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                is_shadow INTEGER DEFAULT 0,
                shadow_reason TEXT,
                resolved_at INTEGER,
                review_notes TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                start_balance REAL,
                end_balance REAL,
                trades_count INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS api_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                api_key TEXT,
                api_secret TEXT,
                passphrase TEXT,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS signal_stats (
                signal_type TEXT PRIMARY KEY,
                attempts INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                weight REAL DEFAULT 1.0,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS trade_reviews (
                trade_id INTEGER PRIMARY KEY,
                asset TEXT DEFAULT 'BTC',
                strategy_name TEXT,
                outcome TEXT,
                pnl REAL DEFAULT 0,
                entry_price REAL DEFAULT 0,
                implied_multiple REAL DEFAULT 0,
                grade TEXT,
                primary_reason TEXT,
                tags_json TEXT,
                metrics_json TEXT,
                recommendation TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS asset_strategy_stats (
                asset TEXT DEFAULT 'BTC',
                strategy_name TEXT,
                trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                shadow_trades INTEGER DEFAULT 0,
                shadow_wins INTEGER DEFAULT 0,
                shadow_pnl REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                avg_multiple REAL DEFAULT 0,
                max_multiple REAL DEFAULT 0,
                loss_streak INTEGER DEFAULT 0,
                weight REAL DEFAULT 1.0,
                updated_at INTEGER,
                PRIMARY KEY (asset, strategy_name)
            );


            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                window_ts INTEGER,
                market_slug TEXT,
                asset TEXT DEFAULT 'BTC',
                timeframe TEXT DEFAULT '5m',
                condition_id TEXT,
                price_to_beat REAL,
                btc_proxy_price REAL,
                up_best_ask REAL,
                up_best_bid REAL,
                down_best_ask REAL,
                down_best_bid REAL,
                up_spread REAL,
                down_spread REAL,
                up_depth REAL,
                down_depth REAL,
                volume_24h REAL DEFAULT 0,
                liquidity REAL DEFAULT 0,
                raw_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_window ON market_snapshots(window_ts);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON market_snapshots(timestamp);

            CREATE TABLE IF NOT EXISTS calibration_stats (
                bucket TEXT PRIMARY KEY,
                direction TEXT,
                attempts INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS public_trade_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                market_slug TEXT,
                condition_id TEXT,
                asset TEXT,
                side TEXT,
                outcome TEXT,
                price REAL,
                size REAL,
                tx_hash TEXT UNIQUE,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                market_slug TEXT,
                timeframe TEXT DEFAULT '5m',
                token_id TEXT,
                outcome TEXT,
                price REAL,
                raw_json TEXT,
                UNIQUE(timestamp, token_id, outcome)
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_token_ts ON price_history_samples(token_id, timestamp);

            CREATE TABLE IF NOT EXISTS decision_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                window_ts INTEGER,
                market_slug TEXT,
                asset TEXT DEFAULT 'BTC',
                timeframe TEXT DEFAULT '5m',
                direction TEXT,
                action TEXT,
                reason TEXT,
                model_probability REAL DEFAULT 0,
                token_price REAL DEFAULT 0,
                edge_after_fees REAL DEFAULT 0,
                details_json TEXT
            );

            CREATE TABLE IF NOT EXISTS order_attempts (
                window_ts INTEGER NOT NULL,
                asset TEXT DEFAULT 'BTC',
                timeframe TEXT DEFAULT '5m',
                market_slug TEXT,
                order_id TEXT,
                status TEXT,
                reason TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (asset, timeframe, window_ts)
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decision_logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_decisions_reason ON decision_logs(reason);
            CREATE INDEX IF NOT EXISTS idx_order_attempts_created ON order_attempts(created_at);

            CREATE TABLE IF NOT EXISTS execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                stage TEXT,
                window_ts INTEGER,
                asset TEXT DEFAULT 'BTC',
                timeframe TEXT DEFAULT '5m',
                market_slug TEXT,
                token_id TEXT,
                direction TEXT,
                reason TEXT,
                details_json TEXT,
                trade_id INTEGER,
                order_id TEXT,
                event_type TEXT,
                message TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_execution_events_ts ON execution_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_execution_events_reason ON execution_events(reason);
            CREATE INDEX IF NOT EXISTS idx_reviews_asset_strategy ON trade_reviews(asset, strategy_name);
            CREATE INDEX IF NOT EXISTS idx_reviews_created ON trade_reviews(created_at);

            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_ts);
            """)

    def _migrate_schema(self):
        """Add V2 columns without breaking old databases."""
        extra_cols = {
            "order_id": "TEXT",
            "order_status": "TEXT",
            "filled_shares": "REAL DEFAULT 0",
            "filled_cost": "REAL DEFAULT 0",
            "avg_price": "REAL DEFAULT 0",
            "model_probability": "REAL DEFAULT 0",
            "edge_after_fees": "REAL DEFAULT 0",
            "reference_price": "REAL DEFAULT 0",
            "market_question": "TEXT",
            "settlement_source": "TEXT",
            "final_price": "REAL DEFAULT 0",
            "review_version": "TEXT",
            "asset": "TEXT DEFAULT 'BTC'",
            # v14.2.23 order lifecycle / observability fields
            "attempting_at": "INTEGER",
            "submitted_at": "INTEGER",
            "submitted_order_id": "TEXT",
            "submission_response_json": "TEXT",
            "submission_error": "TEXT",
            "filled_at": "INTEGER",
            "first_fill_response_json": "TEXT",
            "cancel_attempt_count": "INTEGER DEFAULT 0",
            "last_status_check_at": "INTEGER",
            # v14.2.34 shadow trading / signer guard
            "is_shadow": "INTEGER DEFAULT 0",
            "shadow_reason": "TEXT",
        }
        with self.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
            for name, ddl in extra_cols.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
            # Add timeframe columns for 5m/15m support.
            for table in ("trades", "market_snapshots", "decision_logs", "order_attempts"):
                try:
                    tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                    if "timeframe" not in tcols:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN timeframe TEXT DEFAULT '5m'")
                except Exception:
                    pass

            # Add asset columns to public-data tables if upgrading an older single-asset database.
            for table, ddl in {
                "market_snapshots": "TEXT DEFAULT 'BTC'",
                "decision_logs": "TEXT DEFAULT 'BTC'",
            }.items():
                try:
                    tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                    if "asset" not in tcols:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN asset {ddl}")
                except Exception:
                    pass

            # V13.1 multi-asset: order_attempts must be per-asset, not globally keyed by window_ts.
            try:
                info = c.execute("PRAGMA table_info(order_attempts)").fetchall()
                cols2 = {r["name"] for r in info}
                pk_cols = [r["name"] for r in sorted(info, key=lambda x: x["pk"] or 999) if r["pk"]]
                if "asset" not in cols2 or pk_cols != ["asset", "timeframe", "window_ts"]:
                    c.execute("""
                        CREATE TABLE IF NOT EXISTS order_attempts_new (
                            window_ts INTEGER NOT NULL,
                            asset TEXT DEFAULT 'BTC',
                            timeframe TEXT DEFAULT '5m',
                            market_slug TEXT,
                            order_id TEXT,
                            status TEXT,
                            reason TEXT,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            PRIMARY KEY (asset, timeframe, window_ts)
                        )
                    """)
                    existing = c.execute("SELECT * FROM order_attempts").fetchall()
                    for row in existing:
                        rd = dict(row)
                        c.execute("""
                            INSERT OR REPLACE INTO order_attempts_new
                            (window_ts, asset, market_slug, order_id, status, reason, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            rd.get("window_ts"), rd.get("asset", "BTC") or "BTC", rd.get("market_slug", ""),
                            rd.get("order_id", ""), rd.get("status", ""), rd.get("reason", ""),
                            rd.get("created_at") or int(time.time()), rd.get("updated_at") or int(time.time()),
                        ))
                    c.execute("DROP TABLE order_attempts")
                    c.execute("ALTER TABLE order_attempts_new RENAME TO order_attempts")
            except Exception:
                pass
            # v14.2.24 execution_events observability columns.
            try:
                ecols = {r["name"] for r in c.execute("PRAGMA table_info(execution_events)").fetchall()}
                for name, ddl in {
                    "trade_id": "INTEGER",
                    "order_id": "TEXT",
                    "event_type": "TEXT",
                    "message": "TEXT",
                    "error": "TEXT",
                }.items():
                    if name not in ecols:
                        c.execute(f"ALTER TABLE execution_events ADD COLUMN {name} {ddl}")
                c.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_trade_id ON execution_events(trade_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_order_id ON execution_events(order_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_type ON execution_events(event_type)")
            except Exception:
                pass

            c.execute("""
                CREATE TABLE IF NOT EXISTS trade_reviews (
                    trade_id INTEGER PRIMARY KEY,
                    asset TEXT DEFAULT 'BTC',
                    strategy_name TEXT,
                    outcome TEXT,
                    pnl REAL DEFAULT 0,
                    entry_price REAL DEFAULT 0,
                    implied_multiple REAL DEFAULT 0,
                    grade TEXT,
                    primary_reason TEXT,
                    tags_json TEXT,
                    metrics_json TEXT,
                    recommendation TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS asset_strategy_stats (
                    asset TEXT DEFAULT 'BTC',
                    strategy_name TEXT,
                    trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    avg_entry_price REAL DEFAULT 0,
                    avg_multiple REAL DEFAULT 0,
                    max_multiple REAL DEFAULT 0,
                    loss_streak INTEGER DEFAULT 0,
                    weight REAL DEFAULT 1.0,
                    updated_at INTEGER,
                    PRIMARY KEY (asset, strategy_name)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_order_attempts_created ON order_attempts(created_at)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    stage TEXT,
                    window_ts INTEGER,
                    asset TEXT DEFAULT 'BTC',
                    timeframe TEXT DEFAULT '5m',
                    market_slug TEXT,
                    token_id TEXT,
                    direction TEXT,
                    reason TEXT,
                    details_json TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_ts ON execution_events(timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_execution_events_reason ON execution_events(reason)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_trades_asset_window ON trades(asset, window_ts)")
            # v14.2.36: create indexes that depend on migrated columns only after
            # the ALTER TABLE migration has ensured those columns exist.
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_shadow_key
                ON trades(is_shadow, status, asset, timeframe, window_ts, direction, token_id)
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_asset_strategy ON trade_reviews(asset, strategy_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_created ON trade_reviews(created_at)")
            # v14.2.34 shadow learning columns on existing asset_strategy_stats.
            try:
                scols = {r["name"] for r in c.execute("PRAGMA table_info(asset_strategy_stats)").fetchall()}
                for name, ddl in {
                    "shadow_trades": "INTEGER DEFAULT 0",
                    "shadow_wins": "INTEGER DEFAULT 0",
                    "shadow_pnl": "REAL DEFAULT 0",
                }.items():
                    if name not in scols:
                        c.execute(f"ALTER TABLE asset_strategy_stats ADD COLUMN {name} {ddl}")
            except Exception:
                pass

    # ============ Bot state ============
    def get_state(self, key: str, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value):
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), int(time.time()))
            )

    def reset_today_stats_view(self):
        """Start today's Telegram/stat view from the current moment. Trades remain in history."""
        self.set_state("stats_reset_ts", int(time.time()))
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.conn() as c:
            c.execute("DELETE FROM daily_stats WHERE date = ?", (date_str,))

    # ============ API credentials ============
    def save_api_creds(self, api_key: str, api_secret: str, passphrase: str):
        with self.conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO api_credentials
                (id, api_key, api_secret, passphrase, created_at)
                VALUES (1, ?, ?, ?, ?)
            """, (api_key, api_secret, passphrase, int(time.time())))

    def load_api_creds(self) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM api_credentials WHERE id = 1").fetchone()
            if row:
                return {
                    "api_key": row["api_key"],
                    "api_secret": row["api_secret"],
                    "passphrase": row["passphrase"]
                }
        return None

    def has_api_creds(self) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM api_credentials WHERE id = 1 LIMIT 1").fetchone()
            return row is not None

    def clear_api_creds(self) -> int:
        with self.conn() as c:
            cur = c.execute("DELETE FROM api_credentials")
            return int(cur.rowcount or 0)

    # ============ Trades ============
    def insert_trade(self, trade: dict) -> int:
        """Insert a trade/lifecycle row.

        v14.2.23 allows rows to start as status='attempted' before an order is
        actually accepted by Polymarket. Existing filled rows continue to work.
        """
        now = int(time.time())
        with self.conn() as c:
            cursor = c.execute("""
                INSERT INTO trades
                (timestamp, window_ts, market_slug, asset, timeframe, market_question, token_id, direction,
                 entry_price, size, shares, cost, filled_shares, filled_cost, avg_price,
                 order_id, order_status, model_probability, edge_after_fees, reference_price,
                 is_shadow, shadow_reason, signal_data, status, attempting_at, submitted_at, submitted_order_id,
                 submission_response_json, submission_error, filled_at, first_fill_response_json,
                 cancel_attempt_count, last_status_check_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(trade.get("timestamp") or now),
                trade["window_ts"],
                trade.get("market_slug", ""),
                trade.get("asset", "BTC"),
                trade.get("timeframe", "5m"),
                trade.get("market_question", ""),
                trade.get("token_id", ""),
                trade.get("direction", ""),
                float(trade.get("entry_price") or 0),
                float(trade.get("size") or 0),
                float(trade.get("shares") or 0),
                float(trade.get("cost") or 0),
                float(trade.get("filled_shares") or 0),
                float(trade.get("filled_cost") or 0),
                float(trade.get("avg_price") or trade.get("entry_price") or 0),
                trade.get("order_id", ""),
                trade.get("order_status", ""),
                float(trade.get("model_probability") or 0),
                float(trade.get("edge_after_fees") or 0),
                float(trade.get("reference_price") or 0),
                1 if trade.get("is_shadow") else 0,
                trade.get("shadow_reason", ""),
                _safe_json_dumps(trade.get("signal_data", {})),
                trade.get("status", "open"),
                trade.get("attempting_at"),
                trade.get("submitted_at"),
                trade.get("submitted_order_id", ""),
                json.dumps(trade.get("submission_response_json", {}), ensure_ascii=False) if isinstance(trade.get("submission_response_json"), (dict, list)) else trade.get("submission_response_json"),
                trade.get("submission_error", ""),
                trade.get("filled_at"),
                json.dumps(trade.get("first_fill_response_json", {}), ensure_ascii=False) if isinstance(trade.get("first_fill_response_json"), (dict, list)) else trade.get("first_fill_response_json"),
                int(trade.get("cancel_attempt_count") or 0),
                trade.get("last_status_check_at"),
            ))
            return cursor.lastrowid

    def update_trade_lifecycle(self, trade_id: int, **fields) -> None:
        """Update lifecycle fields on a trade row. Unknown fields are ignored."""
        allowed = {
            "status", "attempting_at", "submitted_at", "submitted_order_id", "submission_response_json",
            "submission_error", "filled_at", "first_fill_response_json", "cancel_attempt_count",
            "last_status_check_at", "order_id", "order_status", "filled_shares", "filled_cost",
            "avg_price", "shares", "cost", "entry_price", "size", "signal_data",
            "is_shadow", "shadow_reason",
        }
        updates = {}
        for key, val in fields.items():
            if key not in allowed:
                continue
            if key in {"submission_response_json", "first_fill_response_json", "signal_data"} and isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            updates[key] = val
        if not updates:
            return
        assignments = ", ".join([f"{k} = ?" for k in updates])
        params = list(updates.values()) + [int(trade_id)]
        with self.conn() as c:
            c.execute(f"UPDATE trades SET {assignments} WHERE id = ?", params)

    def get_recent_trade_lifecycles(self, limit: int = 20) -> list:
        limit = max(1, min(int(limit or 20), 50))
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM trades
                ORDER BY COALESCE(timestamp, attempting_at, submitted_at, id) DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_order_submission_details(self, limit: int = 5) -> list:
        limit = max(1, min(int(limit or 5), 20))
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM trades
                WHERE status IN ('attempted','submitted','matched','filled','unmatched','failed','cancelled')
                   OR submitted_at IS NOT NULL
                   OR submission_response_json IS NOT NULL
                   OR submission_error IS NOT NULL
                ORDER BY COALESCE(submitted_at, attempting_at, timestamp, id) DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_execution_events_for_trade(self, trade: dict, limit: int = 30) -> list:
        limit = max(1, min(int(limit or 30), 100))
        trade_id = int(trade.get("id") or 0)
        window_ts = int(trade.get("window_ts") or 0)
        asset = str(trade.get("asset") or "BTC").upper()
        timeframe = config.timeframe_slug(trade.get("timeframe") or "5m")
        market_slug = trade.get("market_slug") or ""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM execution_events
                WHERE (trade_id = ? AND ? > 0)
                   OR (window_ts = ? AND asset = ? AND timeframe = ?)
                   OR (? != '' AND market_slug = ?)
                ORDER BY id DESC
                LIMIT ?
            """, (trade_id, trade_id, window_ts, asset, timeframe, market_slug, market_slug, limit)).fetchall()
            return [dict(r) for r in rows]

    def resolve_trade(self, trade_id: int, outcome: str, payout: float, pnl: float,
                      notes: str = "", settlement_source: str = "", final_price: float = 0.0,
                      status: str = "resolved"):
        with self.conn() as c:
            c.execute("""
                UPDATE trades
                SET status = ?, outcome = ?, payout = ?, pnl = ?,
                    resolved_at = ?, review_notes = ?, settlement_source = ?, final_price = ?
                WHERE id = ?
            """, (status or "resolved", outcome, payout, pnl, int(time.time()), notes, settlement_source, final_price, trade_id))

    def get_existing_shadow_trade(self, *, asset: str, timeframe: str, window_ts: int,
                                  direction: str, token_id: str = "") -> Optional[dict]:
        """Return an existing shadow trade for the same market/window/outcome.

        This prevents the signer-guard shadow path from recording the same
        asset/timeframe/window/direction every few seconds while the live signal
        remains active. A resolved shadow row is also considered existing so the
        same historical window is not re-learned multiple times after restart.
        """
        asset = str(asset or "BTC").upper()
        timeframe = config.timeframe_slug(timeframe or "5m")
        direction = str(direction or "")
        token_id = str(token_id or "")
        with self.conn() as c:
            if token_id:
                row = c.execute("""
                    SELECT * FROM trades
                    WHERE COALESCE(is_shadow,0)=1
                      AND status IN ('shadow_open','shadow_resolved')
                      AND asset = ? AND timeframe = ? AND window_ts = ?
                      AND direction = ? AND token_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (asset, timeframe, int(window_ts or 0), direction, token_id)).fetchone()
            else:
                row = c.execute("""
                    SELECT * FROM trades
                    WHERE COALESCE(is_shadow,0)=1
                      AND status IN ('shadow_open','shadow_resolved')
                      AND asset = ? AND timeframe = ? AND window_ts = ?
                      AND direction = ?
                    ORDER BY id DESC LIMIT 1
                """, (asset, timeframe, int(window_ts or 0), direction)).fetchone()
            return dict(row) if row else None

    def get_shadow_open_trades(self) -> list:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE status = 'shadow_open' AND COALESCE(is_shadow,0)=1 ORDER BY timestamp ASC"
            ).fetchall()]

    def get_shadow_pnl_summary(self) -> dict:
        with self.conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(pnl),0) AS pnl,
                       COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),0) AS wins
                FROM trades WHERE COALESCE(is_shadow,0)=1 AND status='shadow_resolved'
            """).fetchone()
            open_row = c.execute("SELECT COUNT(*) AS n FROM trades WHERE COALESCE(is_shadow,0)=1 AND status='shadow_open'").fetchone()
            return {"resolved": int(row["n"] if row else 0), "wins": int(row["wins"] if row else 0), "pnl": float(row["pnl"] if row else 0.0), "open": int(open_row["n"] if open_row else 0)}

    def get_shadow_overdue_summary(self, grace_sec: int = 120) -> dict:
        """Count shadow rows that are past their close time + grace period."""
        grace = max(0, int(grace_sec or 0))
        now = int(time.time())
        with self.conn() as c:
            rows = c.execute("""
                SELECT id, window_ts, timeframe FROM trades
                WHERE COALESCE(is_shadow,0)=1 AND status='shadow_open'
            """).fetchall()
        overdue = 0
        oldest = 0
        for r in rows:
            sec = 900 if str(r["timeframe"] or "5m").lower() in {"15", "15m", "15min"} else 300
            late = now - (int(r["window_ts"] or 0) + sec)
            if late > grace:
                overdue += 1
                oldest = max(oldest, late)
        return {"open": len(rows), "overdue": overdue, "oldest_seconds_after_close": oldest}


    def mark_order_attempt(self, window_ts: int, market_slug: str = "", order_id: str = "", status: str = "attempting", reason: str = "", asset: str = "BTC", timeframe: str = "5m"):
        now = int(time.time())
        asset = str(asset or "BTC").upper()
        timeframe = config.timeframe_slug(timeframe)
        with self.conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO order_attempts
                (window_ts, asset, timeframe, market_slug, order_id, status, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM order_attempts WHERE asset=? AND timeframe=? AND window_ts=?), ?), ?)
            """, (int(window_ts), asset, timeframe, market_slug or "", order_id or "", status or "attempting", reason or "", asset, timeframe, int(window_ts), now, now))

    def has_recent_order_attempt(self, window_ts: int, lock_sec: int = None, asset: str = "BTC", timeframe: str = "5m") -> bool:
        """Return True only when a *meaningful* recent order attempt should cool down retries.

        Older builds locked the whole market window after any row in order_attempts, including
        failed/no-order submissions. In $1 live mode that made TG show "recent_order_attempt_lock"
        even when no order was accepted by Polymarket. This method now uses status-aware
        cooldowns so failed submissions can retry quickly while real submitted/live/unmatched
        attempts still avoid duplicate orders.
        """
        base_lock_sec = int(lock_sec or config.ORDER_ATTEMPT_LOCK_SEC)
        now = int(time.time())
        asset = str(asset or "BTC").upper()
        timeframe = config.timeframe_slug(timeframe)
        submitted_cutoff = now - max(1, base_lock_sec)
        submitting_cutoff = now - max(1, int(getattr(config, "SUBMITTING_ORDER_LOCK_SEC", 20) or 20))
        failed_cutoff = now - max(1, int(getattr(config, "FAILED_ORDER_RETRY_LOCK_SEC", 15) or 15))
        not_matched_cutoff = now - max(1, int(getattr(config, "NOT_MATCHED_ORDER_RETRY_LOCK_SEC", 45) or 45))
        with self.conn() as c:
            row = c.execute("""
                SELECT status, order_id, reason, created_at, updated_at FROM order_attempts
                WHERE asset = ? AND timeframe = ? AND window_ts = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            """, (asset, timeframe, int(window_ts))).fetchone()
            if row is None:
                return False
            status = str(row["status"] or "").lower()
            order_id = str(row["order_id"] or "").strip()
            updated_at = int(row["updated_at"] or row["created_at"] or 0)

            if status in {"matched", "submitted", "live", "delayed", "unmatched"}:
                return updated_at >= submitted_cutoff
            if status in {"not_matched", "unconfirmed"}:
                return updated_at >= not_matched_cutoff
            if status == "submitting":
                # v14.2.29: do not let pre-submit rows with no Polymarket order id
                # lock the whole window. They are only in-process markers; if the
                # process crashes or the submission path exits before post_order,
                # keeping them as locks creates the repeated recent_order_attempt_lock
                # bug seen in live logs.
                return bool(order_id) and updated_at >= submitting_cutoff
            if status == "attempting":
                # ``mark_order_attempt`` historically defaults to "attempting".
                # Treat it as a short, meaningful dedup lock, but not as a full
                # submitted-order lock unless a later status/order id arrives.
                return updated_at >= submitting_cutoff
            if status in {"pre_submit", "stale_pre_submit", "attempted", "dry_run"}:
                return False
            if status in {"submit_timeout_unknown", "timeout_unknown"}:
                timeout_cutoff = now - max(1, int(getattr(config, "SUBMIT_TIMEOUT_UNKNOWN_LOCK_SEC", 75) or 75))
                return updated_at >= timeout_cutoff
            if status in {"failed", "rejected", "error"}:
                # If Polymarket returned an order id, still pause briefly. If no order id exists,
                # this was a failed submission attempt and should not lock the full window.
                return bool(order_id) and updated_at >= failed_cutoff
            return False


    def cleanup_stale_pre_submit_attempts(self, stale_sec: int = 15) -> int:
        """Mark stale pre-submit/submitting rows that never received an order id.

        These rows should not behave as real order attempts. They are useful for
        observability, but if left as `submitting` they confuse operators and can
        accidentally look like an active lock.
        """
        now = int(time.time())
        cutoff = now - max(1, int(stale_sec or 15))
        with self.conn() as c:
            cur = c.execute("""
                UPDATE order_attempts
                SET status = 'stale_pre_submit',
                    reason = 'stale_submitting_no_order_id',
                    updated_at = ?
                WHERE status IN ('submitting', 'pre_submit')
                  AND (order_id IS NULL OR order_id = '')
                  AND updated_at < ?
            """, (now, cutoff))
            return int(cur.rowcount or 0)

    def has_trade_for_window(self, window_ts: int, asset: str = "BTC", timeframe: str = "5m") -> bool:
        asset = str(asset or "BTC").upper()
        with self.conn() as c:
            row = c.execute(
                "SELECT id FROM trades WHERE asset = ? AND timeframe = ? AND window_ts = ? AND status IN ('open','matched','filled','resolved') LIMIT 1",
                (asset, config.timeframe_slug(timeframe), window_ts)
            ).fetchone()
            return row is not None

    def get_asset_balance_used(self, asset: str) -> float:
        asset = str(asset or "BTC").upper()
        with self.conn() as c:
            row = c.execute("""
                SELECT COALESCE(SUM(COALESCE(filled_cost, cost, 0)), 0) AS used
                FROM trades WHERE asset = ? AND status IN ('open','matched','filled')
            """, (asset,)).fetchone()
            return float(row["used"] if row else 0.0)

    def count_open_trades(self) -> int:
        with self.conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM trades WHERE status IN ('open','matched','filled')").fetchone()
            return int(row["n"] if row else 0)

    def get_open_trades(self) -> list:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE status IN ('open','matched','filled') ORDER BY timestamp ASC"
            ).fetchall()]

    def get_active_market_tokens(self) -> set:
        """Return token ids that must remain subscribed on the public WS.

        This intentionally uses real open/matched/filled trades only. Expired
        candidate-market subscriptions are safe to drop because the next live
        scan will resubscribe current market tokens.
        """
        with self.conn() as c:
            rows = c.execute("""
                SELECT DISTINCT token_id FROM trades
                WHERE status IN ('open','matched','filled')
                  AND token_id IS NOT NULL AND token_id != ''
            """).fetchall()
            return {str(r["token_id"]) for r in rows if r["token_id"]}

    def get_recent_trades(self, limit: int = 10) -> list:
        """Recent *real* trades for the history widget.

        Do not show attempted/submitting/unmatched rows here; those belong in
        the order lifecycle page. This prevents misleading TG lines such as
        "❌ -$0.00" for orders that were never submitted or never filled.
        """
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """
                SELECT * FROM trades
                WHERE status IN ('open','matched','filled','resolved')
                  AND (COALESCE(filled_cost, 0) > 0 OR status = 'resolved' OR status = 'open')
                ORDER BY id DESC LIMIT ?
                """, (limit,)
            ).fetchall()]

    def get_trade_by_id(self, trade_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            return dict(row) if row else None

    def _today_start_ts(self) -> int:
        today_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        reset_ts = int(self.get_state("stats_reset_ts", 0) or 0)
        return max(today_start, reset_ts)

    def get_today_trades(self) -> list:
        start_ts = self._today_start_ts()
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """
                SELECT * FROM trades
                WHERE timestamp >= ?
                  AND status IN ('open','matched','filled','resolved','shadow_open','shadow_resolved')
                ORDER BY timestamp ASC
                """,
                (start_ts,)
            ).fetchall()]


    def get_today_outcome_stats(self, include_shadow: bool = True) -> dict:
        """Resolved outcome stats for today, used by the high-win-rate governor."""
        start_ts = self._today_start_ts()
        statuses = ("resolved", "closed", "settled", "shadow_resolved") if include_shadow else ("resolved", "closed", "settled")
        placeholders = ",".join(["?"] * len(statuses))
        with self.conn() as c:
            row = c.execute(f"""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),0) AS wins,
                       COALESCE(SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END),0) AS losses,
                       COALESCE(SUM(COALESCE(pnl,0)),0) AS pnl
                FROM trades
                WHERE timestamp >= ?
                  AND status IN ({placeholders})
                  AND outcome IN ('win','loss')
            """, (start_ts, *statuses)).fetchone()
        n = int(row["n"] if row else 0)
        wins = int(row["wins"] if row else 0)
        losses = int(row["losses"] if row else 0)
        pnl = float(row["pnl"] if row else 0.0)
        return {
            "trades": n,
            "attempts": n,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / n) if n > 0 else 0.0,
            "pnl": pnl,
            "since_ts": start_ts,
        }

    def get_strategy_outcome_stats(self, *, asset: str = "", timeframe: str = "",
                                   strategy_name: str = "", limit: int = 80,
                                   include_shadow: bool = True) -> dict:
        """Recent resolved stats for one strategy bucket.

        SQLite JSON support is not assumed, so signal_data is parsed in Python.
        """
        limit = max(1, min(int(limit or 80), 500))
        statuses = ("resolved", "closed", "settled", "shadow_resolved") if include_shadow else ("resolved", "closed", "settled")
        placeholders = ",".join(["?"] * len(statuses))
        where = [f"status IN ({placeholders})", "outcome IN ('win','loss')"]
        params = list(statuses)
        if asset:
            where.append("asset = ?")
            params.append(str(asset).upper())
        if timeframe:
            where.append("timeframe = ?")
            params.append(config.timeframe_slug(timeframe))
        sql = f"""
            SELECT id, asset, timeframe, direction, entry_price, outcome, pnl,
                   model_probability, signal_data
            FROM trades
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit * 5)
        wanted = str(strategy_name or "").lower()
        rows_out = []
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, tuple(params)).fetchall()]
        for r in rows:
            try:
                sig = json.loads(r.get("signal_data") or "{}")
            except Exception:
                sig = {}
            indicators = sig.get("indicators") if isinstance(sig.get("indicators"), dict) else {}
            strat = str(sig.get("strategy_name") or indicators.get("strategy_name") or sig.get("pattern") or "").lower()
            if strat.startswith("barrier_reclaim") or "barrier_reclaim" in strat:
                strat = "barrier_reclaim"
            if wanted and wanted not in {strat, str(r.get("direction") or "").lower()} and not strat.startswith(wanted):
                continue
            rows_out.append(r)
            if len(rows_out) >= limit:
                break
        n = len(rows_out)
        wins = sum(1 for r in rows_out if r.get("outcome") == "win")
        pnl = sum(float(r.get("pnl") or 0.0) for r in rows_out)
        loss_streak = 0
        for r in rows_out:
            if r.get("outcome") == "loss":
                loss_streak += 1
            else:
                break
        return {
            "trades": n,
            "attempts": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": (wins / n) if n > 0 else 0.0,
            "pnl": pnl,
            "avg_pnl": (pnl / n) if n > 0 else 0.0,
            "loss_streak": loss_streak,
            "limit": limit,
        }

    def get_today_trade_count(self) -> int:
        start_ts = self._today_start_ts()
        with self.conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS n FROM trades
                WHERE timestamp >= ?
                  AND COALESCE(is_shadow,0)=0
                  AND status IN ('open','matched','filled','resolved')
            """, (start_ts,)).fetchone()
            return int(row["n"] if row else 0)

    def get_today_order_attempt_count(self) -> int:
        start_ts = self._today_start_ts()
        with self.conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS n FROM order_attempts
                WHERE created_at >= ?
                  AND status IN ('submitted','matched','filled','live','delayed','unmatched')
                  AND COALESCE(order_id,'') != ''
            """, (start_ts,)).fetchone()
            return int(row["n"] if row else 0)

    def get_today_realized_pnl(self) -> float:
        start_ts = self._today_start_ts()
        with self.conn() as c:
            row = c.execute("""
                SELECT COALESCE(SUM(pnl), 0) AS pnl
                FROM trades
                WHERE status = 'resolved' AND COALESCE(is_shadow,0)=0 AND resolved_at >= ?
            """, (start_ts,)).fetchone()
            return float(row["pnl"] if row else 0.0)

    def get_consecutive_losses(self) -> int:
        with self.conn() as c:
            rows = c.execute("""
                SELECT outcome FROM trades
                WHERE status = 'resolved' AND COALESCE(is_shadow,0)=0
                ORDER BY resolved_at DESC LIMIT 20
            """).fetchall()
            count = 0
            for r in rows:
                if r["outcome"] == "loss":
                    count += 1
                else:
                    break
            return count

    # ============ Intelligent review / per-strategy stats ============
    def record_trade_review(self, review) -> None:
        """Persist a TradeReviewer.ReviewResult-like object."""
        with self.conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO trade_reviews
                (trade_id, asset, strategy_name, outcome, pnl, entry_price, implied_multiple,
                 grade, primary_reason, tags_json, metrics_json, recommendation, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(getattr(review, "trade_id", 0) or 0),
                str(getattr(review, "asset", "BTC") or "BTC").upper(),
                str(getattr(review, "strategy_name", "unknown") or "unknown"),
                str(getattr(review, "outcome", "") or ""),
                float(getattr(review, "pnl", 0.0) or 0.0),
                float(getattr(review, "entry_price", 0.0) or 0.0),
                float(getattr(review, "implied_multiple", 0.0) or 0.0),
                str(getattr(review, "grade", "") or ""),
                str(getattr(review, "primary_reason", "") or ""),
                json.dumps(getattr(review, "tags", []) or [], ensure_ascii=False),
                json.dumps(getattr(review, "metrics", {}) or {}, ensure_ascii=False),
                str(getattr(review, "recommendation", "") or ""),
                int(time.time()),
            ))

    def record_asset_strategy_outcome(self, asset: str, strategy_name: str, won: bool, pnl: float,
                                      entry_price: float = 0.0, implied_multiple: float = 0.0) -> None:
        asset = str(asset or "BTC").upper()
        strategy_name = str(strategy_name or "unknown")
        pnl = float(pnl or 0.0)
        entry_price = float(entry_price or 0.0)
        implied_multiple = float(implied_multiple or 0.0)
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM asset_strategy_stats WHERE asset = ? AND strategy_name = ?",
                (asset, strategy_name)
            ).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO asset_strategy_stats
                    (asset, strategy_name, trades, wins, losses, total_pnl, avg_entry_price,
                     avg_multiple, max_multiple, loss_streak, weight, updated_at)
                    VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, ?)
                """, (asset, strategy_name, int(time.time())))
                row = c.execute(
                    "SELECT * FROM asset_strategy_stats WHERE asset = ? AND strategy_name = ?",
                    (asset, strategy_name)
                ).fetchone()
            trades = int(row["trades"] or 0)
            new_trades = trades + 1
            new_avg_entry = ((float(row["avg_entry_price"] or 0) * trades) + entry_price) / new_trades
            new_avg_mult = ((float(row["avg_multiple"] or 0) * trades) + implied_multiple) / new_trades
            new_loss_streak = 0 if won else int(row["loss_streak"] or 0) + 1
            wins = int(row["wins"] or 0) + (1 if won else 0)
            losses = int(row["losses"] or 0) + (0 if won else 1)
            total_pnl = float(row["total_pnl"] or 0.0) + pnl
            win_rate = wins / new_trades if new_trades else 0.0
            avg_pnl = total_pnl / new_trades if new_trades else 0.0
            weight = self._compute_strategy_weight(new_trades, wins, total_pnl)
            c.execute("""
                UPDATE asset_strategy_stats
                SET trades = ?, wins = ?, losses = ?, total_pnl = ?, avg_entry_price = ?,
                    avg_multiple = ?, max_multiple = MAX(max_multiple, ?), loss_streak = ?,
                    weight = ?, updated_at = ?
                WHERE asset = ? AND strategy_name = ?
            """, (
                new_trades, wins, losses, total_pnl, new_avg_entry, new_avg_mult,
                implied_multiple, new_loss_streak, weight, int(time.time()), asset, strategy_name,
            ))

    def _compute_strategy_weight(self, total_trades: int, wins: int, total_pnl: float, *, min_samples: int | None = None) -> float:
        """Conservative weight helper shared by real and shadow learning.

        High-multiple strategies can have low win rates, so the weight gives PnL more
        importance than raw hit rate.  It still clamps aggressively to avoid overfitting
        small samples.
        """
        total_trades = int(total_trades or 0)
        wins = int(wins or 0)
        total_pnl = float(total_pnl or 0.0)
        min_samples = int(min_samples or config.REVIEW_MIN_SAMPLES_FOR_WEIGHT)
        if total_trades < min_samples:
            return 1.0
        win_rate = wins / total_trades if total_trades else 0.0
        avg_pnl = total_pnl / total_trades if total_trades else 0.0
        # Profitability drives the multiplier; hit-rate only nudges it.
        profit_component = 1.0 + max(-0.35, min(0.35, avg_pnl))
        win_component = 0.90 + max(0.0, min(0.25, win_rate - 0.20))
        weight = profit_component * win_component
        return max(config.REVIEW_WEIGHT_MIN, min(config.REVIEW_WEIGHT_MAX, weight))

    def record_shadow_asset_strategy_outcome(self, asset: str, strategy_name: str, won: bool, pnl: float,
                                             entry_price: float = 0.0, implied_multiple: float = 0.0) -> None:
        asset = str(asset or "BTC").upper()
        strategy_name = str(strategy_name or "unknown")
        pnl = float(pnl or 0.0)
        entry_price = float(entry_price or 0.0)
        implied_multiple = float(implied_multiple or (1.0 / entry_price if entry_price > 0 else 0.0) or 0.0)
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM asset_strategy_stats WHERE asset = ? AND strategy_name = ?",
                (asset, strategy_name)
            ).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO asset_strategy_stats
                    (asset, strategy_name, trades, wins, losses, total_pnl, shadow_trades, shadow_wins, shadow_pnl,
                     avg_entry_price, avg_multiple, max_multiple, loss_streak, weight, updated_at)
                    VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, ?)
                """, (asset, strategy_name, int(time.time())))
                row = c.execute(
                    "SELECT * FROM asset_strategy_stats WHERE asset = ? AND strategy_name = ?",
                    (asset, strategy_name)
                ).fetchone()

            old_shadow = int(row["shadow_trades"] or 0)
            new_shadow = old_shadow + 1
            new_shadow_wins = int(row["shadow_wins"] or 0) + (1 if won else 0)
            new_shadow_pnl = float(row["shadow_pnl"] or 0.0) + pnl
            old_avg_entry = float(row["avg_entry_price"] or 0.0)
            old_avg_mult = float(row["avg_multiple"] or 0.0)
            new_avg_entry = ((old_avg_entry * old_shadow) + entry_price) / new_shadow if new_shadow else entry_price
            new_avg_mult = ((old_avg_mult * old_shadow) + implied_multiple) / new_shadow if new_shadow else implied_multiple
            new_max_mult = max(float(row["max_multiple"] or 0.0), implied_multiple)
            old_loss_streak = int(row["loss_streak"] or 0)
            # Until there are real trades, the loss streak shown in TG should reflect Shadow behavior.
            real_trades = int(row["trades"] or 0)
            new_loss_streak = old_loss_streak if real_trades > 0 else (0 if won else old_loss_streak + 1)
            weight = float(row["weight"] or 1.0)
            if bool(getattr(config, "STRATEGY_WEIGHT_USE_SHADOW_DATA", True)) and real_trades == 0:
                weight = self._compute_strategy_weight(new_shadow, new_shadow_wins, new_shadow_pnl)

            c.execute("""
                UPDATE asset_strategy_stats
                SET shadow_trades = ?,
                    shadow_wins = ?,
                    shadow_pnl = ?,
                    avg_entry_price = ?,
                    avg_multiple = ?,
                    max_multiple = ?,
                    loss_streak = ?,
                    weight = ?,
                    updated_at = ?
                WHERE asset = ? AND strategy_name = ?
            """, (new_shadow, new_shadow_wins, new_shadow_pnl, new_avg_entry, new_avg_mult,
                  new_max_mult, new_loss_streak, weight, int(time.time()), asset, strategy_name))

    def get_asset_strategy_weight(self, asset: str, strategy_name: str) -> float:
        with self.conn() as c:
            row = c.execute(
                "SELECT weight, trades, shadow_trades FROM asset_strategy_stats WHERE asset = ? AND strategy_name = ?",
                (str(asset or "BTC").upper(), str(strategy_name or "unknown"))
            ).fetchone()
            if not row:
                return 1.0
            real_trades = int(row["trades"] or 0)
            shadow_trades = int(row["shadow_trades"] or 0) if "shadow_trades" in row.keys() else 0
            if real_trades >= config.REVIEW_MIN_SAMPLES_FOR_WEIGHT:
                return float(row["weight"] or 1.0)
            if bool(getattr(config, "STRATEGY_WEIGHT_USE_SHADOW_DATA", True)) and shadow_trades >= config.REVIEW_MIN_SAMPLES_FOR_WEIGHT:
                return float(row["weight"] or 1.0)
            return 1.0

    def get_asset_strategy_stats(self) -> list:
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM asset_strategy_stats
                ORDER BY total_pnl DESC, trades DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_recent_trade_reviews(self, limit: int = 10) -> list:
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM trade_reviews
                ORDER BY created_at DESC LIMIT ?
            """, (int(limit),)).fetchall()
            return [dict(r) for r in rows]


    # ============ V14 stream-flow stats ============
    def get_recent_decision_flow_stats(self, lookback_sec: int = 3600, limit: int = 1000) -> dict:
        """Return recent opportunity-stream pressure stats for adaptive gating.

        This deliberately uses already-written decision_logs, not a fixed daily top list.
        A candidate is counted when the trading engine actually evaluated or skipped it.
        """
        cutoff = int(time.time()) - max(60, int(lookback_sec or 3600))
        limit = max(10, min(int(limit or 1000), 5000))
        rows = []
        with self.conn() as c:
            rows = [dict(r) for r in c.execute("""
                SELECT timestamp, action, reason, model_probability, token_price, edge_after_fees, details_json
                FROM decision_logs
                WHERE timestamp >= ?
                ORDER BY id DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()]

        total = len(rows)
        if total <= 0:
            return {
                "lookback_sec": int(lookback_sec or 3600),
                "recent_decisions": 0,
                "candidates_per_min": 0.0,
                "quality_pass": 0,
                "quality_pass_ratio": 0.0,
                "orders": 0,
                "score_p50": 0.0,
                "score_p75": 0.0,
                "score_p90": 0.0,
                "top_score": 0.0,
            }

        scores = []
        quality_pass = 0
        orders = 0
        for r in rows:
            action = str(r.get("action") or "")
            reason = str(r.get("reason") or "")
            if action in {"quality_pass", "order_matched", "dry_run_signal"} or reason == "quality_gate_pass":
                quality_pass += 1
            if action in {"order_matched", "dry_run_signal"} or reason in {"trade_opened", "dry_run_not_submitted"}:
                orders += 1
            raw = r.get("details_json") or ""
            try:
                details = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
                score_val = None
                if isinstance(details, dict):
                    if "score" in details and isinstance(details["score"], (int, float)):
                        score_val = float(details["score"])
                    elif isinstance(details.get("score"), dict):
                        score_val = float(details["score"].get("final_score") or 0.0)
                    elif isinstance(details.get("details"), dict) and isinstance(details["details"].get("score"), dict):
                        score_val = float(details["details"]["score"].get("final_score") or 0.0)
                if score_val is not None and score_val > 0:
                    scores.append(score_val)
            except Exception:
                continue

        def pct(values, q):
            if not values:
                return 0.0
            ordered = sorted(values)
            idx = int(round((len(ordered) - 1) * float(q)))
            return float(ordered[max(0, min(len(ordered) - 1, idx))])

        lookback_min = max(1.0, float(lookback_sec or 3600) / 60.0)
        return {
            "lookback_sec": int(lookback_sec or 3600),
            "recent_decisions": total,
            "candidates_per_min": round(total / lookback_min, 4),
            "quality_pass": int(quality_pass),
            "quality_pass_ratio": round(quality_pass / max(1, total), 4),
            "orders": int(orders),
            "score_p50": round(pct(scores, 0.50), 4),
            "score_p75": round(pct(scores, 0.75), 4),
            "score_p90": round(pct(scores, 0.90), 4),
            "top_score": round(max(scores) if scores else 0.0, 4),
        }

    # ============ Daily stats ============
    def update_daily_stats(self, date_str: str, **kwargs):
        allowed = {"start_balance", "end_balance", "trades_count", "wins", "losses", "pnl"}
        with self.conn() as c:
            row = c.execute("SELECT * FROM daily_stats WHERE date = ?", (date_str,)).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO daily_stats (date, start_balance, end_balance, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (date_str, kwargs.get("start_balance", 0), kwargs.get("start_balance", 0), int(time.time())))

            for k, v in kwargs.items():
                if k not in allowed:
                    continue
                c.execute(
                    f"UPDATE daily_stats SET {k} = ?, updated_at = ? WHERE date = ?",
                    (v, int(time.time()), date_str)
                )

    def get_today_stats(self) -> dict:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.conn() as c:
            row = c.execute("SELECT * FROM daily_stats WHERE date = ?", (date_str,)).fetchone()
            return dict(row) if row else {
                "date": date_str, "start_balance": 0, "end_balance": 0,
                "trades_count": 0, "wins": 0, "losses": 0, "pnl": 0
            }

    # ============ Signal learning stats ============
    def record_signal_outcome(self, signal_type: str, won: bool, pnl: float):
        with self.conn() as c:
            row = c.execute("SELECT * FROM signal_stats WHERE signal_type = ?", (signal_type,)).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO signal_stats (signal_type, attempts, wins, total_pnl, weight, updated_at)
                    VALUES (?, 0, 0, 0, 1.0, ?)
                """, (signal_type, int(time.time())))

            c.execute("""
                UPDATE signal_stats
                SET attempts = attempts + 1,
                    wins = wins + ?,
                    total_pnl = total_pnl + ?,
                    updated_at = ?
                WHERE signal_type = ?
            """, (1 if won else 0, pnl, int(time.time()), signal_type))

    def get_signal_weight(self, signal_type: str) -> float:
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM signal_stats WHERE signal_type = ?", (signal_type,)
            ).fetchone()
            if not row or row["attempts"] < config.MIN_SAMPLES_FOR_LEARNING:
                return 1.0
            win_rate = row["wins"] / row["attempts"]
            # Conservative: learning nudges filters, it does not take over sizing/entry.
            raw = 1.0 + (win_rate - config.LEARNING_TARGET_WIN_RATE) * 0.5
            return max(config.LEARNING_WEIGHT_MIN, min(config.LEARNING_WEIGHT_MAX, raw))

    def get_all_signal_stats(self) -> list:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM signal_stats").fetchall()]

    # ============ Public data / calibration ============
    def insert_market_snapshot(self, snapshot: dict):
        with self.conn() as c:
            c.execute("""
                INSERT INTO market_snapshots
                (timestamp, window_ts, market_slug, asset, timeframe, condition_id, price_to_beat, btc_proxy_price,
                 up_best_ask, up_best_bid, down_best_ask, down_best_bid,
                 up_spread, down_spread, up_depth, down_depth, volume_24h, liquidity, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(snapshot.get("timestamp") or time.time()),
                snapshot.get("window_ts"),
                snapshot.get("market_slug", ""),
                snapshot.get("asset", "BTC"),
                config.timeframe_slug(snapshot.get("timeframe", "5m")),
                snapshot.get("condition_id", ""),
                snapshot.get("price_to_beat", 0.0),
                snapshot.get("btc_proxy_price", 0.0),
                snapshot.get("up_best_ask"),
                snapshot.get("up_best_bid"),
                snapshot.get("down_best_ask"),
                snapshot.get("down_best_bid"),
                snapshot.get("up_spread"),
                snapshot.get("down_spread"),
                snapshot.get("up_depth"),
                snapshot.get("down_depth"),
                snapshot.get("volume_24h", 0.0),
                snapshot.get("liquidity", 0.0),
                json.dumps(snapshot.get("raw", {}), ensure_ascii=False),
            ))

    def insert_public_trade_samples(self, trades: list):
        if not trades:
            return 0
        inserted = 0
        with self.conn() as c:
            for t in trades:
                tx = t.get("transactionHash") or t.get("transaction_hash") or f"{t.get('timestamp')}-{t.get('asset')}-{t.get('price')}-{t.get('size')}"
                try:
                    cur = c.execute("""
                        INSERT OR IGNORE INTO public_trade_samples
                        (timestamp, market_slug, condition_id, asset, side, outcome, price, size, tx_hash, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        int(t.get("timestamp") or time.time()),
                        t.get("slug") or t.get("market_slug") or "",
                        t.get("conditionId") or t.get("condition_id") or "",
                        t.get("asset") or "",
                        t.get("side") or "",
                        t.get("outcome") or "",
                        float(t.get("price") or 0),
                        float(t.get("size") or 0),
                        tx,
                        json.dumps(t, ensure_ascii=False),
                    ))
                    if cur.rowcount > 0:
                        inserted += 1
                except Exception:
                    continue
        return inserted

    def record_calibration_bucket(self, probability: float, direction: str, won: bool, pnl: float):
        step = max(0.01, float(config.CALIBRATION_BUCKET_SIZE))
        lo = int(max(0, min(0.99, probability)) / step) * step
        hi = min(1.0, lo + step)
        bucket = f"{direction.lower()}_{lo:.2f}_{hi:.2f}"
        with self.conn() as c:
            row = c.execute("SELECT bucket FROM calibration_stats WHERE bucket = ?", (bucket,)).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO calibration_stats (bucket, direction, attempts, wins, total_pnl, updated_at)
                    VALUES (?, ?, 0, 0, 0, ?)
                """, (bucket, direction, int(time.time())))
            c.execute("""
                UPDATE calibration_stats
                SET attempts = attempts + 1,
                    wins = wins + ?,
                    total_pnl = total_pnl + ?,
                    updated_at = ?
                WHERE bucket = ?
            """, (1 if won else 0, float(pnl or 0), int(time.time()), bucket))

    def calibrate_probability(self, probability: float, direction: str) -> float:
        """Shrink model probability toward empirical bucket result when enough samples exist."""
        p = max(0.0, min(0.98, float(probability or 0)))
        step = max(0.01, float(config.CALIBRATION_BUCKET_SIZE))
        lo = int(p / step) * step
        hi = min(1.0, lo + step)
        bucket = f"{direction.lower()}_{lo:.2f}_{hi:.2f}"
        with self.conn() as c:
            row = c.execute("SELECT * FROM calibration_stats WHERE bucket = ?", (bucket,)).fetchone()
            if not row or int(row["attempts"] or 0) < config.MIN_SAMPLES_FOR_CALIBRATION:
                return p
            empirical = float(row["wins"] or 0) / max(1, int(row["attempts"] or 1))
            strength = max(0.0, min(0.75, float(config.CALIBRATION_STRENGTH)))
            calibrated = p * (1 - strength) + empirical * strength
            return max(0.0, min(0.98, calibrated))

    def get_calibration_summary(self) -> list:
        with self.conn() as c:
            rows = c.execute("""
                SELECT *, CASE WHEN attempts > 0 THEN wins * 1.0 / attempts ELSE 0 END AS win_rate
                FROM calibration_stats ORDER BY attempts DESC, bucket ASC
            """).fetchall()
            return [dict(r) for r in rows]

    def insert_price_history_samples(self, market_slug: str, token_id: str, outcome: str, history: list):
        if not history:
            return 0
        inserted = 0
        with self.conn() as c:
            for h in history:
                try:
                    ts = int(h.get("t") or h.get("timestamp") or h.get("time") or time.time())
                    price = float(h.get("p") or h.get("price") or h.get("value") or 0)
                    cur = c.execute("""
                        INSERT OR IGNORE INTO price_history_samples
                        (timestamp, market_slug, token_id, outcome, price, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (ts, market_slug or "", token_id or "", outcome or "", price, json.dumps(h, ensure_ascii=False)))
                    if cur.rowcount > 0:
                        inserted += 1
                except Exception:
                    continue
        return inserted

    def get_recent_market_snapshots(self, asset: str, window_ts: int, limit: int = 8, timeframe: str = "5m") -> list:
        """Return recent orderbook/price snapshots for an asset/window, newest first."""
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM market_snapshots
                WHERE asset = ? AND timeframe = ? AND window_ts = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (str(asset or "BTC").upper(), config.timeframe_slug(timeframe), int(window_ts or 0), int(limit or 8))).fetchall()
            return [dict(r) for r in rows]

    def record_decision(self, *, window_ts=None, market_slug="", direction="", action="skip",
                        reason="", model_probability=0.0, token_price=0.0,
                        edge_after_fees=0.0, details=None, asset="BTC", timeframe="5m"):
        if not config.ENABLE_DECISION_LOGS:
            return
        with self.conn() as c:
            c.execute("""
                INSERT INTO decision_logs
                (timestamp, window_ts, market_slug, asset, timeframe, direction, action, reason,
                 model_probability, token_price, edge_after_fees, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(time.time()), window_ts, market_slug or "", str(asset or "BTC").upper(), config.timeframe_slug(timeframe), direction or "", action or "skip", reason or "",
                float(model_probability or 0), float(token_price or 0), float(edge_after_fees or 0),
                json.dumps(details or {}, ensure_ascii=False),
            ))

    def get_decision_summary(self, since_ts: int = 0):
        with self.conn() as c:
            rows = c.execute("""
                SELECT reason, action, COUNT(*) AS n
                FROM decision_logs
                WHERE timestamp >= ?
                GROUP BY reason, action
                ORDER BY n DESC
            """, (int(since_ts or 0),)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_decisions(self, limit: int = 20, since_ts: int = 0, action: str = "") -> list:
        """Return recent decision logs for Telegram inspection.

        This is intentionally read-only and never exposes secrets.
        action can be "skip", "dry_run_signal", "order_matched", or empty for all.
        """
        limit = max(1, min(int(limit or 20), 80))
        params = [int(since_ts or 0)]
        where = "timestamp >= ?"
        if action:
            where += " AND action = ?"
            params.append(str(action))
        params.append(limit)
        with self.conn() as c:
            rows = c.execute(f"""
                SELECT id, timestamp, window_ts, market_slug, asset, timeframe, direction, action, reason,
                       model_probability, token_price, edge_after_fees, details_json
                FROM decision_logs
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
            """, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def get_decision_reason_counts(self, since_ts: int = 0, action: str = "skip", limit: int = 12) -> list:
        """Aggregated skip/action reasons for Telegram.

        By default this answers the common question: why is the bot not trading?
        """
        limit = max(1, min(int(limit or 12), 30))
        params = [int(since_ts or 0)]
        where = "timestamp >= ?"
        if action:
            where += " AND action = ?"
            params.append(str(action))
        params.append(limit)
        with self.conn() as c:
            rows = c.execute(f"""
                SELECT COALESCE(reason, '') AS reason, COALESCE(action, '') AS action, COUNT(*) AS n, MAX(timestamp) AS last_ts
                FROM decision_logs
                WHERE {where}
                GROUP BY reason, action
                ORDER BY n DESC, last_ts DESC
                LIMIT ?
            """, tuple(params)).fetchall()
            return [dict(r) for r in rows]





    # ============ V14.2 execution diagnostics ============
    def record_execution_event(self, *, stage: str, window_ts=None, asset: str = "BTC", timeframe: str = "5m",
                               market_slug: str = "", token_id: str = "", direction: str = "",
                               reason: str = "", details=None, trade_id=None, order_id: str = "",
                               event_type: str = "", message: str = "", error: str = ""):
        details = details or {}
        if trade_id is None and isinstance(details, dict):
            trade_id = details.get("trade_id")
        if not order_id and isinstance(details, dict):
            order_id = details.get("order_id") or details.get("submitted_order_id") or ""
        if not event_type:
            event_type = str(stage or "")
        with self.conn() as c:
            c.execute("""
                INSERT INTO execution_events
                (timestamp, stage, window_ts, asset, timeframe, market_slug, token_id, direction, reason, details_json,
                 trade_id, order_id, event_type, message, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                int(time.time()), str(stage or ""), window_ts,
                str(asset or "BTC").upper(), config.timeframe_slug(timeframe),
                market_slug or "", token_id or "", direction or "", reason or "",
                json.dumps(details, ensure_ascii=False),
                int(trade_id) if str(trade_id or "").isdigit() else None,
                order_id or "", event_type or "", message or "", error or "",
            ))

    def get_recent_execution_events(self, lookback_sec: int = 3600, limit: int = 50) -> list:
        cutoff = int(time.time()) - max(1, int(lookback_sec or 3600))
        limit = max(1, min(int(limit or 50), 200))
        with self.conn() as c:
            rows = c.execute("""
                SELECT * FROM execution_events
                WHERE timestamp >= ?
                ORDER BY id DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_execution_event_stats(self, lookback_sec: int = 3600) -> dict:
        cutoff = int(time.time()) - max(1, int(lookback_sec or 3600))
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM execution_events WHERE timestamp >= ?", (cutoff,)).fetchone()["n"]
            by_reason = [dict(r) for r in c.execute("""
                SELECT COALESCE(reason, '') AS reason, COALESCE(stage, '') AS stage, COUNT(*) AS n, MAX(timestamp) AS last_ts
                FROM execution_events
                WHERE timestamp >= ?
                GROUP BY reason, stage
                ORDER BY n DESC, last_ts DESC
                LIMIT 20
            """, (cutoff,)).fetchall()]
        return {"lookback_sec": int(lookback_sec or 3600), "total": int(total or 0), "by_reason": by_reason}

    def get_quality_gate_funnel(self, since_ts: int = 0) -> dict:
        """Aggregate V14 gate records from decision_logs for Telegram.

        We intentionally reuse decision_logs instead of adding another hot table.
        Gate passes are action=quality_pass; gate rejections are action=skip with reasons starting quality_.
        """
        since_ts = int(since_ts or 0)
        with self.conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM decision_logs WHERE timestamp >= ?", (since_ts,)).fetchone()["n"]
            gate_rows = c.execute("""
                SELECT action, reason, COUNT(*) AS n
                FROM decision_logs
                WHERE timestamp >= ? AND (action IN ('quality_pass','dry_run_signal','order_matched') OR reason LIKE 'quality_%')
                GROUP BY action, reason
                ORDER BY n DESC
            """, (since_ts,)).fetchall()
            passed = c.execute("""
                SELECT COUNT(*) AS n FROM decision_logs
                WHERE timestamp >= ? AND action IN ('quality_pass','dry_run_signal','order_matched')
            """, (since_ts,)).fetchone()["n"]
            rejected = c.execute("""
                SELECT COUNT(*) AS n FROM decision_logs
                WHERE timestamp >= ? AND reason LIKE 'quality_%'
            """, (since_ts,)).fetchone()["n"]
            trades = c.execute("SELECT COUNT(*) AS n FROM trades WHERE timestamp >= ?", (since_ts,)).fetchone()["n"]
            return {
                "total_decisions": int(total or 0),
                "gate_passed": int(passed or 0),
                "gate_rejected": int(rejected or 0),
                "trades": int(trades or 0),
                "rows": [dict(r) for r in gate_rows],
            }

    def get_recent_quality_passes(self, limit: int = 8, since_ts: int = 0) -> list:
        limit = max(1, min(int(limit or 8), 30))
        with self.conn() as c:
            rows = c.execute("""
                SELECT id, timestamp, window_ts, market_slug, asset, timeframe, direction, action, reason,
                       model_probability, token_price, edge_after_fees, details_json
                FROM decision_logs
                WHERE timestamp >= ? AND action IN ('quality_pass','dry_run_signal','order_matched')
                ORDER BY id DESC
                LIMIT ?
            """, (int(since_ts or 0), limit)).fetchall()
            return [dict(r) for r in rows]

    def get_snapshot_summary(self, since_ts: int = 0):
        with self.conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS snapshots, COUNT(DISTINCT window_ts) AS windows,
                       AVG(CASE WHEN up_spread IS NOT NULL THEN up_spread END) AS avg_up_spread,
                       AVG(CASE WHEN down_spread IS NOT NULL THEN down_spread END) AS avg_down_spread,
                       AVG(CASE WHEN price_to_beat > 0 THEN ABS(btc_proxy_price - price_to_beat) END) AS avg_ref_gap
                FROM market_snapshots WHERE timestamp >= ?
            """, (int(since_ts or 0),)).fetchone()
            return dict(row) if row else {}

    def prune_public_data(self, keep_days: int = None):
        keep_days = int(keep_days or config.DATA_KEEP_DAYS)
        cutoff = int(time.time()) - keep_days * 86400
        with self.conn() as c:
            c.execute("DELETE FROM market_snapshots WHERE timestamp < ?", (cutoff,))
            c.execute("DELETE FROM public_trade_samples WHERE timestamp < ?", (cutoff,))
            c.execute("DELETE FROM price_history_samples WHERE timestamp < ?", (cutoff,))
            if bool(getattr(config, "PRUNE_DECISION_LOGS", True)):
                c.execute("DELETE FROM decision_logs WHERE timestamp < ?", (cutoff,))
            if bool(getattr(config, "PRUNE_EXECUTION_EVENTS", True)):
                c.execute("DELETE FROM execution_events WHERE timestamp < ?", (cutoff,))
            c.execute("DELETE FROM order_attempts WHERE created_at < ?", (cutoff,))

    def table_counts(self) -> dict:
        names = [
            "trades", "order_attempts", "decision_logs", "market_snapshots",
            "public_trade_samples", "price_history_samples", "calibration_stats", "signal_stats"
        ]
        out = {}
        with self.conn() as c:
            for name in names:
                try:
                    out[name] = int(c.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"] or 0)
                except Exception:
                    out[name] = None
        return out

