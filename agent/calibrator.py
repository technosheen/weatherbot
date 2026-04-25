"""Self-improving Bayesian calibrator.

Tracks win rates per (category, price_bucket, time_bucket) cell and
computes a calibration multiplier that scales the agent's confidence in
future bets in that cell.

A multiplier > 1 means the category/price historically performs better than
the market price implies → lower threshold, bet more aggressively.
A multiplier < 1 means underperformance → raise threshold.
"""

import json
import math
from pathlib import Path


CALIBRATION_PATH = Path(__file__).parent.parent / "data" / "agent" / "calibration.json"

PRICE_BUCKETS = [(0.65, 0.75), (0.75, 0.85), (0.85, 0.92), (0.92, 0.97), (0.97, 1.0)]
TIME_BUCKETS  = [(0, 6), (6, 24), (24, 48), (48, 72)]

# Minimum bets in a cell before we trust the calibration
MIN_BETS_FOR_SIGNAL = 8
# Bayesian prior: assume market is correctly priced until data says otherwise
PRIOR_WEIGHT = 5  # equivalent to 5 "virtual" bets at the market price


def price_bucket(p: float) -> str:
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "0.97-1.00"


def time_bucket(hours: float) -> str:
    for lo, hi in TIME_BUCKETS:
        if lo <= hours < hi:
            return f"{lo}h-{hi}h"
    return "48h-72h"


def _cell_key(category: str, p: float, hours: float) -> str:
    return f"{category}|{price_bucket(p)}|{time_bucket(hours)}"


def _load() -> dict:
    if CALIBRATION_PATH.exists():
        return json.loads(CALIBRATION_PATH.read_text())
    return {"cells": {}, "version": 1}


def _save(data: dict) -> None:
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps(data, indent=2))


def record_outcome(category: str, entry_price: float, hours_at_entry: float, won: bool) -> None:
    """Call after a market resolves. Persists the result."""
    data = _load()
    key = _cell_key(category, entry_price, hours_at_entry)
    cell = data["cells"].setdefault(key, {"bets": 0, "wins": 0})
    cell["bets"] += 1
    if won:
        cell["wins"] += 1
    _save(data)


def get_calibration_factor(category: str, entry_price: float, hours: float) -> float:
    """Return a multiplier for confidence.

    Factor > 1.0: we historically win more than the price implies → go bigger.
    Factor < 1.0: we historically underperform → be conservative.
    Returns 1.0 if insufficient data.
    """
    data = _load()
    key = _cell_key(category, entry_price, hours)
    cell = data["cells"].get(key)

    if not cell or cell["bets"] < MIN_BETS_FOR_SIGNAL:
        return 1.0

    # Bayesian estimate: blend actual win rate with prior (market price)
    prior_wins = entry_price * PRIOR_WEIGHT
    total = cell["bets"] + PRIOR_WEIGHT
    smoothed_win_rate = (cell["wins"] + prior_wins) / total

    # Factor = how much better (or worse) we do vs market price
    if entry_price <= 0:
        return 1.0
    factor = smoothed_win_rate / entry_price
    return round(max(0.3, min(3.0, factor)), 3)


def get_min_score_threshold(category: str) -> float:
    """Return a category-level minimum score threshold.

    Calibrated to the achievable score range (~0.001–0.015 with 2.5% base edge).
    Tightens for categories with poor historical win rates.
    """
    data = _load()
    cat_bets = cat_wins = 0
    for key, cell in data["cells"].items():
        if key.startswith(f"{category}|"):
            cat_bets += cell["bets"]
            cat_wins += cell["wins"]

    if cat_bets < MIN_BETS_FOR_SIGNAL:
        return 0.002  # default minimum quick-win score

    win_rate = cat_wins / cat_bets
    if win_rate < 0.60:
        return 0.006   # very conservative
    elif win_rate < 0.70:
        return 0.004
    elif win_rate > 0.80:
        return 0.001   # more aggressive
    return 0.002


def summary() -> list[dict]:
    data = _load()
    rows = []
    for key, cell in sorted(data["cells"].items()):
        bets = cell["bets"]
        wins = cell["wins"]
        win_rate = wins / bets if bets else 0
        cat, pb, tb = key.split("|")
        rows.append({
            "key": key,
            "category": cat,
            "price_bucket": pb,
            "time_bucket": tb,
            "bets": bets,
            "wins": wins,
            "win_rate": round(win_rate, 3),
            "factor": get_calibration_factor(cat, (float(pb.split("-")[0]) + float(pb.split("-")[1])) / 2, 1),
        })
    return rows
