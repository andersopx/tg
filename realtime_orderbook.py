"""Real-time Polymarket orderbook cache.

V14.1 turns the former placeholder into a production-safe local cache used by the
optional Polymarket Market WebSocket layer.  It remains a data-source cache only:
it does not touch wallet auth, balances, risk, strategies, or order placement.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import time


PriceLevel = Tuple[float, float]


@dataclass
class OrderbookState:
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    bids: List[PriceLevel] = field(default_factory=list)
    asks: List[PriceLevel] = field(default_factory=list)
    market: Optional[str] = None
    exchange_ts: Optional[float] = None
    last_trade_price: Optional[float] = None
    last_trade_side: Optional[str] = None
    tick_size: Optional[float] = None
    last_update_ts: float = 0.0
    last_event_type: str = ""


class RealtimeOrderbookCache:
    """In-memory orderbook cache keyed by Polymarket CLOB token id."""

    def __init__(self):
        self.books: Dict[str, OrderbookState] = {}

    # ---------- public read helpers ----------

    def get(self, token_id: str):
        return self.books.get(str(token_id)) if token_id else None

    def is_fresh(self, token_id: str, max_age_sec: float = 3.0) -> bool:
        st = self.get(token_id)
        return bool(st and st.last_update_ts and (time.time() - st.last_update_ts) <= float(max_age_sec))

    def summary(self, token_id: str, max_price: Optional[float] = None, max_age_sec: float = 3.0) -> Optional[dict]:
        """Return a Trader-compatible orderbook summary if the WS book is fresh."""
        st = self.get(token_id)
        if not st or not st.last_update_ts:
            return None
        age_sec = time.time() - st.last_update_ts
        if max_age_sec is not None and age_sec > float(max_age_sec):
            return None

        asks = list(st.asks or [])
        bids = list(st.bids or [])
        best_ask = st.best_ask if st.best_ask is not None else (asks[0][0] if asks else None)
        best_bid = st.best_bid if st.best_bid is not None else (bids[0][0] if bids else None)
        ask_size = sum(size for price, size in asks if best_ask is not None and price == best_ask) if best_ask is not None else 0.0
        cap = max_price if max_price is not None else best_ask
        capped_asks = [(p, s) for p, s in asks if cap is not None and p <= cap]
        depth_to_cap = sum(s for _, s in capped_asks)
        notional_to_cap = sum(p * s for p, s in capped_asks)
        weighted_avg = (notional_to_cap / depth_to_cap) if depth_to_cap > 0 else None
        spread = st.spread
        if spread is None and best_ask is not None and best_bid is not None:
            spread = round(max(0.0, float(best_ask) - float(best_bid)), 6)

        return {
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_size_at_best": ask_size,
            "ask_depth_to_cap": depth_to_cap,
            "weighted_avg_ask_to_cap": weighted_avg,
            "spread": spread,
            "asks": asks[:10],
            "bids": bids[:10],
            "source": "polymarket_ws",
            "age_sec": age_sec,
            "last_event_type": st.last_event_type,
            "market": st.market,
            "tick_size": st.tick_size,
        }

    # ---------- mutation helpers used by WS ----------

    def update_best(self, token_id: str, best_bid=None, best_ask=None, market: Optional[str] = None,
                    exchange_ts: Optional[Any] = None, event_type: str = "best_bid_ask"):
        st = self.books.get(str(token_id)) or OrderbookState(token_id=str(token_id))
        if best_bid is not None and str(best_bid) != "":
            st.best_bid = self._safe_float(best_bid)
        if best_ask is not None and str(best_ask) != "":
            st.best_ask = self._safe_float(best_ask)
        if st.best_bid is not None and st.best_ask is not None:
            st.spread = round(max(0.0, st.best_ask - st.best_bid), 6)
        if market:
            st.market = market
        st.exchange_ts = self._parse_exchange_ts(exchange_ts) if exchange_ts is not None else st.exchange_ts
        st.last_update_ts = time.time()
        st.last_event_type = event_type
        self.books[str(token_id)] = st
        return st

    def update_book(self, token_id: str, bids=None, asks=None, market: Optional[str] = None,
                    exchange_ts: Optional[Any] = None, event_type: str = "book"):
        st = self.books.get(str(token_id)) or OrderbookState(token_id=str(token_id))
        st.bids = self._normalise_levels(bids or [], reverse=True)
        st.asks = self._normalise_levels(asks or [], reverse=False)
        st.best_bid = st.bids[0][0] if st.bids else None
        st.best_ask = st.asks[0][0] if st.asks else None
        st.spread = round(max(0.0, st.best_ask - st.best_bid), 6) if (st.best_ask is not None and st.best_bid is not None) else None
        if market:
            st.market = market
        st.exchange_ts = self._parse_exchange_ts(exchange_ts) if exchange_ts is not None else st.exchange_ts
        st.last_update_ts = time.time()
        st.last_event_type = event_type
        self.books[str(token_id)] = st
        return st

    def apply_price_change(self, change: dict, market: Optional[str] = None, exchange_ts: Optional[Any] = None):
        token_id = str(change.get("asset_id") or change.get("assetId") or "")
        if not token_id:
            return None
        st = self.books.get(token_id) or OrderbookState(token_id=token_id)
        price = self._safe_float(change.get("price"))
        size = self._safe_float(change.get("size"), default=0.0)
        side = str(change.get("side") or "").upper()
        if price is not None:
            if side == "BUY":
                st.bids = self._upsert_level(st.bids, price, size, reverse=True)
            elif side == "SELL":
                st.asks = self._upsert_level(st.asks, price, size, reverse=False)
        if change.get("best_bid") is not None:
            st.best_bid = self._safe_float(change.get("best_bid"))
        elif st.bids:
            st.best_bid = st.bids[0][0]
        if change.get("best_ask") is not None:
            st.best_ask = self._safe_float(change.get("best_ask"))
        elif st.asks:
            st.best_ask = st.asks[0][0]
        if st.best_bid is not None and st.best_ask is not None:
            st.spread = round(max(0.0, st.best_ask - st.best_bid), 6)
        if market:
            st.market = market
        st.exchange_ts = self._parse_exchange_ts(exchange_ts) if exchange_ts is not None else st.exchange_ts
        st.last_update_ts = time.time()
        st.last_event_type = "price_change"
        self.books[token_id] = st
        return st

    def update_last_trade(self, msg: dict):
        token_id = str(msg.get("asset_id") or msg.get("assetId") or "")
        if not token_id:
            return None
        st = self.books.get(token_id) or OrderbookState(token_id=token_id)
        st.last_trade_price = self._safe_float(msg.get("price"))
        st.last_trade_side = str(msg.get("side") or "").upper() or None
        if msg.get("market"):
            st.market = msg.get("market")
        st.exchange_ts = self._parse_exchange_ts(msg.get("timestamp")) if msg.get("timestamp") is not None else st.exchange_ts
        st.last_update_ts = time.time()
        st.last_event_type = "last_trade_price"
        self.books[token_id] = st
        return st

    def update_tick_size(self, msg: dict):
        token_id = str(msg.get("asset_id") or msg.get("assetId") or "")
        if not token_id:
            return None
        st = self.books.get(token_id) or OrderbookState(token_id=token_id)
        st.tick_size = self._safe_float(msg.get("new_tick_size"))
        if msg.get("market"):
            st.market = msg.get("market")
        st.exchange_ts = self._parse_exchange_ts(msg.get("timestamp")) if msg.get("timestamp") is not None else st.exchange_ts
        st.last_update_ts = time.time()
        st.last_event_type = "tick_size_change"
        self.books[token_id] = st
        return st

    def apply_ws_message(self, msg: dict):
        """Apply a decoded Polymarket market-channel message to the cache."""
        if not isinstance(msg, dict):
            return None
        event_type = str(msg.get("event_type") or msg.get("type") or "").lower()
        if event_type == "book":
            return self.update_book(
                msg.get("asset_id") or msg.get("assetId"),
                bids=msg.get("bids") or [],
                asks=msg.get("asks") or [],
                market=msg.get("market"),
                exchange_ts=msg.get("timestamp"),
                event_type="book",
            )
        if event_type == "price_change":
            out = []
            for change in msg.get("price_changes") or []:
                out.append(self.apply_price_change(change, market=msg.get("market"), exchange_ts=msg.get("timestamp")))
            return out
        if event_type == "best_bid_ask":
            return self.update_best(
                msg.get("asset_id") or msg.get("assetId"),
                best_bid=msg.get("best_bid"),
                best_ask=msg.get("best_ask"),
                market=msg.get("market"),
                exchange_ts=msg.get("timestamp"),
                event_type="best_bid_ask",
            )
        if event_type == "last_trade_price":
            return self.update_last_trade(msg)
        if event_type == "tick_size_change":
            return self.update_tick_size(msg)
        return None

    # ---------- parsing internals ----------

    @staticmethod
    def _safe_float(value, default: Optional[float] = None) -> Optional[float]:
        try:
            if value is None or value == "":
                return default
            return float(str(value).strip())
        except Exception:
            return default

    @classmethod
    def _normalise_levels(cls, levels, reverse: bool) -> List[PriceLevel]:
        out = []
        for level in levels or []:
            if isinstance(level, dict):
                p = cls._safe_float(level.get("price"))
                s = cls._safe_float(level.get("size"), default=0.0)
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                p = cls._safe_float(level[0])
                s = cls._safe_float(level[1], default=0.0)
            else:
                p = cls._safe_float(getattr(level, "price", None))
                s = cls._safe_float(getattr(level, "size", None), default=0.0)
            if p is not None and s is not None and s > 0:
                out.append((p, s))
        out.sort(key=lambda x: x[0], reverse=reverse)
        return out

    @classmethod
    def _upsert_level(cls, levels: List[PriceLevel], price: float, size: float, reverse: bool) -> List[PriceLevel]:
        out = [(p, s) for p, s in (levels or []) if p != price]
        if size and size > 0:
            out.append((price, size))
        out.sort(key=lambda x: x[0], reverse=reverse)
        return out

    @staticmethod
    def _parse_exchange_ts(value) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            v = float(value)
            if v > 100000000000:  # milliseconds
                return v / 1000.0
            return v
        except Exception:
            return None
