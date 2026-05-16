# BTC Polymarket Bot

## 1. 产品介绍

BTC Polymarket Bot 是一个面向 Polymarket BTC 短周期市场的 Telegram 自动化机器人，主要用于 5 分钟 / 15 分钟 Up/Down 市场的观察、影子交易学习和受控真实下单。

核心目标：

- **先学习，后实盘**：默认启用 Shadow（影子）模式，先用真实市场数据做本地模拟记录，不直接提交真实订单。
- **平台价格优先**：默认使用 Polymarket / Gamma 的市场规则和 Price-to-Beat；外部价格兜底默认关闭。
- **显式武装真实交易**：真实下单必须同时打开多个开关，避免误部署、误启动、误下单。
- **服务器部署友好**：安装脚本会保留 `.env`、数据库和 Telegram 保存的运行状态，并提供服务器本机体检脚本。
- **学习闭环**：Shadow / Real 交易结算后会写入复盘、胜率、Profit Rule 和策略统计，用于后续风控和筛选。

重要提醒：

- 本项目不能保证每天盈利，也不能保证零亏损。
- 默认配置是安全保守配置，不会自动真实下单。
- 服务器上线前必须在真实服务器目录运行体检，不要只相信容器里的测试结果。

---

## 2. 使用方法

### 2.1 服务器安装

把压缩包上传到服务器 `/root/` 后执行：

```bash
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd /root/btc_bot_v15_1_3
sudo bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
```

安装脚本默认不会自动启动服务：

```bash
START_AFTER_INSTALL=false
```

这是为了让你先检查 `.env`、Telegram 配置、数据库迁移和服务器体检结果，再手动启动。

### 2.2 配置 `.env`

新安装时可以从示例文件复制：

```bash
cd /root/btc_bot_v15_1_3
cp .env.example .env
nano .env
```

至少需要填写 Telegram：

```env
ENABLE_TELEGRAM=true
TG_BOT_TOKEN=你的 Telegram Bot Token
TG_USER_ID=你的 Telegram 数字用户 ID
```

安全默认值应保持：

```env
MODE=small_live
DRY_RUN=true
REAL_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false
POLYMARKET_REPLAY_ON_START=false
REFERENCE_PRICE_SOURCE=polymarket_gamma
PRICE_TO_BEAT_FALLBACK_ENABLED=false
SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED=false
```

### 2.3 服务器本机体检

部署到服务器后，在真实运行目录执行：

```bash
cd /root/btc_bot_v15_1_3
./.venv/bin/python server_readiness_check.py --service btc-bot
```

这个检查会验证：

- Python 版本和依赖是否正确；
- `.env` 是否存在、是否有明显危险配置；
- 数据库目录是否可写；
- systemd 的 `WorkingDirectory` / `ExecStart` 是否指向当前部署目录；
- Polymarket、Telegram、Binance 基础网络是否能连通。

如果只想先检查本机文件和 systemd，不检查外网：

```bash
./.venv/bin/python server_readiness_check.py --service btc-bot --skip-network
```

### 2.4 启动 / 停止 / 查看日志

启动服务：

```bash
systemctl restart btc-bot
```

查看服务状态：

```bash
systemctl status btc-bot --no-pager --full
```

查看实时日志：

```bash
journalctl -u btc-bot -f
```

停止服务：

```bash
systemctl stop btc-bot
```

### 2.5 Telegram 使用

Telegram 里主要使用两个模式：

#### 影子模式

按钮：`🎭 切到影子模式`

含义：

```env
MODE=small_live
DRY_RUN=false
REAL_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
```

效果：

- 机器人会跑真实市场检查、质量检查、风控检查；
- 通过检查的机会只写入本地 Shadow 记录；
- 不调用 Polymarket 真实下单接口；
- 结算后用于学习、复盘和策略统计。

#### 真实下单

按钮：`🟢 切到真实下单`

真实下单必须同时满足：

```env
MODE=small_live      # 或 live
DRY_RUN=false
REAL_TRADING_ENABLED=true
OBSERVER_ONLY=false
```

如果使用 signature type 3 / proxy wallet 真实提交，还必须显式打开：

```env
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true
```

