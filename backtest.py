#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Historical backtester for the BTC Polymarket 5m Up/Down bot.

Two different questions are intentionally separated:
1) BTC proxy backtest: uses years of BTCUSDT 1m candles to test direction quality.
   This is fast and useful for parameter selection, but it does NOT prove exact
   Polymarket fill price because the market resolves with Chainlink and trades
   through Polymarket CLOB.
2) Polymarket replay hooks: optional helpers can be used for shorter ranges to
   enrich exact markets with Gamma/CLOB price-history when available.

Default mode is BTC proxy, because it gives much more sample size.
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from config import config
except Exception:
    config = None

BINANCE_SPOT_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_VISION_MONTHLY = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{ym}.zip"
POLY_GAMMA = os.getenv("POLYMARKET_GAMMA", "https://gamma-api.polymarket.com")
POLY_CLOB = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def ts(self) -> int:
        return self.open_time_ms // 1000


@dataclass
class BacktestParams:
    entry_seconds_left: int = 60
    vol_lookback_min: int = 20
    momentum_lookback_min: int = 3
    momentum_weight: float = 0.35
    min_price_distance_usd: float = 35.0
    min_distance_sigma_mult: float = 0.45
    min_model_prob: float = 0.62
    pin_bar_tail_ratio: float = 2.0
    pin_bar_body_max_ratio: float = 0.35
    pin_bar_min_range_usd: float = 25.0
    crypto_taker_fee_rate: float = 0.07
    assumed_edge: Optional[float] = None
    assumed_ask: Optional[float] = None
    min_token_price: float = 0.25
    max_token_price: float = 0.90


@dataclass
class SignalRow:
    window_ts: int
    entry_ts: int
    direction: str
    outcome: str
    win: int
    reference_price: float
    entry_price_btc: float
    end_price_btc: float
    distance: float
    sigma_remaining: float
    p_up: float
    p_down: float
    target_probability: float
    assumed_ask: Optional[float]
    pnl_per_share: Optional[float]
    hour_utc: int
    params: str


def utc_ts(date_text: str, *, end_of_day: bool = False) -> int:
    text = str(date_text).strip()
    if text.isdigit():
        return int(text)
    if len(text) == 10:
        base = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        if end_of_day:
            base += dt.timedelta(days=1)
        return int(base.timestamp())
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    price = max(0.0, min(1.0, float(price)))
    return fee_rate * price * (1.0 - price)


def http_json(url: str, params: Optional[dict] = None, timeout: int = 20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "btc-bot-backtest/10.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data.decode("utf-8"))


def http_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "btc-bot-backtest/10.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def cache_key(symbol: str, start_ts: int, end_ts: int, source: str) -> str:
    return f"{symbol}-1m-{source}-{start_ts}-{end_ts}.csv"


def load_candles_csv(path: Path) -> List[Candle]:
    candles: List[Candle] = []
    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].lower() in {"open_time", "open time"}:
                continue
            try:
                # Binance CSV / API export format.
                candles.append(Candle(
                    open_time_ms=int(float(row[0])),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 and row[5] != "" else 0.0,
                ))
            except Exception:
                continue
    candles.sort(key=lambda c: c.open_time_ms)
    return dedupe_candles(candles)


def save_candles_csv(path: Path, candles: Sequence[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.open_time_ms, c.open, c.high, c.low, c.close, c.volume])


def dedupe_candles(candles: Sequence[Candle]) -> List[Candle]:
    by_ts: Dict[int, Candle] = {c.open_time_ms: c for c in candles}
    return [by_ts[k] for k in sorted(by_ts)]


def fetch_binance_api(symbol: str, start_ts: int, end_ts: int, cache_dir: Path, use_cache: bool = True) -> List[Candle]:
    cache = cache_dir / cache_key(symbol, start_ts, end_ts, "binance-api")
    if use_cache and cache.exists():
        return load_candles_csv(cache)

    candles: List[Candle] = []
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        batch = http_json(BINANCE_SPOT_KLINES, params=params, timeout=20)
        if not batch:
            break
        for row in batch:
            candles.append(Candle(
                open_time_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            ))
        last_open = int(batch[-1][0])
        next_ms = last_open + 60_000
        if next_ms <= start_ms:
            break
        start_ms = next_ms
        # polite pacing; Binance limits depend on endpoint weight.
        time.sleep(0.04)

    candles = [c for c in dedupe_candles(candles) if start_ts <= c.ts < end_ts]
    save_candles_csv(cache, candles)
    return candles


