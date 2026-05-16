#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click startup gate for the BTC Polymarket 5m bot.

This script is the intended start.sh entrypoint:
  1) optional online preflight
  2) historical BTC backtest gate
  3) optional Polymarket CLOB replay for recent windows
  4) exec main.py only when safety gates allow it

Real orders are blocked if the required gate fails.
"""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence, Tuple, List

from config import config

ROOT = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[oneclick] {msg}", flush=True)


def run_cmd(args: Sequence[str], *, required_for_live: bool = False) -> int:
    log("$ " + " ".join(args))
    proc = subprocess.run(list(args), cwd=str(ROOT))
    if proc.returncode != 0:
        log(f"command exited with code {proc.returncode}")
    return proc.returncode


def utc_dates_for_lookback(days: int) -> tuple[str, str]:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=max(1, int(days)))
    # backtest.py treats YYYY-MM-DD end as inclusive day; use yesterday for finished candles.
    end = today - dt.timedelta(days=1)
    if end <= start:
        end = start + dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def load_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log(f"cannot read {path}: {e}")
        return None


def evaluate_backtest_result(out_dir):
    # type: (Path) -> Tuple[bool, str, Dict[str, Any]]
    """Return pass/fail against configured minimums."""
    metrics: Dict[str, Any] = {}
    if config.BACKTEST_SWEEP:
        sweep = load_json(out_dir / "sweep_results.json")
        if not isinstance(sweep, list) or not sweep:
            return False, "no sweep result rows", metrics
        best = sweep[0]
        metrics = dict(best)
        trades = int(best.get("trades") or 0)
        win_rate = float(best.get("win_rate") or 0.0)
        loss_streak = int(best.get("max_loss_streak") or 999999)
    else:
        summary = load_json(out_dir / "backtest_summary.json")
        if not isinstance(summary, dict):
            return False, "missing backtest_summary.json", metrics
        metrics = dict(summary)
        trades = int(summary.get("trades") or 0)
        win_rate = float(summary.get("win_rate") or 0.0)
        loss_streak = int(summary.get("max_loss_streak") or 999999)

    problems = []
    if trades < config.BACKTEST_MIN_TRADES:
        problems.append(f"trades {trades} < BACKTEST_MIN_TRADES {config.BACKTEST_MIN_TRADES}")
    if win_rate < config.BACKTEST_MIN_WIN_RATE:
        problems.append(f"win_rate {win_rate:.4f} < BACKTEST_MIN_WIN_RATE {config.BACKTEST_MIN_WIN_RATE:.4f}")
    if loss_streak > config.BACKTEST_MAX_LOSS_STREAK:
        problems.append(f"max_loss_streak {loss_streak} > BACKTEST_MAX_LOSS_STREAK {config.BACKTEST_MAX_LOSS_STREAK}")
    if problems:
        return False, "; ".join(problems), metrics
    return True, f"ok: trades={trades}, win_rate={win_rate:.4f}, max_loss_streak={loss_streak}", metrics


def write_gate_report(out_dir, status, reason, metrics):
    # type: (Path, str, str, Dict[str, Any]) -> None
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "version": config.VERSION,
        "status": status,
        "reason": reason,
        "mode": config.MODE,
        "dry_run": config.DRY_RUN,
        "real_orders_enabled": config.real_orders_enabled,
        "backtest_required_before_live": config.BACKTEST_REQUIRED_BEFORE_LIVE,
        "metrics": metrics,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (out_dir / "startup_gate_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))


def run_preflight_gate():
    # type: () -> bool
    if not config.AUTO_PREFLIGHT_ON_START:
        log("preflight skipped by AUTO_PREFLIGHT_ON_START=false")
        return True
    code = run_cmd([sys.executable, "preflight_check.py"])
    if code == 0:
        log("preflight passed")
        return True
    if config.real_orders_enabled:
        log("preflight failed; real orders are blocked")
        return False
    log("preflight failed, but DRY_RUN/non-live mode may continue for debugging")
    return True


def run_backtest_gate():
    # type: () -> bool
    if not config.AUTO_BACKTEST_ON_START and not (config.real_orders_enabled and config.BACKTEST_REQUIRED_BEFORE_LIVE):
        log("historical backtest skipped by AUTO_BACKTEST_ON_START=false")
        return True

    out_dir = ROOT / config.BACKTEST_OUT_DIR
    start, end = utc_dates_for_lookback(config.BACKTEST_LOOKBACK_DAYS)
    args = [
        sys.executable, "backtest.py",
        "--start", start,
        "--end", end,
        "--source", config.BACKTEST_SOURCE,
        "--symbol", config.BACKTEST_SYMBOL,
        "--cache-dir", config.BACKTEST_CACHE_DIR,
        "--out-dir", config.BACKTEST_OUT_DIR,
    ]
    if config.BACKTEST_SWEEP:
        args.extend([
            "--sweep",
            "--sweep-entries", config.BACKTEST_SWEEP_ENTRIES,
            "--sweep-probs", config.BACKTEST_SWEEP_PROBS,
            "--sweep-distances", config.BACKTEST_SWEEP_DISTANCES,
            "--min-sweep-trades", str(config.BACKTEST_MIN_TRADES),
        ])

    code = run_cmd(args)
    if code != 0:
        reason = f"backtest.py failed with exit code {code}"
        write_gate_report(out_dir, "failed", reason, {})
        if config.real_orders_enabled and config.BACKTEST_REQUIRED_BEFORE_LIVE:
            log(reason + "; real orders are blocked")
            return False
        if not config.ONECLICK_CONTINUE_ON_DRYRUN_BACKTEST_FAIL:
            return False
        log(reason + "; continuing only because DRY_RUN/non-live gate allows it")
        return True

    ok, reason, metrics = evaluate_backtest_result(out_dir)
    write_gate_report(out_dir, "passed" if ok else "failed", reason, metrics)
    if ok:
        log("historical backtest gate passed: " + reason)
        return True
    log("historical backtest gate failed: " + reason)
    if config.real_orders_enabled and config.BACKTEST_REQUIRED_BEFORE_LIVE:
        log("real orders are blocked until backtest gate passes")
        return False
    if not config.ONECLICK_CONTINUE_ON_DRYRUN_BACKTEST_FAIL:
        return False
    log("continuing only because DRY_RUN/non-live gate allows it")
    return True


def evaluate_polymarket_replay_summary(summary_path):
    # type: (Path) -> Tuple[bool, str, Dict[str, Any]]
    metrics: Dict[str, Any] = {}
    summary = load_json(summary_path)
    if not isinstance(summary, dict):
        return False, f"missing replay summary: {summary_path}", metrics
    metrics = dict(summary)
    priced = int(summary.get("priced_rows") or 0)
    win_rate = float(summary.get("win_rate_priced") or 0.0)
    avg_pnl = summary.get("avg_pnl_per_share_priced")
    avg_pnl = float(avg_pnl) if avg_pnl is not None else -999.0
    problems = []
    if priced < config.POLYMARKET_REPLAY_MIN_PRICED_ROWS:
        problems.append(f"priced_rows {priced} < POLYMARKET_REPLAY_MIN_PRICED_ROWS {config.POLYMARKET_REPLAY_MIN_PRICED_ROWS}")
    if win_rate < config.POLYMARKET_REPLAY_MIN_WIN_RATE:
        problems.append(f"win_rate_priced {win_rate:.4f} < POLYMARKET_REPLAY_MIN_WIN_RATE {config.POLYMARKET_REPLAY_MIN_WIN_RATE:.4f}")
    if avg_pnl < config.POLYMARKET_REPLAY_MIN_AVG_PNL_PER_SHARE:
        problems.append(f"avg_pnl_per_share {avg_pnl:.4f} < POLYMARKET_REPLAY_MIN_AVG_PNL_PER_SHARE {config.POLYMARKET_REPLAY_MIN_AVG_PNL_PER_SHARE:.4f}")
    if problems:
        return False, "; ".join(problems), metrics
    return True, f"ok: priced_rows={priced}, win_rate={win_rate:.4f}, avg_pnl/share={avg_pnl:.4f}", metrics


def run_polymarket_replay_if_enabled():
    # type: () -> bool
    must_run = config.POLYMARKET_REPLAY_ON_START or (config.real_orders_enabled and config.POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE)
    if not must_run:
        log("Polymarket replay skipped by POLYMARKET_REPLAY_ON_START=false")
        return True
    if config.real_orders_enabled and config.POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE:
        log("REAL MONEY REQUESTED: Polymarket CLOB replay gate is mandatory")
    start, end = utc_dates_for_lookback(config.POLYMARKET_REPLAY_DAYS)
    out_dir = ROOT / config.BACKTEST_OUT_DIR
    # Generate recent fixed-parameter signals first, because sweep does not write backtest_trades.csv.
    code = run_cmd([
        sys.executable, "backtest.py",
        "--start", start,
        "--end", end,
        "--source", config.BACKTEST_SOURCE,
        "--symbol", config.BACKTEST_SYMBOL,
        "--cache-dir", config.BACKTEST_CACHE_DIR,
        "--out-dir", config.BACKTEST_OUT_DIR,
        "--entry-seconds-left", str(max(config.ENTRY_MIN_SECONDS_LEFT, min(config.ENTRY_MAX_SECONDS_LEFT, 60))),
    ])
    if code != 0:
        log("recent signal generation for Polymarket replay failed")
        return not config.real_orders_enabled

    input_csv = out_dir / "backtest_trades.csv"
    output_csv = out_dir / "polymarket_replay_trades.csv"
    code = run_cmd([sys.executable, "polymarket_replay.py", "--input", str(input_csv), "--output", str(output_csv)])
    if code != 0:
        log("Polymarket replay failed")
        return not (config.real_orders_enabled and config.POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE)

    summary_path = output_csv.with_suffix(".summary.json")
    ok, reason, metrics = evaluate_polymarket_replay_summary(summary_path)
    write_gate_report(out_dir, "replay_passed" if ok else "replay_failed", reason, metrics)
    if ok:
        log("Polymarket replay gate passed: " + reason)
        return True
    log("Polymarket replay gate failed: " + reason)
    if config.real_orders_enabled and config.POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE:
        log("real orders are blocked until Polymarket replay gate passes")
        return False
    return True


def main():
    # type: () -> int
    log(f"{config.VERSION} startup gate | mode={config.mode_display} | DRY_RUN={config.DRY_RUN}")
    if config.real_orders_enabled:
        log("REAL MONEY REQUESTED: preflight and backtest gates are mandatory")
    else:
        log("non-live/dry-run: gates still run, but failures do not submit orders")

    if not run_preflight_gate():
        return 20
    if not run_backtest_gate():
        return 21
    if not run_polymarket_replay_if_enabled():
        return 22

    log("startup gates finished; launching main.py")
    os.execv(sys.executable, [sys.executable, str(ROOT / "main.py")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
