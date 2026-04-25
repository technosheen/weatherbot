import types

import bot_v3


def test_live_startup_reports_wallet_without_overwriting_accounting(monkeypatch):
    saved = []
    monkeypatch.setattr(bot_v3, "load_state", lambda: {"balance": 95.68})
    monkeypatch.setattr(bot_v3, "save_state", lambda state: saved.append(state.copy()))
    monkeypatch.setattr(
        bot_v3,
        "clob_trader",
        types.SimpleNamespace(get_balance=lambda: 97.69),
        raising=False,
    )

    message = bot_v3.live_startup_balance_message()

    assert saved == []
    assert "wallet $97.69" in message
    assert "accounting $95.68" in message
