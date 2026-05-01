#!/usr/bin/env python3
"""
clob_trader.py — Polymarket CLOB execution wrapper for bot_v3
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from py_clob_client_v2 import (
    ClobClient, ApiCreds, BalanceAllowanceParams, AssetType,
    OrderArgs, OrderType, OrderPayload, Side,
)

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137

_client = None


def _make_client() -> ClobClient:
    pk       = os.getenv("PK")
    wallet   = os.getenv("WALLET")
    sig_type = int(os.getenv("SIG_TYPE", "0"))
    api_key  = os.getenv("CLOB_API_KEY")
    secret   = os.getenv("CLOB_SECRET")
    passph   = os.getenv("CLOB_PASSPHRASE")
    if not all([pk, wallet, api_key, secret, passph]):
        raise RuntimeError("Missing CLOB credentials — check .env")
    creds = ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passph)
    return ClobClient(HOST, key=pk, chain_id=CHAIN_ID,
                      creds=creds, signature_type=sig_type, funder=wallet)


def get_client() -> ClobClient:
    global _client
    if _client is None:
        _client = _make_client()
    return _client


def get_balance() -> float:
    """Returns spendable USDC collateral balance in dollars."""
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    result = get_client().get_balance_allowance(params)
    return int(result["balance"]) / 1_000_000


MIN_SELL_SHARES = 5.0


def place_buy(token_id: str, price: float, size_usd: float) -> dict | None:
    """
    Place a GTC limit buy order for YES-outcome tokens.

    token_id  : clobTokenIds[0] from the market (YES token)
    price     : limit price per share (0.01 – 0.99)
    size_usd  : dollars to spend

    Returns {"order_id", "token_id", "price", "shares", "cost"} or None on failure.
    """
    import math
    price  = round(price, 2)
    # ceil to 2dp so price*shares never rounds below size_usd (avoids <$1 rejections)
    shares = math.ceil(size_usd / price * 100) / 100
    if shares < MIN_SELL_SHARES:
        print(f"  [CLOB] place_buy skipped: shares {shares} below future sell minimum {MIN_SELL_SHARES}")
        return None
    if shares < 0.01 or price <= 0 or price >= 1:
        return None

    order_args = OrderArgs(token_id=str(token_id), price=price, size=shares, side=Side.BUY)
    try:
        signed = get_client().create_order(order_args)
        result = get_client().post_order(signed, OrderType.GTC)
        if result and result.get("success"):
            return {
                "order_id": result["orderID"],
                "token_id": token_id,
                "price":    price,
                "shares":   shares,
                "cost":     round(price * shares, 2),
            }
        print(f"  [CLOB] Order rejected: {result}")
        return None
    except Exception as e:
        print(f"  [CLOB] place_buy failed: {e}")
        return None


def place_sell(token_id: str, price: float, shares: float) -> dict | None:
    """
    Place a GTC limit sell order to exit a filled YES position.

    price  : minimum price to accept per share
    shares : number of shares to sell
    """
    price = round(price, 2)
    if shares < MIN_SELL_SHARES:
        print(f"  [CLOB] place_sell skipped: shares {shares} below minimum {MIN_SELL_SHARES}")
        return None
    if price <= 0 or price >= 1:
        return None

    order_args = OrderArgs(token_id=str(token_id), price=price, size=shares, side=Side.SELL)
    try:
        signed = get_client().create_order(order_args)
        result = get_client().post_order(signed, OrderType.GTC)
        if result and result.get("success"):
            return {"order_id": result["orderID"], "price": price, "shares": shares}
        print(f"  [CLOB] Sell rejected: {result}")
        return None
    except Exception as e:
        print(f"  [CLOB] place_sell failed: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel a single open order. Returns True only when CLOB confirms success."""
    try:
        result = get_client().cancel_order(OrderPayload(orderID=order_id))
        if result is None:
            print(f"  [CLOB] cancel_order {order_id}: empty response")
            return False
        if result is True:
            return True
        if isinstance(result, dict):
            if result.get("success") is True:
                return True
            canceled = result.get("canceled") or result.get("cancelled") or []
            not_canceled = result.get("not_canceled") or result.get("not_cancelled") or {}
            if str(order_id) in {str(x) for x in canceled} and str(order_id) not in {str(x) for x in not_canceled}:
                return True
            if result.get("success") is False or not_canceled:
                print(f"  [CLOB] cancel_order rejected: {result}")
                return False
        print(f"  [CLOB] cancel_order unrecognized response: {result}")
        return False
    except Exception as e:
        print(f"  [CLOB] cancel_order {order_id}: {e}")
        return False


def get_order_status(order_id: str) -> str:
    """
    Returns: 'open', 'filled', 'partial', 'cancelled', or 'unknown'.

    Fail closed on future/unmapped CLOB statuses. Unknown must never be treated
    as open/filled by callers because that can hide lost exposure or fake fills.
    """
    try:
        result = get_client().get_order(order_id)
        if not result:
            return "unknown"
        status = (result.get("status") or "").lower().replace("-", "_").replace(" ", "_")

        filled = result.get("filled_size") or result.get("size_matched") or result.get("matched_size")
        original = result.get("original_size") or result.get("size") or result.get("order_size")
        try:
            filled_f = float(filled) if filled is not None else None
            original_f = float(original) if original is not None else None
        except (TypeError, ValueError):
            filled_f = original_f = None

        if status in ("matched", "filled"):
            if filled_f is not None and original_f is not None and filled_f + 1e-9 < original_f:
                return "partial"
            return "filled"
        if status in ("partially_filled", "partial", "partially_matched"):
            return "partial"
        if filled_f is not None and filled_f > 0 and original_f is not None and filled_f + 1e-9 < original_f:
            return "partial"
        if status in ("cancelled", "canceled", "expired"):
            return "cancelled"
        if status in ("open", "live", "active", "unmatched", "pending"):
            return "open"
        return "unknown"
    except Exception as e:
        print(f"  [CLOB] get_order_status {order_id}: {e}")
        return "unknown"


def get_open_orders() -> list:
    """Returns all open orders on the account."""
    try:
        return get_client().get_open_orders() or []
    except Exception as e:
        print(f"  [CLOB] get_open_orders: {e}")
        return []
