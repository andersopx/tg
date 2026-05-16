from binance_feed import log


def test_bootstrap_log_message_formats_with_comma_price():
    """The bootstrap log format must be valid for %-style logging."""
    message = "✅ Binance feed %s bootstrapped %s klines from %s, last price $%s"
    args = ("BTCUSDT", 180, "https://data-api.binance.vision/api/v3/klines", f"{78174.27:,.2f}")

    assert message % args == "✅ Binance feed BTCUSDT bootstrapped 180 klines from https://data-api.binance.vision/api/v3/klines, last price $78,174.27"
    log.info(message, *args)
