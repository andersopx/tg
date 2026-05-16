"""
Post-trade review and conservative learning.

V13.4 adds intelligent review:
- classifies why each trade won/lost,
- records per-asset/per-strategy stats,
- writes detailed review_notes for Telegram and analytics.
"""
import json
from datetime import datetime, timezone
from database import Database
from config import config
from trade_reviewer import TradeReviewer


class Learner:
    def __init__(self, db: Database):
        self.db = db
        self.reviewer = TradeReviewer()

    def review_trade(self, trade: dict) -> str:
        """Generate and persist an intelligent review for a resolved trade."""
        if not trade or trade.get("status") not in {"resolved", "shadow_resolved"}:
            return ""

        sig_data = trade.get("signal_data")
        if isinstance(sig_data, str):
            try:
                sig_data = json.loads(sig_data)
            except Exception:
                sig_data = {}
        sig_data = sig_data or {}
        indicators = sig_data.get("indicators", {}) or {}
        strategy_name = sig_data.get("strategy_name") or indicators.get("strategy_name") or sig_data.get("pattern") or "unknown"
        asset = trade.get("asset") or sig_data.get("asset") or indicators.get("asset") or "BTC"

        if config.ENABLE_INTELLIGENT_REVIEW:
            result = self.reviewer.review(trade)
            review = result.note
            self.db.record_trade_review(result)
            if int(trade.get("is_shadow") or 0):
                self.db.record_shadow_asset_strategy_outcome(
                    result.asset, result.strategy_name, result.outcome == "win", result.pnl,
                    result.entry_price, result.implied_multiple
                )
            else:
                self.db.record_asset_strategy_outcome(
                    result.asset,
                    result.strategy_name,
                    result.outcome == "win",
                    result.pnl,
                    result.entry_price,
                    result.implied_multiple,
                )
        else:
            review = self._basic_review(trade, sig_data)
            entry = float(trade.get("avg_price") or trade.get("entry_price") or 0)
            if int(trade.get("is_shadow") or 0):
                self.db.record_shadow_asset_strategy_outcome(
                    str(asset).upper(), str(strategy_name), trade.get("outcome") == "win", float(trade.get("pnl") or 0),
                    entry, (1 / entry if entry > 0 else 0.0)
                )
            else:
                self.db.record_asset_strategy_outcome(
                    str(asset).upper(), str(strategy_name), trade.get("outcome") == "win",
                    float(trade.get("pnl") or 0), entry, (1 / entry if entry > 0 else 0.0)
                )

        with self.db.conn() as c:
            c.execute(
                "UPDATE trades SET review_notes = ?, review_version = ? WHERE id = ?",
                (review, config.VERSION, trade["id"])
            )
        return review

    def _basic_review(self, trade: dict, sig_data: dict) -> str:
        pattern = sig_data.get("pattern", "unknown")
        confidence = float(sig_data.get("model_probability") or sig_data.get("confidence") or trade.get("model_probability") or 0)
        edge = float(sig_data.get("edge_after_fees") or trade.get("edge_after_fees") or 0)
        indicators = sig_data.get("indicators", {}) or {}
        ref = float(sig_data.get("reference_price") or trade.get("reference_price") or 0)
        final_price = float(trade.get("final_price") or 0)
        settlement_source = trade.get("settlement_source") or "unknown"
        outcome = trade.get("outcome")
        pnl = float(trade.get("pnl") or 0)
        entry = float(trade.get("avg_price") or trade.get("entry_price") or 0)
        payout_ratio = 1 / entry if entry > 0 else 0
        direction = trade.get("direction", "")
        if outcome == "win":
            return (
                f"✅ 胜 +${pnl:.2f} | {direction} | {pattern} | "
                f"入场 ${entry:.3f} / 赔率 {payout_ratio:.2f}x | "
                f"模型 {confidence:.1%} / 费后边际 {edge:.1%} | "
                f"规则线 ${ref:,.2f} | 结算源 {settlement_source}"
            )
        return (
            f"❌ 负 -${abs(pnl):.2f} | {direction} | {pattern} | "
            f"入场 ${entry:.3f} | 模型 {confidence:.1%} / 费后边际 {edge:.1%} | "
            f"规则线 ${ref:,.2f} | 最终价/结算价 {final_price:.3f} | "
            f"距离 ${indicators.get('distance_to_reference', 0)} | 结算源 {settlement_source}"
        )

    def daily_summary(self, date_str: str = None) -> dict:
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        trades = self.db.get_today_trades()
        resolved = [t for t in trades if t["status"] == "resolved" and not int(t.get("is_shadow") or 0)]
        shadow_resolved = [t for t in trades if t["status"] == "shadow_resolved" or int(t.get("is_shadow") or 0)]
        wins = [t for t in resolved if t["outcome"] == "win"]
        losses = [t for t in resolved if t["outcome"] == "loss"]

        total_pnl = sum(float(t.get("pnl") or 0) for t in resolved)
        win_rate = len(wins) / len(resolved) if resolved else 0
        avg_win = sum(float(t.get("pnl") or 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(abs(float(t.get("pnl") or 0)) for t in losses) / len(losses) if losses else 0
        rr = avg_win / avg_loss if avg_loss > 0 else 0

        pattern_stats = {}
        for t in resolved:
            sig_data = t.get("signal_data")
            if isinstance(sig_data, str):
                try:
                    sig_data = json.loads(sig_data)
                except Exception:
                    sig_data = {}
            pattern = (sig_data or {}).get("strategy_name") or (sig_data or {}).get("pattern", "unknown")
            pattern_stats.setdefault(pattern, {"trades": 0, "wins": 0, "pnl": 0.0})
            pattern_stats[pattern]["trades"] += 1
            pattern_stats[pattern]["pnl"] += float(t.get("pnl") or 0)
            if t["outcome"] == "win":
                pattern_stats[pattern]["wins"] += 1

        return {
            "date": date_str,
            "trades_total": len([t for t in trades if not int(t.get("is_shadow") or 0)]),
            "trades_resolved": len(resolved),
            "trades_open": len([t for t in trades if not int(t.get("is_shadow") or 0)]) - len(resolved),
            "shadow_trades": len(shadow_resolved),
            "shadow_resolved": len([t for t in shadow_resolved if t.get("status") == "shadow_resolved"]),
            "shadow_pnl": sum(float(t.get("pnl") or 0) for t in shadow_resolved if t.get("status") == "shadow_resolved"),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "risk_reward": rr,
            "pattern_breakdown": pattern_stats,
        }

    def signal_performance(self) -> list:
        stats = self.db.get_all_signal_stats()
        result = []
        for s in stats:
            attempts = int(s.get("attempts") or 0)
            if attempts >= 3:
                wr = float(s.get("wins") or 0) / attempts
                result.append({
                    "signal_type": s["signal_type"],
                    "attempts": attempts,
                    "wins": int(s.get("wins") or 0),
                    "win_rate": wr,
                    "total_pnl": float(s.get("total_pnl") or 0),
                    "weight": s.get("weight", 1.0),
                })
        return sorted(result, key=lambda x: (-x["win_rate"], -x["attempts"]))

    def asset_strategy_performance(self) -> list:
        """Detailed per-asset/per-strategy stats used by Telegram/analytics."""
        rows = self.db.get_asset_strategy_stats()
        out = []
        for r in rows:
            trades = int(r.get("trades") or 0)
            wins = int(r.get("wins") or 0)
            out.append({
                "asset": r.get("asset", "BTC"),
                "strategy_name": r.get("strategy_name", "unknown"),
                "trades": trades,
                "wins": wins,
                "losses": int(r.get("losses") or 0),
                "win_rate": wins / trades if trades else 0.0,
                "total_pnl": float(r.get("total_pnl") or 0),
                "avg_entry_price": float(r.get("avg_entry_price") or 0),
                "avg_multiple": float(r.get("avg_multiple") or 0),
                "max_multiple": float(r.get("max_multiple") or 0),
                "loss_streak": int(r.get("loss_streak") or 0),
                "weight": float(r.get("weight") or 1.0),
            })
        return out
