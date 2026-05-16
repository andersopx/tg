from pathlib import Path


def test_tg_trade_key_prompt_tracks_and_cleans_prompt_message():
    s = Path("telegram_bot.py").read_text()
    assert "trade_key_prompt_message_id" in s
    assert "trade_key_prompt_chat_id" in s
    assert "delete_message(chat_id=prompt_chat_id" in s
    assert "stale “取消”" in s or "stale" in s


def test_skip_reason_labels_for_orderbook_and_price_range_are_user_readable():
    s = Path("telegram_bot.py").read_text()
    assert '"orderbook_missing": "盘口暂时没有可买价"' in s
    assert '"token_price_outside_range": "价格太贵/太接近1，不追"' in s
