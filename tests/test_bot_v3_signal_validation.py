import bot_v3


def _signal(**overrides):
    signal = {
        "p": 0.75,
        "cost": 2.0,
        "entry_price": 0.25,
        "bid_at_entry": 0.24,
        "spread": 0.01,
        "shares": 8.0,
        "ev": 2.0,
    }
    signal.update(overrides)
    return signal


def test_repriced_signal_rejects_when_real_ask_drops_ev_below_minimum():
    signal = _signal(p=0.1915, cost=2.0)

    ok, reason = bot_v3.validate_repriced_signal(signal, real_ask=0.30, real_bid=0.29, min_ev=0.10)

    assert ok is False
    assert "EV" in reason
    assert "below min" in reason


def test_repriced_signal_rejects_when_share_count_would_be_too_small_to_sell():
    signal = _signal(p=0.7544, cost=1.0)

    ok, reason = bot_v3.validate_repriced_signal(signal, real_ask=0.39, real_bid=0.36, min_ev=0.10)

    assert ok is False
    assert "shares" in reason
    assert "below sell minimum" in reason


def test_repriced_signal_updates_price_spread_shares_and_ev_when_still_valid():
    signal = _signal(p=0.75, cost=2.0)

    ok, reason = bot_v3.validate_repriced_signal(signal, real_ask=0.30, real_bid=0.28, min_ev=0.10)

    assert ok is True
    assert reason is None
    assert signal["entry_price"] == 0.30
    assert signal["bid_at_entry"] == 0.28
    assert signal["spread"] == 0.02
    assert signal["shares"] == 6.67
    assert signal["ev"] == bot_v3.calc_ev(0.75, 0.30)
    assert signal["kelly"] == bot_v3.calc_kelly(0.75, 0.30)
