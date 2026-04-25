import json
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")


def load_crypto_config(path: str | None = None) -> dict:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with config_path.open(encoding="utf-8") as handle:
        return json.load(handle)
