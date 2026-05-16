import re
from pathlib import Path


def test_all_trader_skip_reasons_have_chinese_mapping():
    trader_src = Path("trader.py").read_text()
    tg_src = Path("telegram_bot.py").read_text()
    reasons = set(re.findall(r'self\._skip\(signal,\s*"([a-zA-Z0-9_]+)"', trader_src))
    assert reasons, "expected to find _skip reasons in trader.py"

    # Only require direct reason keys in the display mapping section, not hidden fallback text.
    mapping_start = tg_src.index("def _display_reason")
    mapping_end = tg_src.index("def _reason_hint", mapping_start)
    mapping_src = tg_src[mapping_start:mapping_end]
    missing = sorted(r for r in reasons if f'"{r}"' not in mapping_src)
    assert missing == []


def test_all_trader_skip_reasons_have_hints_or_safe_fallback():
    trader_src = Path("trader.py").read_text()
    tg_src = Path("telegram_bot.py").read_text()
    reasons = set(re.findall(r'self\._skip\(signal,\s*"([a-zA-Z0-9_]+)"', trader_src))
    hint_start = tg_src.index("def _reason_hint")
    hint_end = tg_src.index("def _assets_scan_summary", hint_start)
    hint_src = tg_src[hint_start:hint_end]
    # Most critical reasons should have explicit hints; all others still have the generic fallback.
    critical = {
        "order_placement_failed",
        "order_not_matched",
        "order_fill_unconfirmed",
        "recent_order_attempt_lock",
        "token_price_outside_range",
        "orderbook_missing",
        "fee_adjusted_edge_low",
        "slippage_cap_exceeded",
        "quality_price_impact_high",
    }
    missing = sorted(r for r in critical if r in reasons and f'"{r}"' not in hint_src)
    assert missing == []
    assert "内部代码" in hint_src
