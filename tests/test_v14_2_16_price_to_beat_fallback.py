from pathlib import Path


def test_price_to_beat_fallback_disabled_by_default():
    s = Path('config.py').read_text()
    assert 'PRICE_TO_BEAT_FALLBACK_ENABLED' in s
    assert '_env_bool("PRICE_TO_BEAT_FALLBACK_ENABLED", False)' in s


def test_trader_refuses_missing_price_to_beat_unless_fallback_explicitly_enabled():
    s = Path('trader.py').read_text()
    assert 'signal_reference_fallback' in s
    assert 'fallback_price = float(getattr(signal, "reference_price"' in s
    assert 'Polymarket CLOB orderbook/edge/slippage checks still apply' in s
    assert 'missing_polymarket_price_to_beat' in s
    assert 'refusing external reference fallback' in s
    # The old direct hard-stop should no longer be the only behavior.
    old = 'if config.REQUIRE_MARKET_PRICE_TO_BEAT and not market.get("price_to_beat"):\n            log.info("⚠️  Skip: missing Gamma Price-to-Beat; avoiding rule-source mismatch")'
    assert old not in s
