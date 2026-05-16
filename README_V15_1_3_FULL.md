# btc_bot_v15_1_3 full package

这是完整项目包，不是升级包/补丁包。包含完整源码、历史说明、测试文件、安装脚本和运行配置模板。

## 安全默认

- `REAL_TRADING_ENABLED=false`
- `CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false`
- `SHADOW_TRADING_ENABLED=true`
- `DRY_RUN=false` 仅用于允许本地 Shadow 写入，不等于真实下单
- `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER` 不硬编码进包；交易密钥继续通过 TG 写入本地运行状态/数据库。

## v15.1.3 核心修复

1. database.py: `_safe_json_dumps` 防止 signal_data 循环引用导致写库失败。
2. profit_rule_engine.py: Shadow / Live 模式隔离，NEW 规则可在 Adaptive Edge 通过后进入 Shadow 学习。
3. trader.py: `real_orders_enabled=false` 时硬拦截真实 `/order`，只写本地 Shadow。
4. install_v15_1_3.sh: 新安装/覆盖安装时优先保留旧 `.env`、`btc_bot.db`、TG 保存的运行状态。

## 安装

```bash
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd btc_bot_v15_1_3
bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
```

安装后验证：

```bash
cd /root/btc_bot_v15_1_3
./.venv/bin/python -m py_compile config.py database.py trader.py profit_rule_engine.py telegram_bot.py main.py
systemctl status btc-bot --no-pager
```
