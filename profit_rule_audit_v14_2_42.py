#!/usr/bin/env python3
import sqlite3

con = sqlite3.connect("btc_bot.db")
con.row_factory = sqlite3.Row

print("==============================")
print("Profit rules by rating")
print("==============================")
for r in con.execute("""
SELECT rating, COUNT(*) AS n, ROUND(SUM(total_pnl),2) AS pnl
FROM profit_rules
GROUP BY rating
ORDER BY n DESC
"""):
    print(dict(r))

print("\n==============================")
print("Top positive rules")
print("==============================")
for r in con.execute("""
SELECT rule_type, rating, strategy_name, asset, timeframe, direction, entry_bucket, prob_bucket, gap_bucket,
       attempts, wins, losses, ROUND(win_rate*100,1) AS win_rate_pct,
       ROUND(total_pnl,2) AS pnl, ROUND(recent_pnl,2) AS recent_pnl,
       loss_streak, reason
FROM profit_rules
WHERE total_pnl > 0
ORDER BY total_pnl DESC
LIMIT 20
"""):
    print(dict(r))

print("\n==============================")
print("Rejected/frozen rules")
print("==============================")
for r in con.execute("""
SELECT rule_type, rating, strategy_name, asset, timeframe, direction, entry_bucket, prob_bucket, gap_bucket,
       attempts, wins, losses, ROUND(total_pnl,2) AS pnl, ROUND(recent_pnl,2) AS recent_pnl,
       loss_streak, reason, datetime(frozen_until,'unixepoch','localtime') AS frozen_until
FROM profit_rules
WHERE rating='REJECT' OR frozen_until > strftime('%s','now')
ORDER BY updated_at DESC
LIMIT 30
"""):
    print(dict(r))

print("\n==============================")
print("Recent profit rule events")
print("==============================")
for r in con.execute("""
SELECT id, datetime(timestamp,'unixepoch','localtime') AS ts, action, mode, reason,
       strategy_name, asset, timeframe, direction, ROUND(entry_price,4) AS entry,
       ROUND(model_probability,3) AS prob, ROUND(model_market_gap,3) AS gap,
       ROUND(net_profit_multiple,2) AS mult, rule_rating
FROM profit_rule_events
ORDER BY id DESC
LIMIT 30
"""):
    print(dict(r))
