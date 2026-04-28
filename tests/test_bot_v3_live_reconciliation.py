import types

import pytest

import bot_v3


def _market(order_id="0xlocal", status="open", needs_reconciliation=False, exit_order_id=None):
    pos = {
        "status": status,
        "order_id": order_id,
        "market_id": "123",
        "shares": 5.0,
        "cost": 1.0,
        "entry_price": 0.20,
        "needs_reconciliation": needs_reconciliation,
    }
    if exit_order_id:
        pos["exit_order_id"] = exit_order_id
    return {"city": "nyc", "date": "2099-01-01", "status": "open", "position": pos}


def test_live_reconciliation_blocks_unknown_clob_open_order(monkeypatch):
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [])
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [{"id": "0xlive", "status": "LIVE", "side": "BUY"}]),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="untracked live CLOB open order"):
        bot_v3.assert_live_reconciliation_safe()


def test_live_reconciliation_blocks_local_pending_order_even_without_clob_open_order(monkeypatch):
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [_market(status="pending_buy", needs_reconciliation=True)])
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "unknown"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="local reconciliation required"):
        bot_v3.assert_live_reconciliation_safe()


def test_live_reconciliation_allows_confirmed_filled_local_position(monkeypatch):
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [_market(status="open")])
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "filled"),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True


def test_live_reconciliation_is_noop_in_paper_mode(monkeypatch):
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", False)
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: (_ for _ in ()).throw(AssertionError("should not query CLOB"))),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True


def test_live_reconciliation_allows_wallet_reconciled_held_position_when_old_order_lookup_unknown(monkeypatch):
    market = _market(status="open", needs_reconciliation=True)
    market["position"].update(
        {
            "entry_status": "filled_wallet_reconciled",
            "wallet_reconciled_at": "2026-04-26T09:02:14+00:00",
            "wallet_shares": 7.7369,
            "reconciliation_reason": "wallet_holds_tokens_after_local_close",
        }
    )
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [market])
    monkeypatch.setattr(bot_v3, "save_market", lambda market: None)
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "unknown"),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True
    assert market["position"]["needs_reconciliation"] is False


def test_wallet_reconciled_positions_are_not_zombie_closed_when_market_is_closed():
    pos = {
        "status": "open",
        "entry_status": "filled_wallet_reconciled",
        "wallet_reconciled_at": "2026-04-28T11:34:24+00:00",
        "wallet_shares": 5.641,
    }

    assert bot_v3.should_skip_zombie_close({"status": "closed", "position": pos}) is True


def test_live_reconciliation_reopens_top_level_closed_wallet_reconciled_position(monkeypatch):
    market = _market(status="open", needs_reconciliation=False)
    market["status"] = "closed"
    market["position"].update(
        {
            "entry_status": "filled_wallet_reconciled",
            "wallet_reconciled_at": "2026-04-28T11:34:24+00:00",
            "wallet_shares": 5.641,
        }
    )
    saved = []
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [market])
    monkeypatch.setattr(bot_v3, "save_market", lambda m: saved.append(m.copy()))
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "unknown"),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True
    assert market["status"] == "open"
    assert market["position"]["status"] == "open"
    assert market["position"]["needs_reconciliation"] is False
    assert saved


def test_live_reconciliation_does_not_reopen_confirmed_wallet_sell(monkeypatch):
    market = _market(status="open", needs_reconciliation=False)
    market["status"] = "closed"
    market["position"].update(
        {
            "status": "closed",
            "entry_status": "filled_wallet_reconciled",
            "wallet_reconciled_at": "2026-04-28T12:10:00+00:00",
            "wallet_shares": 0,
            "exit_status": "filled_wallet_sell_confirmed",
            "close_reason": "manual_market_exit",
            "closed_at": "2026-04-27T18:27:00+00:00",
        }
    )
    saved = []
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [market])
    monkeypatch.setattr(bot_v3, "save_market", lambda m: saved.append(m.copy()))
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "unknown"),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True
    assert market["status"] == "closed"
    assert market["position"]["status"] == "closed"
    assert saved == []


def test_live_reconciliation_does_not_reopen_resolved_claimable_wallet_token(monkeypatch):
    market = _market(status="closed", needs_reconciliation=False)
    market["status"] = "resolved"
    market["resolved_outcome"] = "win"
    market["position"].update(
        {
            "entry_status": "filled_wallet_reconciled",
            "wallet_reconciled_at": "2026-04-28T12:17:49+00:00",
            "wallet_shares": 19.7955,
            "close_reason": "resolved",
            "closed_at": "2026-04-28T12:17:49+00:00",
        }
    )
    saved = []
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [market])
    monkeypatch.setattr(bot_v3, "save_market", lambda m: saved.append(m.copy()))
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_open_orders=lambda: [], get_order_status=lambda order_id: "unknown"),
        raising=False,
    )

    assert bot_v3.assert_live_reconciliation_safe() is True
    assert market["status"] == "resolved"
    assert market["position"]["status"] == "closed"
    assert saved == []


def test_market_close_cutoff_does_not_hide_open_wallet_reconciled_position():
    market = _market(status="open", needs_reconciliation=False)
    market["position"].update(
        {
            "entry_status": "filled_wallet_reconciled",
            "wallet_reconciled_at": "2026-04-28T12:10:00+00:00",
            "wallet_shares": 3.0,
        }
    )

    assert bot_v3.should_mark_market_closed_for_no_new_entries(market, hours=0.0) is False


def test_market_close_cutoff_can_close_market_without_open_position():
    market = {"status": "open", "position": {"status": "closed"}}

    assert bot_v3.should_mark_market_closed_for_no_new_entries(market, hours=0.0) is True


def test_prepare_live_exit_does_not_local_close_wallet_reconciled_unsellable_position(monkeypatch):
    pos = {
        "status": "open",
        "entry_status": "filled_wallet_reconciled",
        "wallet_reconciled_at": "2026-04-28T11:34:24+00:00",
        "wallet_shares": 4.0,
        "shares": 4.0,
        "token_id": "123",
        "entry_price": 0.22,
        "cost": 0.88,
    }
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(place_sell=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not sell unsellable position"))),
        raising=False,
    )

    assert bot_v3.prepare_live_exit(pos, 0.95) is False


def test_prepare_live_exit_places_sell_for_wallet_reconciled_sellable_position(monkeypatch):
    pos = {
        "status": "open",
        "entry_status": "filled_wallet_reconciled",
        "wallet_reconciled_at": "2026-04-28T11:34:24+00:00",
        "wallet_shares": 5.2699,
        "shares": 5.2699,
        "token_id": "456",
        "entry_price": 0.19,
        "cost": 1.0,
    }
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    calls = []
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(place_sell=lambda token_id, price, shares: calls.append((token_id, price, shares)) or {"order_id": "0xsell"}),
        raising=False,
    )

    assert bot_v3.prepare_live_exit(pos, 0.05) is False
    assert calls == [("456", 0.05, 5.2699)]
    assert pos["exit_order_id"] == "0xsell"
    assert pos["exit_status"] == "open"
