"""
Polymarket CLOB client wrapper.

V6 changes:
- Uses market BUY amount for FOK/FAK, matching Polymarket docs.
- Confirms fills from post response and, when possible, get_order().
- Supports Gamma market metadata for price-to-beat and safer settlement.
"""
import json
import logging
import re
import time
import inspect
from types import SimpleNamespace
from typing import Optional, Any

from http_utils import client_session

try:
    # Polymarket official docs now point new integrations to the CLOB V2 client.
    # Do not silently fall back to the archived V1 SDK for real trading.
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2 import OrderArgs, OrderType, ApiCreds
    from py_clob_client_v2 import MarketOrderArgs
    try:
        from py_clob_client_v2 import PartialCreateOrderOptions as CreateOrderOptions
    except Exception:
        try:
            from py_clob_client_v2.clob_types import PartialCreateOrderOptions as CreateOrderOptions
        except Exception:
            CreateOrderOptions = None
    from py_clob_client_v2.order_builder.constants import BUY
    USING_V2 = True
except Exception:
    ClobClient = None
    OrderArgs = None
    ApiCreds = None
    MarketOrderArgs = None
    CreateOrderOptions = None
    BUY = "BUY"
    USING_V2 = False
    class OrderType:
        FOK = "FOK"
        FAK = "FAK"
        GTC = "GTC"
        GTD = "GTD"

from config import config
from database import Database

log = logging.getLogger(__name__)


