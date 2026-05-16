#!/usr/bin/env python3
"""Server deployment readiness checks for btc-bot.

This script is intentionally conservative: it checks the *real server runtime*
filesystem, service unit, .env, Python packages, write permissions, and optional
network endpoints. It does not place orders and does not require real trading to
be enabled.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import socket
import subprocess
import sys
from typing import Iterable


ROOT = pathlib.Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
EXAMPLE_FILE = ROOT / ".env.example"
DEFAULT_SERVICE = "btc-bot"

WARNINGS = 0
ERRORS = 0


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def warn(msg: str) -> None:
    global WARNINGS
    WARNINGS += 1
    print(f"⚠️  {msg}")


def fail(msg: str) -> None:
    global ERRORS
    ERRORS += 1
    print(f"❌ {msg}")


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def bool_env(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_placeholder(value: str | None) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    return (
        v in {"your_private_key_here", "your_wallet_address_here", "your_telegram_bot_token_here", "123456789", "0", "none", "null", "changeme"}
        or v.startswith("your_")
        or "placeholder" in v
    )


def run(cmd: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "command timed out"


def check_python() -> None:
    version = sys.version_info
    if version < (3, 10):
        fail(f"Python 版本过低：{sys.version.split()[0]}；服务器建议 Python 3.10+，至少要满足依赖包安装要求")
    else:
        ok(f"Python 版本：{sys.version.split()[0]}")

    required = ["dotenv", "aiohttp", "numpy", "websockets"]
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        fail("缺少 Python 依赖：" + ", ".join(missing) + "；请在服务器执行：./.venv/bin/pip install -r requirements.txt")
    else:
        ok("核心 Python 依赖已安装")

    if importlib.util.find_spec("py_clob_client") is None and importlib.util.find_spec("py_clob_client_v2") is None:
        warn("未检测到 Polymarket CLOB SDK；Observer/部分离线检查可运行，但真实订单簿/下单前需要安装 requirements.txt 中的 SDK")
    else:
        ok("已检测到 Polymarket CLOB SDK")


def check_files(env: dict[str, str]) -> None:
    if ENV_FILE.exists():
        ok(f"找到服务器运行配置：{ENV_FILE}")
    elif EXAMPLE_FILE.exists():
        warn("未找到 .env；这是新服务器首次部署可接受，但启动服务前必须从 .env.ubuntu2404.example 复制并填写")
    else:
        fail("缺少 .env 且缺少 .env.ubuntu2404.example / .env.example")

    db_path = pathlib.Path(env.get("DB_PATH", "btc_bot.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db_parent = db_path.parent
    if db_parent.exists() and os.access(db_parent, os.W_OK):
        ok(f"数据库目录可写：{db_parent}")
    else:
        fail(f"数据库目录不可写或不存在：{db_parent}")

    for filename in ["main.py", "config.py", "trader.py", "polymarket_client.py"]:
        if (ROOT / filename).exists():
            ok(f"源码文件存在：{filename}")
        else:
            fail(f"源码文件缺失：{filename}")


def check_env_safety(env: dict[str, str]) -> None:
    if not env:
        return

    dry_run = bool_env(env, "DRY_RUN", True)
    real_enabled = bool_env(env, "REAL_TRADING_ENABLED", False)
    sig3_enabled = bool_env(env, "CLOB_V2_SIG3_REAL_SUBMIT_ENABLED", False)

    if real_enabled and dry_run:
        fail(".env 同时设置 REAL_TRADING_ENABLED=true 和 DRY_RUN=true；服务器真实下单状态不一致")
    elif real_enabled and not sig3_enabled:
        fail(".env 设置 REAL_TRADING_ENABLED=true，但 CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false；真实下单不会提交")
    elif real_enabled:
        warn("服务器已武装真实交易：REAL_TRADING_ENABLED=true 且 CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true，请确认风控额度")
    else:
        ok("真实交易默认未武装：REAL_TRADING_ENABLED=false")

    if env.get("REFERENCE_PRICE_SOURCE", "polymarket_gamma") != "polymarket_gamma":
        warn("REFERENCE_PRICE_SOURCE 不是 polymarket_gamma；请确认服务器确实要使用非平台参考源")
    else:
        ok("参考价格源为 Polymarket/Gamma")

    if bool_env(env, "PRICE_TO_BEAT_FALLBACK_ENABLED", False):
        warn("PRICE_TO_BEAT_FALLBACK_ENABLED=true；服务器会允许非 Gamma Price-to-Beat 兜底")
    else:
        ok("Price-to-Beat 外部兜底默认关闭")

    if bool_env(env, "SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED", False):
        warn("SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED=true；Shadow 可能用外部 close price 结算")
    else:
        ok("Shadow 外部结算兜底默认关闭")

    if bool_env(env, "ENABLE_TELEGRAM", False):
        if is_placeholder(env.get("TG_BOT_TOKEN")) or is_placeholder(env.get("TG_USER_ID")):
            fail("ENABLE_TELEGRAM=true，但 TG_BOT_TOKEN/TG_USER_ID 仍为空或占位值")
        else:
            ok("Telegram 已启用且凭据看起来不是占位值")
    else:
        ok("Telegram 未启用；服务器可先用 Observer/Shadow 验证")


def check_systemd(service: str) -> None:
    code, out = run(["systemctl", "cat", service], timeout=8)
    if code != 0:
        warn(f"未读取到 systemd 服务 {service}；安装后请执行：systemctl enable {service}")
        return
    ok(f"systemd 服务存在：{service}")

    expected_workdir = f"WorkingDirectory={ROOT}"
    expected_exec = f"ExecStart={ROOT}/.venv/bin/python {ROOT}/main.py"
    if expected_workdir in out:
        ok("systemd WorkingDirectory 指向当前部署目录")
    else:
        fail(f"systemd WorkingDirectory 不是当前目录；应包含：{expected_workdir}")
    if expected_exec in out:
        ok("systemd ExecStart 使用当前目录虚拟环境 main.py")
    else:
        fail(f"systemd ExecStart 不是当前目录入口；应包含：{expected_exec}")

    code, active = run(["systemctl", "is-enabled", service], timeout=8)
    if code == 0:
        ok(f"systemd 服务已 enable：{active}")
    else:
        warn(f"systemd 服务未 enable 或无法确认：{service}")


def check_network(hosts: Iterable[tuple[str, int]]) -> None:
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=5):
                ok(f"服务器网络可连接：{host}:{port}")
        except OSError as exc:
            warn(f"服务器网络暂时无法连接 {host}:{port}：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查真实服务器部署是否具备运行 btc-bot 的条件")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="systemd 服务名，默认 btc-bot")
    parser.add_argument("--skip-network", action="store_true", help="跳过外网连通性检查")
    args = parser.parse_args()

    print("=== btc-bot 服务器部署体检 ===")
    print(f"部署目录：{ROOT}")

    env = parse_env(ENV_FILE)
    check_python()
    check_files(env)
    check_env_safety(env)
    check_systemd(args.service)
    if not args.skip_network:
        check_network([
            ("gamma-api.polymarket.com", 443),
            ("clob.polymarket.com", 443),
            ("api.telegram.org", 443),
            ("api.binance.com", 443),
        ])

    print(f"=== 完成：{ERRORS} 个阻断问题，{WARNINGS} 个提醒 ===")
    if ERRORS:
        print("服务器还不能安全启动；请先修复上面的 ❌ 项。")
        return 1
    print("服务器基础条件通过；如果要真实下单，还需要确认资金、额度和 TG 运行态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