def month_iter(start_ts: int, end_ts: int) -> Iterable[str]:
    start = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = dt.datetime.fromtimestamp(end_ts, tz=dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cur = start
    while cur <= end:
        yield cur.strftime("%Y-%m")
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)


def fetch_binance_vision_monthly(symbol: str, start_ts: int, end_ts: int, cache_dir: Path, use_cache: bool = True) -> List[Candle]:
    out: List[Candle] = []
    raw_dir = cache_dir / "binance-vision"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for ym in month_iter(start_ts, end_ts):
        zip_path = raw_dir / f"{symbol}-1m-{ym}.zip"
        if not zip_path.exists() or not use_cache:
            url = BINANCE_VISION_MONTHLY.format(symbol=symbol, ym=ym)
            try:
                zip_path.write_bytes(http_bytes(url, timeout=90))
            except Exception as e:
                print(f"WARN: failed monthly download {ym}: {e}. Falling back may be needed.", file=sys.stderr)
                continue
        try:
            with zipfile.ZipFile(zip_path) as z:
                name = next(n for n in z.namelist() if n.endswith(".csv"))
                with z.open(name) as f:
                    text = io.TextIOWrapper(f)
                    reader = csv.reader(text)
                    for row in reader:
                        if not row or row[0].lower().startswith("open"):
                            continue
                        try:
                            c = Candle(int(float(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                            if start_ts <= c.ts < end_ts:
                                out.append(c)
                        except Exception:
                            continue
        except Exception as e:
            print(f"WARN: failed parse {zip_path}: {e}", file=sys.stderr)
    return dedupe_candles(out)


def recent_returns(closes: Sequence[float]) -> List[float]:
    rets = []
    for a, b in zip(closes, closes[1:]):
        if a > 0:
            rets.append((b - a) / a)
    return rets


def pinbar_bonus(candles: Sequence[Candle], distance: float, p: BacktestParams) -> Tuple[str, float]:
    if not candles:
        return "none", 0.0
    latest = candles[-1]
    total_range = latest.high - latest.low
    if total_range < p.pin_bar_min_range_usd:
        return "none", 0.0
    body = abs(latest.close - latest.open) or total_range * 0.05
    body_ratio = body / total_range if total_range > 0 else 1.0
    upper_wick = latest.high - max(latest.open, latest.close)
    lower_wick = min(latest.open, latest.close) - latest.low
    upper_wick_ratio = upper_wick / body if body > 0 else 0
    lower_wick_ratio = lower_wick / body if body > 0 else 0
    bearish_rejection = upper_wick_ratio >= p.pin_bar_tail_ratio and body_ratio <= p.pin_bar_body_max_ratio and upper_wick > lower_wick * 1.5
    bullish_rejection = lower_wick_ratio >= p.pin_bar_tail_ratio and body_ratio <= p.pin_bar_body_max_ratio and lower_wick > upper_wick * 1.5
    if distance > 0 and bullish_rejection:
        return "bullish_confirm", 0.035
    if distance < 0 and bearish_rejection:
        return "bearish_confirm", 0.035
    if (distance > 0 and bearish_rejection) or (distance < 0 and bullish_rejection):
        return "against_signal", 0.0
    return "none", 0.0


def eval_index_for_seconds_left(window_start_idx: int, seconds_left: int) -> int:
    elapsed = max(0, min(299, 300 - int(seconds_left)))
    completed_idx = int(elapsed // 60) - 1
    return window_start_idx + max(0, min(3, completed_idx))


def evaluate_window(candles: Sequence[Candle], idx: int, params: BacktestParams) -> Optional[SignalRow]:
    if idx + 4 >= len(candles):
        return None
    # Require exact 1m spacing for the 5m window.
    if candles[idx + 4].ts - candles[idx].ts != 240:
        return None
    entry_idx = eval_index_for_seconds_left(idx, params.entry_seconds_left)
    if entry_idx <= 0 or entry_idx >= idx + 5:
        return None
    lookback_start = max(0, entry_idx - params.vol_lookback_min)
    close_series = [c.close for c in candles[lookback_start:entry_idx + 1]]
    rets = recent_returns(close_series)
    if len(rets) < 5:
        return None

    reference = candles[idx].open
    current = candles[entry_idx].close
    final = candles[idx + 4].close
    distance = current - reference
    per_min_vol = statistics.pstdev(rets) or 0.0
    remaining_min = max(params.entry_seconds_left / 60.0, 1.0 / 60.0)
    sigma_remaining = max(1.0, current * per_min_vol * math.sqrt(remaining_min))
    min_distance = max(params.min_price_distance_usd, sigma_remaining * params.min_distance_sigma_mult)
    if abs(distance) < min_distance:
        return None

    mom_rets = rets[-params.momentum_lookback_min:] if params.momentum_lookback_min > 0 else []
    avg_ret = statistics.mean(mom_rets) if mom_rets else 0.0
    avg_ret = max(-0.0015, min(0.0015, avg_ret))
    drift = current * avg_ret * remaining_min * params.momentum_weight
    z = (current + drift - reference) / sigma_remaining
    p_up = normal_cdf(z)
    p_down = 1.0 - p_up

    pin_kind, bonus = pinbar_bonus(candles[idx:entry_idx + 1], distance, params)
    if pin_kind == "bullish_confirm":
        p_up = min(0.98, p_up + bonus)
        p_down = 1.0 - p_up
    elif pin_kind == "bearish_confirm":
        p_down = min(0.98, p_down + bonus)
        p_up = 1.0 - p_down
    elif pin_kind == "against_signal":
        return None

    direction = "UP" if p_up >= p_down else "DOWN"
    target_prob = p_up if direction == "UP" else p_down
    if target_prob < params.min_model_prob:
        return None

    outcome = "UP" if final >= reference else "DOWN"  # Polymarket tie-goes-Up rule.
    win = int(direction == outcome)
    assumed_ask = params.assumed_ask
    if assumed_ask is None and params.assumed_edge is not None:
        # Not a real historical quote. This means: "what if Polymarket ask were low enough
        # to satisfy this edge requirement?" Good for threshold stress testing only.
        assumed_ask = max(params.min_token_price, min(params.max_token_price, target_prob - params.assumed_edge))
    pnl = None
    if assumed_ask is not None:
        fee = taker_fee_per_share(assumed_ask, params.crypto_taker_fee_rate)
        pnl = (1.0 if win else 0.0) - assumed_ask - fee
    entry_ts = candles[entry_idx].ts
    return SignalRow(
        window_ts=candles[idx].ts,
        entry_ts=entry_ts,
        direction=direction,
        outcome=outcome,
        win=win,
        reference_price=reference,
        entry_price_btc=current,
        end_price_btc=final,
        distance=distance,
        sigma_remaining=sigma_remaining,
        p_up=p_up,
        p_down=p_down,
        target_probability=target_prob,
        assumed_ask=assumed_ask,
        pnl_per_share=pnl,
        hour_utc=dt.datetime.fromtimestamp(entry_ts, tz=dt.timezone.utc).hour,
        params=f"entry={params.entry_seconds_left},prob={params.min_model_prob:.3f},dist={params.min_price_distance_usd:.1f}",
    )


def run_backtest(candles: Sequence[Candle], params: BacktestParams) -> List[SignalRow]:
    rows: List[SignalRow] = []
    # Align to 5m boundaries. Binance candle open_time is UTC ms.
    by_ts = {c.ts: i for i, c in enumerate(candles)}
    for c in candles:
        if c.ts % 300 != 0:
            continue
        idx = by_ts.get(c.ts)
        if idx is None or idx + 4 >= len(candles):
            continue
        row = evaluate_window(candles, idx, params)
        if row:
            rows.append(row)
    return rows


def summarize(rows: Sequence[SignalRow]) -> dict:
    n = len(rows)
    wins = sum(r.win for r in rows)
    losses = n - wins
    win_rate = wins / n if n else 0.0
    pnl_values = [r.pnl_per_share for r in rows if r.pnl_per_share is not None]
    pnl = sum(pnl_values) if pnl_values else None
    avg_pnl = pnl / len(pnl_values) if pnl_values else None
    max_loss_streak = 0
    cur = 0
    for r in rows:
        if r.win:
            cur = 0
        else:
            cur += 1
            max_loss_streak = max(max_loss_streak, cur)
    by_dir = {}
    for d in ("UP", "DOWN"):
        subset = [r for r in rows if r.direction == d]
        by_dir[d] = {"trades": len(subset), "win_rate": (sum(x.win for x in subset) / len(subset) if subset else 0.0)}
    by_hour = {}
    for h in range(24):
        subset = [r for r in rows if r.hour_utc == h]
        if subset:
            by_hour[str(h)] = {"trades": len(subset), "win_rate": sum(x.win for x in subset) / len(subset)}
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "max_loss_streak": max_loss_streak,
        "pnl_per_share_sum": pnl,
        "pnl_per_share_avg": avg_pnl,
        "by_direction": by_dir,
        "by_hour_utc": by_hour,
    }


def write_rows(path: Path, rows: Sequence[SignalRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        if not rows:
            f.write("")
            return
        fieldnames = list(asdict(rows[0]).keys())
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def parse_float_list(text: str) -> List[float]:
    return [float(x.strip()) for x in str(text).split(",") if x.strip()]


def parse_int_list(text: str) -> List[int]:
    return [int(float(x.strip())) for x in str(text).split(",") if x.strip()]


def run_sweep(candles: Sequence[Candle], args) -> List[dict]:
    results = []
    for entry in parse_int_list(args.sweep_entries):
        for prob in parse_float_list(args.sweep_probs):
            for dist in parse_float_list(args.sweep_distances):
                p = build_params(args, entry_seconds_left=entry, min_model_prob=prob, min_price_distance_usd=dist)
                rows = run_backtest(candles, p)
                s = summarize(rows)
                if s["trades"] < args.min_sweep_trades:
                    continue
                # Conservative ranking: prioritize hit-rate first, but penalize tiny sample count.
                sample_penalty = min(1.0, math.log(max(1, s["trades"])) / math.log(max(2, args.min_sweep_trades * 10)))
                score = (s["win_rate"] - 0.5) * sample_penalty
                if s["pnl_per_share_avg"] is not None:
                    score += float(s["pnl_per_share_avg"]) * 0.25
                results.append({
                    "entry_seconds_left": entry,
                    "min_model_prob": prob,
                    "min_price_distance_usd": dist,
                    "trades": s["trades"],
                    "win_rate": s["win_rate"],
                    "max_loss_streak": s["max_loss_streak"],
                    "pnl_per_share_avg": s["pnl_per_share_avg"],
                    "score": score,
                })
    results.sort(key=lambda x: (x["score"], x["trades"]), reverse=True)
    return results


def build_params(args, **overrides) -> BacktestParams:
    def cfg(name: str, default):
        return getattr(config, name, default) if config is not None else default
    p = BacktestParams(
        entry_seconds_left=args.entry_seconds_left,
        vol_lookback_min=args.vol_lookback_min or cfg("VOL_LOOKBACK_MIN", 20),
        momentum_lookback_min=args.momentum_lookback_min or cfg("MOMENTUM_LOOKBACK_MIN", 3),
        momentum_weight=args.momentum_weight if args.momentum_weight is not None else cfg("MOMENTUM_WEIGHT", 0.35),
        min_price_distance_usd=args.min_price_distance_usd if args.min_price_distance_usd is not None else cfg("MIN_PRICE_DISTANCE_USD", 35.0),
        min_distance_sigma_mult=args.min_distance_sigma_mult if args.min_distance_sigma_mult is not None else cfg("MIN_DISTANCE_SIGMA_MULT", 0.45),
        min_model_prob=args.min_model_prob if args.min_model_prob is not None else cfg("MIN_MODEL_PROB", 0.62),
        pin_bar_tail_ratio=cfg("PIN_BAR_TAIL_RATIO", 2.0),
        pin_bar_body_max_ratio=cfg("PIN_BAR_BODY_MAX_RATIO", 0.35),
        pin_bar_min_range_usd=cfg("PIN_BAR_MIN_RANGE_USD", 25.0),
        crypto_taker_fee_rate=cfg("CRYPTO_TAKER_FEE_RATE", 0.07),
        assumed_edge=args.assumed_edge,
        assumed_ask=args.assumed_ask,
        min_token_price=cfg("MIN_TOKEN_PRICE", 0.25),
        max_token_price=cfg("MAX_TOKEN_PRICE", 0.90),
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def load_candles(args) -> List[Candle]:
    start_ts = utc_ts(args.start)
    end_ts = utc_ts(args.end, end_of_day=True)
    cache_dir = Path(args.cache_dir)
    if args.csv:
        candles = load_candles_csv(Path(args.csv))
        return [c for c in candles if start_ts <= c.ts < end_ts]
    if args.source == "binance-vision":
        candles = fetch_binance_vision_monthly(args.symbol, start_ts, end_ts, cache_dir, use_cache=not args.no_cache)
        if candles:
            return candles
        print("WARN: binance-vision returned no candles, falling back to binance-api", file=sys.stderr)
    return fetch_binance_api(args.symbol, start_ts, end_ts, cache_dir, use_cache=not args.no_cache)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Historical BTC 5m strategy backtester")
    ap.add_argument("--start", required=True, help="UTC date, e.g. 2025-01-01")
    ap.add_argument("--end", required=True, help="UTC date inclusive if YYYY-MM-DD, e.g. 2025-02-01")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--source", choices=["binance-api", "binance-vision"], default="binance-api")
    ap.add_argument("--csv", help="Use local Binance-format 1m CSV instead of downloading")
    ap.add_argument("--cache-dir", default="backtest_cache")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--entry-seconds-left", type=int, default=60)
    ap.add_argument("--vol-lookback-min", type=int)
    ap.add_argument("--momentum-lookback-min", type=int)
    ap.add_argument("--momentum-weight", type=float)
    ap.add_argument("--min-price-distance-usd", type=float)
    ap.add_argument("--min-distance-sigma-mult", type=float)
    ap.add_argument("--min-model-prob", type=float)
    ap.add_argument("--assumed-edge", type=float, default=None, help="For stress-test PnL only: assume CLOB ask = model_prob - edge")
    ap.add_argument("--assumed-ask", type=float, default=None, help="For stress-test PnL only: fixed ask price for all simulated trades")
    ap.add_argument("--out-dir", default="backtest_reports")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--sweep-entries", default="60,120,180")
    ap.add_argument("--sweep-probs", default="0.60,0.62,0.65,0.68,0.70")
    ap.add_argument("--sweep-distances", default="25,35,50,75")
    ap.add_argument("--min-sweep-trades", type=int, default=50)
    args = ap.parse_args(argv)

    candles = load_candles(args)
    if len(candles) < 200:
        print(f"ERROR: not enough candles loaded: {len(candles)}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Loaded {len(candles):,} 1m candles from {dt.datetime.fromtimestamp(candles[0].ts, tz=dt.timezone.utc)} to {dt.datetime.fromtimestamp(candles[-1].ts, tz=dt.timezone.utc)} UTC")

    if args.sweep:
        sweep = run_sweep(candles, args)
        sweep_path = out_dir / "sweep_results.json"
        sweep_path.write_text(json.dumps(sweep[:100], indent=2, ensure_ascii=False))
        print(f"Sweep complete. Top results written to {sweep_path}")
        for row in sweep[:10]:
            print(json.dumps(row, ensure_ascii=False))
        if sweep:
            best = sweep[0]
            patch = (
                f"ENTRY_MIN_SECONDS_LEFT={max(12, int(best['entry_seconds_left']) - 15)}\n"
                f"ENTRY_MAX_SECONDS_LEFT={int(best['entry_seconds_left']) + 15}\n"
                f"MIN_MODEL_PROB={best['min_model_prob']:.3f}\n"
                f"MIN_PRICE_DISTANCE_USD={best['min_price_distance_usd']:.1f}\n"
            )
            (out_dir / "recommended_env_patch.txt").write_text(patch)
            print("Recommended .env patch written to", out_dir / "recommended_env_patch.txt")
        return 0

    params = build_params(args)
    rows = run_backtest(candles, params)
    summary = summarize(rows)
    summary.update({"params": asdict(params), "source": args.source, "symbol": args.symbol, "start": args.start, "end": args.end})
    rows_path = out_dir / "backtest_trades.csv"
    summary_path = out_dir / "backtest_summary.json"
    write_rows(rows_path, rows)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Rows: {rows_path}")
    print(f"Summary: {summary_path}")
    if params.assumed_edge is not None or params.assumed_ask is not None:
        print("NOTE: PnL uses an assumed CLOB entry price. For exact PnL, replay Polymarket token price-history for the same windows.")
    else:
        print("NOTE: No PnL was calculated because no historical CLOB entry price was supplied/assumed. Use win-rate first, then verify CLOB prices separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
