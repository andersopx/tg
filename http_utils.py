"""HTTP client helpers shared by network integrations."""
import aiohttp


def client_session(**kwargs):
    """Create an aiohttp session that honors proxy/cert environment variables.

    The runtime container reaches the public internet through HTTP(S)_PROXY and
    SSL_CERT_FILE. aiohttp ignores those variables unless trust_env=True, while
    curl and many SDKs use them automatically. Keeping this in one helper avoids
    direct-connect failures in Binance/Polymarket code paths.
    """
    kwargs.setdefault("trust_env", True)
    return aiohttp.ClientSession(**kwargs)
