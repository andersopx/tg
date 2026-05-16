# v15.1.4 - Shadow 孤儿单与 runtime override 修复

## 这一版修了什么

v15.1.3 已经把"shadow 单错误提交到 Polymarket"的事故修好了，
但留下了 3 个更隐蔽的 bug，会让整个学习闭环失效。

### Bug 1（致命）：Shadow 单变孤儿，学习闭环失效

**位置**：`trader.py` 第 922-943 行的 v15.1.3 "hard safety" 路径。

**问题**：
- 这条路径先用 `insert_trade()` 写一行 `status='attempted'`（没传 `is_shadow=1`）
- 然后 `UPDATE trades SET status='shadow_open' WHERE id=?`（**还是没设 is_shadow=1**）
- 但 `database.py` 里所有 shadow 查询都要求 `COALESCE(is_shadow,0)=1`：
  - `get_shadow_open_trades()` (行 695)
  - `get_shadow_pnl_summary()` (行 701)
  - `get_shadow_overdue_summary()` (行 711)

**影响**：用户当前 `REAL_TRADING_ENABLED=false`，**100% 的"成功决策"都走这条路径**，
导致所有 shadow 单变成孤儿：
- 永远不会被 `settle_resolved_trades()` 结算
- 永远不会写入 `trade_reviews`
- `asset_strategy_stats` 永远是空的
- `strategy_weight_controller` 永远学不到东西

机器人**看起来在工作**（TG 显示 SHADOW BUY 数量增加），
**实际什么都没学到**。

**修复**：hard safety 路径现在用单次 UPDATE 设置完整字段：
```sql
UPDATE trades SET
  status='shadow_open',
  is_shadow=1,
  shadow_reason='real_orders_disabled_shadow_mode',
  submitted_at=?,
  submitted_order_id=?,
  filled_shares=?, filled_cost=?, avg_price=?
WHERE id=?
```

这样 `get_shadow_open_trades()` 能找到它，
`_settle_one_shadow_trade()` 会用 Gamma/Binance 真实价格结算，
真实进入复盘 + 权重学习闭环。

### Bug 2：SHADOW BUY 日志信息丢失

**位置**：`trader.py` 第 931-941 行。

**问题**：
```python
_asset = str(signal.get("asset") or ...)
```
但 `signal` 是 `ReversalSignal` 这个 dataclass，**没有 `.get()` 方法**。
每次调用都会抛 `AttributeError`，被外层 except 兜住，
日志只显示 `"SHADOW BUY trade_id=XX reason=..."`，
丢失了 asset / timeframe / direction / entry / shares / cost 全部信息。

**修复**：改用局部变量直接拿值（这些变量在该作用域内都已存在）：
```python
_asset = str(asset or getattr(signal, "asset", "") or "")
_entry = float(token_price or 0.0)
_size = float(expected_cost or 0.0)
_shares = float(estimated_shares or 0.0)
```

### Bug 3：profit_rule_engine 看不到 TG runtime override

**位置**：`profit_rule_engine.py` 第 68-77 行。

**问题**：`_env_float / _env_int / _env_bool` 直接调 `os.getenv()`，
完全绕过 `config._runtime_state`。用户在 Telegram 上调
`PROFIT_RULE_*` 参数时以为生效了，**实际上引擎还在读 .env 的老值**。

**修复**：这三个函数现在优先尝试 `config._runtime_float` /
`config._runtime_int` / `config._state_get_first`，
读不到才 fallback 到 `os.getenv`。

### Bug 4：`config.BET_SIZE` 不存在

**位置**：`trader.py` 第 936 行。

**问题**：
```python
_size = float(expected_cost or getattr(config, "BET_SIZE", 1.0) or 1.0)
```
`config` 类没有 `BET_SIZE` 这个字段（只有 `TEST_BET_SIZE` / `LIVE_MIN_BET`），
所以 `getattr` 永远 fallback 到 `1.0`。

**修复**：直接用 `expected_cost`，这个变量在该作用域内已经是正确的下单金额。

### Bug 5：profit_rule_engine 硬编码 `"btc_bot.db"`

**位置**：`profit_rule_engine.py` 第 299/300/310/311/451/548/550 行。

**问题**：如果服务器进程的工作目录不是包目录，引擎会在错误的位置
**默默创建一个空的 `btc_bot.db`**，然后所有规则查询都返回空结果，
profit rule 永远 unknown。

**修复**：新增 `_config_db_path()`，优先用 `config.DB_PATH`，
所有硬编码全部替换。`rebuild_profit_rules` 的默认参数也改成动态读取。

## 没改的部分

- ✅ 4 道安全开关（`REAL_TRADING_ENABLED` / `DRY_RUN` / `OBSERVER_ONLY` / `MODE`）
- ✅ `signer_guard` 路径的 `_insert_shadow_trade`（这条路径本来就是对的）
- ✅ `execution_quality_guard` / `adaptive_edge_engine` / `high_win_rate_guard` 算法
- ✅ Polymarket SDK 调用方式（v14.2.31/32 已稳定）
- ✅ `silent fallback` 修复（v14.2.32 保留）
- ✅ `halt` 启动清理（v14.2.33 保留）
- ✅ WebSocket LRU（v14.2.33 保留）
- ✅ TG UI 精简（v14.2.33 保留）
- ✅ 私钥 / Funder / Relayer

## 部署后必须验证的

跑 1 小时后：

```bash
sqlite3 btc_bot.db "
  SELECT status, COUNT(*) AS n, SUM(COALESCE(is_shadow,0)) AS shadow_flagged
  FROM trades GROUP BY status
"
```

期望：
```
shadow_open       | 5+  | 5+    <- 全部 is_shadow=1
shadow_resolved   | 1+  | 1+    <- 已开始结算
```

如果还看到：
```
shadow_open       | 5+  | 0     <- 孤儿仍存在
```
说明部署有问题。

## 测试

新加 `tests/test_v15_1_4_hard_safety.py`，覆盖：

1. `test_hard_safety_update_sets_is_shadow_and_reason`
   验证新的 UPDATE 真的设了 `is_shadow=1` + `shadow_reason` + `submitted_order_id`。

2. `test_v15_1_3_bug_would_not_set_is_shadow`
   反证：v15.1.3 的老 UPDATE 创建的是孤儿行。

3. `test_signal_attribute_access_does_not_raise`
   验证 `getattr` 风格不会抛 `AttributeError`。

4. `test_profit_rule_engine_uses_config_db_path`
   验证引擎用 `config.DB_PATH` 而不是硬编码。

5. `test_profit_rule_engine_env_float_respects_runtime_state`
   验证 TG runtime override 真的能影响 profit rule。

## VERSION

`v15.1.4-shadow-orphan-fix`
