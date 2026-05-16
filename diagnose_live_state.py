#!/usr/bin/env python3
"""V14.2.20 live diagnostics for skip/order issues.
Safe: reads .env, logs and SQLite only; does not trade or modify credentials.
"""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

try:
    from config import config
except Exception:
    class _DiagConfig:
        VERSION = "unknown"
    config = _DiagConfig()

DB = Path("btc_bot.db")
ENV = Path(".env")

def sh(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT, timeout=15)
    except Exception as e:
        return f"[command failed] {cmd}\n{e}"

def env_value(key: str) -> str:
    if not ENV.exists():
        return ""
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""

def q(*args):
    # Backward-compatible: q(sql) or q(db, sql).
    sql = args[-1] if args else ""
    if not DB.exists():
        return []
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    except Exception as e:
        print(f"[SQL failed] {e}\n{sql.strip()[:240]}")
        return []
    finally:
        con.close()

def ms_from_seconds(seconds):
    try:
        return int(float(seconds or 0) * 1000)
    except Exception:
        return 0

def bar(n, total, width=24):
    if total <= 0:
        return ""
    k = int(round(width * n / total))
    return "█" * k + "░" * (width-k)


def barrier_reclaim_stats(db):
    print("\n【目标线回收专项统计｜最近1小时】")
    try:
        rows = q(db, """
            SELECT reason, COUNT(*) AS n
            FROM decision_logs
            WHERE timestamp >= strftime('%s','now','-1 hour')
              AND lower(details_json) LIKE '%barrier_reclaim%'
            GROUP BY reason
            ORDER BY n DESC
            LIMIT 20
        """)
        if not rows:
            print("暂无 barrier_reclaim 记录")
        for r in rows:
            print(f"{r['reason']}: {r['n']}")
        tiers = q(db, """
            SELECT
              CASE
                WHEN token_price > 0 AND token_price < 0.20 THEN '深度埋伏 0.05-0.19'
                WHEN token_price >= 0.20 AND token_price <= 0.45 THEN '标准埋伏 0.20-0.45'
                WHEN token_price > 0.45 AND token_price <= 0.90 THEN '确认/较高 0.45-0.90'
                WHEN token_price > 0.90 THEN '高价不追 >0.90'
                ELSE '无价格'
              END AS tier,
              COUNT(*) AS n
            FROM decision_logs
            WHERE timestamp >= strftime('%s','now','-1 hour')
              AND lower(details_json) LIKE '%barrier_reclaim%'
            GROUP BY tier
            ORDER BY n DESC
        """)
        print("\n分档：")
        for r in tiers:
            print(f"{r['tier']}: {r['n']}")
    except Exception as e:
        print(f"读取目标线回收专项统计失败: {e}")

