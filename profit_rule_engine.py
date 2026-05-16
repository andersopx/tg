"""
v14.2.42 Profit Rule Engine

Purpose:
- Stop treating a fixed 75% threshold as the final truth.
- Mine Shadow/real resolved trades into rule buckets.
- Convert review data into pre-trade decisions: PROMOTE / SHADOW / OBSERVE / REJECT.
- Feed every candidate through learned rules before Shadow/live execution.

This is intentionally conservative. If a bucket recently loses, it blocks. If a
bucket has no data, it only allows Shadow exploration above a configurable floor.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ------------------------- basic utilities -------------------------

def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _to_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    return default


def _json_obj(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, dict) else {}
        except Exception:
            return {}
    return {}


def _env_float(name: str, default: float) -> float:
    # v15.1.4: prefer config runtime state (TG overrides) over raw os.getenv
    try:
        from config import config as _cfg
        rt = getattr(_cfg, "_runtime_float", None)
        if rt is not None:
            return float(rt([name.lower(), name], default))
    except Exception:
        pass
    return _to_float(os.getenv(name), default)


def _env_int(name: str, default: int) -> int:
    try:
        from config import config as _cfg
        rt = getattr(_cfg, "_runtime_int", None)
        if rt is not None:
            return int(rt([name.lower(), name], default))
    except Exception:
        pass
    return _to_int(os.getenv(name), default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        from config import config as _cfg
        rt = getattr(_cfg, "_state_get_first", None)
        if rt is not None:
            val = rt([name.lower(), name], None)
            if val is not None:
                return _to_bool(val, default)
    except Exception:
        pass
    return _to_bool(os.getenv(name), default)


def _config_db_path() -> str:
    """v15.1.4: prefer config.DB_PATH over hardcoded 'btc_bot.db'."""
    try:
        from config import config as _cfg
        path = getattr(_cfg, "DB_PATH", None)
        if path:
            return str(path)
    except Exception:
        pass
    return "btc_bot.db"


def _wilson_lower_bound(wins: int, attempts: int, z: float = 1.2815515655446004) -> float:
    """One-sided Wilson lower bound used to avoid promoting tiny lucky buckets."""
    if attempts <= 0:
        return 0.0
    p = float(wins) / float(attempts)
    z2 = z * z
    denom = 1.0 + z2 / attempts
    centre = p + z2 / (2.0 * attempts)
    margin = z * ((p * (1.0 - p) + z2 / (4.0 * attempts)) / attempts) ** 0.5
    return max(0.0, (centre - margin) / denom)


# ------------------------- bucketing -------------------------

def entry_bucket(entry: float) -> str:
    if entry < 0.10:
        return "entry_<0.10"
    if entry < 0.15:
        return "entry_0.10_0.15"
    if entry < 0.25:
        return "entry_0.15_0.25"
    if entry < 0.35:
        return "entry_0.25_0.35"
    if entry <= 0.50:
        return "entry_0.35_0.50"
    return "entry_>0.50"


def prob_bucket(prob: float) -> str:
    if prob < 0.50:
        return "prob_<0.50"
    if prob < 0.55:
        return "prob_0.50_0.55"
    if prob < 0.60:
        return "prob_0.55_0.60"
    if prob < 0.65:
        return "prob_0.60_0.65"
    if prob < 0.70:
        return "prob_0.65_0.70"
    if prob < 0.75:
        return "prob_0.70_0.75"
    if prob < 0.85:
        return "prob_0.75_0.85"
    if prob < 0.95:
        return "prob_0.85_0.95"
    return "prob_>=0.95"


def gap_bucket(gap: float) -> str:
    if gap < 0.20:
        return "gap_<0.20"
    if gap < 0.30:
        return "gap_0.20_0.30"
    if gap < 0.40:
        return "gap_0.30_0.40"
    if gap < 0.50:
        return "gap_0.40_0.50"
    return "gap_>=0.50"


def remaining_bucket(signal: Dict[str, Any]) -> str:
    indicators = _json_obj(signal.get("indicators"))
    candidates = [
        signal.get("seconds_remaining"),
        signal.get("remaining_seconds"),
        signal.get("seconds_left"),
        signal.get("left_seconds"),
        signal.get("time_left"),
        indicators.get("seconds_remaining"),
        indicators.get("remaining_seconds"),
        indicators.get("seconds_to_close"),
    ]
    rem = 0
    for c in candidates:
        rem = _to_int(c, 0)
        if rem > 0:
            break
    if rem <= 0:
        return "rem_NA"
    if rem < 30:
        return "rem_<30s"
    if rem < 60:
        return "rem_30_60s"
    if rem < 120:
        return "rem_60_120s"
    if rem < 180:
        return "rem_120_180s"
    if rem < 300:
        return "rem_180_300s"
    return "rem_>=300s"


# ------------------------- extraction/signature -------------------------

def extract_features(signal: Any, *, asset: str = "", timeframe: str = "", direction: str = "", entry_price: Any = None) -> Dict[str, Any]:
    s = _json_obj(signal)
    indicators = _json_obj(s.get("indicators"))
    adaptive_edge = _json_obj(indicators.get("adaptive_edge") or s.get("adaptive_edge"))
    strategy = str(s.get("strategy_name") or s.get("strategy") or s.get("pattern") or "UNKNOWN")
    if strategy.startswith("barrier_reclaim") or "barrier_reclaim" in strategy:
        strategy = "barrier_reclaim"
    asset = str(asset or s.get("asset") or indicators.get("asset") or "UNKNOWN")
    timeframe = str(timeframe or s.get("timeframe") or indicators.get("timeframe") or "5m")
    direction = str(direction or s.get("direction") or s.get("side") or "").capitalize()
    if direction not in ("Up", "Down"):
        pat = str(s.get("pattern") or "").lower()
        if "_up" in pat or pat.endswith("up"):
            direction = "Up"
        elif "_down" in pat or pat.endswith("down"):
            direction = "Down"
        else:
            direction = str(direction or "UNKNOWN")

    entry = _to_float(entry_price, 0.0)
    if entry <= 0:
        entry = _to_float(s.get("entry_price") or s.get("token_price") or s.get("ask") or s.get("price"), 0.0)
    prob = _to_float(s.get("calibrated_probability") or adaptive_edge.get("calibrated_probability"), 0.0)
    if prob <= 0:
        prob = _to_float(s.get("settlement_probability") or adaptive_edge.get("raw_settlement_probability"), 0.0)
    if prob <= 0:
        prob = _to_float(s.get("model_probability") or s.get("confidence"), 0.0)
    gap = _to_float(s.get("model_market_gap"), 0.0)
    if gap <= 0:
        mm = _to_float(s.get("market_mid_probability"), -1.0)
        if mm >= 0 and prob > 0:
            gap = abs(prob - mm)

    eb = entry_bucket(entry)
    pb = prob_bucket(prob)
    gb = gap_bucket(gap)
    rb = remaining_bucket(s)
    return {
        "strategy_name": strategy,
        "asset": asset,
        "timeframe": timeframe,
        "direction": direction,
        "entry_price": entry,
        "model_probability": prob,
        "model_market_gap": gap,
        "entry_bucket": eb,
        "prob_bucket": pb,
        "gap_bucket": gb,
        "remaining_bucket": rb,
        "net_profit_multiple": (1.0 / entry - 1.0) if entry > 0 else 0.0,
        "adaptive_edge": adaptive_edge,
        "signal": s,
    }


def signature_exact(x: Dict[str, Any]) -> str:
    parts = [
        "exact",
        x["strategy_name"],
        x["asset"],
        x["timeframe"],
        x["direction"],
        x["entry_bucket"],
        x["prob_bucket"],
        x["gap_bucket"],
        x["remaining_bucket"],
    ]
    return "|".join(str(p) for p in parts)


def signature_relaxed(x: Dict[str, Any]) -> str:
    # Relaxed rule can generalize faster by ignoring the raw probability bucket.
    parts = [
        "relaxed",
        x["strategy_name"],
        x["asset"],
        x["timeframe"],
        x["direction"],
        x["entry_bucket"],
        x["gap_bucket"],
    ]
    return "|".join(str(p) for p in parts)


# ------------------------- database -------------------------

def _connect_from_db(db: Any) -> Tuple[Optional[sqlite3.Connection], bool]:
    """Return (conn, should_close)."""
    if db is None:
        _path = _config_db_path()
        if os.path.exists(_path):
            return sqlite3.connect(_path, timeout=3.0), True
        return None, False
    for attr in ("conn", "connection", "_conn"):
        c = getattr(db, attr, None)
        if isinstance(c, sqlite3.Connection):
            return c, False
    for attr in ("db_path", "path", "database_path"):
        p = getattr(db, attr, None)
        if p and os.path.exists(str(p)):
            return sqlite3.connect(str(p), timeout=3.0), True
    _path = _config_db_path()
    if os.path.exists(_path):
        return sqlite3.connect(_path, timeout=3.0), True
    return None, False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profit_rules (
            signature TEXT PRIMARY KEY,
            rule_type TEXT,
            strategy_name TEXT,
            asset TEXT,
            timeframe TEXT,
            direction TEXT,
            entry_bucket TEXT,
            prob_bucket TEXT,
            gap_bucket TEXT,
            remaining_bucket TEXT,
            attempts INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            avg_entry REAL DEFAULT 0,
            avg_multiplier REAL DEFAULT 0,
            max_multiplier REAL DEFAULT 0,
            loss_streak INTEGER DEFAULT 0,
            recent_attempts INTEGER DEFAULT 0,
            recent_wins INTEGER DEFAULT 0,
            recent_pnl REAL DEFAULT 0,
            rating TEXT DEFAULT 'OBSERVE',
            reason TEXT,
            frozen_until INTEGER DEFAULT 0,
            last_trade_id INTEGER DEFAULT 0,
            updated_at INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profit_rules_rating ON profit_rules(rating, frozen_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_profit_rules_dims ON profit_rules(strategy_name,asset,timeframe,direction)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profit_rule_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            action TEXT,
            mode TEXT,
            reason TEXT,
            signature_exact TEXT,
            signature_relaxed TEXT,
            strategy_name TEXT,
            asset TEXT,
            timeframe TEXT,
            direction TEXT,
            entry_price REAL,
            model_probability REAL,
            model_market_gap REAL,
            net_profit_multiple REAL,
            rule_rating TEXT,
            details_json TEXT
        )
        """
    )
    conn.commit()


