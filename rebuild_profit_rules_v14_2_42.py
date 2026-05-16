#!/usr/bin/env python3
from profit_rule_engine import rebuild_profit_rules

if __name__ == "__main__":
    n = rebuild_profit_rules("btc_bot.db")
    print(f"PROFIT_RULES_REBUILT rows={n}")
