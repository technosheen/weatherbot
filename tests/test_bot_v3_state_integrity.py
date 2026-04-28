import json

import pytest

import bot_v3


def test_load_all_markets_fails_closed_on_corrupt_market_json(monkeypatch, tmp_path):
    markets_dir = tmp_path / "markets"
    markets_dir.mkdir()
    good = markets_dir / "good.json"
    bad = markets_dir / "bad.json"
    good.write_text(json.dumps({"city": "nyc", "date": "2099-01-01", "status": "open"}), encoding="utf-8")
    bad.write_text('{"city": ', encoding="utf-8")
    monkeypatch.setattr(bot_v3, "MARKETS_DIR", markets_dir)

    with pytest.raises(bot_v3.StateIntegrityError, match="bad.json"):
        bot_v3.load_all_markets()


def test_load_state_fails_closed_on_corrupt_state_json(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"balance": ', encoding="utf-8")
    monkeypatch.setattr(bot_v3, "STATE_FILE", state_file)

    with pytest.raises(bot_v3.StateIntegrityError, match="state.json"):
        bot_v3.load_state()


def test_load_state_fails_closed_on_wrong_top_level_type(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bot_v3, "STATE_FILE", state_file)

    with pytest.raises(bot_v3.StateIntegrityError, match="expected dict"):
        bot_v3.load_state()


def test_optional_learning_json_returns_default_on_corruption(monkeypatch, tmp_path):
    learned = tmp_path / "learned_params.json"
    learned.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(bot_v3, "LEARNED_PARAMS", learned)

    assert bot_v3._load_learned() == {}
