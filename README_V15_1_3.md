# btc_bot_v15_1_3 完整包

这是完整源码包，不是小补丁包。

## 核心修复

- 真实盘关闭时，执行层绝不调用 Polymarket `/order`。
- Shadow 模式只写本地 `shadow_open`，用于学习和复盘。
- 修复 `signal_data` 循环引用导致 `json.dumps` 崩溃。
- 修复 Profit Rule 中 live/shadow 模式混乱。
- NEW 规则可在 Adaptive Edge 放行后进入 Shadow 学习。
- systemd 默认入口修正为 `main.py`。

## 默认安全开关

```text
DRY_RUN=false
SHADOW_TRADING_ENABLED=true
REAL_TRADING_ENABLED=false
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false
```

## 安装

```bash
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd /root/btc_bot_v15_1_3
bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
```

安装器会尽量保留旧目录中的 `.env`、`btc_bot.db` 和 Telegram 中保存的交易密钥状态。
