"""
Telegram bot with inline button UI.

All user interaction happens through buttons - no commands to remember.
Pushes notifications for trades, settlements, daily summaries, and alerts.

Restricted to a single user_id for security.
"""
import asyncio
import logging
import json
import time
import html
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, ContextTypes
    )
    try:
        from telegram.ext import MessageHandler, filters
    except Exception:  # offline tests may stub a minimal telegram.ext
        MessageHandler = None
        filters = None
except Exception:  # pragma: no cover - allows offline diagnostics/tests without telegram installed
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class Update:
        pass

    class _DummyApp:
        def add_handler(self, *args, **kwargs):
            return None
        async def initialize(self):
            return None
        async def start(self):
            return None
        async def stop(self):
            return None
        async def shutdown(self):
            return None

    class _DummyBuilder:
        def token(self, token):
            return self
        def build(self):
            return _DummyApp()

    class Application:
        @classmethod
        def builder(cls):
            return _DummyBuilder()

    class CommandHandler:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class CallbackQueryHandler(CommandHandler):
        pass

    class _ContextTypes:
        DEFAULT_TYPE = object

    ContextTypes = _ContextTypes()
    MessageHandler = None
    filters = None

from database import Database
from config import config

log = logging.getLogger(__name__)


def authorized(func):
    """Decorator: only allow the configured TG_USER_ID to interact"""
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id != config.TG_USER_ID:
            log.warning(f"Unauthorized access attempt from {user_id}")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper


