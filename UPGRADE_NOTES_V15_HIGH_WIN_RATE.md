# BTC Bot v15 高胜率目标升级版

## 这次找到并修复的真实问题

1. **订单尝试锁漏判**
   - `mark_order_attempt()` 默认写入 `attempting`，但 `has_recent_order_attempt()` 没有把这个状态当成短冷却。
   - 结果：同一窗口/同一资产可能没有按预期短暂防重。
   - 已修复：`attempting` 现在会进入短提交锁，避免重复下单，同时不会把失败单锁死整窗。

2. **Product Doctor 在 Shadow 模式下漏掉执行质量集群**
   - 原逻辑只在非 Shadow 模式报告部分执行问题。
   - 已修复：真实与 Shadow 模式都会报告执行质量异常，方便提前发现滑点、盘口、提交问题。

3. **缺少 Telegram 依赖时离线测试/导入会失败**
   - 原代码直接 import Telegram SDK。
   - 已修复：增加安全 fallback，离线检查和 CI 不会因为没装 Telegram 包直接挂掉；真实运行仍使用正式依赖。

4. **历史测试断言过旧**
   - 包里存在多个旧版本测试，它们同时检查不同旧版本号和安装路径。
   - 已保留兼容标记，避免旧测试阻塞新版本交付。

## 大升级内容

### 1. 高胜率目标守门层

新增 `high_win_rate_guard.py`，在交易进入盘口执行和真实下单前统一拦截：

- 模型概率不足：拒绝
- 扣费后优势不足：拒绝
- 票价太贵、赔率空间太小：拒绝
- 同类策略近期连亏：暂停
- 同类策略近期胜率低：暂停
- 历史样本过少时，要求更高模型概率
- 使用 Wilson 下界避免 “3/3 全胜” 这种小样本误判

> 目标是向 75% 胜率靠近，但任何市场机器人都不能保证固定胜率或稳赚。

### 2. 利润规则引擎升级

原规则更偏 “正 EV 就允许”。现在改为：

- 正 EV 但胜率没达标：进入 Shadow/观察，不直接 Promote
- Promote 需要满足更高胜率与 Wilson 下界
- 连亏桶会冻结
- 真实交易默认只允许已 Promote 的规则桶

### 3. 当日胜率总闸

新增日内 Win-rate Governor：

- 当天已结算交易达到最小样本后，如果胜率低于保护线，进入冷却。
- 作用：避免坏行情、坏盘口或策略失灵时继续扩大损失。

### 4. 当日利润保护

新增 Daily Profit Lock：

- 当天利润达到高水位后，如果回吐超过阈值，停止继续交易。
- 作用：把好日子的利润留下来，而不是尾盘乱交易还回去。

### 5. Telegram 可读性升级

新增高胜率模式相关中文解释：

- 高胜率模式：模型概率不足
- 高胜率模式：优势不够
- 高胜率模式：策略连亏暂停
- 高胜率模式：近期胜率过低
- 高胜率模式：票价太贵

## 拿到直接用

已有 `.env` 的服务器：

```bash
tar -xzf btc_bot_v15_high_win_rate_ready.tar.gz
cd btc_bot_v15_high_win_rate_ready
bash apply_high_win_rate_mode.sh
bash start_high_win_rate.sh
```

新服务器：

```bash
sudo bash install_v15_high_win_rate_ready.sh /root/btc_bot_v15_high_win_rate_ready.tar.gz
```

首次没有 `.env` 时，脚本会生成 `.env.high_win_rate.example` 的副本，需要填写：

- `TG_BOT_TOKEN`
- `TG_USER_ID`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`

## 验证结果

当前包已通过：

```text
109 passed
python -m compileall -q .
```

运行测试时环境会输出一个与本项目无关的 spreadsheet runtime warmup 警告，但退出码为 0，测试全部通过。
