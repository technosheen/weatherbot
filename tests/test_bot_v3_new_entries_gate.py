import bot_v3


def _valid_signal():
    return {
        "market_id": "m1",
        "bucket_low": 70.0,
        "bucket_high": 71.0,
        "p": 0.65,
        "ev": 1.0,
        "entry_price": 0.25,
        "cost": 1.0,
        "kelly": 0.1,
        "forecast_src": "hrrr",
        "forecast_temp": 70.5,
        "ensemble_std": 2.0,
        "ensemble_n": 3,
        "sigma": 2.0,
    }


def test_new_entries_disabled_blocks_pre_trade_analysis(monkeypatch):
    monkeypatch.setattr(bot_v3, "NEW_ENTRIES_ENABLED", False, raising=False)
    monkeypatch.setattr(bot_v3, "BALANCE_FLOOR", 0.0)
    monkeypatch.setattr(bot_v3, "MAX_OPEN_POSITIONS", 99)
    monkeypatch.setattr(bot_v3, "MAX_UNREALIZED_LOSS", -999.0)
    monkeypatch.setattr(bot_v3, "get_learned_min_price", lambda: 0.0)
    monkeypatch.setattr(bot_v3, "load_state", lambda: {"balance": 100.0})
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: [])

    proceed, reason = bot_v3.analyze_signal(
        _valid_signal(),
        [{"market_id": "m1", "bid": 0.25, "price": 0.25, "range": (70.0, 71.0), "volume": 1000}],
        {"hrrr": 70.5, "ensemble_mean": 70.5},
        {"name": "Test City", "unit": "F"},
        "test-city",
        "2099-01-01",
        "D+1",
    )

    assert proceed is False
    assert "new entries disabled" in reason


def _open_market(shares, market_id="held"):
    return {
        "city": "other-city",
        "position": {
            "status": "open",
            "market_id": market_id,
            "shares": shares,
            "entry_price": 0.25,
        },
        "all_outcomes": [{"market_id": market_id, "bid": 0.25, "price": 0.25}],
    }


def test_open_position_cap_ignores_under_five_share_hold_to_resolution_positions(monkeypatch):
    monkeypatch.setattr(bot_v3, "NEW_ENTRIES_ENABLED", True, raising=False)
    monkeypatch.setattr(bot_v3, "BALANCE_FLOOR", 0.0)
    monkeypatch.setattr(bot_v3, "MAX_OPEN_POSITIONS", 10)
    monkeypatch.setattr(bot_v3, "MAX_UNREALIZED_LOSS", -999.0)
    monkeypatch.setattr(bot_v3, "get_learned_min_price", lambda: 0.0)
    monkeypatch.setattr(bot_v3, "load_state", lambda: {"balance": 100.0})
    # 14 dust/unsellable positions should be held to resolution but should not
    # consume trading slots. Four sellable positions count, so this is 4/10.
    markets = [_open_market(2.5, f"dust-{i}") for i in range(14)]
    markets += [_open_market(5.0, f"sellable-{i}") for i in range(4)]
    monkeypatch.setattr(bot_v3, "load_all_markets", lambda: markets)

    proceed, reason = bot_v3.analyze_signal(
        _valid_signal(),
        [
            {"market_id": "m1", "bid": 0.25, "price": 0.25, "range": (70.0, 71.0), "volume": 1000},
            {"market_id": "m2", "bid": 0.20, "price": 0.20, "range": (69.0, 70.0), "volume": 1000},
        ],
        {"hrrr": 70.5, "ensemble_mean": 70.5, "icon": 70.4},
        {"name": "Test City", "unit": "F"},
        "test-city",
        "2099-01-01",
        "D+1",
    )

    assert proceed is True
    assert reason == "approved"
