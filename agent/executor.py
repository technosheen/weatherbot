"""Live CLOB order execution via py_clob_client."""

import os

from agent.evaluator import Opportunity


def place_order(opp: Opportunity) -> bool:
    """Submit a market buy order. Returns True on success."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs

        pk = os.environ.get("PK", "")
        if not pk:
            print("  [ERROR] PK not set in .env")
            return False

        client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
        client.set_api_creds(client.create_or_derive_api_creds())

        token_id = opp.market.yes_token_id if opp.direction == "yes" else opp.market.no_token_id
        price = round(round(opp.entry_price / 0.01) * 0.01, 2)
        size = opp.bet_size

        order_args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        resp = client.create_and_post_order(order_args)

        if resp and resp.get("success"):
            print(f"  [LIVE] {opp.direction.upper()} ${size:.2f} @ {price:.3f} | {opp.market.question[:60]}")
            print(f"         order_id={resp.get('orderID', 'unknown')}")
            return True
        else:
            print(f"  [LIVE] Order failed: {resp}")
            return False
    except Exception as exc:
        print(f"  [LIVE] Exception: {exc}")
        return False