def _rating_for_stats(*, attempts: int, wins: int, total_pnl: float, avg_entry: float, loss_streak: int, recent_attempts: int, recent_wins: int, recent_pnl: float, now: int, cfg: Dict[str, Any]) -> Tuple[str, str, int]:
    win_rate = wins / attempts if attempts > 0 else 0.0
    min_samples = int(cfg["min_samples_promote"])
    max_loss_streak = int(cfg["max_loss_streak"])
    min_recent = int(cfg["min_recent_samples"])
    freeze_seconds = int(cfg["freeze_minutes"]) * 60

    # Hard negatives: recent/current losing pattern should not keep trading.
    if loss_streak >= max_loss_streak:
        return "REJECT", f"loss_streak>={max_loss_streak}", now + freeze_seconds
    if recent_attempts >= min_recent and recent_pnl < 0:
        return "REJECT", "recent_pnl_negative", now + freeze_seconds
    if attempts >= min_recent and total_pnl <= -1.0:
        return "REJECT", "historical_pnl_negative", now + freeze_seconds

    # v15 high-win target: positive EV alone may still produce too many losing
    # tickets. Promote only when the bucket has both profit and a statistically
    # acceptable win profile; otherwise keep it in Shadow/learning mode.
    if attempts >= min_samples and total_pnl > 0 and recent_pnl >= 0:
        avg_pnl = total_pnl / max(attempts, 1)
        wilson_lb = _wilson_lower_bound(wins, attempts, float(cfg.get("wilson_z", 1.2815515655446004)))
        if bool(cfg.get("promote_require_target_win_rate", True)):
            target_wr = float(cfg.get("target_win_rate", cfg["min_promote_win_rate"]))
            min_wilson = float(cfg.get("min_wilson_promote", 0.55))
            if win_rate >= target_wr and wilson_lb >= min_wilson:
                return "PROMOTE", "target_winrate_rule", 0
            if avg_pnl >= float(cfg["min_avg_pnl_promote"]) and win_rate >= float(cfg["min_promote_win_rate"]) and wilson_lb >= min_wilson:
                return "PROMOTE", "high_ev_high_confidence_rule", 0
            return "SHADOW", "positive_ev_but_below_target_winrate", 0
        if win_rate >= float(cfg["min_promote_win_rate"]) or avg_pnl >= float(cfg["min_avg_pnl_promote"]):
            return "PROMOTE", "positive_ev_rule", 0
        return "SHADOW", "positive_ev_but_low_winrate", 0

    # Potential rule: keep collecting shadow data if not obviously bad.
    if total_pnl > 0:
        return "SHADOW", "positive_pnl_needs_more_samples", 0
    if attempts == 0:
        return "SHADOW", "new_rule_needs_shadow_samples", 0
    return "OBSERVE", "not_enough_edge_yet", 0


