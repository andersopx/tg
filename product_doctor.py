"""Product Doctor: active self-diagnosis for the trading product.

V14.2 focuses on evidence and recommendations, not strategy changes. It reads
local logs and public WS stats, then reports problems in Telegram/logs.
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional

from config import config
from ws_health_monitor import WSHealthMonitor


@dataclass
class DoctorIssue:
    severity: str
    code: str
    title: str
    evidence: str
    recommendation: str

    def to_dict(self):
        return {
            "severity": self.severity,
            "code": self.code,
            "title": self.title,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass
class DoctorReport:
    generated_at: int
    lookback_sec: int
    status: str
    issues: List[DoctorIssue] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "generated_at": self.generated_at,
            "lookback_sec": self.lookback_sec,
            "status": self.status,
            "issues": [i.to_dict() for i in self.issues],
            "metrics": self.metrics,
        }


class ProductDoctor:
    def __init__(self, db, market_ws=None):
        self.db = db
        self.ws_monitor = WSHealthMonitor(market_ws)

    def diagnose(self, lookback_sec: Optional[int] = None) -> DoctorReport:
        lookback_sec = int(lookback_sec or config.PRODUCT_DOCTOR_LOOKBACK_SEC)
        issues: List[DoctorIssue] = []
        metrics = {}

        ws = self.ws_monitor.snapshot()
        metrics["ws"] = ws
        if ws.get("enabled"):
            if ws.get("status") == "disconnected":
                issues.append(DoctorIssue(
                    "warning", "ws_disconnected", "Polymarket Market WS 未连接",
                    f"reconnects={ws.get('reconnects', 0)} last_error={ws.get('last_error', '')}",
                    "继续使用 REST 回退；检查服务器网络、websockets 依赖和订阅资产数量。",
                ))
            elif ws.get("status") == "stale":
                issues.append(DoctorIssue(
                    "warning", "ws_stale", "WS 行情过期",
                    f"last_message_age_sec={ws.get('last_message_age_sec')}",
                    "降低订阅资产数量或拉长 REALTIME_ORDERBOOK_MAX_AGE_SEC；先观察 REST fallback 是否升高。",
                ))

        try:
            flow = self.db.get_recent_decision_flow_stats(lookback_sec=lookback_sec)
        except Exception:
            flow = {}
        metrics["decision_flow"] = flow
        if int(flow.get("recent_decisions") or 0) > 0:
            pass_ratio = float(flow.get("quality_pass_ratio") or 0.0)
            candidates = float(flow.get("candidates_per_min") or 0.0)
            # Before credentials are configured, skip clusters are expected and should not be
            # interpreted as strategy/execution problems.
            auth_ready = bool(config.has_polymarket_creds)
            if candidates >= float(config.PRODUCT_DOCTOR_HIGH_CANDIDATES_PER_MIN) and auth_ready:
                issues.append(DoctorIssue(
                    "info", "candidate_stream_crowded", "候选流拥挤",
                    f"candidates_per_min={candidates}, recent_decisions={flow.get('recent_decisions')}",
                    "质量门会自动加严；复盘是否集中在某个币种或某个策略制造噪音。",
                ))
            if pass_ratio > float(config.PRODUCT_DOCTOR_HIGH_PASS_RATIO) and auth_ready:
                issues.append(DoctorIssue(
                    "warning", "quality_gate_too_loose", "质量门通过率偏高",
                    f"quality_pass_ratio={pass_ratio}",
                    "观察实际成交率和PnL；必要时提高 BASE_QUALITY_SCORE 或对应策略门槛。",
                ))
            if pass_ratio < float(config.PRODUCT_DOCTOR_LOW_PASS_RATIO) and int(flow.get("recent_decisions") or 0) >= 20 and auth_ready:
                issues.append(DoctorIssue(
                    "info", "quality_gate_tight", "质量门偏紧或当前行情差",
                    f"quality_pass_ratio={pass_ratio}, score_p75={flow.get('score_p75')}",
                    "不建议立刻放松；先看 missed/skip 原因是否长期集中在同一项。",
                ))

        try:
            skips = self.db.get_decision_reason_counts(since_ts=int(time.time()) - lookback_sec, action="skip", limit=6)
        except Exception:
            skips = []
        metrics["top_skip_reasons"] = skips
        if skips:
            top = skips[0]
            reason = str(top.get("reason") or "")
            n = int(top.get("n") or 0)
            if n >= int(config.PRODUCT_DOCTOR_SKIP_CLUSTER_MIN) and (config.has_polymarket_creds or reason not in {"risk_block", "credentials_not_configured", "client_not_authenticated", "balance_unavailable"}):
                issues.append(DoctorIssue(
                    "info", "skip_reason_cluster", "跳过原因集中",
                    f"top_reason={reason}, count={n}",
                    "优先复盘这个原因对应的样本；这是下一轮参数优化入口。",
                ))

        try:
            exec_stats = self.db.get_recent_execution_event_stats(lookback_sec=lookback_sec)
        except Exception:
            exec_stats = {"total": 0, "by_reason": []}
        metrics["execution_events"] = exec_stats

        shadow_mode = bool(getattr(config, "SHADOW_TRADING_ENABLED", False))
        sig3_guard = bool(getattr(config, "CLOB_V2_SIG3_REAL_SUBMIT_ENABLED", False) is False and int(getattr(config, "POLYMARKET_SIGNATURE_TYPE", 0) or 0) == 3)
        try:
            shadow_summary = self.db.get_shadow_pnl_summary()
        except Exception:
            shadow_summary = {"open": 0, "resolved": 0, "wins": 0, "pnl": 0.0}
        try:
            overdue = self.db.get_shadow_overdue_summary(grace_sec=int(getattr(config, "SHADOW_SETTLEMENT_FALLBACK_AFTER_SEC", 120) or 120))
        except Exception:
            overdue = {"open": 0, "overdue": 0, "oldest_seconds_after_close": 0}
        metrics["shadow"] = shadow_summary
        metrics["shadow_overdue"] = overdue

        # Execution-quality clusters are actionable in both real and Shadow modes.
        # Older code hid them whenever Shadow was enabled, which made the doctor look
        # healthy while the final orderbook recheck was repeatedly blocking entries.
        for row in exec_stats.get("by_reason", [])[:4]:
            reason = str(row.get("reason") or "")
            n = int(row.get("n") or 0)
            if reason.startswith("execution_") and n >= int(config.PRODUCT_DOCTOR_EXECUTION_ISSUE_MIN):
                issues.append(DoctorIssue(
                    "warning", "execution_quality_cluster", "执行质量问题集中",
                    f"reason={reason}, count={n}",
                    "检查 WS 新鲜度、滑点上限和盘口深度；这类问题会直接影响成交质量。",
                ))
                break

        if shadow_mode and int(overdue.get("overdue") or 0) > 0:
            issues.append(DoctorIssue(
                "warning", "shadow_settlement_backlog", "影子结算积压",
                f"overdue={overdue.get('overdue')} oldest={overdue.get('oldest_seconds_after_close')}s",
                "影子交易已记录但部分到期样本尚未结算；系统会用 v14.2.38 catch-up 追赶结算。",
            ))

        status = "ok"
        if any(i.severity == "critical" for i in issues):
            status = "critical"
        elif any(i.severity == "warning" for i in issues):
            status = "warning"
        elif issues:
            status = "info"
        return DoctorReport(int(time.time()), lookback_sec, status, issues, metrics)

    def _translate_issue(self, issue: DoctorIssue) -> str:
        code = issue.code or ""
        if code == "candidate_stream_crowded":
            return "候选很多：行情噪音偏多，系统会自动提高质量门；暂不需要手动调参。"
        if code == "quality_gate_tight":
            return "通过率低：多数机会质量不足，这是保护机制；先看“实时机会/跳过原因”。"
        if code == "quality_gate_too_loose":
            return "通过率偏高：需要关注成交质量；如果误进场多，再提高门槛。"
        if code == "skip_reason_cluster":
            return "跳过原因集中：建议打开“跳过原因”页，看是否长期卡在同一类问题。"
        if code == "execution_quality_cluster":
            return "执行质量异常：重点看盘口新鲜度、滑点和深度。"
        if code == "shadow_settlement_backlog":
            return "影子结算积压：已有到期 shadow_open，系统会追赶结算为 shadow_resolved。"
        if code == "ws_disconnected":
            return "WS未连接：当前会继续使用 REST 盘口轮询，不影响基础扫描。"
        if code == "ws_stale":
            return "WS行情过期：当前会回退 REST，先观察是否频繁出现。"
        return issue.title or "发现一项需要关注的问题。"

    def render_text(self, report: Optional[DoctorReport] = None) -> str:
        """Render a user-facing product doctor report.

        Keep this page concise. Raw diagnostic codes/evidence remain available in
        logs/tests, but Telegram should show only actionable Chinese summaries.
        """
        report = report or self.diagnose()
        ws = (report.metrics or {}).get("ws") or {}
        flow = (report.metrics or {}).get("decision_flow") or {}

        auth_ready = bool(config.has_polymarket_creds)
        ws_enabled = bool(ws.get("enabled"))
        ws_label = "WS 实时盘口" if ws_enabled else "REST 盘口轮询"
        ws_status = ws.get("status", "unknown")

        icon = {"ok": "✅", "info": "ℹ️", "warning": "⚠️", "critical": "🛑"}.get(report.status, "ℹ️")
        cn_status = {"ok": "正常", "info": "提示", "warning": "需要关注", "critical": "严重"}.get(report.status, report.status)

        lines = [
            f"{icon} <b>产品医生</b>",
            f"诊断：<b>{cn_status}</b>｜窗口：{int(report.lookback_sec/60)}分钟",
            f"盘口：<b>{ws_label}</b>",
        ]

        if ws_enabled:
            lines.append(f"WS状态：<code>{ws_status}</code>｜重连：{ws.get('reconnects', 0)}")
        else:
            lines.append("WS状态：未启用，不是故障；当前用 REST 盘口轮询。")

        if flow:
            decisions = int(flow.get('recent_decisions') or 0)
            cpm = float(flow.get('candidates_per_min') or 0.0)
            ratio = float(flow.get('quality_pass_ratio') or 0.0)
            lines.append(f"判断：{decisions}次｜约 {cpm:.1f}/分钟｜通过率 {ratio:.0%}")

        shadow = (report.metrics or {}).get("shadow") or {}
        overdue = (report.metrics or {}).get("shadow_overdue") or {}
        if bool(getattr(config, "SHADOW_TRADING_ENABLED", False)):
            real_submit = "已保护关闭" if not bool(getattr(config, "CLOB_V2_SIG3_REAL_SUBMIT_ENABLED", False)) else "已开启"
            lines.append(f"模式：<b>Shadow 回测</b>｜真实提交：<b>{real_submit}</b>")
            lines.append(f"影子样本：open {int(shadow.get('open') or 0)}｜resolved {int(shadow.get('resolved') or 0)}｜PnL ${float(shadow.get('pnl') or 0):+.2f}")
            if int(overdue.get('overdue') or 0) > 0:
                lines.append(f"影子积压：<b>{int(overdue.get('overdue') or 0)}</b> 笔到期未结算｜最久 {int(overdue.get('oldest_seconds_after_close') or 0)} 秒")

        lines.append("")

        if not auth_ready:
            lines += [
                "🔐 <b>当前重点</b>",
                "交易密钥还没完整认证。当前只观察行情，不判断策略好坏，也不会下单。",
                "下一步：进入 <b>设置 → 设置交易密钥</b>，填私钥、Funder、签名类型3，然后点重新连接认证。",
            ]
            return "\n".join(lines)[:3900]

        visible = []
        for issue in report.issues:
            # Hide low-value internal noise from the main doctor page.
            if issue.code in {"candidate_stream_crowded", "quality_gate_tight"} and len(visible) >= 1:
                continue
            visible.append(issue)
            if len(visible) >= 3:
                break

        if not visible:
            lines += [
                "✅ <b>结论</b>",
                "暂时没有必须处理的问题。继续观察实时机会、跳过原因和余额页即可。",
            ]
        else:
            lines.append("🧭 <b>需要关注</b>")
            for i, issue in enumerate(visible, 1):
                lines.append(f"{i}. {self._translate_issue(issue)}")

        lines.append("")
        lines.append("提示：详细样本请看“实时机会”和“跳过原因”，产品医生只给结论。")
        return "\n".join(lines)[:3900]