只改一个开关不够。这样设计是为了防止服务器误启动后直接真实下单。

### 2.6 常用检查命令

完整测试：

```bash
./.venv/bin/python -m pytest -q
```

源码语法检查：

```bash
./.venv/bin/python -m py_compile config.py database.py trader.py profit_rule_engine.py telegram_bot.py main.py server_readiness_check.py
```

项目安全审计：

```bash
./audit_v15_1_3.sh
```

### 2.7 升级注意事项

升级时安装脚本会尽量保留：

- `.env`
- `btc_bot.db`
- `btc_bot.db-wal`
- `btc_bot.db-shm`
- Telegram 保存的运行状态
- 本地 API credential 状态

推荐升级流程：

```bash
systemctl stop btc-bot
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd /root/btc_bot_v15_1_3
sudo bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
./.venv/bin/python server_readiness_check.py --service btc-bot
systemctl restart btc-bot
journalctl -u btc-bot -f
```

---

## 3. 每次升级了什么

### v15.1.4 / 当前整理版

- README 只保留三类内容：产品介绍、使用方法、升级记录。
- 增加服务器本机体检脚本 `server_readiness_check.py`，用于检查真实服务器环境，而不是只依赖容器测试。
- 安装脚本在写入 systemd 服务后会执行本机体检，确认服务路径和本机环境配置。
- `audit_v15_1_3.sh` 改为更适合服务器：优先使用 `.venv`，不扫描第三方虚拟环境，不强制真实 `.env` 使用占位符。
- Shadow 结算外部价格兜底默认关闭，避免用非 Polymarket 官方结果训练学习闭环。
- Price-to-Beat 外部兜底默认关闭，Gamma 没有 Price-to-Beat 时默认跳过交易。

### v15.1.3

- 增加 `.env.example` 和 `.env.high_win_rate.example`，方便新服务器安全初始化。
- 增加 `.gitignore`，避免提交 `.env`、数据库、缓存、回测输出等运行时文件。
- 安装脚本默认服务名统一为 `btc-bot`，避免多个旧服务同时运行。
- 安装 / 升级时尽量保留旧 `.env`、`btc_bot.db` 和 Telegram 保存的密钥状态。
- 修复真实交易关闭时仍可能进入真实订单路径的问题：`REAL_TRADING_ENABLED=false` 时只允许 Shadow 记录。
- 修复 `signal_data` 循环引用导致写数据库失败的问题。
- 修复 Profit Rule 的 Shadow / Live 模式隔离问题。
- 默认不自动启动服务，避免用户还没检查 `.env` 就直接运行。

### v15.1.1

- 强化真实下单武装逻辑：`REAL_TRADING_ENABLED` 必须被代码层实际检查。
- Telegram 可以切换 Shadow / Real，但真实下单仍受启动门槛、密钥、风控和提交开关限制。
- 改进小额实盘和 Shadow 学习之间的运行状态隔离。

### v14.3.2

- 引入 Adaptive Edge Engine，用动态 EV、质量分和风险缓冲替代固定概率判断。
- 加强 Profit Rule Engine，支持规则健康度、样本数、近期表现和提升 / 冻结逻辑。
- 改进 5m / 15m 短周期市场筛选和执行前质量检查。
- 增强审计脚本，用于检查服务器服务、日志、Shadow、Profit Rule 和异常信息。

### v14.2.x

- 增加 Shadow 交易、Shadow 结算和学习闭环。
- 增加 Telegram 运行控制、资金视图、订单生命周期和诊断信息。
- 增加订单簿深度、滑点、最小成交、价格区间和执行阈值检查。
- 增加多资产 / 多周期相关配置和测试。
- 修复多处价格源、回退逻辑、token 范围诊断和小余额默认值问题。

---

## 4. 安全默认开关速查

新安装默认应保持：

```env
DRY_RUN=true
REAL_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false
POLYMARKET_REPLAY_ON_START=false
PRICE_TO_BEAT_FALLBACK_ENABLED=false
SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED=false
```

真实下单前必须确认：

```env
DRY_RUN=false
REAL_TRADING_ENABLED=true
OBSERVER_ONLY=false
```

signature type 3 / proxy wallet 真实提交还必须确认：

```env
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true
```
