
from telegram_bot import TelegramBot


def test_recent_order_attempt_lock_is_human_readable():
    label = TelegramBot._display_reason(None, "recent_order_attempt_lock")
    hint = TelegramBot._reason_hint(None, "recent_order_attempt_lock")
    assert "recent_order_attempt_lock" not in label
    assert "冷却" in label or "刚尝试" in label
    assert "重复" in hint


def test_unknown_reason_does_not_show_raw_english_as_label():
    label = TelegramBot._display_reason(None, "some_new_internal_code")
    hint = TelegramBot._reason_hint(None, "some_new_internal_code")
    assert label == "其他保护原因"
    assert "some_new_internal_code" in hint
