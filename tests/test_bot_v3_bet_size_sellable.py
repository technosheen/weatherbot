import bot_v3
import math


def test_bet_size_overrides_max_bet_when_needed_for_sellable_shares():
    """At ask=0.20, 5 shares cost ceil(5*0.20*100)/100 = $1.00.
       Kelly raw = $1.20, MAX_BET = $0.80 → bet_size should bump to $1.00."""
    s = bot_v3.bet_size(kelly=0.012, balance=100, entry_price=0.20)
    assert s >= 1.00
    # Verify the resulting shares would be sellable
    sizing = bot_v3.clob_buy_sizing(0.20, s)
    assert sizing["sellable"] is True


def test_bet_size_honors_max_bet_when_already_sellable(monkeypatch):
    """At ask=0.45, 5 shares cost ceil(5*0.45*100)/100 = $2.25.
       Kelly raw = $3.00, MAX_BET = $3.00 → returns $3.00 (already >= min)."""
    monkeypatch.setattr(bot_v3, "MAX_BET", 3.0)
    s = bot_v3.bet_size(kelly=0.03, balance=100, entry_price=0.45)
    assert s == 3.00


def test_bet_size_cap_does_not_override_when_kelly_too_small():
    """At ask=0.45, 5 shares cost $2.25.
       Kelly raw = $1.50, MAX_BET = $3.00 → capped $1.50 still < $2.25.
       Should return capped $1.50 so downstream analyze_signal can reject."""
    s = bot_v3.bet_size(kelly=0.015, balance=100, entry_price=0.45)
    assert s == 1.50
    sizing = bot_v3.clob_buy_sizing(0.45, s)
    assert sizing["sellable"] is False


def test_bet_size_returns_capped_for_out_of_range_price():
    """Invalid entry_price bypasses share check and returns raw capped value."""
    s = bot_v3.bet_size(kelly=0.01, balance=100, entry_price=0.0)
    assert s == round(min(0.01 * 100, bot_v3.MAX_BET), 2)
