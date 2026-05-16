"""
Local analytics report for V6 big-data layer.

Run after collecting data:
    python3 analytics_report.py

It summarizes snapshots, decision logs, calibration buckets, signal stats, and recent trades.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from config import config


def rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else {}


def pct(x):
    if x is None:
        return "-"
    return f"{float(x) * 100:.1f}%"


def main():
    since = int(time.time()) - 24 * 3600
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"Analytics report for {config.VERSION}")
    size_mb = os.path.getsize(config.DB_PATH) / (1024 * 1024) if os.path.exists(config.DB_PATH) else 0
    print(f"DB: {config.DB_PATH} ({size_mb:.2f} MB)")
    print(f"Window: last 24h since {datetime.fromtimestamp(since, tz=timezone.utc).isoformat()}")
    print("=" * 72)

    print("\n🗄️ Table counts")
    for name in ["trades", "trade_reviews", "asset_strategy_stats", "order_attempts", "decision_logs", "market_snapshots", "public_trade_samples", "price_history_samples", "calibration_stats", "signal_stats"]:
        try:
            r = one(conn, f"SELECT COUNT(*) AS n FROM {name}")
            print(f"{name:<24} {r.get('n', 0)}")
        except sqlite3.OperationalError:
            print(f"{name:<24} missing")

    snap = one(conn, """
        SELECT COUNT(*) AS snapshots, COUNT(DISTINCT window_ts) AS windows,
               AVG(up_spread) AS avg_up_spread, AVG(down_spread) AS avg_down_spread,
               AVG(CASE WHEN price_to_beat > 0 THEN ABS(btc_proxy_price - price_to_beat) END) AS avg_ref_gap,
               MAX(CASE WHEN price_to_beat > 0 THEN ABS(btc_proxy_price - price_to_beat) END) AS max_ref_gap
        FROM market_snapshots WHERE timestamp >= ?
    """, (since,))
    print("\n📊 Market snapshots")
    print(json.dumps(snap, ensure_ascii=False, indent=2))

    print("\n🧠 Decision reasons")
    for r in rows(conn, """
        SELECT action, reason, COUNT(*) AS n,
               AVG(model_probability) AS avg_model_prob,
               AVG(token_price) AS avg_token_price,
               AVG(edge_after_fees) AS avg_edge
        FROM decision_logs
        WHERE timestamp >= ?
        GROUP BY action, reason
        ORDER BY n DESC
        LIMIT 20
    """, (since,)):
        print(f"{r['n']:5d} | {r['action']:<15} | {r['reason']:<28} | p={r['avg_model_prob'] or 0:.3f} price={r['avg_token_price'] or 0:.3f} edge={r['avg_edge'] or 0:.3f}")

    print("\n🎯 Calibration buckets")
    for r in rows(conn, """
        SELECT bucket, direction, attempts, wins, total_pnl,
               CASE WHEN attempts > 0 THEN wins*1.0/attempts ELSE 0 END AS win_rate
        FROM calibration_stats
        ORDER BY attempts DESC, bucket ASC
        LIMIT 30
    """):
        print(f"{r['bucket']:<18} attempts={r['attempts']:<4d} win={pct(r['win_rate']):>7} pnl={r['total_pnl']:+.2f}")

    print("\n📌 Signal stats")
    for r in rows(conn, """
        SELECT signal_type, attempts, wins, total_pnl, weight,
               CASE WHEN attempts > 0 THEN wins*1.0/attempts ELSE 0 END AS win_rate
        FROM signal_stats
        ORDER BY attempts DESC, signal_type ASC
        LIMIT 30
    """):
        print(f"{r['signal_type']:<36} attempts={r['attempts']:<4d} win={pct(r['win_rate']):>7} pnl={r['total_pnl']:+.2f} weight={r['weight']:.2f}")

    print("\n🧾 Order attempts")
    try:
        for r in rows(conn, """
            SELECT status, reason, COUNT(*) AS n, MAX(datetime(updated_at, 'unixepoch')) AS last_update
            FROM order_attempts
            WHERE created_at >= ?
            GROUP BY status, reason
            ORDER BY n DESC
            LIMIT 20
        """, (since,)):
            print(f"{r['n']:5d} | {r['status']:<14} | {r['reason']:<22} | last={r['last_update']}")
    except sqlite3.OperationalError:
        print("order_attempts table not found; run main.py/preflight once to migrate DB")

    print("\n💰 Recent trades")
    for r in rows(conn, """
        SELECT id, datetime(timestamp, 'unixepoch') AS ts, direction, status, outcome, pnl,
               model_probability, edge_after_fees, settlement_source
        FROM trades ORDER BY id DESC LIMIT 10
    """):
        print(f"#{r['id']:<4d} {r['ts']} {r['direction']:<4} {r['status']:<8} {str(r['outcome']):<5} pnl={r['pnl']:+.2f} p={r['model_probability']:.3f} edge={r['edge_after_fees']:.3f} src={r['settlement_source']}")

    print("\nSuggested use:")
    print("- If fee_adjusted_edge_low dominates, reduce expectations; do not lower MIN_EDGE_AFTER_FEES until you have enough resolved samples.")
    print("- If missing_price_to_beat dominates, market parsing/rules source must be fixed before real trading.")
    print("- If binary_book_sanity_failed dominates, orderbook is unstable/wide; keep FOK and skip.")


if __name__ == "__main__":
    main()
