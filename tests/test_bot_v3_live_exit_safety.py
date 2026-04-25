import types

import bot_v3


def _position(**overrides):
    pos = {
        "order_id": "0xbuy",
        "token_id": "token-1",
        "shares": 5.5,
        "entry_price": 0.50,
        "cost": 1.50,
        "status": "open",
    }
    pos.update(overrides)
    return pos


def test_live_exit_places_sell_but_keeps_local_position_open_until_exit_fills(monkeypatch):
    calls = []
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "filled",
        place_sell=lambda token_id, price, shares: calls.append((token_id, price, shares))
        or {"order_id": "0xsell", "price": price, "shares": shares},
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    pos = _position()
    assert bot_v3.prepare_live_exit(pos, 0.40) is False
    assert calls == [("token-1", 0.40, 5.5)]
    assert pos["exit_order_id"] == "0xsell"
    assert pos["exit_status"] == "open"


def test_live_exit_allows_local_close_after_exit_order_is_filled(monkeypatch):
    checked = []
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: checked.append(order_id) or "filled",
        place_sell=lambda token_id, price, shares: None,
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    pos = _position(exit_order_id="0xsell", exit_status="open")
    assert bot_v3.prepare_live_exit(pos, 0.40) is True
    assert checked == ["0xsell"]
    assert pos["exit_status"] == "filled"


def test_live_exit_waits_when_existing_exit_order_is_still_open(monkeypatch):
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "open",
        place_sell=lambda token_id, price, shares: None,
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    pos = _position(exit_order_id="0xsell", exit_status="open")
    assert bot_v3.prepare_live_exit(pos, 0.40) is False
    assert pos["exit_status"] == "open"


def test_live_exit_blocks_when_filled_position_is_below_clob_sell_minimum(monkeypatch):
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "filled",
        place_sell=lambda token_id, price, shares: (_ for _ in ()).throw(AssertionError("should not sell")),
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    assert bot_v3.prepare_live_exit(_position(shares=3.22), 0.29) is False


def test_live_exit_blocks_local_close_when_sell_fails(monkeypatch):
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "filled",
        place_sell=lambda token_id, price, shares: None,
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    assert bot_v3.prepare_live_exit(_position(), 0.40) is False


def test_live_exit_cancels_open_buy_order_before_local_close(monkeypatch):
    calls = []
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "open",
        place_sell=lambda token_id, price, shares: None,
        cancel_order=lambda order_id: calls.append(order_id) or True,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    pos = _position()
    assert bot_v3.prepare_live_exit(pos, 0.40) is True
    assert calls == ["0xbuy"]
    assert pos["exit_status"] == "buy_cancelled"
    assert bot_v3.calculate_exit_pnl(pos, 0.40) == 0.0


def test_live_exit_blocks_local_close_when_cancel_fails(monkeypatch):
    fake_clob = types.SimpleNamespace(
        get_order_status=lambda order_id: "open",
        place_sell=lambda token_id, price, shares: None,
        cancel_order=lambda order_id: False,
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", fake_clob, raising=False)

    assert bot_v3.prepare_live_exit(_position(), 0.40) is False


def test_paper_exit_allows_local_close_without_clob(monkeypatch):
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", False)

    assert bot_v3.prepare_live_exit(_position(order_id=None, token_id=None), 0.40) is True