def _config() -> Dict[str, Any]:
    return {
        "min_entry": _env_float("PROFIT_RULE_MIN_ENTRY_PRICE", 0.08),
        "max_entry": _env_float("PROFIT_RULE_MAX_ENTRY_PRICE", 0.75),
        "max_gap": _env_float("PROFIT_RULE_MAX_MODEL_GAP", 0.50),
        "hard_75_enabled": _env_bool("PROFIT_RULE_HARD_75_ENABLED", False),
        "default_high_prob": _env_float("PROFIT_RULE_DEFAULT_HIGH_PROB", 0.75),
        "shadow_min_prob": _env_float("PROFIT_RULE_SHADOW_MIN_PROB", 0.65),
        "min_samples_promote": _env_int("PROFIT_RULE_MIN_SAMPLES_PROMOTE", 6),
        "min_recent_samples": _env_int("PROFIT_RULE_MIN_RECENT_SAMPLES", 3),
        "recent_n": _env_int("PROFIT_RULE_RECENT_N", 10),
        "max_loss_streak": _env_int("PROFIT_RULE_MAX_LOSS_STREAK", 2),
        "freeze_minutes": _env_int("PROFIT_RULE_FREEZE_MINUTES", 60),
        "min_promote_win_rate": _env_float("PROFIT_RULE_MIN_PROMOTE_WIN_RATE", 0.70),
        "min_avg_pnl_promote": _env_float("PROFIT_RULE_MIN_AVG_PNL_PROMOTE", 0.25),
        "promote_require_target_win_rate": _env_bool("PROFIT_RULE_PROMOTE_REQUIRE_TARGET_WIN_RATE", True),
        "target_win_rate": _env_float("TARGET_WIN_RATE", 0.75),
        "min_wilson_promote": _env_float("PROFIT_RULE_MIN_WILSON_PROMOTE", 0.55),
        "wilson_z": _env_float("HIGH_WIN_RATE_WILSON_Z", 1.2815515655446004),
        "min_net_profit_multiple": _env_float("PROFIT_RULE_MIN_NET_PROFIT_MULTIPLE", 1.0),
        "max_net_profit_multiple": _env_float("PROFIT_RULE_MAX_NET_PROFIT_MULTIPLE", 12.0),
        "live_require_promote": _env_bool("PROFIT_RULE_LIVE_REQUIRE_PROMOTE", True),
        "relaxed_can_trade": _env_bool("PROFIT_RULE_RELAXED_CAN_TRADE", False),
        "new_rule_can_shadow": _env_bool("PROFIT_RULE_NEW_RULE_CAN_SHADOW", True),
        "new_rule_requires_adaptive_edge": _env_bool("PROFIT_RULE_NEW_RULE_REQUIRES_ADAPTIVE_EDGE", True),
        "new_rule_min_adaptive_ev": _env_float("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_EV", 0.08),
        "new_rule_min_adaptive_quality": _env_float("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_QUALITY", 0.56),
    }


