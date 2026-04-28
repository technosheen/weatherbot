import json
from pathlib import Path

import bot_v3


def test_data_dir_is_repo_relative_not_cwd():
    repo = Path(bot_v3.__file__).resolve().parent

    assert bot_v3.DATA_DIR == repo / "data"
    assert bot_v3.STATE_FILE == repo / "data" / "state.json"
    assert bot_v3.MARKETS_DIR == repo / "data" / "markets"


def test_atomic_json_write_replaces_with_valid_json(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"balance": 1}', encoding="utf-8")

    bot_v3.atomic_json_write(target, {"balance": 2, "nested": {"ok": True}})

    assert json.loads(target.read_text(encoding="utf-8")) == {"balance": 2, "nested": {"ok": True}}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_write_preserves_existing_file_if_serialization_fails(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"balance": 1}', encoding="utf-8")

    class NotJsonSerializable:
        pass

    try:
        bot_v3.atomic_json_write(target, {"bad": NotJsonSerializable()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected serialization to fail")

    assert target.read_text(encoding="utf-8") == '{"balance": 1}'
    assert list(tmp_path.glob("*.tmp")) == []
