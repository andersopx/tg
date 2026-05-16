#!/usr/bin/env python3
"""Rebuild per-asset/per-strategy learning stats from resolved trades.

This is intentionally conservative and uses the existing Database methods so it
matches runtime behavior. It does not delete trades, trade_reviews, keys, or
runtime state.
"""
import json
import os
import sys
from pathlib import Path

from database import Database


def _strategy_from_signal(sig: dict) -> str:
    indicators = sig.get("indicators") or {}
    return str(sig.get("strategy_name") or indicators.get("strategy_name") or sig.get("pattern") or "unknown")


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "btc_bot.db"
    db_path = str(Path(db_path))
    if not os.path.exists(db_path):
        print(f"DB_NOT_FOUND {db_path}")
        return 2
    db = Database(db_path)
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT * FROM trades
            WHERE status IN ('resolved','shadow_resolved')
              AND outcome IN ('win','loss')
            ORDER BY id ASC
            """
        ).fetchall()
        c.execute("DELETE FROM asset_strategy_stats")
    rebuilt = 0
    for r in rows:
        d = dict(r)
        try:
            sig = json.loads(d.get("signal_data") or "{}")
        except Exception:
            sig = {}
        asset = str(d.get("asset") or sig.get("asset") or "BTC").upper()
        strategy = _strategy_from_signal(sig)
        won = str(d.get("outcome") or "") == "win"
        pnl = float(d.get("pnl") or 0.0)
        entry = float(d.get("avg_price") or d.get("entry_price") or 0.0)
        implied_multiple = (1.0 / entry) if entry > 0 else 0.0
        if str(d.get("status")) == "shadow_resolved" or int(d.get("is_shadow") or 0):
            db.record_shadow_asset_strategy_outcome(asset, strategy, won, pnl, entry, implied_multiple)
        else:
            db.record_asset_strategy_outcome(asset, strategy, won, pnl, entry, implied_multiple)
        rebuilt += 1
    print(f"LEARNING_STATS_REBUILT rows={rebuilt}")
    with db.conn() as c:
        summary = c.execute(
            """
            SELECT asset, strategy_name, trades, wins, losses, total_pnl,
                   shadow_trades, shadow_wins, shadow_pnl,
                   ROUND(avg_entry_price,4) AS avg_entry_price,
                   ROUND(avg_multiple,2) AS avg_multiple,
                   ROUND(max_multiple,2) AS max_multiple,
                   loss_streak, ROUND(weight,3) AS weight
            FROM asset_strategy_stats
            ORDER BY shadow_pnl DESC, total_pnl DESC
            """
        ).fetchall()
        for row in summary:
            print(dict(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
