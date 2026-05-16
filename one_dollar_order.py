"""
One-shot $1 Polymarket order attempt helper.

This script is intentionally separate from the automated strategy loop. It lets an
operator preview the current crypto Up/Down market, then submit exactly one $1
FOK/FAK BUY after the normal live arming switches are enabled.

Preview (no order):
    python3 one_dollar_order.py --asset BTC --side auto

Submit one $1 order (requires live arming in config/.env/runtime DB):
    python3 one_dollar_order.py --asset BTC --side auto --yes
"""
import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

from config import _normalize_mode, config
from database import Database
from polymarket_client import PolymarketClient

log = logging.getLogger("one_dollar_order")
ONE_DOLLAR_AMOUNT = 1.00


@dataclass
class OrderCandidate:
    asset: str
    timeframe: str
    window_ts: int
    market_slug: str
    outcome: str
    token_id: str
    best_ask: float
    best_bid: Optional[float]
    max_price: float
    estimated_shares: float
    estimated_cost: float
    available_notional: float
    tick_size: float
    neg_risk: bool


def current_window_ts(now_ts: Optional[int], timeframe: str) -> int:
    """Return the Polymarket crypto Up/Down window start for the supplied time."""
    now_i = int(now_ts if now_ts is not None else time.time())
    interval = int(config.timeframe_seconds(timeframe))
    return (now_i // interval) * interval


def estimate_market_buy_from_asks(asks: list, amount_usd: float, max_price: float) -> tuple[float, float, float]:
    """Estimate shares/cost available at asks no worse than max_price."""
    remaining = float(amount_usd)
    shares = 0.0
    cost = 0.0
    available_notional = 0.0
    for price, size in asks or []:
        price_f = float(price)
        size_f = float(size)
        if price_f <= 0 or size_f <= 0 or price_f > max_price:
            continue
        level_notional = price_f * size_f
        available_notional += level_notional
        if remaining <= 0:
            continue
        spend = min(remaining, level_notional)
        shares += spend / price_f
        cost += spend
        remaining -= spend
    return shares, cost, available_notional


def load_runtime_overrides(db: Database) -> None:
    """Mirror main.py runtime mode overrides without starting the full bot."""
    config.attach_runtime_state(db)
    saved_mode = db.get_state("mode", None)
    if saved_mode in ("paper", "small_live", "live", "test"):
        config.MODE = _normalize_mode(saved_mode)
        if config.MODE == "paper":
            config.DRY_RUN = True
    saved_dry_run = db.get_state("dry_run", None)
    if saved_dry_run is not None:
        config.DRY_RUN = bool(saved_dry_run)


def choose_candidate(
    pm: PolymarketClient,
    market: dict,
    *,
    asset: str,
    timeframe: str,
    window_ts: int,
    side: str,
    amount_usd: float = ONE_DOLLAR_AMOUNT,
) -> OrderCandidate:
    """Choose an order candidate and verify at least amount_usd is visible in the book."""
    side_norm = str(side or "auto").strip().lower()
    if side_norm not in {"auto", "up", "down"}:
        raise ValueError("--side must be auto, up, or down")

    options = []
    tick_size = float(market.get("tick_size") or config.TICK_SIZE)
    for outcome in ("Up", "Down"):
        if side_norm != "auto" and outcome.lower() != side_norm:
            continue
        token_id = market.get("up_token_id") if outcome == "Up" else market.get("down_token_id")
        if not token_id:
            continue
        initial = pm.get_orderbook_summary(token_id, None)
        best_ask = (initial or {}).get("best_ask")
        if best_ask is None:
            continue
        max_price = pm._round_to_tick(min(0.99, float(best_ask) + float(config.MARKET_BUY_SLIPPAGE)), tick_size)
        book = pm.get_orderbook_summary(token_id, max_price) or initial
        asks = (book or {}).get("asks") or []
        estimated_shares, estimated_cost, available_notional = estimate_market_buy_from_asks(asks, amount_usd, max_price)
        options.append(OrderCandidate(
            asset=str(asset or "BTC").upper(),
            timeframe=config.timeframe_slug(timeframe),
            window_ts=int(window_ts),
            market_slug=str(market.get("slug") or ""),
            outcome=outcome,
            token_id=str(token_id),
            best_ask=float(best_ask),
            best_bid=(float(book.get("best_bid")) if (book or {}).get("best_bid") is not None else None),
            max_price=float(max_price),
            estimated_shares=float(estimated_shares),
            estimated_cost=float(estimated_cost),
            available_notional=float(available_notional),
            tick_size=tick_size,
            neg_risk=bool(market.get("neg_risk", False)),
        ))

    if not options:
        raise RuntimeError("no usable Up/Down orderbook found for this market")
    options.sort(key=lambda c: c.best_ask)
    chosen = options[0]
    if chosen.estimated_cost + 1e-9 < amount_usd:
        raise RuntimeError(
            f"insufficient ask liquidity within slippage cap for {chosen.outcome}: "
            f"need ${amount_usd:.2f}, visible ${chosen.available_notional:.2f}"
        )
    return chosen


def record_matched_trade(db: Database, candidate: OrderCandidate, market: dict, response: dict) -> int:
    """Persist a matched manual order so normal settlement can pick it up later."""
    now = int(time.time())
    order_id = response.get("orderID") or response.get("id") or ""
    filled_cost = float(response.get("filled_cost") or candidate.estimated_cost or ONE_DOLLAR_AMOUNT)
    filled_shares = float(response.get("filled_shares") or candidate.estimated_shares or 0.0)
    avg_price = float(response.get("avg_price") or candidate.best_ask or 0.0)
    return db.insert_trade({
        "timestamp": now,
        "window_ts": candidate.window_ts,
        "asset": candidate.asset,
        "timeframe": candidate.timeframe,
        "market_slug": candidate.market_slug,
        "market_question": market.get("question", ""),
        "token_id": candidate.token_id,
        "direction": candidate.outcome,
        "entry_price": avg_price,
        "size": filled_cost,
        "shares": filled_shares,
        "cost": filled_cost,
        "filled_shares": filled_shares,
        "filled_cost": filled_cost,
        "avg_price": avg_price,
        "order_id": order_id,
        "order_status": str(response.get("status") or "matched").lower(),
        "model_probability": 0,
        "edge_after_fees": 0,
        "reference_price": market.get("price_to_beat") or 0,
        "is_shadow": 0,
        "signal_data": {"manual_one_dollar_attempt": True, "order_response": response},
        "status": "filled",
        "attempting_at": now,
        "submitted_at": now,
        "submitted_order_id": order_id,
        "submission_response_json": response,
        "filled_at": now,
        "first_fill_response_json": response,
        "last_status_check_at": now,
    })


async def attempt_one_dollar_order(
    db: Database,
    pm: PolymarketClient,
    *,
    asset: str = "BTC",
    timeframe: str = "5m",
    side: str = "auto",
    window_ts: int = 0,
    allow_nearby: bool = False,
    order_type: Optional[str] = None,
    submit: bool = False,
    connect_for_submit: bool = False,
) -> dict:
    """Preview or submit one $1 order using an already-initialized Polymarket client.

    ``submit=False`` only inspects the public market/orderbook and returns a
    candidate. ``submit=True`` continues through the live arming, balance and
    post_order path. The caller owns public/auth connection setup. The CLI passes
    ``connect_for_submit=True`` so it can preview with public endpoints first and
    authenticate only after the operator has supplied ``--yes``. The backend
    startup hook passes the already-authenticated client and leaves this false.
    """
    asset = str(asset or "BTC").upper()
    timeframe = config.timeframe_slug(timeframe)
    window_ts = int(window_ts or current_window_ts(None, timeframe))
    order_type = str(order_type or config.ORDER_TYPE).upper()

    market = await pm.find_updown_market(asset, window_ts, timeframe, allow_nearby=bool(allow_nearby))
    if not market:
        return {"ok": False, "submitted": False, "error": "market_not_found", "asset": asset, "timeframe": timeframe, "window_ts": window_ts}

    candidate = choose_candidate(pm, market, asset=asset, timeframe=timeframe, window_ts=window_ts, side=side)
    result = {
        "ok": True,
        "submitted": False,
        "mode": config.mode_display,
        "real_orders_enabled": bool(config.real_orders_enabled),
        "amount_usd": ONE_DOLLAR_AMOUNT,
        "order_type": order_type,
        "candidate": asdict(candidate),
        "safety_note": "preview only unless submit=True/--yes and all live arming switches pass",
    }

    if not submit:
        return result

    guard_reason = pm.real_submit_guard_reason()
    if not config.real_orders_enabled or guard_reason:
        result.update({
            "ok": False,
            "error": guard_reason or "real_orders_disabled",
            "arming_required": {
                "MODE": "small_live or live",
                "DRY_RUN": "false",
                "REAL_TRADING_ENABLED": "true",
                "OBSERVER_ONLY": "false",
                "CLOB_V2_SIG3_REAL_SUBMIT_ENABLED": "true when using signature_type=3 and you intentionally accept that submit path",
            },
        })
        return result

    if connect_for_submit:
        pm.connect()

    balance = pm.get_balance()
    if balance is None or float(balance) < ONE_DOLLAR_AMOUNT:
        result.update({"ok": False, "error": "insufficient_or_unavailable_balance", "balance": balance})
        return result

    db.mark_order_attempt(candidate.window_ts, candidate.market_slug, status="submitting", reason="manual_one_dollar_attempt", asset=asset, timeframe=timeframe)
    response = pm.place_buy_order(
        candidate.token_id,
        candidate.best_ask,
        candidate.estimated_shares,
        ONE_DOLLAR_AMOUNT,
        order_type,
        candidate.tick_size,
        candidate.neg_risk,
    ) or {}
    status = str(response.get("status") or ("matched" if response.get("success") else "failed")).lower()
    order_id = response.get("orderID") or response.get("id") or ""
    db.mark_order_attempt(candidate.window_ts, candidate.market_slug, order_id=order_id, status=status, reason="manual_one_dollar_attempt_response", asset=asset, timeframe=timeframe)

    result.update({"ok": bool(response.get("success")), "submitted": True, "response": response})
    if status == "matched":
        result["trade_id"] = record_matched_trade(db, candidate, market, response)
    return result


async def run(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH)
    load_runtime_overrides(db)
    pm = PolymarketClient(db)

    # Discovery and preview are intentionally public-only; do not derive API keys
    # or touch authenticated endpoints until after --yes and all arming gates pass.
    pm.connect_public()

    try:
        result = await attempt_one_dollar_order(
            db, pm,
            asset=args.asset, timeframe=args.timeframe, side=args.side, window_ts=args.window_ts,
            allow_nearby=bool(args.allow_nearby), order_type=args.order_type, submit=bool(args.yes), connect_for_submit=bool(args.yes),
        )
    except Exception as e:
        log.error("one-dollar order helper failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1

    if not args.yes and result.get("error") in {"real_orders_disabled", "clob_v2_signature_type_3_signer_guard", "clob_v2_sdk_required"}:
        # Preview mode should still be a successful dry inspection; leave the
        # arming failure visible but do not force a non-zero shell status.
        result["ok"] = True
        result["error"] = "preview_only"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.yes:
        return 0 if result.get("candidate") else 2
    return 0 if result.get("ok") and result.get("submitted") else 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or submit exactly one $1 Polymarket Up/Down BUY order.")
    parser.add_argument("--asset", default="BTC", choices=["BTC", "ETH", "SOL", "XRP"], help="Crypto asset market to use")
    parser.add_argument("--timeframe", default="5m", choices=["5m", "15m"], help="Polymarket Up/Down timeframe")
    parser.add_argument("--side", default="auto", choices=["auto", "up", "down"], help="Outcome to buy; auto chooses the cheaper ask")
    parser.add_argument("--window-ts", type=int, default=0, help="Exact market window timestamp; defaults to current window")
    parser.add_argument("--allow-nearby", action="store_true", help="Allow configured nearby slug search if exact market is absent")
    parser.add_argument("--order-type", default=config.ORDER_TYPE, choices=["FOK", "FAK"], help="Immediate market order type")
    parser.add_argument("--yes", action="store_true", help="Actually submit one $1 order after all live arming gates pass")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as e:
        log.error("one-dollar order helper failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