class PolymarketClient:
    def __init__(self, db: Database, realtime_orderbook=None, market_ws=None):
        self.db = db
        self.client: Optional[ClobClient] = None
        self.connected = False
        self.realtime_orderbook = realtime_orderbook
        self.market_ws = market_ws
        self.using_v2 = USING_V2

    def attach_market_ws(self, market_ws):
        """Attach optional public market WebSocket without touching auth/API state."""
        self.market_ws = market_ws
        self.realtime_orderbook = getattr(market_ws, "cache", None)

    async def subscribe_market_orderbook(self, market: dict):
        """Subscribe both outcome token ids on the public market WS, if enabled."""
        if not config.REALTIME_ORDERBOOK_ENABLED or not self.market_ws or not market:
            return False
        tokens = [market.get("up_token_id"), market.get("down_token_id")]
        tokens = [str(t) for t in tokens if t]
        if not tokens:
            return False
        await self.market_ws.subscribe_assets(tokens)
        return True

    async def wait_for_realtime_orderbook(self, token_ids, timeout_sec: Optional[float] = None):
        if not config.REALTIME_ORDERBOOK_ENABLED or not self.market_ws:
            return False
        return await self.market_ws.wait_for_any_book(token_ids, timeout_sec if timeout_sec is not None else config.REALTIME_ORDERBOOK_WARMUP_SEC)


    @staticmethod
    def _field(obj, *names):
        """Read a field from SDK objects or dict-like payloads without assuming schema.

        py-clob-client-v2 and the public /book endpoint may expose order book
        sides as object attributes, dict keys, or aliases such as buys/sells.
        Missing fields are data absence, not an exception-worthy bot failure.
        """
        if obj is None:
            return None
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            try:
                return getattr(obj, name)
            except Exception:
                pass
        return None

    @classmethod
    def _side_entries(cls, book, side: str):
        if side == "asks":
            candidates = ("asks", "sells", "sell", "sell_orders", "offers")
        else:
            candidates = ("bids", "buys", "buy", "buy_orders")
        raw = cls._field(book, *candidates)
        if raw is None:
            return []
        if isinstance(raw, dict):
            # Some payloads wrap levels under data/items/orders.
            for key in ("data", "items", "orders", side):
                if isinstance(raw.get(key), list):
                    raw = raw.get(key)
                    break
        out = []
        for item in raw or []:
            price = cls._field(item, "price", "p")
            size = cls._field(item, "size", "s", "quantity", "qty")
            if price is None and isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            try:
                price_f = float(price)
                size_f = float(size)
            except Exception:
                continue
            if price_f > 0 and size_f > 0:
                out.append((price_f, size_f))
        return out

    @classmethod
    def _parse_orderbook_levels(cls, book):
        asks = sorted(cls._side_entries(book, "asks"), key=lambda x: x[0])
        bids = sorted(cls._side_entries(book, "bids"), key=lambda x: x[0], reverse=True)
        return asks, bids

    def connect_public(self):
        """Initialize CLOB client for public read calls where the SDK supports unauthenticated use."""
        if ClobClient is None:
            self.client = None
            self.connected = False
            log.warning("py_clob_client_v2 not installed; Gamma/Data API still available, orderbook via SDK disabled")
            return
        try:
            try:
                self.client = ClobClient(host=config.POLYMARKET_HOST, chain_id=config.POLYGON_CHAIN_ID)
            except TypeError:
                self.client = ClobClient(config.POLYMARKET_HOST)
            self.connected = False
            log.info("✅ Polymarket public client ready")
        except Exception as e:
            self.client = None
            self.connected = False
            log.warning("Polymarket public client init failed: %s", e)

    def connect(self):
        if ClobClient is None:
            raise RuntimeError("py_clob_client_v2 is not installed; run pip install -r requirements.txt")
        private_key = config.effective_polymarket_private_key
        funder = config.effective_polymarket_funder
        signature_type = config.effective_polymarket_signature_type
        if not private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY not configured (.env or Telegram)")
        if not funder:
            raise ValueError("POLYMARKET_FUNDER not configured (.env or Telegram)")

        # V2 SDK: signature_type=3 for Deposit Wallet.
        self.client = ClobClient(
            host=config.POLYMARKET_HOST,
            key=private_key,
            chain_id=config.POLYGON_CHAIN_ID,
            signature_type=signature_type,
            funder=funder,
        )

        creds = self.db.load_api_creds() if config.STORE_API_CREDS else None
        if creds:
            log.info("Loaded existing API credentials from database")
            api_creds = ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["passphrase"],
            )
        else:
            log.info("Deriving API credentials from private key...")
            api_creds = self.client.create_or_derive_api_key()
            if config.STORE_API_CREDS:
                self.db.save_api_creds(api_creds.api_key, api_creds.api_secret, api_creds.api_passphrase)
                log.info("API credentials saved to database")
            else:
                log.info("API credentials not stored (STORE_API_CREDS=false)")

        self.client.set_api_creds(api_creds)
        self.client.get_ok()
        self.connected = True
        log.info(f"✅ Polymarket client connected (CLOB {'V2' if USING_V2 else 'V1'}) and authenticated")
        self.log_sdk_diagnostics()

    # ============ Market Discovery ============

    async def find_updown_market(self, asset: str, window_ts: int, timeframe: str = "5m", *, allow_nearby: bool = False) -> Optional[dict]:
        """Find the exact crypto Up/Down market for BTC, ETH, SOL or XRP on 5m/15m."""
        asset = str(asset or "BTC").upper()
        prefix = config.asset_slug_prefix(asset)
        tf = config.timeframe_slug(timeframe)
        interval_sec = config.timeframe_seconds(tf)
        offsets = [0]
        radius = max(0, int(config.MARKET_SLUG_SEARCH_RADIUS)) if allow_nearby else 0
        for i in range(1, radius + 1):
            offsets.extend([-i * interval_sec, i * interval_sec])
        for off in offsets:
            slug = f"{prefix}-updown-{tf}-{window_ts + off}"
            market = await self.find_market_by_slug(slug)
            if market:
                slug_window_ts = self._window_ts_from_slug(market.get("slug", ""))
                market["asset"] = asset
                market["timeframe"] = tf
                market["market_interval_sec"] = interval_sec
                market["slug_window_ts"] = slug_window_ts
                market["window_offset_sec"] = (slug_window_ts - int(window_ts)) if slug_window_ts else None
                text = str(market.get("rules_text", "")).lower()
                # Be strict per asset when the rules text is present; if metadata is sparse, keep Chainlink-only check.
                if text:
                    keywords = {"BTC": ["btc", "bitcoin"], "ETH": ["eth", "ethereum"], "SOL": ["sol", "solana"], "XRP": ["xrp", "ripple"]}.get(asset, [asset.lower()])
                    market["rules_chainlink_ok"] = ("chainlink" in text and any(k in text for k in keywords))
                if off or market.get("window_offset_sec"):
                    log.warning("Using nearby %s %s market slug=%s offset=%ss", asset, tf, slug, market.get("window_offset_sec"))
                await self.subscribe_market_orderbook(market)
                return market
        return None

    async def find_5min_market(self, asset: str, window_ts: int, *, allow_nearby: bool = False) -> Optional[dict]:
        return await self.find_updown_market(asset, window_ts, "5m", allow_nearby=allow_nearby)

    async def find_btc_5min_market(self, window_ts: int, *, allow_nearby: bool = False) -> Optional[dict]:
        """Backward-compatible BTC helper."""
        return await self.find_updown_market("BTC", window_ts, "5m", allow_nearby=allow_nearby)

    async def find_market_by_slug(self, slug: str) -> Optional[dict]:
        try:
            async with client_session() as s:
                url = f"{config.POLYMARKET_GAMMA}/markets"
                params = {"slug": slug}
                async with s.get(url, params=params, timeout=5) as r:
                    if r.status != 200:
                        log.warning("Gamma market lookup failed status=%s slug=%s", r.status, slug)
                        return None
                    data = await r.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    if not markets:
                        return None
                    return self._parse_market(markets[0])
        except Exception as e:
            log.error(f"Find market error: {e}")
            return None

    def _parse_market(self, m: dict) -> dict:
        tokens = m.get("tokens") or []
        outcomes = self._loads_list(m.get("outcomes"))
        outcome_prices = self._loads_list(m.get("outcomePrices"))
        clob_ids = self._loads_list(m.get("clobTokenIds"))

        up_token = next((t for t in tokens if str(t.get("outcome", "")).lower() in ["up", "yes"]), None)
        down_token = next((t for t in tokens if str(t.get("outcome", "")).lower() in ["down", "no"]), None)

        # Gamma may omit the tokens array but expose parallel outcomes/clobTokenIds.
        # Never assume [0]=Up, [1]=Down unless outcomes are also missing; a reversal here would trade the wrong side.
        if (not up_token or not down_token) and clob_ids and len(clob_ids) >= 2:
            mapped = {}
            if outcomes and len(outcomes) == len(clob_ids):
                for outcome, token_id in zip(outcomes, clob_ids):
                    low = str(outcome).strip().lower()
                    if low in {"up", "yes"}:
                        mapped["up"] = token_id
                    elif low in {"down", "no"}:
                        mapped["down"] = token_id
            up_token = up_token or ({"token_id": mapped.get("up"), "outcome": "Up"} if mapped.get("up") else None)
            down_token = down_token or ({"token_id": mapped.get("down"), "outcome": "Down"} if mapped.get("down") else None)
            if (not up_token or not down_token) and not outcomes:
                up_token = up_token or {"token_id": clob_ids[0], "outcome": "Up"}
                down_token = down_token or {"token_id": clob_ids[1], "outcome": "Down"}

        outcome_price_map = {}
        if outcomes and outcome_prices and len(outcomes) == len(outcome_prices):
            for o, p in zip(outcomes, outcome_prices):
                try:
                    outcome_price_map[str(o).lower()] = float(p)
                except Exception:
                    pass

        question = m.get("question") or m.get("title") or ""
        text_blob = "\n".join(str(m.get(k, "")) for k in [
            "question", "title", "description", "resolutionSource", "rules", "umaResolutionData",
        ])

        tick = m.get("minimum_tick_size", m.get("minTickSize", m.get("orderMinTickSize", config.TICK_SIZE)))
        try:
            tick_size = float(tick)
        except Exception:
            tick_size = config.TICK_SIZE

        price_to_beat = self._extract_direct_price_to_beat(m)
        if price_to_beat is None:
            price_to_beat = self._extract_price_to_beat(text_blob)

        resolved_outcome = self._extract_resolved_outcome(m, outcome_price_map, text_blob)

        return {
            "id": m.get("id"),
            "condition_id": m.get("conditionId") or m.get("condition_id"),
            "slug": m.get("slug"),
            "question": question,
            "up_token_id": up_token.get("token_id") if up_token else None,
            "down_token_id": down_token.get("token_id") if down_token else None,
            "up_outcome_price": outcome_price_map.get("up", outcome_price_map.get("yes")),
            "down_outcome_price": outcome_price_map.get("down", outcome_price_map.get("no")),
            "tick_size": tick_size,
            "neg_risk": bool(m.get("negRisk", m.get("neg_risk", False))),
            "enable_order_book": bool(m.get("enableOrderBook", m.get("enable_order_book", True))),
            "active": bool(m.get("active", True)),
            "closed": bool(m.get("closed", False)),
            "archived": bool(m.get("archived", False)),
            "end_date": m.get("endDate") or m.get("end_date"),
            "price_to_beat": price_to_beat,
            "resolved_outcome": resolved_outcome,
            "rules_chainlink_ok": ("chainlink" in text_blob.lower()),
            "rules_text": text_blob,
            "volume_24h": self._safe_float(m.get("volume24hr") or m.get("volume_24hr") or m.get("volume24h") or 0),
            "liquidity": self._safe_float(m.get("liquidity") or 0),
            "raw": m,
        }

    @classmethod
    def _extract_resolved_outcome(cls, m: dict, outcome_price_map: dict, text_blob: str = "") -> Optional[str]:
        """Best-effort resolved outcome parser for Gamma objects after market close."""
        for key in ("winningOutcome", "winning_outcome", "winner", "resolvedOutcome", "resolved_outcome", "result", "resolution", "outcome"):
            val = m.get(key)
            if val is None:
                continue
            text = str(val).strip().lower()
            if text in {"up", "yes"}:
                return "Up"
            if text in {"down", "no"}:
                return "Down"

        up_p = outcome_price_map.get("up", outcome_price_map.get("yes"))
        down_p = outcome_price_map.get("down", outcome_price_map.get("no"))
        try:
            if up_p is not None and float(up_p) >= 0.99:
                return "Up"
            if down_p is not None and float(down_p) >= 0.99:
                return "Down"
        except Exception:
            pass

        # Some pages expose final outcome as text; keep this conservative.
        low = (text_blob or "").lower()
        if "final outcome was" in low:
            if "final outcome was \"up\"" in low or "final outcome was up" in low:
                return "Up"
            if "final outcome was \"down\"" in low or "final outcome was down" in low:
                return "Down"
        return None

    @classmethod
    def _extract_direct_price_to_beat(cls, m: dict) -> Optional[float]:
        """Gamma field names can shift. Prefer explicit fields over text regex."""
        keys = (
            "priceToBeat", "price_to_beat", "PriceToBeat",
            "openPrice", "openingPrice", "startPrice", "start_price",
            "referencePrice", "reference_price", "targetPrice",
        )
        for k in keys:
            v = m.get(k)
            n = cls._extract_first_price_number(v)
            if n:
                return n

        for container_key in ("gameData", "customProperties", "umaResolutionData", "metadata"):
            obj = m.get(container_key)
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except Exception:
                    obj = None
            if isinstance(obj, dict):
                for k in keys:
                    n = cls._extract_first_price_number(obj.get(k))
                    if n:
                        return n
        return None

    @staticmethod
    def _extract_first_price_number(value) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if 1000 <= v <= 1000000 else None
        text = str(value)
        m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?|[0-9]{4,7}(?:\.\d+)?)", text)
        if not m:
            return None
        try:
            v = float(m.group(1).replace(",", ""))
            return v if 1000 <= v <= 1000000 else None
        except Exception:
            return None

    @staticmethod
    def _loads_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return []
        return []

    @staticmethod
    def _extract_price_to_beat(text: str) -> Optional[float]:
        patterns = [
            r"Price to Beat[^$0-9]*\$?([0-9]{2,3},?[0-9]{3}(?:\.\d+)?)",
            r"opening [^$0-9]*\$?([0-9]{2,3},?[0-9]{3}(?:\.\d+)?)",
            r"beginning [^$0-9]*\$?([0-9]{2,3},?[0-9]{3}(?:\.\d+)?)",
            r"greater than or equal to [^$0-9]*\$?([0-9]{2,3},?[0-9]{3}(?:\.\d+)?)",
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except Exception:
                    pass
        return None

    # ============ Orderbook & Pricing ============

    def _get_realtime_orderbook_summary(self, token_id: str, max_price: Optional[float] = None) -> Optional[dict]:
        if not config.REALTIME_ORDERBOOK_ENABLED or not self.realtime_orderbook or not token_id:
            return None
        try:
            return self.realtime_orderbook.summary(token_id, max_price=max_price, max_age_sec=config.REALTIME_ORDERBOOK_MAX_AGE_SEC)
        except Exception as e:
            log.debug("Realtime orderbook cache miss/error token=%s error=%s", token_id, e)
            return None

    def get_token_price(self, token_id: str) -> Optional[float]:
        ws_ob = self._get_realtime_orderbook_summary(token_id)
        if ws_ob and ws_ob.get("best_ask") is not None:
            return ws_ob.get("best_ask")
        if not self.client:
            return None
        try:
            book = self.client.get_order_book(token_id)
            asks, _ = self._parse_orderbook_levels(book)
            return asks[0][0] if asks else None
        except Exception as e:
            log.warning("get_token_price orderbook parse failed token=%s: %s", str(token_id)[:12], e)
        return None

    def get_orderbook_summary(self, token_id: str, max_price: Optional[float] = None) -> Optional[dict]:
        ws_ob = self._get_realtime_orderbook_summary(token_id, max_price=max_price)
        if ws_ob and ws_ob.get("best_ask") is not None:
            return ws_ob
        if not self.client:
            return None
        try:
            book = self.client.get_order_book(token_id)
            if not book:
                return None
            asks, bids = self._parse_orderbook_levels(book)
            best_ask = asks[0][0] if asks else None
            best_bid = bids[0][0] if bids else None
            ask_size = sum(size for price, size in asks if price == best_ask) if best_ask is not None else 0.0
            cap = max_price if max_price is not None else best_ask
            capped_asks = [(p, s) for p, s in asks if cap is not None and p <= cap]
            depth_to_cap = sum(s for _, s in capped_asks)
            notional_to_cap = sum(p * s for p, s in capped_asks)
            weighted_avg = (notional_to_cap / depth_to_cap) if depth_to_cap > 0 else None
            if best_ask is None:
                # No ask levels in this particular book snapshot. Treat as unusable
                # liquidity and let the caller skip; do not raise/spam errors.
                log.debug("Orderbook has no ask levels token=%s source=rest_clob", str(token_id)[:12])
            summary = {
                "best_ask": best_ask,
                "best_bid": best_bid,
                "ask_size_at_best": ask_size,
                "ask_depth_to_cap": depth_to_cap,
                "weighted_avg_ask_to_cap": weighted_avg,
                "spread": (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None,
                "asks": asks[:10],
                "bids": bids[:10],
                "source": "rest_clob",
            }
            # Seed the WS cache from REST so a reconnect or first subscription has a baseline.
            if self.realtime_orderbook and token_id:
                try:
                    self.realtime_orderbook.update_book(token_id, bids=bids, asks=asks, event_type="rest_seed")
                except Exception:
                    pass
            return summary
        except Exception as e:
            log.warning("get_orderbook_summary parse failed token=%s: %s", str(token_id)[:12], e)
            return None



    async def fetch_prices_history(self, token_id: str, start_ts: Optional[int] = None,
                                   end_ts: Optional[int] = None, interval: Optional[str] = None,
                                   fidelity: int = 1) -> list:
        """Public CLOB price history. Param name is 'market' but it expects a token id."""
        if not token_id:
            return []
        params = {"market": token_id, "fidelity": fidelity}
        if start_ts:
            params["startTs"] = int(start_ts)
        if end_ts:
            params["endTs"] = int(end_ts)
        if interval:
            params["interval"] = interval
        try:
            async with client_session() as s:
                async with s.get(f"{config.POLYMARKET_HOST}/prices-history", params=params, timeout=8) as r:
                    if r.status != 200:
                        log.warning("prices-history failed status=%s token=%s", r.status, token_id)
                        return []
                    data = await r.json()
                    return data.get("history", data if isinstance(data, list) else []) or []
        except Exception as e:
            log.warning("prices-history error token=%s error=%s", token_id, e)
            return []

    async def fetch_recent_public_trades(self, market: dict, limit: int = 100) -> list:
        """Public Data API trades for a conditionId/market. Used for local sample collection."""
        condition_id = (market or {}).get("condition_id")
        slug = (market or {}).get("slug")
        params = {"limit": int(limit)}
        if condition_id:
            params["market"] = condition_id
        elif slug:
            params["slug"] = slug
        else:
            return []
        try:
            async with client_session() as s:
                async with s.get(f"{config.POLYMARKET_DATA_API}/trades", params=params, timeout=8) as r:
                    if r.status != 200:
                        log.warning("data-api trades failed status=%s market=%s", r.status, condition_id or slug)
                        return []
                    data = await r.json()
                    return data if isinstance(data, list) else data.get("data", []) or []
        except Exception as e:
            log.warning("data-api trades error market=%s error=%s", condition_id or slug, e)
            return []

    def build_market_snapshot(self, market: dict, btc_proxy_price: float = 0.0) -> dict:
        """One synchronous snapshot of both outcome books; cheap and safe enough for recorder."""
        up_ob = self.get_orderbook_summary(market.get("up_token_id"), None) if market.get("up_token_id") else None
        down_ob = self.get_orderbook_summary(market.get("down_token_id"), None) if market.get("down_token_id") else None
        return {
            "timestamp": int(time.time()),
            "window_ts": self._window_ts_from_slug(market.get("slug", "")),
            "asset": market.get("asset", "BTC"),
            "market_slug": market.get("slug", ""),
            "condition_id": market.get("condition_id", ""),
            "price_to_beat": market.get("price_to_beat") or 0,
            "btc_proxy_price": btc_proxy_price,
            "up_best_ask": (up_ob or {}).get("best_ask"),
            "up_best_bid": (up_ob or {}).get("best_bid"),
            "down_best_ask": (down_ob or {}).get("best_ask"),
            "down_best_bid": (down_ob or {}).get("best_bid"),
            "up_spread": (up_ob or {}).get("spread"),
            "down_spread": (down_ob or {}).get("spread"),
            "up_depth": (up_ob or {}).get("ask_depth_to_cap"),
            "down_depth": (down_ob or {}).get("ask_depth_to_cap"),
            "volume_24h": market.get("volume_24h", 0),
            "liquidity": market.get("liquidity", 0),
            "raw": {"up_ob": up_ob, "down_ob": down_ob, "rules_chainlink_ok": market.get("rules_chainlink_ok")},
        }

    @staticmethod
    def _window_ts_from_slug(slug: str) -> Optional[int]:
        m = re.search(r"(\d{10})$", str(slug or ""))
        return int(m.group(1)) if m else None

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(str(value).replace(",", ""))
        except Exception:
            return default

    def _make_order_options(self, tick_size: float, neg_risk: bool = False):
        """Build SDK-compatible order options with attribute access.

        Current py-clob-client releases may access options.tick_size and
        options.neg_risk. Passing a plain dict can fail before the order is
        even posted: "'dict' object has no attribute 'tick_size'".
        """
        tick_str = str(tick_size)
        neg = bool(neg_risk)
        if CreateOrderOptions is not None:
            for kwargs in (
                {"tick_size": tick_str, "neg_risk": neg},
                {"tick_size": tick_str},
            ):
                try:
                    return CreateOrderOptions(**kwargs)
                except Exception:
                    pass
        opt = SimpleNamespace(tick_size=tick_str, neg_risk=neg)
        # Compatibility aliases for code that may look for camelCase attributes.
        opt.tickSize = tick_str
        opt.negRisk = neg
        return opt

    def _sdk_option_variants(self, tick_size: float, neg_risk: bool = False):
        """Return safe option variants only.

        Do not include dict variants by default: the user observed 60/60
        order attempts failing because the SDK read .tick_size from a dict.
        The final None fallback is used only for SDK versions whose signature
        accepts a single order_args argument.
        """
        return [self._make_order_options(tick_size, neg_risk), None]

    def log_sdk_diagnostics(self):
        """Log the installed SDK signature/options support at startup."""
        try:
            cm = getattr(self.client.__class__, "create_market_order", None) if self.client else None
            co = getattr(self.client.__class__, "create_order", None) if self.client else None
            log.info("py_clob_client_v2 diagnostics: CreateOrderOptions=%s create_market_order=%s create_order=%s",
                     CreateOrderOptions, inspect.signature(cm) if cm else "n/a", inspect.signature(co) if co else "n/a")
        except Exception as e:
            log.warning("py_clob_client_v2 diagnostics failed: %s", e)

    def real_submit_guard_reason(self) -> str:
        """Return non-empty reason when live post_order should be blocked locally.

        v14.2.34: Deposit Wallet / signature_type=3 accounts can authenticate and
        read balances, but py_clob_client_v2 may submit orders whose signer does
        not match the derived API key. Guarding here prevents repeated 400s while
        Trader records a shadow trade for learning after all real gates pass.
        """
        if config.real_orders_enabled:
            if config.CLOB_V2_REQUIRE_SDK and not USING_V2:
                return "clob_v2_sdk_required"
            try:
                sig_type = int(config.effective_polymarket_signature_type)
            except Exception:
                sig_type = int(config.POLYMARKET_SIGNATURE_TYPE or 0)
            if USING_V2 and sig_type == 3 and not config.CLOB_V2_SIG3_REAL_SUBMIT_ENABLED:
                return "clob_v2_signature_type_3_signer_guard"
        return ""

    # ============ Order Placement ============

    def place_buy_order(
        self,
        token_id: str,
        price: float,
        shares: float,
        amount_usd: float,
        order_type: Optional[str] = None,
        tick_size: Optional[float] = None,
        neg_risk: bool = False,
    ) -> Optional[dict]:
        """Place a BUY order and normalize execution data."""
        order_type = (order_type or config.ORDER_TYPE).upper()
        tick_size = tick_size or config.TICK_SIZE
        worst_price = self._round_to_tick(min(0.99, price + config.MARKET_BUY_SLIPPAGE), tick_size)

        if amount_usd < config.MIN_MARKET_ORDER_USD and order_type in ("FOK", "FAK"):
            return {"success": False, "errorMsg": f"market order amount below ${config.MIN_MARKET_ORDER_USD:.2f}"}

        # Last-resort safety gate: even if a caller bypasses Trader's shadow/dry-run
        # path and invokes the client directly, never reach post_order unless the
        # full live arming conditions are enabled.
        if not bool(getattr(config, "real_orders_enabled", False)):
            return {"success": False, "errorMsg": "real_orders_disabled"}

        guard_reason = self.real_submit_guard_reason()
        if guard_reason:
            return {"success": False, "errorMsg": guard_reason}

        if not self.client:
            return {"success": False, "errorMsg": "Polymarket client not authenticated/initialized"}

        try:
            if order_type in ("FOK", "FAK") and MarketOrderArgs and hasattr(self.client, "create_market_order"):
                ot = getattr(OrderType, order_type, OrderType.FOK)
                mo_kwargs = {"token_id": token_id, "amount": round(amount_usd, 2), "side": BUY}
                try:
                    sig = inspect.signature(MarketOrderArgs)
                    params = set(sig.parameters.keys())
                except Exception:
                    params = set()
                if not params or "price" in params:
                    mo_kwargs["price"] = worst_price
                if "order_type" in params:
                    mo_kwargs["order_type"] = ot
                order_args = MarketOrderArgs(**mo_kwargs)
                signed_order = None
                last_create_error = None
                for opts in self._sdk_option_variants(tick_size, neg_risk):
                    try:
                        if opts is None:
                            signed_order = self.client.create_market_order(order_args)
                        else:
                            signed_order = self.client.create_market_order(order_args, opts)
                        break
                    except Exception as e:
                        last_create_error = e
                        log.debug("create_market_order option variant failed: %s", e)
                if signed_order is None:
                    raise last_create_error or RuntimeError("create_market_order returned no signed order")
            else:
                # Fallback for older SDK versions. Limit orders use share size, not dollar amount.
                if shares < config.MIN_LIMIT_ORDER_SHARES:
                    return {
                        "success": False,
                        "errorMsg": "SDK lacks MarketOrderArgs and estimated shares are below limit-order minimum; upgrade py-clob-client",
                    }
                order_args = OrderArgs(token_id=token_id, price=worst_price, size=shares, side=BUY)
                signed_order = None
                last_create_error = None
                for opts in self._sdk_option_variants(tick_size, neg_risk):
                    try:
                        if opts is None:
                            signed_order = self.client.create_order(order_args)
                        else:
                            signed_order = self.client.create_order(order_args, opts)
                        break
                    except Exception as e:
                        last_create_error = e
                        log.debug("create_order option variant failed: %s", e)
                if signed_order is None:
                    raise last_create_error or RuntimeError("create_order returned no signed order")

            ot = getattr(OrderType, order_type, OrderType.FOK)
            response = self.client.post_order(signed_order, ot)
            normalized = self._normalize_order_response(response, order_type, price, shares, amount_usd)
            log.info("📤 Order response: %s", normalized)
            return normalized
        except Exception as e:
            log.error(f"place_buy_order error: {e}")
            return {"success": False, "errorMsg": str(e)}

    def confirm_order_fill(self, order_id: str, fallback_price: float, fallback_cost: float) -> Optional[dict]:
        """Fetch order details and normalize matched size/cost if the API exposes it."""
        if not order_id:
            return None
        try:
            order = self.client.get_order(order_id)
            if not isinstance(order, dict):
                order = self._object_to_known_dict(order)
            norm = self._normalize_order_response(order, order.get("order_type", config.ORDER_TYPE), fallback_price, 0, fallback_cost)
            return norm
        except Exception as e:
            log.warning("get_order confirm failed order_id=%s error=%s", order_id, e)
            return None

    def _normalize_order_response(self, resp: Any, order_type: str, price: float, requested_shares: float, requested_cost: float) -> dict:
        if resp is None:
            return {"success": False, "errorMsg": "empty response"}
        if not isinstance(resp, dict):
            resp = self._object_to_known_dict(resp)

        status = str(resp.get("status", "") or "").lower()
        error_msg = resp.get("errorMsg") or resp.get("error") or ""
        success = bool(resp.get("success", False)) and not error_msg
        if status in {"matched", "live", "delayed", "unmatched"} and not error_msg:
            success = True

        making = self._amount_to_float(resp.get("makingAmount"))
        taking = self._amount_to_float(resp.get("takingAmount"))
        size_matched = self._amount_to_float(resp.get("size_matched") or resp.get("sizeMatched") or resp.get("matched_size"))
        order_price = self._amount_to_float(resp.get("price")) or price

        # For BUY matches from CLOB: makingAmount≈USDC spent, takingAmount≈shares received.
        if making > 0:
            filled_cost = making
        elif status == "matched" and order_type.upper() in ("FOK", "FAK"):
            # Market BUY uses dollar amount. For FOK, docs define this as spend-exactly-or-cancel.
            filled_cost = requested_cost
        elif status == "matched":
            filled_cost = requested_cost
        else:
            filled_cost = 0.0

        if taking > 0:
            filled_shares = taking
        elif size_matched > 0:
            filled_shares = size_matched
        elif filled_cost > 0 and order_price > 0:
            filled_shares = filled_cost / order_price
        elif status == "matched" and requested_shares > 0:
            filled_shares = requested_shares
        else:
            filled_shares = 0.0

        avg_price = (filled_cost / filled_shares) if filled_shares > 0 else (order_price or price)

        resp.update({
            "success": success,
            "order_type": order_type,
            "status": status,
            "filled_cost": filled_cost,
            "filled_shares": filled_shares,
            "avg_price": avg_price,
            "normalization_note": "confirmed" if (making > 0 or taking > 0 or size_matched > 0) else "estimated_or_unfilled",
        })
        return resp

    @staticmethod
    def _object_to_known_dict(obj: Any) -> dict:
        keys = [
            "success", "orderID", "id", "status", "makingAmount", "takingAmount", "errorMsg",
            "transactionsHashes", "tradeIDs", "size_matched", "sizeMatched", "original_size", "price", "order_type",
        ]
        return {k: getattr(obj, k) for k in keys if hasattr(obj, k)}

    @staticmethod
    def _amount_to_float(v) -> float:
        if v is None or v == "":
            return 0.0
        try:
            f = float(v)
            # REST fixed-math values often come as 6-decimal integers; SDK examples may already be decimals.
            return f / 1_000_000 if f > 100_000 else f
        except Exception:
            return 0.0

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return round(price, 3)
        precision = max(0, min(6, len(str(tick).split(".")[-1].rstrip("0")))) if "." in str(tick) else 0
        units = round(price / tick)
        return round(units * tick, precision)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id)
            return True
        except Exception as e:
            log.error(f"cancel_order error: {e}")
            return False

    def get_order_status(self, order_id: str) -> Optional[dict]:
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            log.error(f"get_order_status error: {e}")
            return None

    def get_open_orders_safe(self) -> list:
        """Return open orders across SDK method-name variants."""
        try:
            if hasattr(self.client, "get_open_orders"):
                orders = self.client.get_open_orders()
            elif hasattr(self.client, "get_orders"):
                orders = self.client.get_orders()
            else:
                return []
            if isinstance(orders, dict):
                orders = orders.get("data") or orders.get("orders") or []
            return list(orders or [])
        except Exception as e:
            log.warning("get_open_orders failed: %s", e)
            return []

    def cancel_open_orders_safe(self) -> int:
        """Best-effort startup safety: cancel stale resting orders left by a prior crash."""
        orders = self.get_open_orders_safe()
        cancelled = 0
        for o in orders:
            od = o if isinstance(o, dict) else self._object_to_known_dict(o)
            oid = od.get("id") or od.get("orderID")
            if not oid:
                continue
            if self.cancel_order(oid):
                cancelled += 1
        if cancelled:
            log.warning("Canceled %s existing open order(s) on startup", cancelled)
        return cancelled

    # ============ Account Info ============

    def get_balance(self):
        if not self.client:
            return None
        try:
            # Use V2 BalanceAllowanceParams type.
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

            # Query pUSD (COLLATERAL) balance - this is what CLOB uses for trading
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance = self.client.get_balance_allowance(params=params)
            log.debug(f"Raw balance response: {balance}")
            
            if isinstance(balance, dict):
                raw_balance = balance.get("balance", 0)
                pusd_balance = float(raw_balance) / 1e6
                log.info(f"pUSD balance: ${pusd_balance:.2f}")
                
                # If pUSD is 0, also check allowances for USDT/USDC
                if pusd_balance == 0 and "allowances" in balance:
                    allowances = balance.get("allowances", {})
                    # Check USDT on Polygon
                    usdt_address = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
                    usdt_balance = float(allowances.get(usdt_address, "0")) / 1e6
                    # Check USDC on Polygon  
                    usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                    usdc_balance = float(allowances.get(usdc_address, "0")) / 1e6
                    
                    log.debug(f"USDT allowance: ${usdt_balance:.2f}")
                    log.debug(f"USDC allowance: ${usdc_balance:.2f}")
                    
                    # Return max of all balances
                    max_balance = max(pusd_balance, usdt_balance, usdc_balance)
                    if max_balance > 0:
                        log.info(f"Available balance: ${max_balance:.2f}")
                        return max_balance
                
                return pusd_balance
            return float(balance) / 1e6
        except ImportError:
            # Fallback for older versions
            try:
                balance = self.client.get_balance_allowance()
                if isinstance(balance, dict):
                    return float(balance.get("balance", 0)) / 1e6
                return float(balance) / 1e6
            except Exception as e2:
                log.error(f"get_balance error (fallback): {e2}")
                return None
        except Exception as e:
            log.error(f"get_balance error: {e}")
            return None

    def get_positions(self) -> list:
        try:
            return self.client.get_positions()
        except Exception as e:
            log.error(f"get_positions error: {e}")
            return []
