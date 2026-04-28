import types

import bot_v3


def _market():
    return {
        "city": "nyc",
        "city_name": "NYC",
        "date": "2099-01-01",
        "status": "open",
        "position": {
            "status": "open",
            "order_id": "0xbuy",
            "market_id": "123",
            "entry_price": 0.20,
            "cost": 1.00,
            "shares": 5.0,
            "forecast_src": "ecmwf",
        },
    }


def _minimal_scan_patches(monkeypatch, market, saved_markets, saved_states):
    monkeypatch.setattr(bot_v3, "LOCATIONS", {})
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [market])
    monkeypatch.setattr(bot_v3, "load_state", lambda: {"balance": 10.0, "total_trades": 1, "wins": 0, "losses": 0, "peak_balance": 10.0})
    monkeypatch.setattr(bot_v3, "save_market", lambda m: saved_markets.append(m.copy()))
    monkeypatch.setattr(bot_v3, "save_state", lambda s: saved_states.append(s.copy()))
    monkeypatch.setattr(bot_v3, "BALANCE_FLOOR", 0.0)
    monkeypatch.setattr(bot_v3, "CALIBRATION_MIN", 999999)
    monkeypatch.setattr(bot_v3, "require_v3_live_confirmation", lambda: None)


def test_live_resolution_blocks_when_entry_order_is_not_confirmed_filled(monkeypatch):
    market = _market()
    saved_markets = []
    saved_states = []
    _minimal_scan_patches(monkeypatch, market, saved_markets, saved_states)
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", types.SimpleNamespace(get_order_status=lambda order_id: "open"), raising=False)
    monkeypatch.setattr(
        bot_v3,
        "check_market_resolved",
        lambda market_id: (_ for _ in ()).throw(AssertionError("should not resolve unfilled live order")),
    )

    assert bot_v3.scan_and_update() == (0, 0, 0)

    assert market["position"]["needs_reconciliation"] is True
    assert market["position"]["entry_status"] == "open"
    assert market["status"] == "open"
    assert saved_markets
    assert saved_states[-1]["balance"] == 10.0
    assert saved_states[-1]["wins"] == 0
    assert saved_states[-1]["losses"] == 0


def test_live_resolution_refunds_cancelled_unfilled_entry_without_learning_label(monkeypatch):
    market = _market()
    saved_markets = []
    saved_states = []
    _minimal_scan_patches(monkeypatch, market, saved_markets, saved_states)
    monkeypatch.setattr(bot_v3, "LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "clob_trader", types.SimpleNamespace(get_order_status=lambda order_id: "cancelled"), raising=False)
    monkeypatch.setattr(
        bot_v3,
        "check_market_resolved",
        lambda market_id: (_ for _ in ()).throw(AssertionError("cancelled entry is not a settled bet")),
    )

    assert bot_v3.scan_and_update() == (0, 0, 0)

    assert market["position"]["status"] == "closed"
    assert market["position"]["exit_status"] == "buy_cancelled"
    assert market["position"]["pnl"] == 0.0
    assert market["status"] == "open"
    assert saved_states[-1]["balance"] == 11.0
    assert saved_states[-1]["wins"] == 0
    assert saved_states[-1]["losses"] == 0