def rebuild_profit_rules(db_path: Optional[str] = None) -> int:
    if not db_path:
        db_path = _config_db_path()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    cfg = _config()
    now = int(time.time())

    rows = conn.execute(
        """
        SELECT id, asset, timeframe, direction, entry_price, signal_data, outcome, pnl
        FROM trades
        WHERE status IN ('shadow_resolved','resolved','closed','settled')
          AND outcome IN ('win','loss')
        ORDER BY id ASC
        """
    ).fetchall()

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    dims: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sig = _json_obj(r["signal_data"])
        x = extract_features(sig, asset=r["asset"], timeframe=r["timeframe"], direction=r["direction"], entry_price=r["entry_price"])
        for sig_type, signature in (("exact", signature_exact(x)), ("relaxed", signature_relaxed(x))):
            buckets.setdefault(signature, []).append({
                "id": int(r["id"]),
                "outcome": r["outcome"],
                "pnl": _to_float(r["pnl"], 0.0),
                "entry": _to_float(r["entry_price"], 0.0),
            })
            d = dict(x)
            d["rule_type"] = sig_type
            dims[signature] = d

    conn.execute("DELETE FROM profit_rules")
    recent_n = int(cfg["recent_n"])
    for signature, items in buckets.items():
        d = dims[signature]
        attempts = len(items)
        wins = sum(1 for it in items if it["outcome"] == "win")
        losses = sum(1 for it in items if it["outcome"] == "loss")
        total_pnl = sum(float(it["pnl"]) for it in items)
        avg_pnl = total_pnl / attempts if attempts else 0.0
        avg_entry = sum(float(it["entry"]) for it in items) / attempts if attempts else 0.0
        mults = [(1.0 / float(it["entry"])) for it in items if float(it["entry"]) > 0]
        avg_mult = sum(mults) / len(mults) if mults else 0.0
        max_mult = max(mults) if mults else 0.0
        recent = items[-recent_n:]
        recent_attempts = len(recent)
        recent_wins = sum(1 for it in recent if it["outcome"] == "win")
        recent_pnl = sum(float(it["pnl"]) for it in recent)
        loss_streak = 0
        for it in reversed(items):
            if it["outcome"] == "loss":
                loss_streak += 1
            else:
                break
        rating, reason, frozen_until = _rating_for_stats(
            attempts=attempts, wins=wins, total_pnl=total_pnl, avg_entry=avg_entry,
            loss_streak=loss_streak, recent_attempts=recent_attempts, recent_wins=recent_wins,
            recent_pnl=recent_pnl, now=now, cfg=cfg,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO profit_rules (
                signature, rule_type, strategy_name, asset, timeframe, direction, entry_bucket,
                prob_bucket, gap_bucket, remaining_bucket, attempts, wins, losses, win_rate,
                total_pnl, avg_pnl, avg_entry, avg_multiplier, max_multiplier, loss_streak,
                recent_attempts, recent_wins, recent_pnl, rating, reason, frozen_until,
                last_trade_id, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signature, d.get("rule_type"), d.get("strategy_name"), d.get("asset"), d.get("timeframe"), d.get("direction"),
                d.get("entry_bucket"), d.get("prob_bucket") if d.get("rule_type") == "exact" else "ANY",
                d.get("gap_bucket"), d.get("remaining_bucket") if d.get("rule_type") == "exact" else "ANY",
                attempts, wins, losses, wins / attempts if attempts else 0.0, total_pnl, avg_pnl, avg_entry,
                avg_mult, max_mult, loss_streak, recent_attempts, recent_wins, recent_pnl, rating, reason,
                frozen_until, max(it["id"] for it in items), now,
            ),
        )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM profit_rules").fetchone()[0]
    conn.close()
    return int(n)


