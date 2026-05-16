# BTC Polymarket Bot v15.1.4

Telegram + Polymarket short-cycle automation bot for BTC/crypto 5m and 15m markets.

This repository is now consolidated around one primary README, one current installer,
and one current startup path. Historical package manifests and duplicate release notes
were removed to avoid deploying or reading stale instructions.

## Default safety posture

Fresh installs are safe by default. The checked-in `.env` and `.env.example` keep real
orders disabled until you explicitly arm them:

```env
MODE=paper
DRY_RUN=true
REAL_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false
POLYMARKET_REPLAY_ON_START=false
```

Telegram placeholders must be replaced before the Telegram UI can connect:

```env
TG_BOT_TOKEN=your_telegram_bot_token_here
TG_USER_ID=123456789
```

`TG_USER_ID` must be your personal Telegram numeric user ID; commands from other users
are rejected.

## Current entry points

Use these for normal operation:

- `./start.sh` — one-click runtime gate: preflight, backtest, optional replay, then `main.py`.
- `sudo bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz` — current safe installer/upgrader.
- `bash audit_v15_1_3.sh` — package safety audit and test runner.

Legacy helpers remain only where tests or upgrade compatibility still require them:

- `install_v15_1_2.sh` — regression compatibility helper.
- `install_v14_3_2.sh` / `install_ready.sh` — old upgrade path compatibility.
- `start_high_win_rate.sh` — optional high-win-rate profile wrapper.
- `start_py311.sh` — Python 3.11-specific wrapper.

## Install

Upload `btc_bot_v15_1_3.tar.gz` to `/root/`, then run:

```bash
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd btc_bot_v15_1_3
sudo bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
```

The installer defaults to `START_AFTER_INSTALL=false`, so it will not auto-start after
install. Review `.env` first:

```bash
nano /root/btc_bot_v15_1_3/.env
```

Then start manually:

```bash
systemctl restart btc-bot
journalctl -u btc-bot -f
```

## Runtime state and credentials

For upgrades, the installer preserves existing runtime state when present:

- `.env`
- `btc_bot.db`
- `btc_bot.db-wal`
- `btc_bot.db-shm`
- Telegram/runtime state stored in SQLite

Polymarket trading credentials should be entered through Telegram where possible. They
are runtime state, not hardcoded release-package content.

## Live arming rule

Real orders require all of these to be true:

```env
MODE=small_live        # or live
DRY_RUN=false
REAL_TRADING_ENABLED=true
OBSERVER_ONLY=false
```

For signature type 3 / proxy wallet real submission, this must also be intentionally
enabled:

```env
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true
```

## v15.1.4 safety fixes retained

- Shadow hard-safety trades are marked `is_shadow=1` so they can settle and feed review/learning.
- Runtime Telegram overrides are honored by `profit_rule_engine` settings.
- Profit-rule DB resolution uses `config.DB_PATH` for app-default access and treats invalid explicit DB references as unavailable.
- `mode="live"` remains an auditable policy request; non-promoted buckets are blocked when live promotion is required.

## Operational goal

The bot automates signal generation, shadow learning, risk checks, and review loops.
No system can guarantee daily profit or zero losses; real orders should stay behind
explicit arming, backtest/replay checks, and small position sizing.