class TelegramBot:
    def __init__(self, db: Database, trader=None, risk=None, learner=None,
                 polymarket=None, feed=None, product_doctor=None):
        self.db = db
        self.trader = trader
        self.risk = risk
        self.learner = learner
        self.polymarket = polymarket
        self.feed = feed
        self.product_doctor = product_doctor

        if not config.ENABLE_TELEGRAM:
            raise RuntimeError("TelegramBot constructed while ENABLE_TELEGRAM=false")
        self.app = Application.builder().token(config.TG_BOT_TOKEN).build()
        self._register_handlers()
        self._running = False

    def _register_handlers(self):
        # Commands must work even when users type /status or /balance directly.
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("today", self.cmd_today))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        if MessageHandler is not None and filters is not None:
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    # ============ Keyboard layouts ============

    def main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Compact main menu: core controls, results, diagnostics, settings."""
        kb = [
            [
                InlineKeyboardButton("▶️ 启动", callback_data="run"),
                InlineKeyboardButton("⏸ 暂停", callback_data="pause"),
                InlineKeyboardButton("💰 余额", callback_data="balance"),
            ],
            [
                InlineKeyboardButton("📅 今日", callback_data="today"),
                InlineKeyboardButton("📋 订单", callback_data="trade_lifecycle"),
                InlineKeyboardButton("📜 历史", callback_data="history"),
            ],
            [
                InlineKeyboardButton("🧠 复盘报告", callback_data="review_menu"),
                InlineKeyboardButton("🚫 跳过分析", callback_data="skip_analysis_menu"),
                InlineKeyboardButton("🩺 系统健康", callback_data="system_health_menu"),
            ],
            [InlineKeyboardButton("⚙️ 设置", callback_data="settings")],
        ]
        return InlineKeyboardMarkup(kb)

    def _runtime_state_label(self) -> str:
        if not self.risk:
            return "未知"
        try:
            rs = self.risk.status_summary()
            if rs.get("halted"):
                return "已暂停"
            if int(rs.get("cooldown_remaining") or 0) > 0:
                return f"冷却中 {int(rs.get('cooldown_remaining') or 0)}s"
            return "运行中"
        except Exception:
            return "未知"

    def _run_pause_buttons(self, return_to: str = "main") -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton("▶️ 恢复交易", callback_data=f"run:{return_to}"),
            InlineKeyboardButton("⏸ 暂停交易", callback_data=f"pause:{return_to}"),
        ]

    def _trade_mode_label(self) -> str:
        if config.real_orders_enabled:
            return "🟢 真实下单已开启"
        if config.MODE in {"small_live", "live"} and not config.DRY_RUN and not config.REAL_TRADING_ENABLED:
            return "🎭 影子模式｜只记录 Shadow，不真实下单"
        if config.auth_dry_run_enabled:
            return "🟡 认证已配置｜纸面演练（不真实下单）"
        if config.has_polymarket_creds:
            return "🟡 密钥已配置｜真实下单未武装"
        return "⚪ 未配置交易密钥｜不会真实下单"

    def control_keyboard(self, back: str = "main", include_refresh: str | None = None, refresh_label: str = "🔄 刷新") -> InlineKeyboardMarkup:
        """Consistent footer for secondary TG pages."""
        rows = []
        if include_refresh:
            rows.append([InlineKeyboardButton(refresh_label, callback_data=include_refresh)])
        rows.append(self._run_pause_buttons(back))
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data=back)])
        return InlineKeyboardMarkup(rows)

    def settings_keyboard(self) -> InlineKeyboardMarkup:
        """Compact settings menu; diagnostics moved to grouped pages."""
        kb = [
            [InlineKeyboardButton("🎭 切到影子模式", callback_data="trade_mode_shadow"), InlineKeyboardButton("🟢 切到真实下单", callback_data="trade_mode_real")],
            [InlineKeyboardButton("⚪ 切到观察/纸面", callback_data="trade_mode_paper")],
            [InlineKeyboardButton("💵 设置每笔金额", callback_data="set_bet_size")],
            [InlineKeyboardButton("💰 设置可用本金", callback_data="set_authorized_capital")],
            [InlineKeyboardButton("🔐 设置交易密钥", callback_data="trade_keys")],
            [InlineKeyboardButton("🩹 自助修复", callback_data="self_repair")],
            [InlineKeyboardButton("🔧 高级选项", callback_data="advanced_menu")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="main")],
        ]
        return InlineKeyboardMarkup(kb)

    def review_menu_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 币种统计", callback_data="asset_stats")],
            [InlineKeyboardButton("🧠 策略复盘", callback_data="strategy_review")],
            [InlineKeyboardButton("📊 信号统计", callback_data="signal_stats")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="main")],
        ])

    def skip_analysis_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 实时机会", callback_data="recent_decisions")],
            [InlineKeyboardButton("🚫 跳过原因", callback_data="skip_reasons")],
            [InlineKeyboardButton("🧪 筛选漏斗", callback_data="quality_funnel")],
            [InlineKeyboardButton("🎚 当前门槛", callback_data="quality_gate")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="main")],
        ])

    def system_health_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🩺 产品医生", callback_data="product_doctor")],
            [InlineKeyboardButton("📡 WS健康", callback_data="ws_health")],
            [InlineKeyboardButton("🔬 订单提交详情", callback_data="order_submission_details")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="main")],
        ])

    def advanced_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 重置每日统计", callback_data="reset_daily")],
            [InlineKeyboardButton("🔬 订单提交详情", callback_data="order_submission_details")],
            [InlineKeyboardButton("📊 信号统计", callback_data="signal_stats")],
            [InlineKeyboardButton("🗑 清理数据库", callback_data="cleanup_db")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="settings")],
        ])

    # ============ Command handlers ============

    @authorized
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._render_status()
        await update.message.reply_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    @authorized
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._render_status()
        await update.message.reply_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    @authorized
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = await self._build_balance_text()
        await update.message.reply_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    @authorized
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._build_settings_text()
        await update.message.reply_text(text, reply_markup=self.settings_keyboard(), parse_mode="HTML")

    @authorized
    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = self._build_today_text()
        await update.message.reply_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    @authorized
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive one-shot secret/config values requested by the inline UI."""
        runtime_field = context.user_data.pop("awaiting_runtime_field", None)
        field = context.user_data.pop("awaiting_trade_key_field", None)
        if not runtime_field and not field:
            return

        raw = (update.message.text or "").strip()
        try:
            await update.message.delete()
        except Exception:
            pass

        # Remove the previous prompt bubble so stale “取消” buttons do not remain after save.
        # Telegram history may still show older messages sent before this version; they are harmless.
        if runtime_field:
            prompt_chat_id = context.user_data.pop("runtime_prompt_chat_id", None)
            prompt_message_id = context.user_data.pop("runtime_prompt_message_id", None)
        else:
            prompt_chat_id = context.user_data.pop("trade_key_prompt_chat_id", None)
            prompt_message_id = context.user_data.pop("trade_key_prompt_message_id", None)
        if prompt_chat_id and prompt_message_id:
            try:
                await context.bot.delete_message(chat_id=prompt_chat_id, message_id=prompt_message_id)
            except Exception:
                try:
                    await context.bot.edit_message_reply_markup(chat_id=prompt_chat_id, message_id=prompt_message_id, reply_markup=None)
                except Exception:
                    pass

        if runtime_field:
            ok, msg = self._save_runtime_field(runtime_field, raw)
            text = (f"✅ {msg}" if ok else f"⛔ {msg}\n\n请重新点击对应按钮再输入。")
            await update.effective_chat.send_message(text, reply_markup=self.settings_keyboard(), parse_mode="HTML")
            return

        ok, msg = self._save_trade_key_field(field, raw)
        if ok:
            text = f"✅ {msg}\n\n明文消息已尝试删除。你可以点“重新连接认证”立即测试。"
        else:
            text = f"⛔ {msg}\n\n请重新点击对应按钮再输入。"
        await update.effective_chat.send_message(text, reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")

    @authorized
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data

        if data == "set_bet_size_custom":
            await self._action_runtime_custom_prompt(update, context, "test_bet_size", "每笔最大投注金额", "例如：1、2.5、10")
            return
        if data == "set_authorized_capital_custom":
            await self._action_runtime_custom_prompt(update, context, "authorized_capital_usd", "可用本金上限", "例如：50、100、200；输入 0 表示关闭隔离")
            return
        if data.startswith("set_bet_size_value:"):
            await self._action_set_bet_size_value(q, data.split(":", 1)[1])
            return

        if data.startswith("set_authorized_capital_value:"):
            await self._action_set_authorized_capital_value(q, data.split(":", 1)[1])
            return
        if data.startswith("trade_key_set:"):
            await self._action_trade_key_set_prompt(update, context, data.split(":", 1)[1])
            return
        if data.startswith("trade_key_sig:"):
            await self._action_trade_key_set_signature(q, data.split(":", 1)[1])
            return
        if data.startswith("run:"):
            await self._action_run(q, data.split(":", 1)[1] or "main")
            return
        if data.startswith("pause:"):
            await self._action_pause(q, data.split(":", 1)[1] or "main")
            return
        if data.startswith("trade_detail:"):
            await self._show_trade_detail(q, data.split(":", 1)[1])
            return

        handlers = {
            "main": self._show_main,
            "run": self._action_run,
            "pause": self._action_pause,
            "today": self._show_today,
            "balance": self._show_balance,
            "settings": self._show_settings,
            "trade_mode_shadow": self._action_trade_mode_shadow,
            "trade_mode_real": self._action_trade_mode_real,
            "trade_mode_paper": self._action_trade_mode_paper,
            "review_menu": self._show_review_menu,
            "skip_analysis_menu": self._show_skip_analysis_menu,
            "system_health_menu": self._show_system_health_menu,
            "advanced_menu": self._show_advanced_menu,
            "history": self._show_history,
            "trade_lifecycle": self._show_trade_lifecycle,
            "order_submission_details": self._show_order_submission_details,
            "signal_stats": self._show_signal_stats,
            "asset_stats": self._show_asset_stats,
            "strategy_review": self._show_strategy_review,
            "recent_decisions": self._show_recent_decisions,
            "skip_reasons": self._show_skip_reasons,
            "quality_funnel": self._show_quality_funnel,
            "quality_gate": self._show_quality_gate,
            "product_doctor": self._show_product_doctor,
            "ws_health": self._show_ws_health,
            "set_bet_size": self._show_bet_size_picker,
            "set_authorized_capital": self._show_authorized_capital_picker,
            "trade_keys": self._show_trade_keys,
            "trade_keys_clear": self._action_trade_keys_clear,
            "trade_keys_reconnect": self._action_trade_keys_reconnect,
            "reset_daily": self._action_reset_daily,
            "reset_daily_confirm": self._action_reset_daily_confirm,
            "cleanup_db": self._action_cleanup_db,
            "cleanup_db_confirm": self._action_cleanup_db_confirm,
            "self_repair": self._action_self_repair,
        }
        handler = handlers.get(data, self._show_main)
        try:
            await handler(q)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Telegram callback failed: %s data=%s", e, data)
            text = f"⚠️ 按钮处理失败：<code>{html.escape(str(e))[:800]}</code>\n\n请点返回或稍后重试。"
            try:
                await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="main")]]), parse_mode="HTML")
            except Exception:
                try:
                    await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="main")]]), parse_mode="HTML")
                except Exception:
                    pass

    # ============ Action handlers ============

    async def _show_main(self, q):
        await q.edit_message_text(self._render_status(), reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    async def _action_run(self, q, return_to: str = "main"):
        if self.risk:
            self.risk.resume()
        await self._after_run_pause(q, return_to, "✅ 已恢复交易循环。")

    async def _action_pause(self, q, return_to: str = "main"):
        if self.risk:
            self.risk.halt("用户暂停")
        await self._after_run_pause(q, return_to, "⏸ 已暂停交易循环。")

    async def _after_run_pause(self, q, return_to: str, prefix: str):
        """Keep users on the page where they clicked Start/Pause when possible."""
        if return_to == "product_doctor":
            await self._show_product_doctor(q, notice=prefix)
            return
        if return_to == "ws_health":
            await self._show_ws_health(q, notice=prefix)
            return
        if return_to == "recent_decisions":
            await self._show_recent_decisions(q)
            return
        if return_to == "skip_reasons":
            await self._show_skip_reasons(q)
            return
        text = prefix + "\n\n" + self._render_status()
        await q.edit_message_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    def _build_today_text(self) -> str:
        if not self.learner:
            return "学习模块未初始化"

        s = self.learner.daily_summary()

        text = f"<b>📅 今日总结 ({s['date']})</b>\n\n"
        text += f"真实交易: <b>{s['trades_total']}</b>\n"
        text += f"已结算: {s['trades_resolved']} | 待结算: {s['trades_open']}\n"
        if s.get('shadow_trades') or s.get('shadow_resolved'):
            text += f"🎭 影子样本: {s.get('shadow_resolved', 0)} | 影子PnL ${float(s.get('shadow_pnl') or 0):+.2f}\n"
        text += f"胜/负: <b>{s['wins']}</b>/{s['losses']}\n"

        if s['trades_resolved'] > 0:
            text += f"胜率: <b>{s['win_rate']:.1%}</b>\n"
            text += f"今日盈亏: <b>{'🟢' if s['total_pnl'] >= 0 else '🔴'} ${s['total_pnl']:+.2f}</b>\n"
            text += f"平均盈/亏: ${s['avg_win']:.2f} / ${s['avg_loss']:.2f}\n"
            text += f"盈亏比: {s['risk_reward']:.2f}\n"

        if s.get('pattern_breakdown'):
            text += "\n<b>形态表现:</b>\n"
            for pattern, ps in s['pattern_breakdown'].items():
                wr = ps['wins'] / ps['trades'] if ps['trades'] else 0
                text += f"• {pattern}: {ps['wins']}/{ps['trades']} ({wr:.0%}) ${ps['pnl']:+.2f}\n"
        return text

    async def _show_today(self, q):
        await q.edit_message_text(self._build_today_text(), reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    async def _build_balance_text(self) -> str:
        balance = None
        if self.polymarket and self.polymarket.connected:
            balance = await asyncio.to_thread(self.polymarket.get_balance)

        text = f"<b>💰 账户余额</b>\n\n"
        if balance is None:
            text += "当前余额: <b>读取失败/未认证</b>\n"
            text += "说明: 未读取到余额时不会继续实盘下单，也不会把余额当成 0 触发错误止损。"
            return text

        balance = float(balance)
        starting = config.starting_bankroll
        pnl = balance - starting
        roi = (pnl / starting * 100) if starting > 0 else 0

        text += f"钱包余额: <b>${balance:.2f}</b>\n"

        if config.capital_isolation_enabled and self.risk:
            try:
                summary = self.risk.capital_status_summary(balance)
            except Exception as e:
                log.warning("failed to render capital isolation summary: %s", e)
                summary = None
            if summary and summary.get("enabled"):
                authorized = float(summary.get("authorized_capital") or 0.0)
                reserve = float(summary.get("reserve_balance") or 0.0)
                trading_equity = float(summary.get("trading_equity") or 0.0)
                try:
                    today_pnl = float(self.db.get_today_realized_pnl()) if self.db else 0.0
                except Exception:
                    today_pnl = 0.0

                text += "\n─── <b>资本隔离</b> ───\n"
                text += f"可用本金上限: <b>${authorized:.2f}</b>\n"
                text += f"保护储备: <b>${reserve:.2f}</b> (不动用)\n"
                text += f"可交易资产: <b>${trading_equity:.2f}</b>\n"
                text += f"每笔最大投注: <b>${config.effective_per_order_amount:.2f}</b>\n"
                text += f"触发停手余额: <b>${reserve:.2f}</b>\n"
                text += f"今日已结算: <b>{'🟢' if today_pnl >= 0 else '🔴'} ${today_pnl:+.2f}</b>\n"
                try:
                    sh = self.db.get_shadow_pnl_summary() if self.db else {}
                    if sh.get("resolved") or sh.get("open"):
                        wr = (float(sh.get("wins") or 0) / float(sh.get("resolved") or 1)) if sh.get("resolved") else 0.0
                        text += f"🎭 影子账户: <b>${float(sh.get('pnl') or 0):+.2f}</b> | 已结算 {int(sh.get('resolved') or 0)} | 待结算 {int(sh.get('open') or 0)} | 胜率 {wr:.0%}\n"
                except Exception:
                    pass

                bar_len = 10
                progress = min(1.0, max(0.0, trading_equity / authorized)) if authorized > 0 else 0.0
                filled = int(bar_len * progress)
                bar = "🟩" * filled + "⬜" * (bar_len - filled)
                text += f"安全度: {bar} {progress*100:.0f}%"
                return text

        # No capital isolation: do not infer profit from wallet balance. Only show current wallet/risk state.
        try:
            today_pnl = float(self.db.get_today_realized_pnl()) if self.db else 0.0
        except Exception:
            today_pnl = 0.0
        text += "资本隔离: <b>关闭</b>（整个钱包余额可参与交易）\n"
        text += f"每笔最大投注: <b>${config.effective_per_order_amount:.2f}</b>\n"
        text += f"今日已结算: <b>{'🟢' if today_pnl >= 0 else '🔴'} ${today_pnl:+.2f}</b>\n"
        try:
            sh = self.db.get_shadow_pnl_summary() if self.db else {}
            if sh.get("resolved") or sh.get("open"):
                wr = (float(sh.get("wins") or 0) / float(sh.get("resolved") or 1)) if sh.get("resolved") else 0.0
                text += f"🎭 影子账户: <b>${float(sh.get('pnl') or 0):+.2f}</b> | 已结算 {int(sh.get('resolved') or 0)} | 待结算 {int(sh.get('open') or 0)} | 胜率 {wr:.0%}\n"
        except Exception:
            pass
        text += "说明: 此处不再把钱包余额变化当作机器人盈利。盈亏只以已结算交易为准。"
        return text

    async def _show_balance(self, q):
        text = await self._build_balance_text()
        await q.edit_message_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    def _build_settings_text(self) -> str:
        text = f"<b>⚙️ 设置</b>\n\n"
        text += f"交易模式: <b>{html.escape(self._trade_mode_label())}</b>\n"
        text += f"开关: MODE=<code>{html.escape(str(config.MODE))}</code> | DRY_RUN=<code>{str(config.DRY_RUN).lower()}</code> | REAL=<code>{str(config.REAL_TRADING_ENABLED).lower()}</code>\n"
        text += f"认证状态: <b>{'✅ 已配置' if config.has_polymarket_creds else '⛔ 未配置交易密钥'}</b>\n"
        text += f"每笔最大投注金额: <b>${config.effective_per_order_amount:.2f}</b>\n"
        if config.capital_isolation_enabled:
            text += f"可用本金上限: <b>${config.effective_authorized_capital_usd:.2f}</b> (资本隔离已启用)\n"
        else:
            text += "资本隔离: <b>关闭</b>\n"
        text += f"价格范围: ${config.MIN_TOKEN_PRICE:.2f}-${config.MAX_TOKEN_PRICE:.2f}\n"
        text += f"最低模型胜率: {config.MIN_MODEL_PROB:.0%}\n"
        text += f"最低费后边际: {config.MIN_EDGE_AFTER_FEES:.1%}\n"
        _loss_caps = [x for x in (config.effective_max_daily_loss_usd, config.starting_bankroll * config.effective_max_daily_loss_pct) if x and x > 0]
        text += f"每日亏损上限: ${min(_loss_caps) if _loss_caps else 0:.2f}\n"
        text += f"信号窗口: 剩余 {config.ENTRY_MIN_SECONDS_LEFT}-{config.ENTRY_MAX_SECONDS_LEFT} 秒\n"
        text += f"执行容忍: 滑点 {config.EXECUTION_GUARD_SLIPPAGE_CAP:.1%}｜盘口冲击 {config.QUALITY_MAX_PRICE_IMPACT:.1%}｜价差 {config.TARGET_SPREAD_MAX:.1%}\n"
        text += f"订单类型: {config.ORDER_TYPE}"
        return text

    async def _show_settings(self, q):
        await q.edit_message_text(self._build_settings_text(), reply_markup=self.settings_keyboard(), parse_mode="HTML")

    async def _show_review_menu(self, q):
        text = (
            "<b>🧠 复盘报告</b>\n\n"
            "把学习结果相关页面合并在这里：币种表现、策略复盘、信号统计。"
        )
        await q.edit_message_text(text, reply_markup=self.review_menu_keyboard(), parse_mode="HTML")

    async def _show_skip_analysis_menu(self, q):
        text = (
            "<b>🚫 跳过分析</b>\n\n"
            "这里集中查看机会流、跳过原因、筛选漏斗和动态门槛。"
        )
        await q.edit_message_text(text, reply_markup=self.skip_analysis_keyboard(), parse_mode="HTML")

    async def _show_system_health_menu(self, q):
        text = (
            "<b>🩺 系统健康</b>\n\n"
            "这里集中查看产品医生、WS 健康和订单提交详情。"
        )
        await q.edit_message_text(text, reply_markup=self.system_health_keyboard(), parse_mode="HTML")

    async def _show_advanced_menu(self, q):
        text = (
            "<b>🔧 高级选项</b>\n\n"
            "低频维护入口集中放在这里，避免主菜单和设置菜单过长。"
        )
        await q.edit_message_text(text, reply_markup=self.advanced_keyboard(), parse_mode="HTML")

    async def _show_history(self, q):
        trades = self.db.get_recent_trades(10)
        text = f"<b>📜 最近真实成交/结算</b>\n\n"

        if not trades:
            text += "暂无真实成交记录。\n\n"
            text += "说明：进入执行评估、提交失败、未撮合的订单不会再显示成 <code>❌ -$0.00</code>。\n"
            text += "这些记录请看 <b>📋 订单生命周期</b> 或 <b>🔬 订单提交详情</b>。"
        else:
            for t in trades:
                ts = datetime.fromtimestamp(t['timestamp'], tz=timezone.utc).strftime("%m-%d %H:%M")
                status = str(t.get('status') or '').lower()
                filled_cost = float(t.get('filled_cost') or t.get('cost') or 0)
                if status in {'open', 'matched', 'filled'}:
                    text += f"⏳ {ts} | {t['direction']} @ ${float(t.get('entry_price') or 0):.3f} | 成本 ${filled_cost:.2f} | 状态 {html.escape(status)}\n"
                elif t.get('outcome') == 'win':
                    text += f"✅ {ts} | +${float(t.get('pnl') or 0):.2f} | {t['direction']}\n"
                elif t.get('outcome') == 'loss':
                    text += f"❌ {ts} | -${abs(float(t.get('pnl') or 0)):.2f} | {t['direction']}\n"
                else:
                    text += f"📌 {ts} | 已结算 | PnL ${float(t.get('pnl') or 0):+.2f} | {t['direction']}\n"

        await q.edit_message_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")

    def _persist_trade_mode(self, *, mode: str, dry_run: bool, real_trading: bool, sig3_submit: bool | None = None):
        config.MODE = mode
        config.DRY_RUN = bool(dry_run)
        config.REAL_TRADING_ENABLED = bool(real_trading)
        config.OBSERVER_ONLY = False
        if sig3_submit is not None:
            config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED = bool(sig3_submit)
        self.db.set_state("mode", mode)
        self.db.set_state("dry_run", bool(dry_run))
        self.db.set_state("real_trading_enabled", bool(real_trading))
        if sig3_submit is not None:
            self.db.set_state("clob_v2_sig3_real_submit_enabled", bool(sig3_submit))
        updates = {
            "MODE": mode,
            "DRY_RUN": str(bool(dry_run)).lower(),
            "REAL_TRADING_ENABLED": str(bool(real_trading)).lower(),
            "OBSERVER_ONLY": "false",
        }
        if sig3_submit is not None:
            updates["CLOB_V2_SIG3_REAL_SUBMIT_ENABLED"] = str(bool(sig3_submit)).lower()
        self._persist_env(updates)

    async def _action_trade_mode_shadow(self, q):
        self._persist_trade_mode(mode="small_live", dry_run=False, real_trading=False)
        if self.risk:
            self.risk.resume()
        text = (
            "✅ 已切换到 <b>影子模式</b>。\n\n"
            "系统会继续跑完整的真实盘口/质量/风控检查，但不会调用 Polymarket post_order；"
            "通过检查的机会会写入本地 Shadow 样本，用于复盘学习。"
        )
        await q.edit_message_text(text + "\n\n" + self._build_settings_text(), reply_markup=self.settings_keyboard(), parse_mode="HTML")

    async def _action_trade_mode_real(self, q):
        sig3_submit = True if int(config.effective_polymarket_signature_type or 0) == 3 else None
        self._persist_trade_mode(mode="small_live", dry_run=False, real_trading=True, sig3_submit=sig3_submit)
        reconnect_note = ""
        if self.polymarket and config.has_polymarket_creds:
            try:
                await asyncio.to_thread(self.polymarket.connect)
                reconnect_note = "\n✅ 已重新连接 Polymarket 认证。"
            except Exception as e:
                reconnect_note = f"\n⚠️ 重新连接认证失败：<code>{html.escape(str(e))[:500]}</code>"
        if self.risk:
            self.risk.resume()
        text = (
            "🟢 已切换到 <b>真实下单模式</b>。\n\n"
            "现在配置为 MODE=small_live、DRY_RUN=false、REAL_TRADING_ENABLED=true。"
            "如果密钥、余额、风控、盘口和 Profit Rule 都通过，系统会真实调用 Polymarket 下单。"
            f"{reconnect_note}"
        )
        if not config.has_polymarket_creds:
            text += "\n\n⛔ 但当前还没有完整交易密钥，请先进入 🔐 设置交易密钥。"
        await q.edit_message_text(text + "\n\n" + self._build_settings_text(), reply_markup=self.settings_keyboard(), parse_mode="HTML")

    async def _action_trade_mode_paper(self, q):
        self._persist_trade_mode(mode="paper", dry_run=True, real_trading=False)
        text = (
            "⚪ 已切换到 <b>观察/纸面模式</b>。\n\n"
            "系统只观察和记录，不会真实下单，也不会写真实 Shadow 下单样本。"
        )
        await q.edit_message_text(text + "\n\n" + self._build_settings_text(), reply_markup=self.settings_keyboard(), parse_mode="HTML")


    # ============ Live switch controls ============

    def _persist_env(self, updates: dict):
        """Persist small safe runtime switches back to .env for restart consistency."""
        try:
            path = Path(".env")
            if not path.exists():
                return
            text = path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            seen = set()
            out = []
            for line in lines:
                replaced = False
                for k, v in updates.items():
                    if line.startswith(k + "="):
                        out.append(f"{k}={v}")
                        seen.add(k)
                        replaced = True
                        break
                if not replaced:
                    out.append(line)
            for k, v in updates.items():
                if k not in seen:
                    out.append(f"{k}={v}")
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
        except Exception as e:
            log.warning("failed to persist .env updates: %s", e)

    async def _show_signal_stats(self, q):
        if not self.learner:
            return
        stats = self.learner.signal_performance()
        text = "<b>📊 信号表现统计</b>\n\n"
        if not stats:
            text += "数据不足（每个信号至少需要3次样本）"
        else:
            for s in stats:
                text += f"<b>{s['signal_type']}</b>\n"
                text += f"  胜率: {s['win_rate']:.1%} ({s['wins']}/{s['attempts']})\n"
                text += f"  累计PnL: ${s['total_pnl']:+.2f}\n"
                text += f"  权重: {s['weight']:.2f}\n\n"
        await q.edit_message_text(text, reply_markup=self.review_menu_keyboard(), parse_mode="HTML")


    async def _show_asset_stats(self, q):
        text = "<b>📊 币种统计</b>\n\n"
        for asset in config.assets_enabled:
            price = 0.0
            try:
                feed = self.feed.get_feed(asset) if hasattr(self.feed, "get_feed") else self.feed
                if hasattr(feed, "get_current_price"):
                    price = float(feed.get_current_price() or 0.0)
            except Exception:
                price = 0.0
            used = self.db.get_asset_balance_used(asset)
            trades = [t for t in self.db.get_today_trades() if t.get("asset", "BTC") == asset and not int(t.get("is_shadow") or 0)]
            resolved = [t for t in trades if t.get("status") == "resolved"]
            shadow_rows = [t for t in self.db.get_today_trades() if t.get("asset", "BTC") == asset and int(t.get("is_shadow") or 0)]
            shadow_resolved = [t for t in shadow_rows if t.get("status") == "shadow_resolved"]
            wins = len([t for t in resolved if t.get("outcome") == "win"])
            pnl = sum(float(t.get("pnl") or 0) for t in resolved)
            wr = (wins / len(resolved)) if resolved else 0
            text += f"<b>{asset}</b>  ${price:,.2f}\n"
            text += f"今日真实: {len(trades)}笔｜胜{wins}｜胜率 {wr:.0%}｜PnL ${pnl:+.2f}\n"
            if shadow_rows:
                spnl = sum(float(t.get("pnl") or 0) for t in shadow_resolved)
                swins = len([t for t in shadow_resolved if t.get("outcome") == "win"])
                swr = swins / len(shadow_resolved) if shadow_resolved else 0
                text += f"🎭 影子: {len(shadow_resolved)}/{len(shadow_rows)}｜胜率 {swr:.0%}｜PnL ${spnl:+.2f}\n"
            text += f"持仓占用: ${used:.2f}\n\n"
        await q.edit_message_text(text, reply_markup=self.review_menu_keyboard(), parse_mode="HTML")

    async def _show_strategy_review(self, q):
        text = "<b>🧠 策略复盘 / 调权</b>\n\n"
        rows = []
        try:
            rows = self.db.get_asset_strategy_stats()
        except Exception:
            rows = []
        if not rows:
            text += "暂无样本。等交易结算后会显示每个币种/策略的胜率、平均倍率、PnL 和权重。"
        else:
            for r in rows[:18]:
                trades = int(r.get("trades") or 0)
                wins = int(r.get("wins") or 0)
                wr = wins / trades if trades else 0
                sh_trades = int(r.get('shadow_trades') or 0)
                sh_wins = int(r.get('shadow_wins') or 0)
                sh_wr = sh_wins / sh_trades if sh_trades else 0
                text += (
                    f"<b>{r.get('asset','BTC')} {r.get('strategy_name','unknown')}</b>\n"
                    f"  真实样本: {trades} | 胜率: {wr:.1%} | PnL: ${float(r.get('total_pnl') or 0):+.2f}\n"
                    f"  🎭 影子样本: {sh_trades} | 胜率: {sh_wr:.1%} | PnL: ${float(r.get('shadow_pnl') or 0):+.2f}\n"
                    f"  均价: ${float(r.get('avg_entry_price') or 0):.3f} | 平均倍率: {float(r.get('avg_multiple') or 0):.2f}x | 最大倍率: {float(r.get('max_multiple') or 0):.2f}x\n"
                    f"  连亏: {int(r.get('loss_streak') or 0)} | 权重: {float(r.get('weight') or 1):.2f}\n\n"
                )
        await q.edit_message_text(text, reply_markup=self.review_menu_keyboard(), parse_mode="HTML")


    def _safe_text(self, value, max_len: int = 80) -> str:
        txt = str(value if value is not None else "")
        txt = txt.replace("\n", " ").strip()
        if len(txt) > max_len:
            txt = txt[:max_len - 1] + "…"
        return html.escape(txt)

    def _decision_strategy_name(self, row: dict) -> str:
        try:
            details = json.loads(row.get("details_json") or "{}")
            raw = str(details.get("strategy_name") or details.get("pattern") or details.get("signal_type") or "")
            mapping = {
                "barrier_reclaim": "目标线回收",
                "prob_edge": "概率优势",
                "late_line_snipe": "尾盘控线",
                "momentum_follow": "动量跟随",
                "odds_ev": "赔率优势",
                "odds_lag": "盘口滞后",
                "odds_reversal": "赔率反转",
                "wick_rejection_odds": "插针回收",
                "lottery_reversal": "长尾回收",
            }
            return mapping.get(raw, raw)
        except Exception:
            return ""

    def _local_time_text(self, ts: int) -> str:
        try:
            return datetime.fromtimestamp(int(ts or 0)).strftime("%H:%M:%S")
        except Exception:
            return "--:--:--"

    def _display_action(self, action: str) -> str:
        mapping = {
            "observe": "观察",
            "skip": "跳过",
            "candidate": "候选",
            "order_attempt": "开始提交",
            "order_matched": "成交",
            "order_rejected": "拒绝",
            "dry_run_signal": "观察信号",
        }
        return mapping.get(str(action or ""), str(action or "未知"))

    def _display_reason(self, reason: str) -> str:
        """Human-readable reason labels for TG. Avoid raw internal codes."""
        mapping = {
            "strategy_filters_no_match": "策略条件不够，继续观察",
            "outside_strategy_time_window": "不在进场时间窗口",
            "missing_price_to_beat": "没有安全目标价，保护跳过",
            "orderbook_missing": "盘口暂时没有可买价",
            "orderbook_too_thin_no_fill": "盘口太薄，无法按订单大小成交",
            "orderbook_depth_insufficient": "盘口深度不足",
            "token_price_outside_range": "价格太贵/太接近1，不追",
            "risk_block": "风控或认证未通过",
            "credentials_not_configured": "未填写交易密钥",
            "client_not_authenticated": "交易认证未完成",
            "balance_unavailable": "余额读取失败",
            "insufficient_window_klines": "K线样本不足，等待数据",
            "market_not_ready": "市场还没准备好",
            "quality_score_below_dynamic_threshold": "质量分不够，未达到门槛",
            "execution_price_moved_up": "下单前价格变贵，放弃",
            "execution_spread_widened": "价差变大，放弃",
            "execution_depth_evaporated": "盘口深度不足，放弃",
            "execution_edge_evaporated": "优势消失，放弃",
            "execution_recheck_orderbook_stale": "盘口数据过期，放弃",
            "target_spread_high": "买卖价差太大，保护跳过",
            "quality_spread_too_wide": "买卖价差太大，质量门拦截",
            "quality_price_impact_high": "盘口冲击太大，买入会推高价格",
            "slippage_cap_exceeded": "滑点超过上限，暂不追价",
            "execution_slippage_cap_exceeded": "下单前滑点超过上限",
            "insufficient_depth": "盘口深度不足",
            "quality_depth_too_low": "盘口深度不足，质量门拦截",
            "execution_price_above_strategy_cap": "价格超过策略允许上限",
            "execution_recheck_orderbook_missing": "下单前盘口缺失",
            "fee_adjusted_edge_low": "扣除费用后优势不够",
            "high_win_rate_token_too_expensive": "高胜率模式：票价太贵",
            "high_win_rate_edge_low": "高胜率模式：优势不够",
            "high_win_rate_strategy_loss_streak": "高胜率模式：策略连亏暂停",
            "high_win_rate_recent_winrate_low": "高胜率模式：近期胜率过低",
            "high_win_rate_model_prob_low": "高胜率模式：模型概率不足",
            "high_win_rate_stats_promoted": "高胜率模式：历史统计通过",
            "high_win_rate_model_pass": "高胜率模式：模型门槛通过",
            "model_book_gap_high": "模型和盘口分歧太大",
            "binary_book_sanity_failed": "两边盘口异常，保护跳过",
            "opposite_spread_high": "反向盘口价差太大",
            "opposite_orderbook_missing": "反向盘口缺失，保护跳过",
            "opposite_orderbook_missing_after_check": "反向盘口异常，保护跳过",
            "token_range_override_high_confidence": "高概率信号，已放宽价格区间",
            "recent_order_attempt_lock": "刚提交过，短暂冷却",
            "decision_cycle_coalesced": "同一轮重复判断，已合并",
            "already_traded_window": "这个窗口已经交易过，不重复买",
            "stale_proxy_feed": "外部价格数据过期，等待刷新",
            "market_not_found": "没有找到对应市场",
            "market_not_tradable": "市场暂不可交易",
            "market_window_mismatch": "信号窗口和市场窗口不一致",
            "chainlink_rule_text_missing": "市场规则文本缺失，保护跳过",
            "repricing_removed_edge": "重新定价后优势消失",
            "model_probability_low": "模型概率低于最低门槛",
            "token_id_missing": "缺少交易 token ID",
            "asset_risk_block": "单币种风控拦截",
            "order_validation_failed": "下单前校验失败",
            "invalid_bet_amount": "订单金额无效，保护跳过",
            "invalid_share_estimate": "份额估算无效，保护跳过",
            "risk_module_exception": "风控模块异常，保护跳过",
            "order_placement_failed": "订单提交失败，没有真正下单",
            "order_submit_timeout": "提交超时，未确认",
            "order_not_matched": "订单提交了，但没有立即成交",
            "order_fill_unconfirmed": "订单返回了，但未确认成交",
            "submit_started": "开始提交订单",
            "price_unavailable": "价格数据暂不可用",
            "insufficient_vol_history": "波动率历史样本不足",
            "not_evaluated": "尚未完成评估",
            "no_strategy_signal": "当前没有策略信号",
            "no_signal": "当前没有信号",
            "dry_run_not_submitted": "观察模式未提交订单",
            "quality_gate_pass": "质量门已通过",
            "passed_filters": "过滤条件已通过",
            "trade_opened": "交易已打开",
            "barrier_reclaim_candidate": "目标线回收候选",
            "barrier_reclaim_confirm": "目标线回收确认",
            "barrier_reclaim_custom_gate_pass": "目标线回收专属门槛通过",
            "barrier_reclaim_weighted_avg_too_high": "目标线回收成交均价过高",
            "execution_recheck_no_best_ask": "下单前没有可用卖价",
            "profit_rule_engine_error": "利润规则引擎异常，保护跳过",
            "barrier_reclaim_edge_low": "目标线回收优势不够",
            "barrier_reclaim_book_quality_low": "目标线回收盘口质量不足",
            "barrier_reclaim_price_impact_high": "目标线回收盘口冲击过高",
            "barrier_reclaim_impact_too_high": "盘口冲击过大",
            "barrier_reclaim_insufficient_depth": "盘口深度不足",
            "barrier_reclaim_model_prob_too_low": "目标线回收模型概率低于75%",
            "barrier_reclaim_entry_too_low": "目标线回收入场价过低",
            "barrier_reclaim_entry_too_high": "目标线回收入场价过高",
            "barrier_reclaim_model_gap_guard": "目标线回收模型盘口分歧过大",
            "shadow_duplicate_existing": "影子重复信号已忽略",
            "clob_v2_signature_type_3_signer_guard": "真实提交保护，转入影子回测",
        }
        raw = str(reason or "")
        if raw in mapping:
            return mapping[raw]
        if not raw:
            return "未知原因"
        return "其他保护原因"

    def _reason_hint(self, reason: str) -> str:
        """One short explanation for common skip reasons."""
        hints = {
            "token_price_outside_range": "例如 $0.99 的票，收益空间太小，机器人不会追。",
            "orderbook_missing": "当时没有有效 ask/可吃盘口，不能安全下单。",
            "orderbook_too_thin_no_fill": "便宜的票流动性差，实际买入价远高于显示价格，已自动放弃。",
            "orderbook_depth_insufficient": "便宜的票流动性差，实际买入价远高于显示价格，已自动放弃。",
            "missing_price_to_beat": "缺少可接受最高买价；没有安全价就不下单。",
            "credentials_not_configured": "先到 设置 → 交易密钥 填私钥、Funder、签名类型3。",
            "client_not_authenticated": "点 重新连接认证，认证成功后才会真实下单。",
            "balance_unavailable": "余额接口没读到，机器人不会冒险下单。",
            "insufficient_window_klines": "刚切换新窗口时常见，等K线样本够了再判断。",
            "quality_score_below_dynamic_threshold": "机会质量没过门槛，属于保护过滤。",
            "target_spread_high": "买价和卖价距离太大，成交后容易立刻亏价差。",
            "quality_spread_too_wide": "盘口价差偏大，质量门认为不值得追。",
            "quality_price_impact_high": "盘口深度不够，$1 买入也可能把成交均价推高。",
            "slippage_cap_exceeded": "为了成交需要追价，超过当前允许滑点，所以跳过。",
            "execution_slippage_cap_exceeded": "下单前复查发现成交均价会超出滑点上限。",
            "execution_edge_evaporated": "下单前重新读取盘口后，原来的优势已经消失。",
            "insufficient_depth": "当前可买数量不足，FOK 订单大概率无法完整成交。",
            "execution_price_above_strategy_cap": "最新卖价已经超过策略允许的最高价格。",
            "fee_adjusted_edge_low": "看似有概率优势，但扣除费用后不够安全。",
            "high_win_rate_token_too_expensive": "高胜率模式不会追接近1的票，因为收益空间太小，输一次很难靠后续补回。",
            "high_win_rate_edge_low": "模型概率减去票价和费用后的安全垫不足；宁可少交易，也不做薄优势单。",
            "high_win_rate_strategy_loss_streak": "同类策略最近连续失败，系统临时暂停这个桶，等待更多好数据。",
            "high_win_rate_recent_winrate_low": "该策略桶近期胜率低于保护线，先进入观察/影子学习，不继续真实加仓。",
            "high_win_rate_model_prob_low": "当前模型概率没有达到高胜率模式的最低门槛；未知策略需要更高概率才允许进入执行。",
            "high_win_rate_stats_promoted": "该策略桶在近期统计中达到目标胜率和Wilson下界，允许进入后续检查。",
            "high_win_rate_model_pass": "当前模型概率、扣费后优势和策略统计没有触发高胜率拦截。",
            "model_book_gap_high": "模型判断和市场盘口差太远，优先保护不硬冲。",
            "binary_book_sanity_failed": "Up 和 Down 的盘口加总异常，通常是做市商撤单或盘口瞬间错位，等待下次刷新。",
            "opposite_orderbook_missing": "反向盘口缺失，无法确认二元市场两边是否正常。",
            "opposite_orderbook_missing_after_check": "反向盘口没有有效 best_ask，二元市场盘口异常，保护跳过。",
            "opposite_spread_high": "反向盘口价差过大，说明市场流动性异常，保护跳过。",
            "risk_block": "风控、余额或认证检查没有完全通过，机器人不会冒险下单。",
            "token_range_override_high_confidence": "模型概率和扣费后优势都足够，允许 0.70-0.92 区间的小额趋势单继续进入执行检查。",
            "recent_order_attempt_lock": "刚才同一市场/窗口已经进入过提交流程，机器人短暂冷却，避免重复连点式提交；失败提交只冷却十几秒。",
            "decision_cycle_coalesced": "同一轮扫描里出现重复判断，系统已合并，避免刷屏和重复下单。",
            "already_traded_window": "这个 5m/15m 窗口已经交易过，机器人不会在同一窗口反复买。",
            "stale_proxy_feed": "BTC/ETH/SOL/XRP 外部价格数据太旧，等行情刷新后再判断。",
            "market_not_found": "没有匹配到对应 Polymarket 市场，可能是窗口切换中。",
            "market_not_tradable": "市场已关闭、未激活或不可交易，不能下单。",
            "market_window_mismatch": "信号对应的时间窗口和找到的市场窗口不同，保护跳过。",
            "chainlink_rule_text_missing": "市场缺少规则文本，无法确认结算条件，保护跳过。",
            "repricing_removed_edge": "重新读取盘口后，原来的优势已经不够。",
            "model_probability_low": "模型概率没有达到最低要求。",
            "token_id_missing": "市场没有返回可交易 token ID，不能安全下单。",
            "asset_risk_block": "该币种的风险限制触发，比如持仓/次数/资金占用限制。",
            "order_validation_failed": "下单前金额、余额或资本隔离校验没有通过。",
            "invalid_bet_amount": "订单金额或可用交易权益为 0，系统不会继续尝试下单。",
            "invalid_share_estimate": "价格或金额异常导致无法估算份额，系统不会继续下单。",
            "risk_module_exception": "风控/资本隔离计算异常，系统已停止本次下单路径。",
            "order_placement_failed": "请求没有被 Polymarket 接收为有效订单；不会产生持仓，会很快允许重试。",
            "order_not_matched": "FOK/立即成交订单发出后没有被吃掉，所以不会留下挂单；短暂冷却后可重试。",
            "order_fill_unconfirmed": "接口返回后没有确认成交，系统按未成交处理，避免假记录。",
            "submit_started": "已经通过所有本地检查，开始调用 Polymarket 下单接口；还不等于成交。",
            "price_unavailable": "外部币价暂时不可用。",
            "insufficient_vol_history": "波动率历史数据还不够，通常刚启动时会出现。",
            "barrier_reclaim_candidate": "先跌后涨买 UP、先涨后跌买 DOWN 的路径机会；仍会经过盘口、滑点、余额和资本隔离检查。",
            "barrier_reclaim_confirm": "价格已经重新回到目标线正确一侧，属于尾盘确认机会；不是盲目追涨杀跌。",
            "barrier_reclaim_custom_gate_pass": "目标线回收不再套普通质量门，会按路径、便宜程度、edge、盘口和$1小单冲击单独评估。",
            "barrier_reclaim_weighted_avg_too_high": "按订单大小估算的实际成交均价超过策略上限，买进去会太贵。",
            "execution_recheck_no_best_ask": "提交前复查盘口时没有 ask，系统不会盲目下单。",
            "profit_rule_engine_error": "利润规则保护层异常时默认拒绝交易，避免在规则失效时裸奔。",
            "barrier_reclaim_edge_low": "虽然路径像目标线回收，但扣费后优势不够，先不买。",
            "barrier_reclaim_book_quality_low": "路径成立，但盘口质量太差，可能买不到合理价格。",
            "barrier_reclaim_price_impact_high": "便宜票盘口太薄，$1买入也会明显推高均价；等待下一次报价。",
            "barrier_reclaim_impact_too_high": "便宜的票流动性差，实际买入价远高于显示价格，已自动放弃。",
            "barrier_reclaim_insufficient_depth": "便宜的票流动性差，实际买入价远高于显示价格，已自动放弃。",
            "barrier_reclaim_model_prob_too_low": "当前目标是不做 50%-60% 的低把握单；目标线回收必须达到 75% 以上才继续。",
            "barrier_reclaim_entry_too_low": "入场价低于 0.10 的票虽然倍率高，但近期命中率太差，已保护跳过。",
            "barrier_reclaim_entry_too_high": "入场价高于 0.50 的票赢了也低于 1x 净利润，不符合当前盈利目标。",
            "barrier_reclaim_model_gap_guard": "影子审计显示模型与盘口分歧超过 0.50 的目标线回收单目前全亏，已保护跳过。",
            "shadow_duplicate_existing": "同一市场窗口已经记录过影子交易，重复信号已忽略，避免污染学习数据。",
            "clob_v2_signature_type_3_signer_guard": "signature_type=3 真实提交暂时被保护关闭，合格机会会进入 Shadow 回测。",
        }
        raw = str(reason or "")
        if raw in hints:
            return hints[raw]
        if raw:
            return f"内部代码：{raw}。这不是报错本身，表示被某个保护规则拦下；需要继续补中文解释。"
        return ""

    def _assets_scan_summary(self, rows: list[dict]) -> str:
        assets = list(getattr(config, "assets_enabled", []) or [])
        if not assets:
            assets = ["BTC", "ETH", "SOL", "XRP"]
        counts = {a: 0 for a in assets}
        for r in rows or []:
            a = str(r.get("asset") or "").upper()
            if a in counts:
                counts[a] += 1
        parts = []
        for a in assets:
            n = counts.get(a, 0)
            parts.append(f"{a}:{n}条")
        return "｜".join(parts)

    def _short_id(self, value: str, keep: int = 6) -> str:
        v = str(value or "").strip()
        if not v:
            return "-"
        if len(v) <= keep * 2 + 3:
            return html.escape(v)
        return html.escape(f"{v[:keep]}...{v[-keep:]}")

    def _json_preview(self, raw, max_len: int = 620) -> str:
        if raw in (None, "", {}):
            return "-"
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
            txt = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            txt = str(raw)
        txt = txt.replace("</", "<\\/")
        if len(txt) > max_len:
            txt = txt[:max_len - 1] + "…"
        return html.escape(txt)

    def _status_label(self, status: str) -> str:
        mapping = {
            "attempted": "已进入执行评估",
            "submitted": "已提交到 Polymarket",
            "matched": "订单已匹配",
            "filled": "已成交",
            "unmatched": "未撮合",
            "failed": "提交失败",
            "cancelled": "已取消",
            "resolved": "已结算",
            "open": "持仓待结算",
        }
        return mapping.get(str(status or ""), str(status or "未知"))

    def _trade_signal_summary(self, trade: dict) -> dict:
        raw = trade.get("signal_data") or "{}"
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _trade_lifecycle_text_one(self, trade: dict, *, compact: bool = False) -> str:
        ts = self._local_time_text(int(trade.get("timestamp") or trade.get("attempting_at") or 0))
        asset = self._safe_text(trade.get("asset") or "BTC", 8)
        timeframe = self._safe_text(trade.get("timeframe") or "5m", 6)
        direction = self._safe_text(trade.get("direction") or "-", 8)
        price = float(trade.get("entry_price") or trade.get("avg_price") or 0)
        status = str(trade.get("status") or "")
        sig = self._trade_signal_summary(trade)
        model = float(trade.get("model_probability") or sig.get("model_probability") or 0)
        edge = float(trade.get("edge_after_fees") or sig.get("edge_after_fees") or 0)
        order_id = trade.get("submitted_order_id") or trade.get("order_id") or ""
        submitted_at = int(trade.get("submitted_at") or 0)
        filled_at = int(trade.get("filled_at") or 0)
        err = str(trade.get("submission_error") or "").strip()
        filled_cost = float(trade.get("filled_cost") or 0)
        filled_shares = float(trade.get("filled_shares") or 0)
        avg_price = float(trade.get("avg_price") or price or 0)

        text = f"<b>{ts}</b> {asset} {timeframe} {direction}"
        if price > 0:
            text += f" @{price:.3f}"
        text += f"\n  1️⃣ 信号产生 ✅"
        if model > 0 or edge != 0:
            text += f" (model {model:.1%}, edge {edge:.1%})"
        text += "\n  2️⃣ 进入执行评估 ✅"
        if status in {"attempted"}:
            text += "\n  3️⃣ 提交订单：未发生（仅进入执行评估）"
            text += "\n     说明：还没有拿到 Polymarket order_id，不能算真正下单。"
        elif status == "failed":
            text += f"\n  3️⃣ 提交订单 ❌"
            if err:
                text += f"\n     失败: <code>{self._safe_text(err, 160)}</code>"
            text += "\n  4️⃣ 订单状态: 未发生"
            text += "\n  5️⃣ 实际成交 ❌"
        elif status in {"submitted", "matched", "filled", "unmatched", "cancelled", "resolved", "open"}:
            text += "\n  3️⃣ 提交订单 ✅"
            if submitted_at:
                text += f" ({self._local_time_text(submitted_at)}"
                if order_id:
                    text += f", order_id {self._short_id(order_id)}"
                text += ")"
            elif order_id:
                text += f" (order_id {self._short_id(order_id)})"
            if status in {"unmatched"}:
                text += "\n  4️⃣ 订单状态: 未撮合 ⚠️"
                text += "\n  5️⃣ 实际成交 ❌"
            elif status in {"matched", "submitted", "open"}:
                text += f"\n  4️⃣ 订单状态: {self._status_label(status)} ✅"
                text += "\n  5️⃣ 实际成交: 等待确认"
            else:
                text += f"\n  4️⃣ 订单状态: {self._status_label(status)} ✅"
                if filled_cost > 0 and filled_shares > 0:
                    text += f"\n  5️⃣ 实际成交 ✅"
                    if filled_at:
                        text += f" ({self._local_time_text(filled_at)}"
                    else:
                        text += " ("
                    text += f", ${filled_cost:.2f} / {filled_shares:.4f} shares @ {avg_price:.3f})"
                else:
                    text += "\n  5️⃣ 实际成交: 等待确认"
        else:
            text += f"\n  3️⃣ 当前状态: {self._safe_text(self._status_label(status), 40)}"
        return text

    async def _show_trade_lifecycle(self, q):
        try:
            trades = self.db.get_recent_trade_lifecycles(limit=20)
        except Exception as e:
            await q.edit_message_text(f"📋 订单生命周期读取失败: <code>{html.escape(str(e))}</code>", reply_markup=self.main_menu_keyboard(), parse_mode="HTML")
            return
        text = "<b>📋 订单生命周期</b>（最近20笔）\n\n"
        if not trades:
            text += "暂无订单生命周期记录。只有通过质量门并进入执行评估后，才会在这里出现。"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="trade_lifecycle")], [InlineKeyboardButton("⬅️ 返回", callback_data="main")]])
        else:
            rows = []
            for t in trades[:8]:
                text += self._trade_lifecycle_text_one(t) + "\n\n"
                rows.append([InlineKeyboardButton(f"详情 #{t.get('id')}", callback_data=f"trade_detail:{t.get('id')}")])
                if len(text) > 3200:
                    text += "…内容较多，仅显示最近部分。"
                    break
            rows.append([InlineKeyboardButton("🔄 刷新", callback_data="trade_lifecycle"), InlineKeyboardButton("🔬 提交详情", callback_data="order_submission_details")])
            rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="main")])
            kb = InlineKeyboardMarkup(rows)
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _show_trade_detail(self, q, raw_trade_id: str):
        try:
            trade_id = int(raw_trade_id)
            trade = self.db.get_trade_by_id(trade_id)
        except Exception:
            trade = None
        if not trade:
            await q.edit_message_text("⛔ 找不到这笔订单记录。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="trade_lifecycle")]]), parse_mode="HTML")
            return
        events = []
        try:
            events = self.db.get_execution_events_for_trade(trade, limit=12)
        except Exception:
            events = []
        text = f"<b>📋 订单详情 #{trade.get('id')}</b>\n\n"
        text += self._trade_lifecycle_text_one(trade) + "\n\n"
        text += "<b>signal_data</b>\n<pre>" + self._json_preview(trade.get("signal_data"), 720) + "</pre>\n"
        text += "<b>submission_response_json</b>\n<pre>" + self._json_preview(trade.get("submission_response_json"), 720) + "</pre>\n"
        if trade.get("submission_error"):
            text += "<b>submission_error</b>\n<pre>" + self._json_preview(trade.get("submission_error"), 360) + "</pre>\n"
        text += "<b>first_fill_response_json</b>\n<pre>" + self._json_preview(trade.get("first_fill_response_json"), 520) + "</pre>\n"
        if events:
            text += "<b>execution_events</b>\n"
            for ev in events[:8]:
                text += f"• {self._local_time_text(int(ev.get('timestamp') or 0))} {self._safe_text(ev.get('stage'), 24)} / {self._safe_text(ev.get('reason'), 40)}\n"
        else:
            text += "<b>execution_events</b>\n无匹配事件。\n"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回生命周期", callback_data="trade_lifecycle")]])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _show_order_submission_details(self, q):
        try:
            trades = self.db.get_recent_order_submission_details(limit=5)
        except Exception as e:
            await q.edit_message_text(f"🔬 订单提交详情读取失败: <code>{html.escape(str(e))}</code>", reply_markup=self.settings_keyboard(), parse_mode="HTML")
            return
        text = "<b>🔬 最近5次订单提交</b>\n\n"
        if not trades:
            text += "暂无提交记录。通过质量门并进入下单路径后，这里会显示 post_order 的结果。"
        else:
            for i, t in enumerate(trades, 1):
                ts = self._local_time_text(int(t.get("submitted_at") or t.get("attempting_at") or t.get("timestamp") or 0))
                asset = self._safe_text(t.get("asset") or "BTC", 8)
                timeframe = self._safe_text(t.get("timeframe") or "5m", 6)
                direction = self._safe_text(t.get("direction") or "-", 8)
                token_id = self._short_id(t.get("token_id") or "", 8)
                amount = float(t.get("size") or t.get("cost") or 0)
                status = str(t.get("status") or "")
                order_id = t.get("submitted_order_id") or t.get("order_id") or ""
                err = str(t.get("submission_error") or "").strip()
                text += f"[{i}] <b>{ts}</b> {asset} {timeframe} {direction}\n"
                text += f"  请求: tokenID={token_id}, side=BUY, type={html.escape(config.ORDER_TYPE)}, amount=${amount:.2f}\n"
                if status == "failed" or err:
                    text += "  响应: success=False\n"
                    text += f"  错误: <code>{self._safe_text(err or 'unknown', 160)}</code>\n"
                elif not order_id and status in {"attempted", "submitting", ""}:
                    text += "  响应: 未提交到 Polymarket，orderID=-\n"
                    text += "  说明: 这只是进入执行评估，不代表已下单。\n"
                else:
                    text += f"  响应: success=True, orderID={self._short_id(order_id)}, status={self._safe_text(t.get('order_status') or status, 24)}\n"
                text += f"  生命周期: {self._safe_text(self._status_label(status), 24)}\n\n"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 刷新", callback_data="order_submission_details")], [InlineKeyboardButton("⬅️ 返回系统健康", callback_data="system_health_menu"), InlineKeyboardButton("🔧 高级选项", callback_data="advanced_menu")]])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    def _is_low_value_observe(self, row: dict) -> bool:
        action = str(row.get("action") or "")
        reason = str(row.get("reason") or "")
        return action == "observe" and reason in {
            "strategy_filters_no_match",
            "outside_strategy_time_window",
            "insufficient_window_klines",
            "market_not_ready",
            "",
        }


    async def _show_recent_decisions(self, q):
        """Telegram view: concise opportunity stream without raw observe noise."""
        since = int(time.time()) - 60 * 60
        try:
            rows = self.db.get_recent_decisions(limit=120, since_ts=since)
        except Exception as e:
            await q.edit_message_text(
                f"📡 实时机会读取失败: <code>{html.escape(str(e))}</code>",
                reply_markup=self.main_menu_keyboard(), parse_mode="HTML"
            )
            return

        auth_ready = bool(config.has_polymarket_creds and self.polymarket and self.polymarket.connected)
        text = "<b>📡 实时机会 / 最近判断</b>\n"
        text += "<code>显示最近1小时候选、执行拦截、下单尝试和成交；普通观察会合并。</code>\n\n"

        if not rows:
            text += "暂无记录。可能原因：刚启动、日志关闭、当前未进入交易评估窗口，或市场数据还没准备好。"
        else:
            total = len(rows)
            low_observe = [r for r in rows if self._is_low_value_observe(r)]
            auth_reasons = {"risk_block", "credentials_not_configured", "client_not_authenticated", "balance_unavailable"}
            auth_skip = [r for r in rows if str(r.get("action") or "") == "skip" and str(r.get("reason") or "") in auth_reasons]
            noisy_skip_reasons = {"missing_price_to_beat", "risk_block", "credentials_not_configured", "client_not_authenticated", "balance_unavailable"}

            def is_high_value(r):
                action = str(r.get("action") or "")
                reason = str(r.get("reason") or "")
                if action in {"candidate", "order_attempt", "order_matched", "order_rejected"}:
                    return True
                if action == "skip" and reason not in noisy_skip_reasons:
                    return True
                return False

            valuable = [r for r in rows if is_high_value(r)]
            text += f"最近1小时读取: <b>{total}</b> 条\n"
            text += f"扫描币种: <code>{html.escape(','.join(config.assets_enabled))}</code>｜周期: <code>{html.escape(','.join(config.timeframes_enabled))}</code>\n"
            text += f"各币种记录: <code>{self._assets_scan_summary(rows)}</code>\n"
            text += f"普通观察已合并: <b>{len(low_observe)}</b> 条\n"
            if auth_skip:
                text += f"认证/风控拦截已合并: <b>{len(auth_skip)}</b> 条\n"
            skipped_other = len([r for r in rows if str(r.get("action") or "") == "skip"]) - len(auth_skip)
            text += f"候选/执行记录: <b>{len(valuable)}</b> 条｜其他跳过: <b>{max(0, skipped_other)}</b> 条\n\n"

            if not auth_ready:
                text += "🔐 <b>当前重点</b>\n"
                text += "交易密钥未配置或未认证。当前只观察行情，不把认证拦截当成策略问题，也不会下单。\n"
                text += "下一步：设置 → 设置交易密钥 → 填私钥、Funder、签名类型3 → 重新连接认证。\n\n"

            if not valuable:
                text += "暂无真正候选、下单尝试或成交。\n"
                text += "如果这里只常见 BTC，并不代表 ETH/SOL/XRP 没运行；其它币种的普通观察已合并到上方各币种记录。"
            else:
                shown = 0
                for r in valuable[:12]:
                    ts = self._local_time_text(int(r.get("timestamp") or 0))
                    asset = self._safe_text(r.get("asset", "BTC"), 8)
                    timeframe = self._safe_text(r.get("timeframe", ""), 6)
                    direction = self._safe_text(r.get("direction", "-"), 8) or "-"
                    action_raw = str(r.get("action") or "")
                    action = self._safe_text(self._display_action(action_raw), 18)
                    reason_raw = str(r.get("reason") or "")
                    reason = self._safe_text(self._display_reason(reason_raw), 42)
                    strategy_name = self._decision_strategy_name(r)
                    strategy_part = f"｜策略: {self._safe_text(strategy_name, 24)}" if strategy_name else ""
                    token_price = float(r.get("token_price") or 0)
                    prob = float(r.get("model_probability") or 0)
                    edge = float(r.get("edge_after_fees") or 0)

                    if action_raw == "order_matched":
                        icon = "✅"
                    elif action_raw in {"candidate", "order_attempt"}:
                        icon = "🎯"
                    elif action_raw == "skip":
                        icon = "⏭️"
                    else:
                        icon = "👁️"
                    text += f"{icon} <b>{ts}</b> {asset} {timeframe} {direction}\n"
                    text += f"  动作: <code>{action}</code>｜原因: <b>{reason}</b>{strategy_part}\n"
                    extra = []
                    if token_price > 0:
                        extra.append(f"价格: ${token_price:.3f}")
                    if prob > 0:
                        extra.append(f"概率: {prob:.1%}")
                    if edge != 0:
                        extra.append(f"edge: {edge:.1%}")
                    if extra:
                        text += "  " + "｜".join(extra) + "\n"
                    hint = self._reason_hint(reason_raw)
                    if hint:
                        text += f"  说明: {html.escape(hint)}\n"
                    text += "\n"
                    shown += 1
                    if len(text) > 3600:
                        text += "…内容较多，仅显示最近部分。"
                        break
                if shown == 0:
                    text += "暂无需要展示的有效机会。"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="recent_decisions"), InlineKeyboardButton("🚫 跳过原因", callback_data="skip_reasons")],
            self._run_pause_buttons("recent_decisions"),
            [InlineKeyboardButton("⬅️ 返回跳过分析", callback_data="skip_analysis_menu")],
        ])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _show_skip_reasons(self, q):
        """Telegram view: aggregated skip reasons so user needn't inspect sqlite on server."""
        since = int(time.time()) - 60 * 60
        try:
            raw_counts = self.db.get_decision_reason_counts(since_ts=since, action="skip", limit=20)
            recent = self.db.get_recent_decisions(limit=12, since_ts=since, action="skip")
        except Exception as e:
            await q.edit_message_text(
                f"🚫 跳过原因读取失败: <code>{html.escape(str(e))}</code>",
                reply_markup=self.main_menu_keyboard(), parse_mode="HTML"
            )
            return

        text = "<b>🚫 最近1小时跳过原因</b>\n"
        if not config.has_polymarket_creds or not (self.polymarket and self.polymarket.connected):
            text += "⚠️ 交易密钥未配置/未认证时，认证类拦截属于保护行为。\n"
        text += "\n"
        if not raw_counts:
            text += "暂无跳过记录。可能原因：刚启动、日志关闭、没有进入评估窗口，或当前没有候选信号。\n"
        else:
            # Split normal strategy skips from shadow de-dup noise and stale auth history.
            reason_counts = {}
            for r in raw_counts:
                raw = str(r.get("reason") or "unknown")
                reason_counts[raw] = reason_counts.get(raw, 0) + int(r.get("n") or 0)
            shadow_dups = int(reason_counts.pop("shadow_duplicate_existing", 0) or 0)
            auth_history = 0
            if config.has_polymarket_creds and self.polymarket and self.polymarket.connected:
                for auth_raw in ("credentials_not_configured", "client_not_authenticated", "balance_unavailable"):
                    auth_history += int(reason_counts.pop(auth_raw, 0) or 0)
            total = sum(reason_counts.values())
            text += f"策略跳过: <b>{total}</b> 次\n"
            if shadow_dups:
                text += f"影子重复忽略: <b>{shadow_dups}</b> 次（正常去重，不算策略失败）\n"
            if auth_history:
                text += f"历史认证类拦截: <b>{auth_history}</b> 次（当前认证已恢复）\n"
            text += "\n"
            for raw, n in sorted(reason_counts.items(), key=lambda kv: -kv[1])[:10]:
                pct = (n / total * 100) if total else 0
                label = self._display_reason(raw)
                text += f"• <b>{self._safe_text(label, 48)}</b>: <b>{n}</b> 次 ({pct:.0f}%)\n"
                hint = self._reason_hint(raw)
                if hint:
                    text += f"  └ {html.escape(hint)}\n"

        text += "\n<b>最近明细</b>\n"
        if not recent:
            text += "无。"
        else:
            for r in recent:
                ts = self._local_time_text(int(r.get("timestamp") or 0))
                asset = self._safe_text(r.get("asset", "BTC"), 8)
                timeframe = self._safe_text(r.get("timeframe", ""), 6)
                strategy_name = self._decision_strategy_name(r)
                strategy_part = f" {self._safe_text(strategy_name, 22)}" if strategy_name else ""
                direction = self._safe_text(r.get("direction", "-"), 8) or "-"
                reason_raw = str(r.get("reason") or "")
                reason = self._safe_text(self._display_reason(reason_raw), 44)
                price = float(r.get("token_price") or 0)
                prob = float(r.get("model_probability") or 0)
                text += f"⏭ <b>{ts}</b> {asset} {timeframe}{strategy_part} {direction} — <b>{reason}</b>"
                if price > 0:
                    text += f" | ${price:.3f} / {prob:.0%}"
                elif prob > 0:
                    text += f" | 价格 N/A / {prob:.0%}"
                elif reason_raw == "missing_price_to_beat":
                    text += " | 价格 N/A"
                text += "\n"
                if len(text) > 3600:
                    text += "…内容较多，仅显示最近部分。"
                    break

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="skip_reasons"), InlineKeyboardButton("📡 实时机会", callback_data="recent_decisions")],
            [InlineKeyboardButton("▶️ 启动", callback_data="run"), InlineKeyboardButton("⏸ 暂停", callback_data="pause")],
            [InlineKeyboardButton("⬅️ 返回跳过分析", callback_data="skip_analysis_menu")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    async def _show_quality_funnel(self, q):
        import time, json
        since = int(time.time()) - 3600
        try:
            funnel = self.db.get_quality_gate_funnel(since_ts=since)
            passes = self.db.get_recent_quality_passes(limit=5, since_ts=since)
        except Exception as e:
            text = f"⚠️ 读取筛选漏斗失败：<code>{e}</code>"
            await q.edit_message_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")
            return
        text = "<b>🧪 筛选漏斗｜最近1小时</b>\n\n"
        text += f"全部判断: <b>{funnel.get('total_decisions', 0)}</b>\n"
        text += f"质量门通过: <b>{funnel.get('gate_passed', 0)}</b>\n"
        text += f"质量门拒绝: <b>{funnel.get('gate_rejected', 0)}</b>\n"
        text += f"实际开仓: <b>{funnel.get('trades', 0)}</b>\n\n"
        text += "<b>主要筛掉原因</b>\n"
        shown = 0
        for r in funnel.get('rows', [])[:10]:
            reason = str(r.get('reason') or r.get('action') or '')[:48]
            if not reason:
                continue
            text += f"• {self._display_reason(reason)} <code>{html.escape(reason)}</code>: {int(r.get('n') or 0)}\n"
            shown += 1
        if shown == 0:
            text += "<code>暂无质量门记录；刚启动或还没进入机会评估。</code>\n"
        text += "\n<b>最近通过质量门</b>\n"
        if not passes:
            text += "<code>暂无。</code>\n"
        for r in passes:
            details = {}
            try:
                details = json.loads(r.get('details_json') or '{}')
            except Exception:
                pass
            raw_score = details.get('score') if isinstance(details, dict) else None
            if isinstance(raw_score, dict):
                score = raw_score.get('final_score') or raw_score.get('score') or 0
            elif isinstance(raw_score, (int, float)):
                score = float(raw_score)
            else:
                score = 0
            threshold = details.get('threshold', '') if isinstance(details, dict) else ''
            text += f"• {r.get('asset','')} {r.get('timeframe','')} {r.get('direction','')} <code>{r.get('action')}</code> score={score} / 门槛={threshold}\n"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="quality_funnel"), InlineKeyboardButton("🎚 当前门槛", callback_data="quality_gate")],
            [InlineKeyboardButton("⬅️ 返回跳过分析", callback_data="skip_analysis_menu")],
        ])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _show_quality_gate(self, q):
        try:
            from adaptive_quality_gate import AdaptiveQualityGate
            gate = AdaptiveQualityGate(self.db).current_threshold()
        except Exception as e:
            text = f"⚠️ 读取动态门槛失败：<code>{e}</code>"
            await q.edit_message_text(text, reply_markup=self.main_menu_keyboard(), parse_mode="HTML")
            return
        text = "<b>🎚 当前动态质量门槛</b>\n\n"
        text += f"当前门槛: <b>{gate.get('threshold')}</b>\n"
        text += f"今日目标进度: <b>{gate.get('target_so_far')}</b> / {config.effective_max_trades_per_day}\n"
        text += f"今日已交易: <b>{gate.get('trades_so_far')}</b>\n"
        text += f"交易节奏: <b>{gate.get('pace_ratio')}</b>\n"
        text += f"今日已实现PnL: <b>${gate.get('pnl_today')}</b>\n"
        text += f"连续亏损: <b>{gate.get('consecutive_losses')}</b>\n"
        raw_flow = gate.get('flow') if isinstance(gate, dict) else None
        flow = raw_flow if isinstance(raw_flow, dict) else {}
        if flow:
            text += "\n<b>实时机会流</b>\n"
            text += f"近{int((flow.get('lookback_sec') or 0)/60)}分钟候选: <b>{flow.get('recent_decisions', 0)}</b> ｜ 每分钟: <b>{flow.get('candidates_per_min', 0)}</b>\n"
            text += f"通过率: <b>{flow.get('quality_pass_ratio', 0)}</b> ｜ 流量加严: <b>+{flow.get('penalty', 0)}</b>\n"
        text += "\n"
        pace = float(gate.get('pace_ratio') or 0)
        pnl = float(gate.get('pnl_today') or 0)
        flow_penalty = float(flow.get('penalty') or 0)
        if flow_penalty > 0 or pace > 1.25 or pnl < 0:
            text += "状态: <b>收紧</b>｜机会流拥挤、交易偏快或今日表现偏弱。\n"
        elif pace < 0.55:
            text += "状态: <b>略放宽</b>｜交易偏少，但仍需通过质量门。\n"
        else:
            text += "状态: <b>正常</b>｜按当前门槛筛选。\n"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="quality_gate"), InlineKeyboardButton("🧪 筛选漏斗", callback_data="quality_funnel")],
            [InlineKeyboardButton("⬅️ 返回跳过分析", callback_data="skip_analysis_menu")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


    async def _show_product_doctor(self, q, notice: str | None = None):
        """Concise product doctor page; never expose raw diagnostic codes."""
        try:
            from product_doctor import ProductDoctor
            market_ws = getattr(self.polymarket, "market_ws", None)
            doctor = ProductDoctor(self.db, market_ws=market_ws)
            text = doctor.render_text()
        except Exception as e:
            text = f"⚠️ 产品医生读取失败：<code>{html.escape(str(e))}</code>"
        header = ""
        if notice:
            header += f"<b>{html.escape(notice)}</b>\n"
        header += (
            f"机器人交易：<b>{self._runtime_state_label()}</b>｜"
            f"认证：<b>{'已配置' if config.has_polymarket_creds else '未配置'}</b>\n\n"
        )
        text = header + text
        # Hard safety: if old/raw issue codes somehow leak, replace with a safe summary.
        raw_markers = ("candidate_stream_crowded", "quality_gate_tight", "skip_reason_cluster", "证据:", "建议:")
        if any(m in text for m in raw_markers):
            auth_ready = bool(config.has_polymarket_creds and self.polymarket and self.polymarket.connected)
            text = header + "<b>🩺 产品医生</b>\n诊断：<b>提示</b>｜窗口：60分钟\n盘口：<b>REST 盘口轮询</b>\nWS状态：未启用，不是故障。\n\n"
            if not auth_ready:
                text += "🔐 <b>当前重点</b>\n交易密钥还没完整认证。当前只观察行情，不判断策略好坏，也不会下单。\n下一步：设置 → 设置交易密钥，填私钥、Funder、签名类型3，然后重新连接认证。"
            else:
                text += "✅ <b>结论</b>\n暂时没有必须手动处理的问题。详细样本请看“实时机会”和“跳过原因”。"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="product_doctor"), InlineKeyboardButton("📡 WS健康", callback_data="ws_health")],
            self._run_pause_buttons("product_doctor"),
            [InlineKeyboardButton("⬅️ 返回系统健康", callback_data="system_health_menu")],
        ])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _show_ws_health(self, q, notice: str | None = None):
        try:
            market_ws = getattr(self.polymarket, "market_ws", None)
            stats = dict(getattr(market_ws, "stats", {}) or {}) if market_ws else {
                "enabled": config.REALTIME_ORDERBOOK_ENABLED,
                "status": "not_initialized" if config.REALTIME_ORDERBOOK_ENABLED else "disabled",
            }
            text = "<b>📡 Polymarket WS 健康</b>\n\n"
            if notice:
                text += f"<b>{html.escape(notice)}</b>\n"
            text += f"机器人交易: <b>{self._runtime_state_label()}</b>\n"
            enabled = bool(stats.get("enabled", config.REALTIME_ORDERBOOK_ENABLED))
            if not enabled:
                text += "WS启用: <b>否</b>\n"
                text += "WS状态: <b>未启用</b>\n"
                text += "盘口来源: <b>REST 轮询</b>\n"
                text += "说明: 这里的未启用只表示 WS 盘口关闭，不代表机器人停止。下方按钮只控制交易循环。\n"
            else:
                text += "启用: <b>是</b>\n"
                text += f"连接: <b>{bool(stats.get('connected', False))}</b>\n"
                text += f"订阅资产: <b>{stats.get('subscribed_assets', 0)}</b>\n"
                text += f"活跃资产: <b>{stats.get('active_assets', 0)}</b>\n"
                text += f"消息数: <b>{stats.get('messages_received', 0)}</b>\n"
                text += f"重连: <b>{stats.get('reconnects', 0)}</b>\n"
                age = stats.get('last_message_age_sec')
                text += f"最近消息年龄: <b>{float(age):.2f}s</b>\n" if age is not None else "最近消息年龄: <b>暂无</b>\n"
                if stats.get('last_error'):
                    text += f"错误: <code>{self._safe_text(stats.get('last_error'), 240)}</code>\n"
        except Exception as e:
            text = f"⚠️ WS健康读取失败：<code>{html.escape(str(e))}</code>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="ws_health"), InlineKeyboardButton("🩺 产品医生", callback_data="product_doctor")],
            self._run_pause_buttons("ws_health"),
            [InlineKeyboardButton("⬅️ 返回系统健康", callback_data="system_health_menu")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    async def _show_bet_size_picker(self, q):
        current = float(config.effective_per_order_amount or 0.0)
        text = "<b>💵 设置每笔最大投注金额</b>\n\n"
        text += f"当前每笔最大投注金额: <b>${current:.2f}</b>\n"
        text += "这是单笔订单的最大美元金额，设置后立即生效。\n\n"
        text += "选择一个金额，或点自定义输入："
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("$1", callback_data="set_bet_size_value:1"),
                InlineKeyboardButton("$2", callback_data="set_bet_size_value:2"),
                InlineKeyboardButton("$5", callback_data="set_bet_size_value:5"),
            ],
            [
                InlineKeyboardButton("$10", callback_data="set_bet_size_value:10"),
                InlineKeyboardButton("$20", callback_data="set_bet_size_value:20"),
                InlineKeyboardButton("$50", callback_data="set_bet_size_value:50"),
            ],
            [InlineKeyboardButton("✍️ 自定义输入", callback_data="set_bet_size_custom")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="settings")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    async def _action_set_bet_size_value(self, q, raw_value: str):
        try:
            value = max(0.0, float(raw_value))
        except Exception:
            value = 0.0
        if value <= 0:
            await q.edit_message_text("⛔ 每笔金额必须大于 0。", reply_markup=self.settings_keyboard(), parse_mode="HTML")
            return
        self.db.set_state("test_bet_size", value)
        await q.edit_message_text(
            f"✅ 已设置每笔最大投注金额为 <b>${value:.2f}</b>。\n\n设置立即生效，不需要重启。",
            reply_markup=self.settings_keyboard(),
            parse_mode="HTML",
        )

    async def _show_authorized_capital_picker(self, q):
        current = float(config.effective_authorized_capital_usd or 0.0)
        text = "<b>💰 设置可用本金</b>\n\n"
        if current > 0:
            text += f"当前可用本金上限: <b>${current:.2f}</b>\n"
            text += "资本隔离已启用：钱包中保护储备不会参与交易，盈利不封顶。\n"
        else:
            text += "当前状态: <b>资本隔离关闭</b>\n"
            text += "关闭时，整个可用钱包余额都会参与风控计算。\n"
        text += "\n选择一个新的可用本金上限："
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("$20", callback_data="set_authorized_capital_value:20"),
                InlineKeyboardButton("$50", callback_data="set_authorized_capital_value:50"),
            ],
            [
                InlineKeyboardButton("$100", callback_data="set_authorized_capital_value:100"),
                InlineKeyboardButton("$200", callback_data="set_authorized_capital_value:200"),
            ],
            [
                InlineKeyboardButton("$500", callback_data="set_authorized_capital_value:500"),
                InlineKeyboardButton("关闭隔离", callback_data="set_authorized_capital_value:0"),
            ],
            [InlineKeyboardButton("✍️ 自定义输入", callback_data="set_authorized_capital_custom")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="settings")],
        ])
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    async def _action_set_authorized_capital_value(self, q, raw_value: str):
        try:
            value = max(0.0, float(raw_value))
        except Exception:
            value = 0.0

        # New writes use the canonical runtime key.  Also clear the cached reserve
        # anchor so the next real balance read re-anchors protection at the new cap.
        self.db.set_state("authorized_capital_usd", value)
        self.db.set_state("capital_isolation_reserved_balance", None)
        self.db.set_state("capital_isolation_capital_limit_usd", 0.0)
        self.db.set_state("capital_isolation_initial_balance", None)

        if value > 0:
            text = (
                f"✅ 已设置可用本金上限为 <b>${value:.2f}</b>\n\n"
                "保护储备会在下次余额读取时重新锚定。盈利不封顶，未授权余额不会参与交易。"
            )
        else:
            text = "⚠️ 资本隔离已关闭，整个钱包余额都可被交易。"

        await q.edit_message_text(text, reply_markup=self.settings_keyboard(), parse_mode="HTML")

    # ============ Telegram trade key controls ============

    def _mask_secret(self, value: str, keep: int = 6) -> str:
        v = str(value or "").strip()
        if not v:
            return "未设置"
        if len(v) <= keep * 2:
            return "***"
        return f"{v[:keep]}...{v[-keep:]}"

    def _trade_keys_status_line(self) -> str:
        pk = config.effective_polymarket_private_key
        funder = config.effective_polymarket_funder
        sig = config.effective_polymarket_signature_type
        if config.has_polymarket_creds:
            return f"已设置 signer {self._mask_secret(pk, 6)} / funder {self._mask_secret(funder, 6)} / sig={sig}"
        missing = []
        if not pk:
            missing.append("私钥")
        if not funder:
            missing.append("Funder")
        return "未完整设置（缺少 " + ", ".join(missing or ["未知"]) + "）"

    def _trade_keys_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 填写私钥", callback_data="trade_key_set:private_key")],
            [InlineKeyboardButton("🏦 填写 Funder 地址", callback_data="trade_key_set:funder")],
            [InlineKeyboardButton("签名类型 1", callback_data="trade_key_sig:1"), InlineKeyboardButton("签名类型 2", callback_data="trade_key_sig:2"), InlineKeyboardButton("签名类型 3", callback_data="trade_key_sig:3")],
            [InlineKeyboardButton("🔌 重新连接认证", callback_data="trade_keys_reconnect")],
            [InlineKeyboardButton("🧹 清空交易密钥", callback_data="trade_keys_clear")],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ])

    async def _show_trade_keys(self, q):
        text = "<b>🔐 交易密钥管理</b>\n\n"
        text += "用途：让你在 TG 私聊里修改 Polymarket 交易配置，不需要手动改 .env。\n\n"
        text += f"当前状态: <b>{html.escape(self._trade_keys_status_line())}</b>\n"
        text += f"客户端认证: <b>{'已连接' if self.polymarket and self.polymarket.connected else '未认证/公开模式'}</b>\n"
        if config.has_polymarket_creds and not (self.polymarket and self.polymarket.connected):
            text += "提示: 已保存密钥，但需要点击“重新连接认证”后才会读取余额/下单。\n"
        text += "\n"
        text += "可设置：\n"
        text += "• 私钥：只用于本地服务器连接 CLOB，不会在 TG 回显\n"
        text += "• Funder 地址：Polymarket Deposit/Relayer 钱包地址\n"
        text += "• 签名类型：按你的官方配置选择，一般保持当前值即可\n\n"
        text += "⚠️ 建议只在你自己的私聊里粘贴。机器人会尝试删除你发来的明文消息，但 Telegram 客户端/服务端历史无法由程序完全保证清除。"
        await q.edit_message_text(text, reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")

    async def _action_trade_key_set_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE, field: str):
        q = update.callback_query
        if field not in {"private_key", "funder"}:
            await q.edit_message_text("⛔ 未知字段。", reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")
            return
        context.user_data["awaiting_trade_key_field"] = field
        # Track the prompt so we can delete it after a value is saved. Without this,
        # old TG bubbles keep a stale “取消” button and look like the bot is still waiting.
        try:
            context.user_data["trade_key_prompt_chat_id"] = q.message.chat_id
            context.user_data["trade_key_prompt_message_id"] = q.message.message_id
        except Exception:
            pass
        label = "Polymarket 私钥" if field == "private_key" else "Funder 钱包地址"
        hint = "0x 开头的 64 字节私钥" if field == "private_key" else "0x 开头的钱包地址"
        text = (
            f"请输入 <b>{label}</b>。\n\n"
            f"格式：<code>{hint}</code>\n"
            "发送后机器人会保存到本地 SQLite，并尝试删除你的明文消息；不会在界面回显完整内容。"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="trade_keys")]]), parse_mode="HTML")

    async def _action_runtime_custom_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str, hint: str):
        q = update.callback_query
        context.user_data["awaiting_runtime_field"] = field
        try:
            context.user_data["runtime_prompt_chat_id"] = q.message.chat_id
            context.user_data["runtime_prompt_message_id"] = q.message.message_id
        except Exception:
            pass
        text = (
            f"请输入新的 <b>{label}</b>。\n\n"
            f"格式：<code>{hint}</code>\n"
            "发送后会保存到本地 SQLite，并立即生效。"
        )
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="settings")]]), parse_mode="HTML")

    def _save_runtime_field(self, field: str, raw: str) -> tuple[bool, str]:
        try:
            value = float(str(raw or "").strip())
        except Exception:
            return False, "请输入数字，例如 1、2.5、50。"
        if field == "test_bet_size":
            if value <= 0:
                return False, "每笔金额必须大于 0。"
            self.db.set_state("test_bet_size", value)
            return True, f"已设置每笔最大投注金额为 <b>${value:.2f}</b>。"
        if field == "authorized_capital_usd":
            value = max(0.0, value)
            self.db.set_state("authorized_capital_usd", value)
            self.db.set_state("capital_isolation_reserved_balance", None)
            self.db.set_state("capital_isolation_capital_limit_usd", 0.0)
            self.db.set_state("capital_isolation_initial_balance", None)
            if value > 0:
                return True, f"已设置可用本金上限为 <b>${value:.2f}</b>。保护储备会在下次余额读取时重新锚定。"
            return True, "资本隔离已关闭，整个钱包余额都可被交易。"
        return False, "未知设置字段。"

    def _save_trade_key_field(self, field: str, raw: str) -> tuple[bool, str]:
        value = str(raw or "").strip()
        if field == "private_key":
            if not (value.startswith("0x") and len(value) == 66):
                return False, "私钥格式不对：应为 0x 开头、总长度 66。"
            self.db.set_state("polymarket_private_key", value)
            # Signer changed; cached API creds may no longer match.
            if hasattr(self.db, "clear_api_creds"):
                self.db.clear_api_creds()
            return True, f"私钥已保存：<code>{html.escape(self._mask_secret(value, 6))}</code>"
        if field == "funder":
            if not (value.startswith("0x") and len(value) == 42):
                return False, "Funder 地址格式不对：应为 0x 开头、总长度 42。"
            self.db.set_state("polymarket_funder", value)
            if hasattr(self.db, "clear_api_creds"):
                self.db.clear_api_creds()
            return True, f"Funder 已保存：<code>{html.escape(self._mask_secret(value, 6))}</code>"
        return False, "未知字段。"

    async def _action_trade_key_set_signature(self, q, raw_value: str):
        try:
            value = int(raw_value)
        except Exception:
            value = int(config.effective_polymarket_signature_type)
        if value not in {0, 1, 2, 3}:
            value = 1
        self.db.set_state("polymarket_signature_type", value)
        if hasattr(self.db, "clear_api_creds"):
            self.db.clear_api_creds()
        await q.edit_message_text(
            f"✅ 签名类型已设置为 <b>{value}</b>。如已填写私钥和 Funder，可点“重新连接认证”测试。",
            reply_markup=self._trade_keys_keyboard(),
            parse_mode="HTML",
        )

    async def _action_trade_keys_clear(self, q):
        for key in ("polymarket_private_key", "POLYMARKET_PRIVATE_KEY", "runtime_polymarket_private_key", "polymarket_funder", "POLYMARKET_FUNDER", "runtime_polymarket_funder"):
            self.db.set_state(key, None)
        if hasattr(self.db, "clear_api_creds"):
            self.db.clear_api_creds()
        if self.polymarket:
            self.polymarket.connected = False
        await q.edit_message_text("🧹 已清空 TG/SQLite 中的交易密钥。当前处于未认证状态，不会真实下单。", reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")

    async def _action_trade_keys_reconnect(self, q):
        if not self.polymarket:
            await q.edit_message_text("⛔ Polymarket 客户端未初始化。", reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")
            return
        if not config.has_polymarket_creds:
            await q.edit_message_text("⛔ 私钥或 Funder 未完整设置。", reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")
            return
        try:
            await asyncio.to_thread(self.polymarket.connect)
            if config.real_orders_enabled:
                text = "✅ Polymarket 认证连接成功。\n\n当前真实下单已开启；仍会经过风控、Profit Rule、盘口/滑点检查后才允许提交。你可以点余额测试读取。"
            else:
                text = "✅ Polymarket 认证连接成功。\n\n当前真实下单未武装：只允许余额读取、行情检查、影子学习和演练，不会提交真实订单。"
        except Exception as e:
            text = f"⛔ Polymarket 认证失败：<code>{html.escape(str(e))}</code>\n\n密钥已保存，但当前不能真实下单。"
        await q.edit_message_text(text, reply_markup=self._trade_keys_keyboard(), parse_mode="HTML")

    async def _action_reset_daily(self, q):
        text = "⚠️ 重置每日统计会从当前时间重新计算今日面板，不删除历史交易记录。\n\n点击下方按钮确认或取消。"
        kb = [[
            InlineKeyboardButton("✅ 确认重置", callback_data="reset_daily_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data="advanced_menu"),
        ]]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    async def _action_reset_daily_confirm(self, q):
        self.db.reset_today_stats_view()
        text = "✅ 今日统计已从当前时间重新开始计算。历史交易仍保留在数据库。"
        await q.edit_message_text(text, reply_markup=self.advanced_keyboard(), parse_mode="HTML")

    async def _action_cleanup_db(self, q):
        """Manual database prune from TG. Keeps trades; prunes bulky public/decision data by configured retention."""
        keep_days = int(getattr(config, "DATA_KEEP_DAYS", 3) or 3)
        try:
            counts = self.db.table_counts() if hasattr(self.db, "table_counts") else {}
        except Exception:
            counts = {}
        shown = []
        for name in ("market_snapshots", "public_trade_samples", "price_history_samples", "decision_logs", "execution_events", "order_attempts"):
            if name in counts:
                shown.append(f"• {name}: <b>{counts.get(name)}</b>")
        text = f"<b>🗑 清理数据库</b>\n\n将按当前保留天数 <b>{keep_days}</b> 天清理行情快照、决策日志、执行事件和旧订单尝试。\n不会删除 trades 交易历史。\n\n"
        if shown:
            text += "当前计数：\n" + "\n".join(shown) + "\n\n"
        text += "确认后会执行 prune，并尝试 VACUUM 压缩数据库文件。"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 确认清理", callback_data="cleanup_db_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data="advanced_menu"),
        ]])
        await q.edit_message_text(text[:3900], reply_markup=kb, parse_mode="HTML")

    async def _action_cleanup_db_confirm(self, q):
        keep_days = int(getattr(config, "DATA_KEEP_DAYS", 3) or 3)
        try:
            before = self.db.table_counts() if hasattr(self.db, "table_counts") else {}
            self.db.prune_public_data(keep_days=keep_days)
            try:
                with self.db.conn() as c:
                    c.execute("VACUUM")
            except Exception as e:
                log.warning("database vacuum after TG cleanup failed: %s", e)
            after = self.db.table_counts() if hasattr(self.db, "table_counts") else {}
            names = ("market_snapshots", "public_trade_samples", "price_history_samples", "decision_logs", "execution_events", "order_attempts")
            text = f"✅ <b>数据库清理完成</b>\n\n保留天数: <b>{keep_days}</b> 天\n"
            for name in names:
                if name in before or name in after:
                    b = before.get(name, 0) if isinstance(before, dict) else 0
                    a = after.get(name, 0) if isinstance(after, dict) else 0
                    text += f"• {name}: {b} → <b>{a}</b>\n"
            text += "\n说明: trades 交易历史未删除。"
        except Exception as e:
            log.exception("TG database cleanup failed: %s", e)
            text = f"⛔ 数据库清理失败：<code>{html.escape(str(e))[:600]}</code>"
        await q.edit_message_text(text[:3900], reply_markup=self.advanced_keyboard(), parse_mode="HTML")

    async def _action_self_repair(self, q):
        """Clear stuck runtime state from TG without SSH SQL."""
        keys_to_clear = [
            "halted", "halt_reason", "last_halt_notify",
            "capital_isolation_reserved_balance", "capital_isolation_capital_limit_usd",
            "capital_isolation_initial_balance",
            "cooldown_until", "consecutive_losses",
            "stop_loss_balance", "test_stop_loss_balance", "live_stop_loss_balance",
        ]
        for key in keys_to_clear:
            try:
                if key in {"halted", "cooldown_until", "consecutive_losses"}:
                    self.db.set_state(key, False if key == "halted" else 0)
                else:
                    self.db.set_state(key, "" if key in {"halt_reason", "last_halt_notify"} else None)
            except Exception as e:
                log.warning("self repair failed clearing %s: %s", key, e)
        if self.risk:
            try:
                self.risk.resume()
            except Exception:
                pass
        text = (
            "✅ <b>自助修复已执行</b>\n\n"
            "已清理 halted / halt_reason / capital_isolation / cooldown 等卡住状态。\n"
            "建议现在重启服务：<code>systemctl restart btc-bot</code>，然后查看 💰 余额 和 📋 订单生命周期。"
        )
        await q.edit_message_text(text, reply_markup=self.settings_keyboard(), parse_mode="HTML")

    # ============ Status renderer ============

    def _render_status(self) -> str:
        text = f"<b>🤖 Polymarket 多策略 Bot</b>\n<code>{config.VERSION}</code>\n"
        text += f"模式: {html.escape(self._trade_mode_label())}\n"
        text += f"认证: {'✅ 已配置' if config.has_polymarket_creds else '⛔ 未配置交易密钥'} | 单笔: ${config.effective_per_order_amount:.2f}\n\n"

        # Risk status
        if self.risk:
            rs = self.risk.status_summary()
            if rs["halted"]:
                text += f"🛑 状态: <b>已停止</b>\n原因: {rs['halt_reason']}\n"
            elif rs["cooldown_remaining"] > 0:
                text += f"❄️ 状态: <b>冷却中</b> ({rs['cooldown_remaining']}s)\n"
            else:
                text += f"✅ 状态: <b>运行中</b>\n"

            if rs["consecutive_losses"] > 0:
                text += f"⚠️ 连败: {rs['consecutive_losses']}/{config.CONSECUTIVE_LOSS_LIMIT}\n"
            daily_limit = "∞" if int(config.effective_max_trades_per_day or 0) <= 0 else str(config.effective_max_trades_per_day)
            text += f"📌 开仓: {rs.get('open_trades', 0)}/{config.effective_max_open_trades} | 今日: {rs.get('today_trades', 0)}/{daily_limit}\n"

        # Today stats
        if self.learner:
            s = self.learner.daily_summary()
            text += f"\n<b>今日:</b>\n"
            text += f"真实 {s['trades_total']} | 胜率 {s['win_rate']:.0%} | PnL ${s['total_pnl']:+.2f}\n"
            if s.get('shadow_resolved'):
                text += f"🎭 影子 {s.get('shadow_resolved',0)} | PnL ${float(s.get('shadow_pnl') or 0):+.2f}\n"

        # BTC price. v14.2.8: feed may be MultiAssetBinanceFeed, which exposes
        # get_current_price("BTC") instead of last_price. Keep BinanceFeed fallback.
        btc_price = 0.0
        try:
            if self.feed:
                if hasattr(self.feed, "get_current_price"):
                    try:
                        btc_price = float(self.feed.get_current_price("BTC") or 0.0)
                    except TypeError:
                        btc_price = float(self.feed.get_current_price() or 0.0)
                else:
                    btc_price = float(getattr(self.feed, "last_price", 0.0) or 0.0)
        except Exception as e:
            log.debug("status BTC price unavailable: %s", e)
            btc_price = 0.0
        if btc_price:
            text += f"\nBTC: ${btc_price:,.2f}\n"

        return text

    # ============ Push notifications ============

    async def push(self, event: str, payload: dict):
        """Called by trader/learner/risk to push notifications"""
        if not config.ENABLE_TELEGRAM or not config.TG_USER_ID:
            return

        text = ""
        kb = None

        if event == "trade_opened":
            text = (
                f"🎯 <b>概率边际 · 已成交</b>\n\n"
                f"信号: {payload['pattern']}\n"
                f"BTC: ${payload['btc_price']:,.2f}\n"
                f"规则线: ${payload.get('reference_price', 0):,.2f} | 距离: ${payload.get('distance', 0):+.2f}\n"
                f"买入: <b>{payload['direction']}</b> @ ${payload['entry_price']:.3f}\n"
                f"模型胜率: {payload.get('model_probability', 0):.1%} | 费后边际: {payload.get('edge_after_fees', 0):.1%}\n"
                f"股数: {payload['shares']:.4f}\n"
                f"成本: ${payload['cost']:.2f}\n"
                f"最高回收: <b>${payload['max_payout']:.2f}</b>\n\n"
                f"余额: ${payload['balance']:.2f}"
            )

        elif event == "trade_resolved":
            outcome = payload['outcome']
            if outcome == "win":
                text = (
                    f"✅ <b>盈利结算</b>\n\n"
                    f"本笔: <b>+${payload['pnl']:.2f}</b>\n"
                    f"成本 ${payload['cost']:.2f} → 回收 ${payload['payout']:.2f}\n"
                    f"余额: <b>${payload['balance']:.2f}</b>"
                )
                if payload.get("review"):
                    text += f"\n\n<code>{payload['review']}</code>"
            else:
                text = (
                    f"❌ <b>亏损结算</b>\n\n"
                    f"本笔: <b>-${abs(payload['pnl']):.2f}</b>\n"
                    f"余额: <b>${payload['balance']:.2f}</b>"
                )
                if payload.get("review"):
                    text += f"\n\n<code>{payload['review']}</code>"

        elif event == "risk_critical":
            text = (
                f"🛑 <b>关键风险告警</b>\n\n"
                f"原因: {payload['reason']}\n"
                f"余额: ${payload['balance']:.2f}\n\n"
                f"机器人已停止。"
            )
            kb = self.main_menu_keyboard()

        elif event == "daily_summary":
            s = payload
            text = (
                f"📊 <b>每日总结 ({s['date']})</b>\n\n"
                f"交易: {s['trades_resolved']} | 胜率: {s['win_rate']:.0%}\n"
                f"盈亏: <b>${s['total_pnl']:+.2f}</b>\n"
                f"盈亏比: {s['risk_reward']:.2f}"
            )

        elif event == "product_doctor_alert":
            try:
                from product_doctor import ProductDoctor, DoctorReport, DoctorIssue
                issues = [DoctorIssue(**i) for i in payload.get("issues", [])]
                report = DoctorReport(
                    generated_at=int(payload.get("generated_at") or 0),
                    lookback_sec=int(payload.get("lookback_sec") or config.PRODUCT_DOCTOR_LOOKBACK_SEC),
                    status=str(payload.get("status") or "warning"),
                    issues=issues,
                    metrics=payload.get("metrics") or {},
                )
                text = ProductDoctor(self.db, market_ws=getattr(self.polymarket, "market_ws", None)).render_text(report)
            except Exception:
                text = "⚠️ <b>产品医生发现异常</b>\n\n请打开 TG 面板查看 🩺 产品医生。"
            kb = self.main_menu_keyboard()

        if not text:
            return

        try:
            await self.app.bot.send_message(
                chat_id=config.TG_USER_ID,
                text=text,
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Telegram push failed: {e}")

    # ============ Lifecycle ============

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        self._running = True
        log.info("✅ Telegram bot online")

    async def stop(self):
        if self._running:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self._running = False