def main():
    print(f"【{config.VERSION} 现场诊断】")
    print("目录:", os.getcwd())
    print("版本:", sh("grep -n 'VERSION' config.py | head -1").strip())
    print("\n【执行参数】")
    for k in [
        "MARKET_BUY_SLIPPAGE", "EXECUTION_GUARD_SLIPPAGE_CAP", "QUALITY_MAX_PRICE_IMPACT",
        "EXECUTION_GUARD_PRICE_JUMP_CAP", "TARGET_SPREAD_MAX", "QUALITY_MAX_SPREAD",
        "REALTIME_ORDERBOOK_ENABLED", "HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED",
        "HIGH_CONFIDENCE_TOKEN_RANGE_MIN_PROB", "HIGH_CONFIDENCE_TOKEN_RANGE_MIN_EDGE",
        "HIGH_CONFIDENCE_TOKEN_RANGE_MAX_TOKEN_PRICE",
        "BARRIER_RECLAIM_EXECUTION_SLIPPAGE_CAP",
        "BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP",
        "BARRIER_RECLAIM_CONFIRM_EXECUTION_SLIPPAGE_CAP",
        "BARRIER_RECLAIM_MAX_WEIGHTED_AVG_PRICE",
        "BARRIER_RECLAIM_DEEP_MAX_WEIGHTED_AVG_PRICE",
        "BARRIER_RECLAIM_CONFIRM_MAX_WEIGHTED_AVG_PRICE",
    ]:
        print(f"{k}={env_value(k)}")

    print("\n【最近1小时动作统计】")
    rows = q("""
        SELECT action, COUNT(*) n FROM decision_logs
        WHERE timestamp > strftime('%s','now','-1 hour')
        GROUP BY action ORDER BY n DESC
    """)
    for r in rows:
        print(f"{r['action']}: {r['n']}")

    print("\n【最近1小时跳过原因】")
    rows = q("""
        SELECT reason, COUNT(*) n FROM decision_logs
        WHERE timestamp > strftime('%s','now','-1 hour') AND action='skip'
        GROUP BY reason ORDER BY n DESC LIMIT 20
    """)
    total = sum(int(r['n']) for r in rows) or 1
    for r in rows:
        print(f"{r['reason']:<38} {r['n']:>5} {bar(int(r['n']), total)}")

    print("\n【各币种记录数】")
    rows = q("""
        SELECT asset, COUNT(*) n FROM decision_logs
        WHERE timestamp > strftime('%s','now','-1 hour')
        GROUP BY asset ORDER BY asset
    """)
    for r in rows:
        print(f"{r['asset']}: {r['n']}")

    print("\n【token_price_outside_range 价格分布】")
    rows = q("""
        SELECT asset,
          CASE WHEN token_price < 0.10 THEN '0.00-0.10'
               WHEN token_price < 0.30 THEN '0.10-0.30'
               WHEN token_price < 0.50 THEN '0.30-0.50'
               WHEN token_price < 0.70 THEN '0.50-0.70'
               WHEN token_price < 0.90 THEN '0.70-0.90'
               ELSE '0.90-1.00' END band,
          COUNT(*) n
        FROM decision_logs
        WHERE timestamp > strftime('%s','now','-1 hour')
          AND reason='token_price_outside_range' AND token_price > 0
        GROUP BY asset, band ORDER BY asset, n DESC
    """)
    for r in rows:
        print(f"{r['asset']} {r['band']}: {r['n']}")

    print("\n【最近 orderbook_missing 明细】")
    rows = q("""
        SELECT datetime(timestamp,'unixepoch','localtime') ts, asset, timeframe, market_slug, direction, details_json
        FROM decision_logs WHERE reason='orderbook_missing'
        ORDER BY timestamp DESC LIMIT 8
    """)
    for r in rows:
        details = r.get('details_json') or '{}'
        try:
            details = json.dumps(json.loads(details), ensure_ascii=False)[:220]
        except Exception:
            details = str(details)[:220]
        print(f"{r['ts']} {r['asset']} {r['timeframe']} {r['direction']} {r['market_slug']} {details}")

    print("\n【过去24小时订单生命周期统计】")
    rows = q("""
        SELECT COALESCE(status,'unknown') status, COUNT(*) n
        FROM trades
        WHERE COALESCE(timestamp, attempting_at, submitted_at, 0) > strftime('%s','now','-24 hours')
        GROUP BY status ORDER BY n DESC
    """)
    status_counts = {r['status']: int(r['n']) for r in rows}
    for st in ['attempted','submitted','matched','filled','unmatched','failed','cancelled','resolved','open']:
        print(f"{st}: {status_counts.get(st, 0)}")
    attempted = sum(status_counts.get(st, 0) for st in ['attempted','submitted','matched','filled','unmatched','failed','cancelled','open'])
    submitted = sum(status_counts.get(st, 0) for st in ['submitted','matched','filled','unmatched','cancelled','open'])
    filled = status_counts.get('filled', 0) + status_counts.get('open', 0)
    print("\n【订单转化率】")
    print(f"attempted → submitted: {submitted}/{attempted} ({submitted/attempted:.1%})" if attempted else "attempted → submitted: 无样本")
    print(f"submitted → filled: {filled}/{submitted} ({filled/submitted:.1%})" if submitted else "submitted → filled: 无样本")

    print("\n【提交失败原因 TOP5】")
    rows = q("""
        SELECT COALESCE(NULLIF(submission_error,''), 'unknown') err, COUNT(*) n
        FROM trades
        WHERE COALESCE(timestamp, attempting_at, submitted_at, 0) > strftime('%s','now','-24 hours')
          AND (status='failed' OR submission_error IS NOT NULL AND submission_error != '')
        GROUP BY err ORDER BY n DESC LIMIT 5
    """)
    if not rows:
        print("暂无提交失败记录")
    for r in rows:
        print(f"{str(r['err'])[:120]}: {r['n']}")

    print("\n【订单提交耗时】")
    rows = q("""
        SELECT (submitted_at - attempting_at) AS latency_sec
        FROM trades
        WHERE submitted_at IS NOT NULL AND attempting_at IS NOT NULL AND submitted_at >= attempting_at
          AND COALESCE(timestamp, attempting_at, submitted_at, 0) > strftime('%s','now','-24 hours')
        ORDER BY latency_sec
    """)
    if not rows:
        print("暂无 submitted_at/attempting_at 样本")
    else:
        vals = [float(r['latency_sec'] or 0) for r in rows]
        def pct(p):
            idx = min(len(vals)-1, max(0, int(round((len(vals)-1)*p))))
            return vals[idx]
        avg = sum(vals)/len(vals)
        print(f"平均: {ms_from_seconds(avg)}ms")
        print(f"P95: {ms_from_seconds(pct(0.95))}ms")
        print(f"P99: {ms_from_seconds(pct(0.99))}ms")

    print("\n【WS 健康度摘要】")
    ws_lines = sh("journalctl -u btc-bot --since '24 hours ago' | grep -iE 'Market WS|polymarket_ws|websocket|reconnect|ws_' | tail -120")
    reconnects = sum(1 for line in ws_lines.splitlines() if 'reconnect' in line.lower() or 'disconnected' in line.lower())
    msg_lines = [line for line in ws_lines.splitlines() if 'message' in line.lower() or 'book' in line.lower() or 'price_change' in line.lower()]
    print(f"最近日志行数: {len(ws_lines.splitlines())}")
    print(f"疑似重连/断线次数: {reconnects}")
    print(f"疑似消息相关行数: {len(msg_lines)}")

    print("\n【最近15分钟 WS 日志】")
    print(sh("journalctl -u btc-bot --since '15 minutes ago' | grep -iE 'Market WS|polymarket_ws|websocket|reconnect|ws_' | tail -40"))

if __name__ == "__main__":
    main()
