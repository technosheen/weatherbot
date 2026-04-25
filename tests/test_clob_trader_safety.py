import types

import clob_trader


class FakeClient:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def cancel(self, order_id):
        self.calls.append(order_id)
        if self.exc:
            raise self.exc
        return self.result


def test_place_sell_rejects_below_clob_minimum_without_creating_order(monkeypatch):
    monkeypatch.setattr(
        clob_trader,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("should not create CLOB order")),
    )

    assert clob_trader.place_sell("token", 0.29, 3.22) is None


def test_cancel_order_accepts_success_true(monkeypatch):
    client = FakeClient({"success": True})
    monkeypatch.setattr(clob_trader, "get_client", lambda: client)

    assert clob_trader.cancel_order("0xorder") is True


def test_cancel_order_accepts_canceled_list(monkeypatch):
    client = FakeClient({"canceled": ["0xorder"], "not_canceled": {}})
    monkeypatch.setattr(clob_trader, "get_client", lambda: client)

    assert clob_trader.cancel_order("0xorder") is True


def test_cancel_order_rejects_not_canceled_response(monkeypatch):
    client = FakeClient({"canceled": [], "not_canceled": {"0xorder": "not open"}})
    monkeypatch.setattr(clob_trader, "get_client", lambda: client)

    assert clob_trader.cancel_order("0xorder") is False


def test_cancel_order_rejects_explicit_failure(monkeypatch):
    client = FakeClient({"success": False, "error": "nope"})
    monkeypatch.setattr(clob_trader, "get_client", lambda: client)

    assert clob_trader.cancel_order("0xorder") is False
