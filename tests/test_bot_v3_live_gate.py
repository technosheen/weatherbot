import pytest

import bot_v3


def test_v3_live_trading_requires_probability_fix_confirmation(monkeypatch):
    monkeypatch.setattr(bot_v3, "RAW_LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "V3_LIVE_CONFIRMED", False)

    with pytest.raises(SystemExit) as exc:
        bot_v3.require_v3_live_confirmation()

    assert exc.value.code == 2


def test_v3_live_trading_can_be_explicitly_confirmed(monkeypatch):
    monkeypatch.setattr(bot_v3, "RAW_LIVE_TRADE", True)
    monkeypatch.setattr(bot_v3, "V3_LIVE_CONFIRMED", True)

    bot_v3.require_v3_live_confirmation()


def test_v3_paper_mode_does_not_require_confirmation(monkeypatch):
    monkeypatch.setattr(bot_v3, "RAW_LIVE_TRADE", False)
    monkeypatch.setattr(bot_v3, "V3_LIVE_CONFIRMED", False)

    bot_v3.require_v3_live_confirmation()
