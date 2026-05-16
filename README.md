# BTC Polymarket Bot v15.1.3

Telegram + Polymarket short-cycle automation bot.

This release is a new-install friendly safety patch on the v15.1.1 base.

- v14.3.2 strategy core: `adaptive_edge_engine.py`, dynamic EV, calibrated probability, 5m/15m support.
- v15.1.x deployment shell: single `btc-bot` service, duplicate-service stop, `.env` / `btc_bot.db` preservation when present.
- v15.1.1 live arming fix: `REAL_TRADING_ENABLED` is enforced in code.
- v15.1.3 packaging fix: release package includes a runnable `.env` file for users who delete old directories before every install.

## Default posture

Brand-new installs create/keep `.env` immediately. It is safe by default:

```env
MODE=small_live
DRY_RUN=true
REAL_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false
POLYMARKET_REPLAY_ON_START=false
```

Telegram will only connect after you edit these fields in `.env`:

```env
TG_BOT_TOKEN=your_telegram_bot_token_here
TG_USER_ID=123456789
```

`TG_USER_ID` is your personal Telegram numeric user ID. The bot only accepts commands from this ID.

## Runtime state and credentials

For a new install, the package includes `.env` so the runtime directory is complete.

For an upgrade, the installer still preserves existing runtime state when it exists:

- `.env`
- `btc_bot.db`
- `btc_bot.db-wal`
- `btc_bot.db-shm`
- `bot_state` / `api_credentials` tables when present

Polymarket trading credentials should still be entered through Telegram where possible. They are stored in SQLite runtime state, not hardcoded into the release package.

## Install

Upload `btc_bot_v15_1_3.tar.gz` to `/root/`, then run:

```bash
cd /root
tar -xzf btc_bot_v15_1_3.tar.gz
cd btc_bot_v15_1_3

sudo bash install_v15_1_3.sh /root/btc_bot_v15_1_3.tar.gz
```

The installer defaults to `START_AFTER_INSTALL=false`, so it will not auto-start after install.

Before starting, edit Telegram config:

```bash
nano /root/btc_bot_v15_1_3/.env
```

Then start manually:

```bash
systemctl restart btc-bot
journalctl -u btc-bot -f
```

## Live arming rule

Real orders require **all** of these:

```env
MODE=small_live        # or live
DRY_RUN=false
REAL_TRADING_ENABLED=true
OBSERVER_ONLY=false
```

For signature type 3 / proxy wallet real submission, this must also be intentionally enabled:

```env
CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true
```

## Short-cycle automation goal

The bot is designed for small-size, frequent 5m/15m markets:

- Shadow mode collects free learning samples.
- Adaptive Edge filters candidates by EV and quality.
- Profit Rule live gate requires promoted rule buckets.
- Execution gate blocks poor fills, slippage, thin book, and stale markets.
- Daily profit lock and loss controls reduce downside.

No system can guarantee daily profit or zero losses. The engineering goal is to automate learning/trading/review while keeping real orders behind explicit arming and risk gates.
