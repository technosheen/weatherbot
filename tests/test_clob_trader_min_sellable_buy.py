import clob_trader


class _FakeClient:
    def __init__(self):
        self.created = []

    def create_order(self, order_args):
        self.created.append(order_args)
        return "signed-order"

    def post_order(self, signed, order_type):
        return {"success": True, "orderID": "0xorder"}


def test_place_buy_rejects_orders_that_would_create_unsellable_share_count(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(clob_trader, "get_client", lambda: fake)

    assert clob_trader.place_buy("token-1", price=0.39, size_usd=1.00) is None
    assert fake.created == []