def record_trade_result(db: Any, trade: Dict[str, Any]) -> None:
    """Update rule table after one trade resolves. Safe to call after every shadow/live settlement."""
    conn, should_close = _connect_from_db(db)
    if conn is None:
        return
    try:
        # Full rebuild is cheap for current sample size and safer than incremental math.
        path = None
        try:
            path = getattr(db, "db_path", None) or getattr(db, "path", None) or _config_db_path()
        except Exception:
            path = _config_db_path()
        if path and os.path.exists(str(path)):
            rebuild_profit_rules(str(path))
    finally:
        if should_close:
            conn.close()


def _fetch_rule(conn: sqlite3.Connection, signature: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM profit_rules WHERE signature=?", (signature,)).fetchone()


def _log_event(conn: sqlite3.Connection, *, action: str, mode: str, reason: str, x: Dict[str, Any], exact: str, relaxed: str, rating: str, details: Dict[str, Any]) -> None:
    try:
        conn.execute(
            """
            INSERT INTO profit_rule_events (
                timestamp, action, mode, reason, signature_exact, signature_relaxed, strategy_name,
                asset, timeframe, direction, entry_price, model_probability, model_market_gap,
                net_profit_multiple, rule_rating, details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(time.time()), action, mode, reason, exact, relaxed, x["strategy_name"], x["asset"], x["timeframe"], x["direction"],
                float(x["entry_price"] or 0), float(x["model_probability"] or 0), float(x["model_market_gap"] or 0),
                float(x["net_profit_multiple"] or 0), rating, json.dumps(details, ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception:
        pass


def profit_rule_decision(db: Any, signal: Any, asset: str, timeframe: str, direction: str, entry_price: float, *, mode: str = "shadow") -> Tuple[bool, str, Dict[str, Any]]:
    """Return (allowed, reason/action, info)."""
    x = extract_features(signal, asset=asset, timeframe=timeframe, direction=direction, entry_price=entry_price)
    cfg = _config()
    now = int(time.time())
    exact = signature_exact(x)
    relaxed = signature_relaxed(x)
    info = dict(x)
    info.update({"signature_exact": exact, "signature_relaxed": relaxed, "config": cfg})

    # Hard economic guards: the user explicitly wants meaningful 1x-10x net profit.
    if x["entry_price"] < cfg["min_entry"]:
        info["decision"] = "REJECT"
        return False, "profit_rule_entry_too_low", info
    if x["entry_price"] > cfg["max_entry"]:
        info["decision"] = "REJECT"
        return False, "profit_rule_entry_too_high", info
    if x["model_market_gap"] >= cfg["max_gap"]:
        info["decision"] = "REJECT"
        return False, "profit_rule_gap_too_high", info
    if x["net_profit_multiple"] < cfg["min_net_profit_multiple"]:
        info["decision"] = "REJECT"
        return False, "profit_rule_profit_multiple_too_low", info
    if x["net_profit_multiple"] > cfg["max_net_profit_multiple"]:
        info["decision"] = "REJECT"
        return False, "profit_rule_profit_multiple_too_high", info

    conn, should_close = _connect_from_db(db)
    if conn is None:
        # DB unavailable: never allow live real submission because promotion status
        # cannot be proven. Shadow can still collect data for later promotion.
        if str(mode).lower() == "live" and cfg["live_require_promote"]:
            info["decision"] = "SHADOW_ONLY"
            return False, "profit_rule_db_unavailable_live_block", info
        if x["model_probability"] >= cfg["default_high_prob"]:
            info["decision"] = "SHADOW"
            return True, "profit_rule_db_unavailable_high_prob", info
        info["decision"] = "OBSERVE"
        return False, "profit_rule_db_unavailable_observe_only", info

    try:
        ensure_schema(conn)
        exact_rule = _fetch_rule(conn, exact)
        relaxed_rule = _fetch_rule(conn, relaxed)
        chosen = exact_rule or relaxed_rule
        chosen_type = "exact" if exact_rule else ("relaxed" if relaxed_rule else "none")
        rating = str(chosen["rating"] if chosen else "NEW")
        reason = str(chosen["reason"] if chosen and chosen["reason"] is not None else "new_rule")
        frozen_until = _to_int(chosen["frozen_until"] if chosen else 0, 0)
        info.update({
            "chosen_rule_type": chosen_type,
            "rule_rating": rating,
            "rule_reason": reason,
            "rule_attempts": int(chosen["attempts"] if chosen else 0),
            "rule_wins": int(chosen["wins"] if chosen else 0),
            "rule_pnl": float(chosen["total_pnl"] if chosen else 0.0),
            "rule_recent_pnl": float(chosen["recent_pnl"] if chosen else 0.0),
            "rule_loss_streak": int(chosen["loss_streak"] if chosen else 0),
            "rule_frozen_until": frozen_until,
        })

        if frozen_until and frozen_until > now:
            _log_event(conn, action="blocked", mode=mode, reason="profit_rule_frozen", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "REJECT"
            return False, "profit_rule_frozen", info

        if chosen_type == "relaxed" and rating in ("PROMOTE", "SHADOW") and not bool(cfg.get("relaxed_can_trade", False)):
            # Relaxed rules are useful context, but they are too broad to authorize
            # trades by themselves. Exact buckets or adaptive EV must confirm.
            _log_event(conn, action="blocked", mode=mode, reason="profit_rule_relaxed_observe_only", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "OBSERVE"
            return False, "profit_rule_relaxed_observe_only", info

        if rating == "REJECT":
            _log_event(conn, action="blocked", mode=mode, reason=f"profit_rule_reject_{reason}", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "REJECT"
            return False, f"profit_rule_reject_{reason}", info

        # Optional hard 75 mode, off by default. This gives us a kill switch, not final logic.
        if cfg["hard_75_enabled"] and x["model_probability"] < cfg["default_high_prob"]:
            _log_event(conn, action="blocked", mode=mode, reason="profit_rule_hard_75", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "OBSERVE"
            return False, "profit_rule_hard_75", info

        live_mode = str(mode).lower() == "live"

        # Live safety invariant: when PROFIT_RULE_LIVE_REQUIRE_PROMOTE=true, only
        # PROMOTE buckets may reach live-mode submission. NEW/SHADOW/OBSERVE buckets
        # are allowed to keep learning only when the caller explicitly uses shadow
        # mode. This function treats mode="live" as authoritative because Trader
        # only reaches this gate after DRY_RUN has already been cleared.
        effective_live_mode = bool(live_mode)
        info["effective_live_mode"] = effective_live_mode

        if effective_live_mode and cfg["live_require_promote"] and rating != "PROMOTE":
            _log_event(conn, action="blocked", mode=mode, reason="profit_rule_not_promoted_for_live", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "SHADOW_ONLY"
            return False, "profit_rule_not_promoted_for_live", info

        # Known profitable buckets can override the old fixed 75% idea.
        if rating in ("PROMOTE", "SHADOW"):
            _log_event(conn, action="allowed", mode=mode, reason=f"profit_rule_{rating.lower()}", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = rating
            return True, f"profit_rule_{rating.lower()}", info

        # New/unknown buckets: no fixed 75% rule.  Allow Shadow exploration only
        # when Adaptive Edge Engine says this exact candidate has positive EV and
        # enough quality.  Otherwise record/observe without trading.
        adaptive = x.get("adaptive_edge") or {}
        adaptive_decision = str(adaptive.get("decision") or "").upper()
        adaptive_ev = _to_float(adaptive.get("expected_value"), 0.0)
        adaptive_quality = _to_float(adaptive.get("quality_score"), 0.0)
        info["adaptive_edge"] = adaptive
        if (
            bool(cfg.get("new_rule_can_shadow", True))
            and not live_mode
            and (not bool(cfg.get("new_rule_requires_adaptive_edge", True)) or adaptive_decision in ("SHADOW", "PROMOTE"))
            and adaptive_ev >= float(cfg.get("new_rule_min_adaptive_ev", 0.08))
            and adaptive_quality >= float(cfg.get("new_rule_min_adaptive_quality", 0.56))
        ):
            _log_event(conn, action="allowed", mode=mode, reason="profit_rule_new_adaptive_edge_shadow", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
            info["decision"] = "SHADOW"
            return True, "profit_rule_new_adaptive_edge_shadow", info

        # v15.1.3: Allow NEW buckets to collect Shadow samples after Adaptive Edge approval.
        # This does not enable real orders; effective_live_mode still protects live execution.
        if (
            str(mode).lower() == "shadow"
            and rating == "NEW"
            and bool(cfg.get("new_rule_can_shadow", True))
        ):
            ae = info.get("adaptive_edge") or {}
            adaptive_decision = str(ae.get("decision") or "").upper()
            adaptive_ev = float(ae.get("expected_value") or 0.0)
            adaptive_quality = float(ae.get("quality_score") or 0.0)
            min_ev = float(cfg.get("new_rule_min_adaptive_ev", 0.08))
            min_quality = float(cfg.get("new_rule_min_adaptive_quality", 0.56))
            if adaptive_decision in ("SHADOW", "PROMOTE") and adaptive_ev >= min_ev and adaptive_quality >= min_quality:
                info["reason"] = "profit_rule_new_rule_shadow_allowed"
                info["adaptive_decision"] = adaptive_decision
                info["adaptive_ev"] = adaptive_ev
                info["adaptive_quality"] = adaptive_quality
                _log_event(conn, action="allowed", mode=mode, reason="profit_rule_new_rule_shadow_allowed", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
                return True, "profit_rule_new_rule_shadow_allowed", info

        _log_event(conn, action="blocked", mode=mode, reason="profit_rule_observe_only", x=x, exact=exact, relaxed=relaxed, rating=rating, details=info)
        info["decision"] = "OBSERVE"
        return False, "profit_rule_observe_only", info
    finally:
        if should_close:
            conn.close()
