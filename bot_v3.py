#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet.py — Weather Trading Bot for Polymarket (v3)
========================================================
Tracks weather forecasts from 5 sources (ECMWF, ICON, GFS/HRRR, GEM, METAR),
computes multi-model ensemble consensus, and paper trades using Kelly criterion.

Changes from v2:
  - ICON seamless global model added (DWD, excellent worldwide)
  - GEM seamless model added for Americas (Canadian model)
  - Ensemble consensus: when 3+ models agree, confidence increases, sigma tightens
  - 15 new cities (35 total): LA, Denver, Phoenix, Houston, Boston,
    Amsterdam, Madrid, Rome, Stockholm, Dubai, Mumbai, Bangkok,
    Jakarta, Sydney, Johannesburg

Usage:
    python bot_v3.py          # main loop
    python bot_v3.py report   # full report
    python bot_v3.py status   # balance and open positions
"""

import re
import sys
import json
import math
import time
import os
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import weatherbot_redeem  # noqa: E402  used by auto-redeem hook in resolution loop

# =============================================================================
# CONFIG
# =============================================================================

with open(Path(__file__).parent / "config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)
MIN_EV           = _cfg.get("min_ev", 0.10)
MAX_EV           = _cfg.get("max_ev", 999.0)         # 2026-04-27: cap signals — high-claimed-EV bets correlated with losses
MIN_ENS_STD_F    = _cfg.get("min_ensemble_std_f", 0.0)  # skip bets when models agree too tightly (priced in)
MIN_ENS_STD_C    = _cfg.get("min_ensemble_std_c", 0.0)
MAX_PRICE        = _cfg.get("max_price", 0.45)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")
RAW_LIVE_TRADE   = bool(_cfg.get("live_trade", False))
V3_LIVE_CONFIRMED = bool(_cfg.get("v3_live_confirmed", False))
LIVE_TRADE       = RAW_LIVE_TRADE and V3_LIVE_CONFIRMED

# Pre-trade risk gates
MIN_PRICE           = _cfg.get("min_price", 0.08)        # skip bets below this (high-variance low-prob)
MAX_UNREALIZED_LOSS = _cfg.get("max_unrealized_loss", -5.0)  # pause new bets if unrealized PnL < this
MAX_OPEN_POSITIONS  = _cfg.get("max_open_positions", 8)      # hard cap on concurrent positions
BALANCE_FLOOR       = _cfg.get("balance_floor", 0.0)         # hard stop — no new bets below this balance
NEW_ENTRIES_ENABLED = bool(_cfg.get("new_entries_enabled", True))  # manage-only mode when false
AUTO_REDEEM_ON_RESOLVE = bool(_cfg.get("auto_redeem_on_resolve", True))  # call NegRiskAdapter/CTF redeemPositions for wins right after resolution


def require_v3_live_confirmation():
    """Fail closed if v3 live trading is enabled without explicit confirmation.

    v3 previously over-scored matched buckets as p=1.0.  Requiring a fresh,
    explicit config acknowledgement prevents accidentally restarting older live
    configs after the probability model fix.
    """
    if RAW_LIVE_TRADE and not V3_LIVE_CONFIRMED:
        print(
            "[SAFETY] bot_v3 live trading is blocked: config has live_trade=true "
            "but v3_live_confirmed is not true. Review the probability-model fix, "
            "then add \"v3_live_confirmed\": true to config.json to permit live v3 orders.",
            file=sys.stderr,
        )
        raise SystemExit(2)


if LIVE_TRADE:
    import clob_trader


def live_startup_balance_message():
    """Return live startup balance text without mutating bot accounting state."""
    real_bal = clob_trader.get_balance()
    state = load_state()
    accounting_bal = state.get("balance", 0.0)
    return f"  Mode:       LIVE  (wallet ${real_bal:.2f} USDC; accounting ${accounting_bal:.2f})"


class StateIntegrityError(RuntimeError):
    """Critical persisted bot state could not be safely loaded."""


def _read_json_file(path: Path, *, expected_type=None, default_on_error=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        if default_on_error is not None:
            return default_on_error
        raise StateIntegrityError(f"Failed to load {Path(path).name}: {e}") from e
    if expected_type is not None and not isinstance(value, expected_type):
        raise StateIntegrityError(f"Failed to load {Path(path).name}: expected {expected_type.__name__}")
    return value


def _order_identifier(order: dict) -> str | None:
    for key in ("id", "order_id", "orderID", "hash"):
        value = order.get(key)
        if value:
            return str(value)
    return None


def _local_live_order_ids(markets: list[dict]) -> set[str]:
    ids: set[str] = set()
    for market in markets:
        pos = market.get("position") or {}
        for key in ("order_id", "exit_order_id"):
            if pos.get(key):
                ids.add(str(pos[key]))
    return ids


def is_terminal_position_close(pos) -> bool:
    """True when local metadata proves there is no active held exposure left.

    Polymarket's data-api can keep resolved claimable tokens in ``positions``
    until redemption, so "wallet had/has this token" is not by itself proof the
    bot should reopen the market as active exposure.
    """
    if not pos:
        return False
    if pos.get("exit_status") in ("filled", "filled_wallet_sell_confirmed", "buy_cancelled"):
        return True
    if pos.get("close_reason") in ("resolved", "buy_cancelled"):
        return True
    return False


def is_wallet_reconciled_held_position(pos) -> bool:
    """True when reconciliation proves an active wallet-held position.

    Older CLOB order IDs can age out of get_order() and return "unknown" even
    though the ERC1155/data-api wallet balance proves the buy filled. Once a
    local reconciliation pass stores positive wallet_shares +
    filled_wallet_reconciled, the position is represented locally and should not
    keep blocking new-entry safety checks solely because old order lookup is
    unavailable. Terminal sells/cancelled/resolved positions are excluded so a
    later audit or restart does not resurrect sold or claimable positions.
    """
    if not pos or is_terminal_position_close(pos):
        return False
    wallet_shares = float(pos.get("wallet_shares") or 0)
    if pos.get("entry_status") == "filled_wallet_reconciled" and wallet_shares > 0:
        return True
    if pos.get("wallet_reconciled_at") and wallet_shares > 0:
        return True
    return False


def should_skip_zombie_close(market) -> bool:
    """Do not auto-close positions that were explicitly wallet-reconciled as held.

    A reconciliation pass may intentionally reopen a market whose event is already
    closed/resolved because the wallet still holds the ERC1155 tokens. Those
    positions remain real exposure even if the market file's top-level status has
    advanced to ``closed`` or ``resolved``. The zombie guard exists to clean up
    stale locals, not to erase operator-proven wallet exposure on the next scan.
    """
    pos = (market or {}).get("position") or {}
    return pos.get("status") == "open" and is_wallet_reconciled_held_position(pos)


def should_mark_market_closed_for_no_new_entries(market, hours: float) -> bool:
    """Close discovery wrapper after cutoff only when no position is active.

    ``market.status = closed`` is used as a no-new-entry/discovery marker, but if
    a position remains open it becomes hidden from status/audit summaries. Keep
    active positions top-level open until actual resolution closes them.
    """
    if hours >= 0.5 or (market or {}).get("status") != "open":
        return False
    pos = (market or {}).get("position") or {}
    return pos.get("status") != "open"


def assert_live_reconciliation_safe() -> bool:
    """Fail closed if live CLOB/local state has unresolved divergence.

    This is intentionally conservative: any untracked live open order, local
    pending/reconciliation state, or local active order whose CLOB status is not
    confirmed filled blocks new live trading until an operator reconciles it.
    Operator wallet/data-api reconciliation can also prove old filled positions
    when CLOB order lookup no longer returns the historical order.
    """
    if not LIVE_TRADE:
        return True

    markets = load_all_markets()
    local_order_ids = _local_live_order_ids(markets)
    issues: list[str] = []

    get_open_orders = getattr(clob_trader, "get_open_orders", lambda: [])
    for order in get_open_orders():
        oid = _order_identifier(order)
        if oid and oid not in local_order_ids:
            issues.append(f"untracked live CLOB open order {oid}")

    for market in markets:
        pos = market.get("position") or {}
        if not pos:
            continue
        label = f"{market.get('city', '?')} {market.get('date', '?')}"
        if is_wallet_reconciled_held_position(pos):
            pos["entry_status"] = "filled_wallet_reconciled"
            changed = False
            if pos.get("status") != "open" or pos.get("needs_reconciliation"):
                pos["status"] = "open"
                pos["needs_reconciliation"] = False
                changed = True
            if market.get("status") in ("closed", "resolved") and pos.get("status") == "open":
                market["status"] = "open"
                changed = True
            if changed:
                save_market(market)
            continue
        # Markets already closed or resolved have no active position to reconcile.
        if market.get("status") in ("closed", "resolved"):
            continue
        if pos.get("needs_reconciliation") or pos.get("status") == "pending_buy":
            issues.append(f"local reconciliation required for {label} order {pos.get('order_id')}")
            continue
        if pos.get("status") == "open" and pos.get("order_id"):
            status = clob_trader.get_order_status(pos["order_id"])
            pos["entry_status"] = status
            if status != "filled":
                pos["needs_reconciliation"] = True
                save_market(market)
                issues.append(f"local open position {label} has CLOB entry status {status!r}")
            elif pos.get("needs_reconciliation"):
                pos["needs_reconciliation"] = False
                save_market(market)

    if issues:
        joined = "; ".join(issues[:5])
        if len(issues) > 5:
            joined += f"; +{len(issues) - 5} more"
        raise RuntimeError(f"Live reconciliation failed: {joined}")
    return True


def prepare_live_exit(pos, current_price):
    """Prepare an open position for local close without orphaning live exposure.

    In paper mode this is a no-op. In live mode:
    - open/unfilled buy orders must be cancelled successfully before local close;
    - filled buy orders get a sell order, but local close waits until that sell
      order is reported filled;
    - existing exit orders are monitored instead of duplicated;
    - unknown state blocks local close, because hiding the position locally would
      make reconciliation worse.
    """
    if not LIVE_TRADE:
        return True

    exit_order_id = pos.get("exit_order_id")
    if exit_order_id:
        exit_status = clob_trader.get_order_status(exit_order_id)
        pos["exit_status"] = exit_status
        if exit_status == "filled":
            print(f"  [LIVE] Exit order filled: {exit_order_id}")
            return True
        if exit_status == "open":
            print(f"  [LIVE] Exit order still open: {exit_order_id}; keeping position open")
            return False
        if exit_status == "partial":
            pos["exit_status"] = "partial"
            pos["needs_reconciliation"] = True
            print(f"  [LIVE] Exit order partially filled: {exit_order_id}; reconciliation required")
            return False
        if exit_status == "cancelled":
            print(f"  [LIVE] Exit order cancelled: {exit_order_id}; clearing exit to retry")
            pos.pop("exit_order_id", None)
            pos.pop("exit_status", None)
            return False
        # Unknown live status must fail closed.  A network/API retention issue is
        # not proof that a resting sell filled; hiding exposure locally is worse
        # than requiring reconciliation/manual intervention.
        pos["exit_unknown_count"] = pos.get("exit_unknown_count", 0) + 1
        pos["needs_reconciliation"] = True
        print(f"  [LIVE] Exit order status {exit_status!r} for {exit_order_id}; keeping position open pending reconciliation ({pos['exit_unknown_count']} unknown checks)")
        return False

    order_id = pos.get("order_id")

    # Wallet-reconciled positions already proved via balance API — don't let stale
    # CLOB lookups block local close logic.
    if is_wallet_reconciled_held_position(pos):
        status = "filled"
        pos["entry_status"] = "filled_wallet_reconciled"
    else:
        if not order_id:
            print("  [LIVE] Cannot close locally: missing order_id")
            return False
        status = clob_trader.get_order_status(order_id)
    entry_status = status

    if status == "open":
        ok = clob_trader.cancel_order(order_id)
        if ok:
            pos["exit_status"] = "buy_cancelled"
            print(f"  [LIVE] Cancelled order {order_id}")
        else:
            print(f"  [LIVE] Cancel FAILED for {order_id}; keeping position open")
        return bool(ok)

    if status == "partial":
        pos["entry_status"] = "partial"
        pos["needs_reconciliation"] = True
        print(f"  [LIVE] Buy order partially filled: {order_id}; reconciliation required before local close")
        return False

    if status == "filled":
        token_id = pos.get("token_id")
        shares = pos.get("shares")
        if not token_id or not shares:
            print("  [LIVE] Cannot sell filled position: missing token_id/shares")
            return False
        if not is_sellable_share_size(shares):
            print(f"  [LIVE] Holding to resolution: shares {shares:.2f} below CLOB sell minimum {CLOB_MIN_SELL_SHARES} — will resolve at $1 or $0")
            return False
        sell = clob_trader.place_sell(token_id, current_price, shares)
        if sell:
            pos["exit_order_id"] = sell.get("order_id")
            pos["exit_status"] = "open"
            print(f"  [LIVE] Sell order placed: {sell.get('order_id')}; keeping position open until filled")
            return False
        print("  [LIVE] Sell FAILED; keeping position open")
        return False

    if status == "cancelled":
        print(f"  [LIVE] Order already cancelled: {order_id}")
        return True

    # Unknown live status must fail closed.  Do not assume an entry order is dead
    # purely because CLOB status lookup failed repeatedly; it may be filled,
    # partially filled, or still live.
    pos["unknown_count"] = pos.get("unknown_count", 0) + 1
    pos["needs_reconciliation"] = True
    print(f"  [LIVE] Cannot close locally: unknown order status {status!r} for {order_id} ({pos['unknown_count']} unknown checks — reconciliation required)")
    return False


def calculate_exit_pnl(pos, current_price):
    """Calculate realized PnL for local close accounting."""
    if pos.get("exit_status") == "buy_cancelled":
        return 0.0
    return round((current_price - pos["entry_price"]) * pos["shares"], 2)


SIGMA_F = 2.0
SIGMA_C = 1.2

# When ensemble std dev is below this, use tighter sigma
ENSEMBLE_AGREE_F = 1.0   # degrees F (tightened from 1.5 based on backtest analysis)
ENSEMBLE_AGREE_C = 0.7   # degrees C (tightened from 0.8)
ENSEMBLE_SIGMA_REDUCTION = 1.0   # 2026-04-27: disabled — tight agreement was the worst-performing slice (7% win-rate)

# Cities with negative historical edge — forecast is tracked but bets are skipped
CITY_BLACKLIST = {
    "dallas", "paris", "seoul", "nyc", "singapore", "shanghai", "los-angeles"
}

# Ensemble std danger zone: looks close to agreement but historically high error
# F: std in [1.0, 1.5] had MAE=4.4° vs 2.5° baseline — skip all bets in this range
ENSEMBLE_DANGER_LO_F = 1.0
ENSEMBLE_DANGER_HI_F = 1.5
ENSEMBLE_DANGER_LO_C = 0.6
ENSEMBLE_DANGER_HI_C = 0.9

BASE_DIR         = Path(__file__).resolve().parent
DATA_DIR         = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
WIN_RATE_FILE    = DATA_DIR / "win_rates.json"
TRADE_JOURNAL    = DATA_DIR / "trade_journal.json"
LEARNED_PARAMS   = DATA_DIR / "learned_params.json"

# Self-learning adaptive system (ported from nicolastinkl fork)
LEARNING_DIR     = DATA_DIR / "learning"
LEARNING_DIR.mkdir(exist_ok=True)
NICOLAS_MODEL    = LEARNING_DIR / "model.json"
NICOLAS_TRADE_LOG = LEARNING_DIR / "trade_log.json"
LEARNING_WINDOW  = 30   # rolling window for adaptation


def atomic_json_write(path: Path, obj) -> None:
    """Atomically persist JSON in the target directory.

    Critical bot accounting and market files must not be truncated if the
    process dies mid-write.  Write a sibling temp file, fsync it, then replace.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, ensure_ascii=False)
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            fd = None
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Directory fsync is best-effort across filesystems/platforms.
            pass
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

# Minimum resolved bets per cell before win-rate signal is trusted
WIN_RATE_MIN_BETS = 8
# Bayesian prior: assume neutral performance until data says otherwise
WIN_RATE_PRIOR = 5       # virtual bets at baseline win rate
WIN_RATE_BASELINE = 0.60 # expected win rate for well-calibrated weather bets

LOCATIONS = {
    # ── US ──────────────────────────────────────────────────────────────────
    "nyc":           {"lat":  40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA",  "unit": "F", "region": "us"},
    "chicago":       {"lat":  41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD",  "unit": "F", "region": "us"},
    "miami":         {"lat":  25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA",  "unit": "F", "region": "us"},
    "dallas":        {"lat":  32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL",  "unit": "F", "region": "us"},
    "seattle":       {"lat":  47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA",  "unit": "F", "region": "us"},
    "atlanta":       {"lat":  33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL",  "unit": "F", "region": "us"},
    "los-angeles":   {"lat":  33.9425,  "lon": -118.4081, "name": "Los Angeles",   "station": "KLAX",  "unit": "F", "region": "us"},
    "denver":        {"lat":  39.8561,  "lon": -104.6737, "name": "Denver",        "station": "KDEN",  "unit": "F", "region": "us"},
    "phoenix":       {"lat":  33.4373,  "lon": -112.0078, "name": "Phoenix",       "station": "KPHX",  "unit": "F", "region": "us"},
    "houston":       {"lat":  29.9844,  "lon":  -95.3414, "name": "Houston",       "station": "KIAH",  "unit": "F", "region": "us"},
    "boston":        {"lat":  42.3656,  "lon":  -71.0096, "name": "Boston",        "station": "KBOS",  "unit": "F", "region": "us"},
    # ── Europe ──────────────────────────────────────────────────────────────
    "london":        {"lat":  51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC",  "unit": "C", "region": "eu"},
    "paris":         {"lat":  48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG",  "unit": "C", "region": "eu"},
    "munich":        {"lat":  48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM",  "unit": "C", "region": "eu"},
    "ankara":        {"lat":  40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC",  "unit": "C", "region": "eu"},
    "amsterdam":     {"lat":  52.3105,  "lon":    4.7683, "name": "Amsterdam",     "station": "EHAM",  "unit": "C", "region": "eu"},
    "madrid":        {"lat":  40.4936,  "lon":   -3.5668, "name": "Madrid",        "station": "LEMD",  "unit": "C", "region": "eu"},
    "rome":          {"lat":  41.8003,  "lon":   12.2389, "name": "Rome",          "station": "LIRF",  "unit": "C", "region": "eu"},
    "stockholm":     {"lat":  59.6519,  "lon":   17.9186, "name": "Stockholm",     "station": "ESSA",  "unit": "C", "region": "eu"},
    # ── Asia / Middle East ───────────────────────────────────────────────────
    "seoul":         {"lat":  37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI",  "unit": "C", "region": "asia"},
    "tokyo":         {"lat":  35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT",  "unit": "C", "region": "asia"},
    "shanghai":      {"lat":  31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD",  "unit": "C", "region": "asia"},
    "singapore":     {"lat":   1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS",  "unit": "C", "region": "asia"},
    "lucknow":       {"lat":  26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK",  "unit": "C", "region": "asia"},
    "tel-aviv":      {"lat":  32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG",  "unit": "C", "region": "asia"},
    "dubai":         {"lat":  25.2532,  "lon":   55.3657, "name": "Dubai",         "station": "OMDB",  "unit": "C", "region": "asia"},
    "mumbai":        {"lat":  19.0896,  "lon":   72.8656, "name": "Mumbai",        "station": "VABB",  "unit": "C", "region": "asia"},
    "bangkok":       {"lat":  13.6811,  "lon":  100.7475, "name": "Bangkok",       "station": "VTBS",  "unit": "C", "region": "asia"},
    "jakarta":       {"lat":  -6.1256,  "lon":  106.6559, "name": "Jakarta",       "station": "WIII",  "unit": "C", "region": "asia"},
    # ── Americas / Canada / South America ────────────────────────────────────
    "toronto":       {"lat":  43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ",  "unit": "C", "region": "ca"},
    "sao-paulo":     {"lat": -23.4356,  "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR",  "unit": "C", "region": "sa"},
    "buenos-aires":  {"lat": -34.8222,  "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ",  "unit": "C", "region": "sa"},
    # ── Oceania / Africa ─────────────────────────────────────────────────────
    "wellington":    {"lat": -41.3272,  "lon":  174.8052, "name": "Wellington",    "station": "NZWN",  "unit": "C", "region": "oc"},
    "sydney":        {"lat": -33.9399,  "lon":  151.1753, "name": "Sydney",        "station": "YSSY",  "unit": "C", "region": "oc"},
    "johannesburg":  {"lat": -26.1392,  "lon":   28.2460, "name": "Johannesburg",  "station": "FAOR",  "unit": "C", "region": "af"},
}

TIMEZONES = {
    "nyc":          "America/New_York",
    "chicago":      "America/Chicago",
    "miami":        "America/New_York",
    "dallas":       "America/Chicago",
    "seattle":      "America/Los_Angeles",
    "atlanta":      "America/New_York",
    "los-angeles":  "America/Los_Angeles",
    "denver":       "America/Denver",
    "phoenix":      "America/Phoenix",
    "houston":      "America/Chicago",
    "boston":       "America/New_York",
    "london":       "Europe/London",
    "paris":        "Europe/Paris",
    "munich":       "Europe/Berlin",
    "ankara":       "Europe/Istanbul",
    "amsterdam":    "Europe/Amsterdam",
    "madrid":       "Europe/Madrid",
    "rome":         "Europe/Rome",
    "stockholm":    "Europe/Stockholm",
    "seoul":        "Asia/Seoul",
    "tokyo":        "Asia/Tokyo",
    "shanghai":     "Asia/Shanghai",
    "singapore":    "Asia/Singapore",
    "lucknow":      "Asia/Kolkata",
    "tel-aviv":     "Asia/Jerusalem",
    "dubai":        "Asia/Dubai",
    "mumbai":       "Asia/Kolkata",
    "bangkok":      "Asia/Bangkok",
    "jakarta":      "Asia/Jakarta",
    "toronto":      "America/Toronto",
    "sao-paulo":    "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires",
    "wellington":   "Pacific/Auckland",
    "sydney":       "Australia/Sydney",
    "johannesburg": "Africa/Johannesburg",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability actual temperature lands in the market bucket.

    Treat forecast error as normal with standard deviation ``sigma``.  Use a
    half-degree continuity window for exact integer buckets and open-ended edge
    buckets, but preserve explicit finite Polymarket ranges exactly.  Live and
    tests must agree here because small probability shifts materially affect EV
    and Kelly sizing.
    """
    s = sigma or 2.0
    forecast = float(forecast)
    half = 0.5
    if t_low == -999:
        return round(norm_cdf((t_high + half - forecast) / s), 4)
    if t_high == 999:
        return round(1.0 - norm_cdf((t_low - half - forecast) / s), 4)
    if t_low == t_high:
        return round(norm_cdf((t_high + half - forecast) / s) - norm_cdf((t_low - half - forecast) / s), 4)
    return round(norm_cdf((t_high - forecast) / s) - norm_cdf((t_low - forecast) / s), 4)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

CLOB_MIN_BET = 1.0  # Polymarket minimum buy order notional
CLOB_MIN_SELL_SHARES = 5.0  # Polymarket minimum sell order size
ACTIVE_POSITION_STATUSES = {"open", "pending_buy"}


def is_sellable_share_size(shares):
    """Return True when a position is large enough to submit a CLOB sell."""
    return float(shares or 0) >= CLOB_MIN_SELL_SHARES


def is_active_position(pos):
    """Positions/orders that must block duplicate exposure and count risk caps."""
    if not pos:
        return False
    return pos.get("status") in ACTIVE_POSITION_STATUSES or bool(pos.get("needs_reconciliation"))


def clob_buy_sizing(price: float, size_usd: float) -> dict:
    """Single source of truth for submitted CLOB buy price/shares/cost."""
    price = round(float(price), 2)
    if not math.isfinite(price) or price <= 0:
        return {"price": price, "shares": 0.0, "cost": 0.0, "sellable": False}
    shares = math.ceil(float(size_usd) / price * 100) / 100
    cost = round(price * shares, 2)
    return {"price": price, "shares": shares, "cost": cost, "sellable": is_sellable_share_size(shares)}


def validate_repriced_signal(signal, real_ask, real_bid, min_ev=MIN_EV):
    """Apply the live orderbook quote and reject signals that no longer qualify.

    The scanner first computes EV from the cached Gamma market quote, then fetches
    a fresh ask/bid just before placing an order. A signal must still clear the EV
    threshold after that repricing, and the resulting share count must be large
    enough to be sellable later through the CLOB.
    """
    try:
        real_ask = float(real_ask)
        real_bid = float(real_bid)
    except (TypeError, ValueError):
        return False, "invalid live quote: bid/ask not numeric"

    if not (math.isfinite(real_ask) and math.isfinite(real_bid)):
        return False, "invalid live quote: bid/ask not finite"
    if not (0 < real_bid <= real_ask < 1):
        return False, f"invalid live quote: bid ${real_bid:.4f}, ask ${real_ask:.4f}"

    real_spread = round(real_ask - real_bid, 4)
    sizing = clob_buy_sizing(real_ask, signal["cost"])

    signal["entry_price"] = sizing["price"]
    signal["bid_at_entry"] = real_bid
    signal["spread"] = real_spread
    signal["shares"] = sizing["shares"]
    signal["cost"] = sizing["cost"]
    signal["ev"] = round(calc_ev(signal["p"], sizing["price"]), 4)
    signal["kelly"] = calc_kelly(signal["p"], sizing["price"])

    if signal["ev"] < min_ev:
        return False, f"EV {signal['ev']:+.4f} below min {min_ev:+.4f} after repricing"
    if signal["ev"] > MAX_EV:
        return False, f"EV {signal['ev']:+.4f} above cap {MAX_EV:+.4f} after repricing"
    if not sizing["sellable"]:
        return False, f"shares {signal['shares']:.2f} below sell minimum {CLOB_MIN_SELL_SHARES:.0f}"
    return True, None


def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)


def estimate_clob_buy_cost(price: float, size_usd: float) -> float:
    """Mirror CLOB buy sizing so risk gates use submitted notional."""
    return clob_buy_sizing(price, size_usd)["cost"]


def analyze_signal(signal, outcomes, snap, loc, city_slug, date, horizon) -> tuple[bool, str]:
    """Deep pre-trade analysis. Returns (proceed, reason).

    Checks:
    1. Price floor — reject very-low-price bets (high variance, model noise)
    2. Portfolio unrealized PnL gate — pause new bets during drawdown
    3. Open position cap — hard stop on concurrent exposure
    4. Model consensus — warn when only 1 model supports the forecast
    5. Bucket dominance — reject if the target bucket is not top-3 by market volume

    Prints a detailed breakdown for every signal reviewed.
    """
    unit_sym   = "F" if loc["unit"] == "F" else "C"
    city_name  = loc["name"]
    entry      = signal["entry_price"]
    ev         = signal["ev"]
    p          = signal["p"]
    sigma      = signal["sigma"]
    ens_std    = signal.get("ensemble_std")
    ens_n      = signal.get("ensemble_n", 0)
    src        = signal.get("forecast_src", "?")
    forecast   = signal.get("forecast_temp")
    bucket     = f"{signal['bucket_low']}-{signal['bucket_high']}{unit_sym}"

    print(f"\n  ┌─ PRE-TRADE ANALYSIS: {city_name} {horizon} {date} ─")
    print(f"  │  Forecast ({src.upper()}): {forecast}{unit_sym}  sigma={sigma}  Target: {bucket}")

    # Model snapshot
    model_vals = {k: snap.get(k) for k in ("ecmwf","icon","hrrr","gem","metar","ensemble_mean") if snap.get(k) is not None}
    model_str  = "  ".join(f"{k.upper()}={v}{unit_sym}" for k, v in model_vals.items())
    print(f"  │  Models: {model_str or 'none'}")
    if ens_std is not None:
        agree = "TIGHT" if ens_std < (1.0 if loc["unit"] == "F" else 0.7) else ("MODERATE" if ens_std < (2.5 if loc["unit"] == "F" else 1.5) else "WIDE")
        print(f"  │  Ensemble: std={ens_std}{unit_sym} ({ens_n} models) — {agree} consensus")

    # All bucket prices
    sorted_outcomes = sorted(outcomes, key=lambda x: x.get("bid", 0), reverse=True)
    print(f"  │  Market buckets (top by price):")
    target_rank = None
    for rank, o in enumerate(sorted_outcomes[:6], 1):
        marker = " ◄ TARGET" if o["market_id"] == signal["market_id"] else ""
        if o["market_id"] == signal["market_id"]:
            target_rank = rank
        rng = o["range"]
        label = f"{rng[0]}-{rng[1]}{unit_sym}" if rng[0] != -999 and rng[1] != 999 else (f"≤{rng[1]}{unit_sym}" if rng[0] == -999 else f"≥{rng[0]}{unit_sym}")
        print(f"  │    #{rank} {label:<14} bid=${o.get('bid',0):.3f}  vol={o.get('volume',0):.0f}{marker}")

    print(f"  │  Signal: p={p:.3f}  EV={ev:+.4f}  entry=${entry:.3f}  size=${signal['cost']:.2f}  kelly={signal['kelly']:.4f}")

    # Gate -1: explicit manage-only mode — monitor/resolve existing exposure but never open new positions
    if not NEW_ENTRIES_ENABLED:
        reason = "new entries disabled — manage-only mode"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    # Gate 0: balance floor — hard stop, no new bets
    current_balance = load_state().get("balance", 0.0)
    if BALANCE_FLOOR > 0 and current_balance < BALANCE_FLOOR:
        reason = f"balance ${current_balance:.2f} below floor ${BALANCE_FLOOR:.2f} — all betting paused"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    # Gate 1: price floor (uses learned value if data supports raising it)
    effective_min_price = get_learned_min_price()
    if entry < effective_min_price:
        reason = f"price ${entry:.3f} below floor ${effective_min_price:.2f} — high-variance low-probability bet"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    # Gate 2: unrealized PnL portfolio check
    open_markets = [m for m in load_all_markets() if is_active_position(m.get("position"))]
    unrealized = 0.0
    for m in open_markets:
        pos = m["position"]
        if pos.get("status") != "open":
            continue
        for o in m.get("all_outcomes", []):
            if o["market_id"] == pos["market_id"]:
                unrealized += (o.get("bid", o["price"]) - pos["entry_price"]) * pos["shares"]
                break
    unrealized = round(unrealized, 2)
    if unrealized < MAX_UNREALIZED_LOSS:
        reason = f"portfolio unrealized PnL ${unrealized:+.2f} below gate ${MAX_UNREALIZED_LOSS:.2f} — pausing new bets"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    # Gate 3: open position cap
    if len(open_markets) >= MAX_OPEN_POSITIONS:
        reason = f"{len(open_markets)}/{MAX_OPEN_POSITIONS} positions open — at cap"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    # Gate 4: single-model warning (don't block, but flag)
    if ens_n is not None and ens_n < 2:
        print(f"  │  ⚠ WARNING: only {ens_n} model(s) — no ensemble confidence")

    # Gate 5: target bucket rank
    if target_rank is not None and target_rank > 4:
        reason = f"target bucket ranked #{target_rank} by market price — market disagrees strongly"
        print(f"  └─ BLOCKED: {reason}\n")
        return False, reason

    print(f"  └─ APPROVED  unrealized=${unrealized:+.2f}  open={len(open_markets)}/{MAX_OPEN_POSITIONS}\n")
    return True, "approved"

def ensemble_stddev(temps):
    if len(temps) < 2:
        return 0.0
    mean = sum(temps) / len(temps)
    return math.sqrt(sum((t - mean) ** 2 for t in temps) / len(temps))

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf"):
    loc     = LOCATIONS[city_slug]
    default = SIGMA_F if loc["unit"] == "F" else SIGMA_C
    key     = f"{city_slug}_{source}"

    if key in _cal:
        return _cal[key]["sigma"]

    if source == "ensemble":
        # Calibration key not yet populated — derive from ECMWF sigma
        base_key = f"{city_slug}_ecmwf"
        base     = _cal[base_key]["sigma"] if base_key in _cal else default
        return round(base * ENSEMBLE_SIGMA_REDUCTION, 3)

    return default

def run_calibration(markets):
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar", "icon", "gem", "ensemble"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s.get("best_source") == source or s.get(source) is not None), None)
                if snap:
                    temp = snap.get(source) if source != "ensemble" else snap.get("ensemble_mean")
                    if temp is not None:
                        errors.append(abs(temp - m["actual_temp"]))
            if len(errors) < CALIBRATION_MIN:
                continue
            mae  = sum(errors) / len(errors)
            key  = f"{city}_{source}"
            old  = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            new  = round(mae, 3)
            cal[key] = {"sigma": new, "n": len(errors), "updated_at": datetime.now(timezone.utc).isoformat()}
            if abs(new - old) > 0.05:
                updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

    atomic_json_write(CALIBRATION_FILE, cal)
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal


_win_rates_cache: dict | None = None


def _load_win_rates() -> dict:
    global _win_rates_cache
    if _win_rates_cache is None:
        _win_rates_cache = (
            json.loads(WIN_RATE_FILE.read_text(encoding="utf-8"))
            if WIN_RATE_FILE.exists() else {}
        )
    return _win_rates_cache


def _save_win_rates(data: dict) -> None:
    global _win_rates_cache
    _win_rates_cache = data
    atomic_json_write(WIN_RATE_FILE, data)


def record_bet_outcome(city_slug: str, source: str, won: bool) -> None:
    data = _load_win_rates()
    key  = f"{city_slug}|{source}"
    cell = data.setdefault(key, {"bets": 0, "wins": 0})
    cell["bets"] += 1
    if won:
        cell["wins"] += 1
    _save_win_rates(data)


def get_ev_multiplier(city_slug: str, source: str) -> float:
    """Return an EV threshold multiplier for this city/source combination.

    > 1.0 → historically underperforms → require higher EV to bet.
    < 1.0 → historically outperforms  → accept lower EV.
    Returns 1.0 when insufficient data (< WIN_RATE_MIN_BETS).
    """
    data = _load_win_rates()
    key  = f"{city_slug}|{source}"
    cell = data.get(key)
    if not cell or cell["bets"] < WIN_RATE_MIN_BETS:
        return 1.0

    # Bayesian smoothed win rate
    prior_wins   = WIN_RATE_BASELINE * WIN_RATE_PRIOR
    total        = cell["bets"] + WIN_RATE_PRIOR
    smoothed     = (cell["wins"] + prior_wins) / total

    # How far off baseline: factor < 1 means we're beating baseline
    factor = WIN_RATE_BASELINE / smoothed
    return round(max(0.5, min(2.0, factor)), 3)


def win_rate_summary() -> list[dict]:
    data = _load_win_rates()
    rows = []
    for key, cell in sorted(data.items()):
        bets = cell["bets"]
        wins = cell["wins"]
        city, source = key.split("|", 1)
        rows.append({
            "key":      key,
            "city":     LOCATIONS.get(city, {}).get("name", city),
            "source":   source,
            "bets":     bets,
            "wins":     wins,
            "win_rate": round(wins / bets, 3) if bets else 0,
            "ev_mult":  get_ev_multiplier(city, source),
        })
    return rows


# =============================================================================
# NICOLAS SELF-LEARNING SYSTEM (adaptive Kelly + EV floor from resolved trades)
# =============================================================================

_NICOLAS_DEFAULT_MODEL = {
    "version": 1,
    "city_knowledge": {},   # city_slug -> {wins, losses, total_pnl, trades}
    "bucket_knowledge": {}, # bucket_range -> {wins, losses}
    "global": {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0},
    "kelly_adjustment": 1.0,
    "ev_floor": MIN_EV,
    "max_kelly_frac": KELLY_FRACTION,
    "confidence": 0.0,
}

def _nicolas_load_model() -> dict:
    if NICOLAS_MODEL.exists():
        try:
            return json.loads(NICOLAS_MODEL.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _NICOLAS_DEFAULT_MODEL.copy()

def _nicolas_save_model(model: dict) -> None:
    atomic_json_write(NICOLAS_MODEL, model)

def _nicolas_load_log() -> list:
    if NICOLAS_TRADE_LOG.exists():
        try:
            return json.loads(NICOLAS_TRADE_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def _nicolas_save_log(log: list) -> None:
    atomic_json_write(NICOLAS_TRADE_LOG, log)

def nicolas_record_trade(city_slug: str, bucket_low: float, bucket_high: float,
                         outcome: str, pnl: float, cost: float, kelly: float, ev: float) -> None:
    """Record a completed trade into Nicolas's learning log and recompute model."""
    model = _nicolas_load_model()
    log = _nicolas_load_log()

    trade = {
        "id": len(log) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": city_slug,
        "bucket": f"{bucket_low}-{bucket_high}",
        "outcome": outcome,
        "pnl": round(pnl, 4),
        "cost": round(cost, 4),
        "kelly": round(kelly, 4),
        "ev": round(ev, 4),
    }
    log.append(trade)
    log = log[-LEARNING_WINDOW:]
    _nicolas_save_log(log)

    resolved = [t for t in log if t["outcome"] in ("win", "loss")]
    if not resolved:
        _nicolas_save_model(model)
        return

    wins       = sum(1 for t in resolved if t["outcome"] == "win")
    losses     = sum(1 for t in resolved if t["outcome"] == "loss")
    total_pnl  = sum(t["pnl"] for t in resolved)
    total_trades = len(resolved)
    winrate    = wins / total_trades if total_trades > 0 else 0.5

    avg_win  = sum(t["pnl"] for t in resolved if t["outcome"] == "win") / wins if wins > 0 else 1.0
    avg_loss = abs(sum(t["pnl"] for t in resolved if t["outcome"] == "loss") / losses) if losses > 0 else 1.0

    model["global"] = {
        "wins": wins, "losses": losses,
        "total_pnl": round(total_pnl, 4),
        "trades": total_trades,
    }

    for city in set(t["city"] for t in resolved):
        ct = [t for t in resolved if t["city"] == city]
        model["city_knowledge"][city] = {
            "wins": sum(1 for t in ct if t["outcome"] == "win"),
            "losses": sum(1 for t in ct if t["outcome"] == "loss"),
            "total_pnl": round(sum(t["pnl"] for t in ct), 4),
            "trades": len(ct),
        }

    for bucket in set(t["bucket"] for t in resolved):
        bt = [t for t in resolved if t["bucket"] == bucket]
        model["bucket_knowledge"][bucket] = {
            "wins": sum(1 for t in bt if t["outcome"] == "win"),
            "losses": sum(1 for t in bt if t["outcome"] == "loss"),
        }

    if total_trades >= 5:
        if winrate < 0.45 or total_pnl < -1.0:
            model["kelly_adjustment"] = max(0.25, model["kelly_adjustment"] * 0.8)
            model["ev_floor"] = min(0.20, model["ev_floor"] * 1.1)
        elif winrate > 0.55 and total_pnl > 2.0:
            model["kelly_adjustment"] = min(1.0, model["kelly_adjustment"] * 1.1)
            model["ev_floor"] = max(MIN_EV, model["ev_floor"] * 0.95)

        model["max_kelly_frac"] = round(KELLY_FRACTION * model["kelly_adjustment"], 4)
        model["confidence"] = min(1.0, total_trades / 20.0)

    _nicolas_save_model(model)

def get_nicolas_adjusted_kelly(base_kelly: float) -> float:
    model = _nicolas_load_model()
    adj = model.get("kelly_adjustment", 1.0)
    capped = min(base_kelly * adj, model.get("max_kelly_frac", KELLY_FRACTION))
    return round(capped, 4)

def get_nicolas_adjusted_ev_floor() -> float:
    model = _nicolas_load_model()
    return model.get("ev_floor", MIN_EV)

def get_nicolas_city_winrate(city_slug: str) -> float:
    model = _nicolas_load_model()
    city = model.get("city_knowledge", {}).get(city_slug)
    if not city or city.get("trades", 0) < 2:
        return 0.5
    total = city["wins"] + city["losses"]
    return city["wins"] / total if total else 0.5

def get_nicolas_learning_stats() -> dict:
    model = _nicolas_load_model()
    g = model.get("global", {})
    trades = g.get("trades", 0)
    if trades == 0:
        return {"trades": 0, "winrate": "N/A", "pnl": "$0.00", "confidence": "0%",
                "kelly_adj": "1.0x", "ev_floor": f"{MIN_EV*100:.0f}%"}
    wr = g.get("wins", 0) / trades
    return {
        "trades": trades,
        "winrate": f"{wr:.0%}",
        "pnl": f"${g.get('total_pnl', 0):.2f}",
        "confidence": f"{model.get('confidence', 0)*100:.0f}%",
        "kelly_adj": f"{model.get('kelly_adjustment', 1.0):.2f}x",
        "ev_floor": f"{model.get('ev_floor', MIN_EV)*100:.0f}%",
    }


# =============================================================================
# POST-TRADE LEARNING
# =============================================================================

def _price_tier(price: float) -> str:
    if price < 0.15:  return "low(<0.15)"
    if price < 0.30:  return "mid(0.15-0.30)"
    if price < 0.45:  return "high(0.30-0.45)"
    return "top(>0.45)"

def _ens_bucket(std: float | None, unit: str) -> str:
    if std is None: return "no_ensemble"
    thresh_tight = 1.0 if unit == "F" else 0.7
    thresh_wide  = 2.5 if unit == "F" else 1.5
    if std < thresh_tight:  return "tight"
    if std < thresh_wide:   return "moderate"
    return "wide"

def _load_journal() -> list:
    if TRADE_JOURNAL.exists():
        return json.loads(TRADE_JOURNAL.read_text(encoding="utf-8"))
    return []

def _save_journal(entries: list) -> None:
    atomic_json_write(TRADE_JOURNAL, entries)

def _load_learned() -> dict:
    if LEARNED_PARAMS.exists():
        return _read_json_file(LEARNED_PARAMS, expected_type=dict, default_on_error={})
    return {}

def _save_learned(params: dict) -> None:
    atomic_json_write(LEARNED_PARAMS, params)


def post_trade_forensics(mkt: dict, won: bool) -> dict:
    """Capture full forensics for a resolved trade and return a journal entry."""
    pos       = mkt.get("position", {})
    snaps     = mkt.get("forecast_snapshots", [])
    loc       = LOCATIONS.get(mkt["city"], {})
    unit      = mkt.get("unit", loc.get("unit", "C"))
    unit_sym  = "F" if unit == "F" else "C"

    # Forecast at entry (first snap after position opened)
    entry_snap  = snaps[0] if snaps else {}
    final_snap  = snaps[-1] if snaps else {}
    actual_temp = mkt.get("actual_temp")

    entry_forecast = entry_snap.get("best")
    final_forecast = final_snap.get("best")
    forecast_drift = round(abs(final_forecast - entry_forecast), 2) if (entry_forecast and final_forecast) else None

    # Forecast error vs actual
    fc_error = round(abs(entry_forecast - actual_temp), 2) if (entry_forecast and actual_temp is not None) else None

    # Was the actual temperature near the bucket boundary? (edge case bets)
    bucket_low  = pos.get("bucket_low", 0)
    bucket_high = pos.get("bucket_high", 0)
    if actual_temp is not None and not math.isinf(bucket_low) and not math.isinf(bucket_high):
        bucket_mid  = (bucket_low + bucket_high) / 2
        near_edge   = abs(actual_temp - bucket_low) <= 1.0 or abs(actual_temp - bucket_high) <= 1.0
    else:
        bucket_mid = None
        near_edge  = None

    entry_price = pos.get("entry_price", 0)
    ens_std     = pos.get("ensemble_std")
    horizon_tag = entry_snap.get("horizon", "?")

    entry = {
        "city":           mkt["city"],
        "city_name":      mkt.get("city_name", ""),
        "date":           mkt["date"],
        "resolved_at":    datetime.now(timezone.utc).isoformat(),
        "won":            won,
        "pnl":            mkt.get("pnl"),
        "source":         pos.get("forecast_src", "unknown"),
        "horizon":        horizon_tag,
        "entry_price":    entry_price,
        "price_tier":     _price_tier(entry_price),
        "entry_forecast": entry_forecast,
        "final_forecast": final_forecast,
        "actual_temp":    actual_temp,
        "forecast_error": fc_error,
        "forecast_drift": forecast_drift,
        "ens_std":        ens_std,
        "ens_bucket":     _ens_bucket(ens_std, unit),
        "ens_n":          pos.get("ensemble_n"),
        "near_edge":      near_edge,
        "ev_at_entry":    pos.get("ev"),
        "kelly":          pos.get("kelly"),
        "sigma":          pos.get("sigma"),
        "bucket":         f"{bucket_low}-{bucket_high}{unit_sym}",
    }

    journal = _load_journal()
    journal.append(entry)
    _save_journal(journal)

    # One-line forensics log
    fc_err_str   = f"fc_err={fc_error}{unit_sym}" if fc_error is not None else "fc_err=?"
    drift_str    = f"drift={forecast_drift}{unit_sym}" if forecast_drift is not None else ""
    edge_str     = " NEAR_EDGE" if near_edge else ""
    actual_str   = f"actual={actual_temp}{unit_sym}" if actual_temp is not None else "actual=?"
    result_str   = "WIN " if won else "LOSS"
    print(f"  [FORENSICS] {result_str} {mkt['city_name']} | {actual_str} {fc_err_str} {drift_str} "
          f"ens={ens_std}{unit_sym if ens_std else ''} tier={_price_tier(entry_price)}{edge_str}")

    return entry


def adapt_thresholds() -> None:
    """Read trade journal and tighten/loosen learned parameters based on patterns.

    Updates LEARNED_PARAMS with adjusted values for:
    - danger zone ensemble std thresholds (if wide-ensemble bets lose more)
    - min_price floor (if low-price bets systematically underperform)
    - horizon-specific EV premiums (if D+2 bets underperform D+0)

    Requires at least 15 resolved trades before adapting.
    """
    journal = _load_journal()
    if len(journal) < 15:
        return

    learned  = _load_learned()
    changes  = []
    resolved = [e for e in journal if e.get("pnl") is not None]

    # ── 1. Ensemble danger zone ──────────────────────────────────────────────
    for bucket in ("tight", "moderate", "wide"):
        group = [e for e in resolved if e.get("ens_bucket") == bucket]
        if len(group) < 5:
            continue
        wr = sum(1 for e in group if e["won"]) / len(group)
        if bucket == "wide" and wr < 0.45:
            # Wide ensemble losing too much — tighten the danger zone ceiling
            new_hi_f = round(learned.get("ensemble_danger_hi_f", ENSEMBLE_DANGER_HI_F) - 0.1, 2)
            new_hi_c = round(learned.get("ensemble_danger_hi_c", ENSEMBLE_DANGER_HI_C) - 0.05, 3)
            if new_hi_f > learned.get("ensemble_danger_lo_f", ENSEMBLE_DANGER_LO_F):
                learned["ensemble_danger_hi_f"] = new_hi_f
                learned["ensemble_danger_hi_c"] = new_hi_c
                changes.append(f"danger_zone_hi tightened to {new_hi_f}°F/{new_hi_c}°C (wide wr={wr:.0%})")
        elif bucket == "tight" and wr > 0.70:
            # Tight ensemble performing well — can loosen danger zone floor
            new_lo_f = round(learned.get("ensemble_danger_lo_f", ENSEMBLE_DANGER_LO_F) - 0.1, 2)
            if new_lo_f > 0.5:
                learned["ensemble_danger_lo_f"] = new_lo_f
                learned["ensemble_danger_lo_c"] = round(new_lo_f * 0.6, 3)
                changes.append(f"danger_zone_lo loosened to {new_lo_f}°F (tight wr={wr:.0%})")

    # ── 2. Price tier floor ──────────────────────────────────────────────────
    low_tier = [e for e in resolved if e.get("price_tier") == "low(<0.15)"]
    if len(low_tier) >= 5:
        wr = sum(1 for e in low_tier if e["won"]) / len(low_tier)
        if wr < 0.40:
            new_floor = round(learned.get("min_price", MIN_PRICE) + 0.02, 3)
            if new_floor <= 0.20:
                learned["min_price"] = new_floor
                changes.append(f"min_price raised to {new_floor:.3f} (low-tier wr={wr:.0%})")

    # ── 3. Horizon EV premium ────────────────────────────────────────────────
    for horizon in ("D+0", "D+1", "D+2", "D+3"):
        group = [e for e in resolved if e.get("horizon") == horizon]
        if len(group) < 5:
            continue
        wr = sum(1 for e in group if e["won"]) / len(group)
        key = f"ev_premium_{horizon}"
        current = learned.get(key, 0.0)
        if wr < 0.50 and current < 0.10:
            learned[key] = round(current + 0.01, 3)
            changes.append(f"ev_premium {horizon} raised to {learned[key]:.3f} (wr={wr:.0%})")
        elif wr > 0.70 and current > 0.0:
            learned[key] = round(current - 0.005, 3)
            changes.append(f"ev_premium {horizon} lowered to {learned[key]:.3f} (wr={wr:.0%})")

    _save_learned(learned)
    if changes:
        print(f"  [ADAPT] {' | '.join(changes)}")


def get_learned_ev_premium(horizon: str) -> float:
    """Return any learned horizon-specific EV premium."""
    return _load_learned().get(f"ev_premium_{horizon}", 0.0)


def get_learned_min_price() -> float:
    return _load_learned().get("min_price", MIN_PRICE)


def get_learned_danger_zone() -> tuple[float, float, float, float]:
    """Return (lo_f, hi_f, lo_c, hi_c) with learned adjustments applied."""
    p = _load_learned()
    return (
        p.get("ensemble_danger_lo_f", ENSEMBLE_DANGER_LO_F),
        p.get("ensemble_danger_hi_f", ENSEMBLE_DANGER_HI_F),
        p.get("ensemble_danger_lo_c", ENSEMBLE_DANGER_LO_C),
        p.get("ensemble_danger_hi_c", ENSEMBLE_DANGER_HI_C),
    )

# =============================================================================
# FORECASTS
# =============================================================================

def _open_meteo_fetch(city_slug, dates, model, label):
    """Generic Open-Meteo daily tmax fetch for a single model."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models={model}&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" in data:
                break
            for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if date in dates and temp is not None:
                    result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [{label}] {city_slug}: {e}")
    return result

def get_ecmwf(city_slug, dates):
    """ECMWF IFS 0.25° via Open-Meteo. Global, bias-corrected."""
    return _open_meteo_fetch(city_slug, dates, "ecmwf_ifs025", "ECMWF")

def get_icon(city_slug, dates):
    """ICON seamless (DWD) via Open-Meteo. Global, strong for EU."""
    return _open_meteo_fetch(city_slug, dates, "icon_seamless", "ICON")

def get_hrrr(city_slug, dates):
    """GFS seamless (HRRR for D+0/D+1, GFS thereafter) via Open-Meteo. US cities only.
    Uses the shared bias-corrected fetch path so HRRR receives the same lat/lon
    historical-station calibration that ECMWF/ICON/GEM already get."""
    if LOCATIONS[city_slug]["region"] != "us":
        return {}
    return _open_meteo_fetch(city_slug, dates, "gfs_seamless", "HRRR")

def get_gem(city_slug, dates):
    """GEM seamless (Canadian) via Open-Meteo. Americas only."""
    loc = LOCATIONS[city_slug]
    if loc["region"] not in ("us", "ca", "sa"):
        return {}
    return _open_meteo_fetch(city_slug, dates, "gem_seamless", "GEM")

def _metar_fetch(station, unit):
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
    r = requests.get(url, timeout=(3, 10))
    if r.status_code != 200:
        return None
    if not r.text or r.text.strip() in ("", "[]"):
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if data and isinstance(data, list):
        temp_c = data[0].get("temp")
        if temp_c is not None:
            if unit == "F":
                return round(float(temp_c) * 9/5 + 32)
            return round(float(temp_c), 1)
    return None

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    import concurrent.futures
    loc     = LOCATIONS[city_slug]
    station = loc["station"]
    unit    = loc["unit"]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_metar_fetch, station, unit)
            return future.result(timeout=8)
    except concurrent.futures.TimeoutError:
        print(f"  [METAR] {city_slug}: hard timeout (8s)")
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Actual temperature via Visual Crossing for closed markets."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        if not data.get("closed", False):
            return None
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True
        elif yes_price <= 0.05:
            return False
        return None
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    """True when forecast falls within the bucket range.

    Supports open-ended buckets from "or below" / "or higher" markets
    (t_low=-inf or t_high=+inf).
    """
    fc = float(forecast)
    if t_low == t_high:                       # single-target market
        return round(fc) == round(t_low)
    return t_low <= fc <= t_high

# =============================================================================
# MARKET DATA STORAGE
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return _read_json_file(p, expected_type=dict)
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    atomic_json_write(p, market)

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        market = _read_json_file(f, expected_type=dict)
        markets.append(market)
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",
        "position":           None,
        "actual_temp":        None,
        "resolved_outcome":   None,
        "pnl":                None,
        "forecast_snapshots": [],
        "market_snapshots":   [],
        "all_outcomes":       [],
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return _read_json_file(STATE_FILE, expected_type=dict)
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    atomic_json_write(STATE_FILE, state)
    # sync config.json so restarts pick up correct balance
    try:
        cfg_path = Path(__file__).parent / "config.json"
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
        cfg["balance"] = state.get("balance", cfg.get("balance", 100.0))
        cfg["peak_balance"] = state.get("peak_balance", cfg.get("peak_balance", cfg["balance"]))
        atomic_json_write(cfg_path, cfg)
    except Exception:
        pass

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches all model forecasts in parallel, computes ensemble consensus."""
    import concurrent.futures as _cf
    now_str = datetime.now(timezone.utc).isoformat()
    loc     = LOCATIONS[city_slug]
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _cf.ThreadPoolExecutor(max_workers=4) as pool:
        f_ecmwf = pool.submit(get_ecmwf, city_slug, dates)
        f_icon  = pool.submit(get_icon,  city_slug, dates)
        f_hrrr  = pool.submit(get_hrrr,  city_slug, dates)
        f_gem   = pool.submit(get_gem,   city_slug, dates)
        ecmwf = f_ecmwf.result()
        icon  = f_icon.result()
        hrrr  = f_hrrr.result()
        gem   = f_gem.result()

    hrrr_cutoff = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "icon":  icon.get(date),
            "hrrr":  hrrr.get(date) if date <= hrrr_cutoff else None,
            "gem":   gem.get(date),
            "metar": get_metar(city_slug) if date == today else None,
        }

        # Build ensemble from all available NWP model temps
        model_temps = {k: snap[k] for k in ("ecmwf", "icon", "hrrr", "gem") if snap.get(k) is not None}
        n_models = len(model_temps)

        if n_models >= 2:
            temps = list(model_temps.values())
            mean_t = sum(temps) / n_models
            std_t  = ensemble_stddev(temps)
            snap["ensemble_mean"]   = round(mean_t, 1) if loc["unit"] == "C" else round(mean_t)
            snap["ensemble_std"]    = round(std_t, 2)
            snap["ensemble_models"] = list(model_temps.keys())
            snap["ensemble_n"]      = n_models
        else:
            snap["ensemble_mean"]   = None
            snap["ensemble_std"]    = None
            snap["ensemble_models"] = list(model_temps.keys())
            snap["ensemble_n"]      = n_models

        # Best forecast: ensemble if 3+ models agree tightly, else regional priority
        agree_thresh = ENSEMBLE_AGREE_F if loc["unit"] == "F" else ENSEMBLE_AGREE_C

        if (n_models >= 3
                and snap["ensemble_std"] is not None
                and snap["ensemble_std"] < agree_thresh):
            snap["best"]        = snap["ensemble_mean"]
            snap["best_source"] = "ensemble"
        elif loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"]        = snap["hrrr"]
            snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"]        = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        elif snap["icon"] is not None:
            snap["best"]        = snap["icon"]
            snap["best_source"] = "icon"
        else:
            snap["best"]        = None
            snap["best_source"] = None

        snapshots[date] = snap
    return snapshots

def scan_and_update():
    require_v3_live_confirmation()
    live_reconciliation_ok = True
    try:
        assert_live_reconciliation_safe()
    except RuntimeError as e:
        # Reconciliation problems must block new live exposure, but should not
        # prevent the scan from updating/settling existing markets safely.
        live_reconciliation_ok = False
        print(f"  [LIVE] New bets blocked: {e}")
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    # Hard balance floor — skip all new bets but still resolve/close existing positions
    if BALANCE_FLOOR > 0 and balance < BALANCE_FLOOR:
        print(f"  [FLOOR] Balance ${balance:.2f} below floor ${BALANCE_FLOOR:.2f} — skipping new bets")

    # Cities that already have an active position/order on any date — no double-dipping
    cities_with_open = {
        m["city"] for m in load_all_markets()
        if is_active_position(m.get("position"))
    }

    # Startup gate: if positions already exceed max_open, block new bets
    open_markets = [m for m in load_all_markets() if is_active_position(m.get("position"))]
    over_cap = len(open_markets) >= MAX_OPEN_POSITIONS
    if over_cap:
        print(f"  [STARTUP-GATE] {len(open_markets)}/{MAX_OPEN_POSITIONS} positions open — new bets blocked until count drops")

    for city_slug, loc in LOCATIONS.items():
        unit     = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        _t0_city = time.time()
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates     = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            if mkt["status"] == "resolved":
                continue

            # Update outcomes
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
                    # outcomePrices is [yes_price, no_price] — use yes_price as midpoint
                    # bid/ask spread comes from CLOB; approximate as ±1¢ until repriced
                    bid = max(0.0, round(yes_price - 0.01, 4))
                    ask = round(yes_price + 0.01, 4)
                except Exception:
                    continue
                try:
                    clob_ids     = json.loads(market.get("clobTokenIds", "[]"))
                    yes_token_id = clob_ids[0] if clob_ids else None
                except Exception:
                    yes_token_id = None
                outcomes.append({
                    "question":     question,
                    "market_id":    mid,
                    "range":        rng,
                    "bid":          round(bid, 4),
                    "ask":          round(ask, 4),
                    "price":        round(bid, 4),
                    "spread":       round(ask - bid, 4),
                    "volume":       round(volume, 0),
                    "yes_token_id": yes_token_id,
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":             snap.get("ts"),
                "horizon":        horizon,
                "hours_left":     round(hours, 1),
                "ecmwf":          snap.get("ecmwf"),
                "icon":           snap.get("icon"),
                "hrrr":           snap.get("hrrr"),
                "gem":            snap.get("gem"),
                "metar":          snap.get("metar"),
                "ensemble_mean":  snap.get("ensemble_mean"),
                "ensemble_std":   snap.get("ensemble_std"),
                "ensemble_n":     snap.get("ensemble_n"),
                "best":           snap.get("best"),
                "best_source":    snap.get("best_source"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            mkt["market_snapshots"].append({
                "ts":         snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            })

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # Price stop-loss / trailing stop disabled 2026-04-27: 48h analysis
            # showed 29 stop_loss closures realizing -$8.84, while only 4
            # forecast-changed exits made +$0.43 and 2 settled markets paid
            # +152% / +187%. The thin Polymarket book makes the bid drop 30%+
            # on noise, converting +EV setups into realized losses. Forecast-
            # driven exits and end-of-life resolution remain in place.

            # Close if forecast shifted outside bucket for 2 consecutive scans
            # (prevents whipsaw exits on intraday model oscillations)
            if mkt.get("position") and forecast_temp is not None:
                pos = mkt["position"]
                if pos.get("status") == "open":
                    old_low  = pos["bucket_low"]
                    old_high = pos["bucket_high"]
                    buffer   = 2.0 if unit == "F" else 1.0
                    mid_bucket = (old_low + old_high) / 2 if old_low != -999 and old_high != 999 else forecast_temp
                    forecast_far = abs(forecast_temp - mid_bucket) > (abs(mid_bucket - old_low) + buffer)
                    outside_now = not in_bucket(forecast_temp, old_low, old_high) and forecast_far
                    if outside_now:
                        pos["drift_count"] = pos.get("drift_count", 0) + 1
                    else:
                        pos["drift_count"] = 0
                    if outside_now and pos["drift_count"] >= 2:
                        current_price = None
                        for o in outcomes:
                            if o["market_id"] == pos["market_id"]:
                                current_price = o["price"]
                                break
                        if current_price is not None:
                            if not prepare_live_exit(pos, current_price):
                                save_market(mkt)
                                continue
                            pnl = calculate_exit_pnl(pos, current_price)
                            balance += pos["cost"] + pnl
                            pos["closed_at"]    = snap.get("ts")
                            pos["close_reason"] = "forecast_changed"
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            cities_with_open.discard(city_slug)
                            pos["status"]       = "closed"
                            closed += 1
                            print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # Open new position
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS \
                    and live_reconciliation_ok \
                    and not (BALANCE_FLOOR > 0 and balance < BALANCE_FLOOR) \
                    and not over_cap:
                # Blacklisted cities: track forecast but never bet
                if city_slug in CITY_BLACKLIST:
                    save_market(mkt)
                    continue

                # Per-city position limit: only one open bet per city at a time
                if city_slug in cities_with_open:
                    save_market(mkt)
                    continue

                # Danger zone: use learned thresholds if available
                ens_std = snap.get("ensemble_std")
                if ens_std is not None:
                    _dlo_f, _dhi_f, _dlo_c, _dhi_c = get_learned_danger_zone()
                    _dlo = _dlo_f if loc["unit"] == "F" else _dlo_c
                    _dhi = _dhi_f if loc["unit"] == "F" else _dhi_c
                    if _dlo <= ens_std <= _dhi:
                        save_market(mkt)
                        continue

                sigma = get_sigma(city_slug, best_source or "ecmwf")
                # Sigma inflation by model disagreement: calibrated sigma reflects historical
                # ECMWF error, but when models disagree more than that, true uncertainty is higher.
                # Blend in quadrature so the wider distribution dominates.
                _ens_std_sig = snap.get("ensemble_std")
                if _ens_std_sig is not None and _ens_std_sig > sigma:
                    sigma = round(math.sqrt(sigma * sigma + _ens_std_sig * _ens_std_sig), 3)
                best_signal    = None
                matched_bucket = None

                for o in outcomes:
                    t_low, t_high = o["range"]
                    if in_bucket(forecast_temp, t_low, t_high):
                        matched_bucket = o
                        break

                if matched_bucket:
                    o      = matched_bucket
                    t_low, t_high = o["range"]
                    volume = o["volume"]
                    bid    = o.get("bid", o["price"])
                    ask    = o.get("ask", o["price"])
                    spread = o.get("spread", 0)

                    # Skip early if quote is already above MAX_PRICE — repricing only makes ask higher
                    if ask >= MAX_PRICE:
                        save_market(mkt)
                        continue

                    # Skip exact-match buckets when models disagree severely
                    # (single-degree target with high uncertainty → mostly noise)
                    _ens_std_skip = snap.get("ensemble_std")
                    if _ens_std_skip is not None and t_low == t_high:
                        _severe = 3.0 if loc["unit"] == "F" else 1.8
                        if _ens_std_skip > _severe:
                            save_market(mkt)
                            continue

                    # Skip when ensemble agreement is too tight: 48h analysis showed
                    # the lowest-std slate (0–0.5°F) had 7% win-rate / -$2.49 — the
                    # market had likely already priced in the consensus signal.
                    if _ens_std_skip is not None:
                        _min_std = MIN_ENS_STD_F if loc["unit"] == "F" else MIN_ENS_STD_C
                        if _min_std > 0 and _ens_std_skip < _min_std:
                            save_market(mkt)
                            continue

                    # Exact-match buckets: forecast must be reasonably centered.
                    # Bucket 49 covers actual 48.5-49.5; forecast 48.6 is risky (0.1 from edge).
                    # Skip when forecast is more than ~0.3° from bucket center (≈60% of half-width).
                    if t_low == t_high:
                        center_dist = abs(forecast_temp - t_low)
                        max_dist = 0.30 if loc["unit"] == "F" else 0.20
                        if center_dist > max_dist:
                            save_market(mkt)
                            continue

                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, ask)
                        ev_mult    = get_ev_multiplier(city_slug, best_source or "ecmwf")
                        # Time-scaled EV floor + learned horizon premium + Nicolas adaptive
                        time_premium    = 0.05 * max(0.0, 1.0 - hours / MAX_HOURS)
                        learned_premium = get_learned_ev_premium(f"D+{i}")
                        base_ev         = max(MIN_EV, get_nicolas_adjusted_ev_floor())
                        ev_min_adj      = round((base_ev + time_premium + learned_premium) * ev_mult, 4)
                        # 2026-04-27: cap claimed EV — bets with EV > MAX_EV had 0–12% win-rate.
                        if ev > MAX_EV:
                            print(f"  [SKIP-EV] {loc['name']} {date} — claimed EV {ev:+.2f} above cap {MAX_EV:+.2f}")
                            save_market(mkt)
                            continue
                        if ev >= ev_min_adj:
                            kelly = calc_kelly(p, ask)
                            kelly = get_nicolas_adjusted_kelly(kelly)
                            # Scale bet size by ensemble confidence: tight consensus → full size,
                            # wide spread → reduced size (linear from 1.0 at 0° to 0.5 at 3°F/1.8°C)
                            ens_scale = 1.0
                            if snap.get("ensemble_std") is not None:
                                _max_std = 3.0 if loc["unit"] == "F" else 1.8
                                ens_scale = max(0.5, 1.0 - snap["ensemble_std"] / _max_std / 2)
                            size  = bet_size(kelly * ens_scale, balance)
                            if size >= CLOB_MIN_BET:
                                best_signal = {
                                    "market_id":      o["market_id"],
                                    "question":       o["question"],
                                    "bucket_low":     t_low,
                                    "bucket_high":    t_high,
                                    "entry_price":    ask,
                                    "bid_at_entry":   bid,
                                    "spread":         spread,
                                    "shares":         round(size / ask, 2),
                                    "cost":           size,
                                    "p":              round(p, 4),
                                    "ev":             round(ev, 4),
                                    "ev_mult":        round(ev_mult, 3),
                                    "ev_min_adj":     ev_min_adj,
                                    "ens_scale":      round(ens_scale, 3),
                                    "kelly":          round(kelly, 4),
                                    "forecast_temp":  forecast_temp,
                                    "forecast_src":   best_source,
                                    "ensemble_std":   snap.get("ensemble_std"),
                                    "ensemble_n":     snap.get("ensemble_n"),
                                    "sigma":          sigma,
                                    "opened_at":      snap.get("ts"),
                                    "status":         "open",
                                    "pnl":            None,
                                    "exit_price":     None,
                                    "close_reason":   None,
                                    "closed_at":      None,
                                }

                if best_signal:
                    skip_position = False

                    # Pre-trade analysis: price floor, portfolio gates, model consensus
                    proceed, gate_reason = analyze_signal(
                        best_signal, outcomes, snap, loc, city_slug, date, horizon
                    )
                    if not proceed:
                        skip_position = True

                    if not skip_position:
                        try:
                            r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                            r.raise_for_status()
                            mdata    = r.json()
                            if LIVE_TRADE and (mdata.get("bestAsk") is None or mdata.get("bestBid") is None):
                                raise ValueError("live quote missing bestAsk/bestBid")
                            real_ask = float(mdata.get("bestAsk") if mdata.get("bestAsk") is not None else best_signal["entry_price"])
                            real_bid = float(mdata.get("bestBid") if mdata.get("bestBid") is not None else best_signal["bid_at_entry"])
                            real_spread = round(real_ask - real_bid, 4)
                            if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE:
                                print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                                skip_position = True
                            else:
                                ok, reason = validate_repriced_signal(best_signal, real_ask, real_bid, best_signal["ev_min_adj"])
                                if not ok:
                                    print(f"  [SKIP] {loc['name']} {date} — {reason}")
                                    skip_position = True
                        except Exception as e:
                            print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")
                            if LIVE_TRADE:
                                print("  [SKIP] live quote refresh failed — refusing to trade on stale Gamma proxy prices")
                                skip_position = True

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        projected_cost = estimate_clob_buy_cost(best_signal["entry_price"], best_signal["cost"])
                        if BALANCE_FLOOR > 0 and balance - projected_cost < BALANCE_FLOOR:
                            print(f"  [SKIP] {loc['name']} {date} — submitted cost ${projected_cost:.2f} would breach balance floor ${BALANCE_FLOOR:.2f}")
                            skip_position = True

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        # Place real order if live trading is enabled
                        if LIVE_TRADE:
                            yes_token = matched_bucket.get("yes_token_id")
                            if not yes_token:
                                print(f"  [LIVE] No token_id for {loc['name']} {date} — skipping")
                                skip_position = True
                            else:
                                order = clob_trader.place_buy(
                                    yes_token,
                                    best_signal["entry_price"],
                                    best_signal["cost"],
                                )
                                if order:
                                    best_signal["order_id"]  = order["order_id"]
                                    best_signal["token_id"]  = order["token_id"]
                                    best_signal["shares"]    = order["shares"]
                                    best_signal["cost"]      = order["cost"]
                                    best_signal["status"]    = "pending_buy"
                                    best_signal["entry_status"] = "submitted"
                                    best_signal["needs_reconciliation"] = True
                                    mkt["position"] = best_signal
                                    cities_with_open.add(city_slug)
                                    save_market(mkt)
                                    print(f"  [LIVE] Order submitted: {order['order_id']} (pending fill reconciliation)")

                                    order_status = clob_trader.get_order_status(order["order_id"])
                                    best_signal["entry_status"] = order_status
                                    if order_status == "filled":
                                        best_signal["status"] = "open"
                                        best_signal["needs_reconciliation"] = False
                                    elif order_status == "cancelled":
                                        best_signal["status"] = "closed"
                                        best_signal["exit_status"] = "buy_cancelled"
                                        best_signal["close_reason"] = "buy_cancelled"
                                        best_signal["closed_at"] = datetime.now(timezone.utc).isoformat()
                                        print(f"  [LIVE] Order already cancelled/unfilled: {order['order_id']} — not booking position")
                                        skip_position = True
                                    else:
                                        # A posted GTC order is not a filled position. Persist it for
                                        # reconciliation and refuse to debit accounting until the fill
                                        # is confirmed.
                                        best_signal["needs_reconciliation"] = True
                                        print(f"  [LIVE] Order status {order_status!r}; not booking as filled position")
                                        skip_position = True
                                    save_market(mkt)
                                else:
                                    print(f"  [LIVE] Order FAILED — skipping {loc['name']} {date}")
                                    skip_position = True

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        balance -= best_signal["cost"]
                        mkt["position"] = best_signal
                        cities_with_open.add(city_slug)
                        state["total_trades"] += 1
                        new_pos += 1
                        bucket_label  = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        ens_tag = (f" ens±{best_signal['ensemble_std']:.1f}({best_signal['ensemble_n']}m)"
                                   if best_signal.get("ensemble_std") is not None else "")
                        live_tag = " [LIVE]" if LIVE_TRADE else ""
                        mult_tag  = (f" evx{best_signal['ev_mult']:.2f}" if best_signal.get("ev_mult", 1.0) != 1.0 else "")
                        scale_tag = (f" ens_scale={best_signal['ens_scale']:.2f}" if best_signal.get("ens_scale", 1.0) < 0.99 else "")
                        print(f"  [BUY{live_tag}]  {loc['name']} {horizon} {date} | {bucket_label} | "
                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f}{mult_tag} | "
                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()}){ens_tag}{scale_tag}")

            if should_mark_market_closed_for_no_new_entries(mkt, hours):
                mkt["status"] = "closed"

            save_market(mkt)

        _elapsed = time.time() - _t0_city
        print(f"ok ({_elapsed:.1f}s)")

    def _auto_redeem_if_won(mkt: dict, pos: dict, now_iso: str) -> None:
        """Settle a winning resolved market on-chain. Idempotent. Bot accounting
        is unchanged here — the win was already booked when status flipped to
        resolved. This is purely the wallet-level conversion of CTF tokens
        into USDC.e via NegRiskAdapter (or CTF for non-negRisk markets).
        Failures set ``needs_redemption`` so the next scan retries."""
        if not (AUTO_REDEEM_ON_RESOLVE and LIVE_TRADE):
            return
        if mkt.get("resolved_outcome") != "win":
            return
        if pos.get("redeemed_at"):
            return
        try:
            result = weatherbot_redeem.redeem_market(mkt)
        except Exception as e:
            pos["needs_redemption"] = True
            print(f"  [REDEEM-err] {mkt.get('city_name')} {mkt.get('date')}: {e!r}")
            return
        alert = result.get("alert")
        if alert:
            print(f"  [REDEEM-{alert}]")
        if result.get("redeemed"):
            pos["redeemed_at"]      = now_iso
            pos["redeem_tx"]        = result["tx"]
            pos["redeemed_amount"]  = round(float(result["payout"]), 6)
            pos.pop("needs_redemption", None)
            tx_short = result["tx"][:18] + "…" if result.get("tx") else "?"
            print(f"  [REDEEM]  {mkt.get('city_name')} {mkt.get('date')} +{result['payout']:.4f} USDC.e tx={tx_short}")
        elif result.get("skipped"):
            # Benign: condition not yet reported on-chain, no tokens, already redeemed, etc.
            reason = result.get("reason", "skipped")
            if reason not in ("already_redeemed", "no_tokens_held"):
                print(f"  [REDEEM-skip] {mkt.get('city_name')} {mkt.get('date')}: {reason}")
        else:
            pos["needs_redemption"] = True
            print(f"  [REDEEM-retry] {mkt.get('city_name')} {mkt.get('date')}: {result.get('reason')}")

    # Auto-resolution
    for mkt in load_all_markets():
        # ZOMBIE GUARD: market already resolved but position still open — data corruption from ghost reopening
        if mkt.get("status") == "resolved" and mkt.get("position", {}).get("status") == "open":
            if should_skip_zombie_close(mkt):
                continue
            pos = mkt["position"]
            won = mkt.get("resolved_outcome") == "win"
            price  = pos.get("entry_price", 0.0)
            size   = pos.get("cost", 0.0)
            shares = pos.get("shares", size / price if price else 0.0)
            if shares is None or shares == 0.0:
                shares = size
            pnl = round(shares * (1 - price), 2) if won else round(-size, 2)
            balance += size + pnl
            if won:
                state["wins"] += 1
            else:
                state["losses"] += 1
            pos["exit_price"]   = 1.0 if won else 0.0
            pos["pnl"]          = pnl
            pos["close_reason"] = "resolved"
            pos["closed_at"]    = pos.get("closed_at") or now.isoformat()
            pos["status"]       = "closed"
            pos["learning_recorded"] = True
            mkt["pnl"]          = pnl
            save_market(mkt)
            result = "ZWIN" if won else "ZLOSS"
            print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f} (zombie fix)")
            _auto_redeem_if_won(mkt, pos, pos.get("closed_at") or now.isoformat())
            if pos.get("redeemed_at") or pos.get("needs_redemption"):
                save_market(mkt)
            resolved += 1
            time.sleep(0.3)
            continue

        # ZOMBIE GUARD: market closed but position still open
        if mkt.get("status") == "closed" and mkt.get("position", {}).get("status") == "open":
            if should_skip_zombie_close(mkt):
                continue
            pos = mkt["position"]
            existing_pnl = pos.get("pnl", 0.0) or 0.0
            pos["status"] = "closed"
            pos["learning_recorded"] = True
            pos["closed_at"] = pos.get("closed_at") or now.isoformat()
            save_market(mkt)
            print(f"  [ZCLOSE] {mkt['city_name']} {mkt['date']} | PnL: {existing_pnl:+.2f} (zombie fix)")
            continue

        if mkt.get("status") in ("closed", "resolved"):
            continue
        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue
        market_id = pos.get("market_id")
        if not market_id:
            continue

        if LIVE_TRADE and pos.get("order_id") and not pos.get("close_reason"):
            # Wallet-reconciled positions already proved via balance API / operator pass.
            # Do not let stale CLOB lookups overwrite that.
            if is_wallet_reconciled_held_position(pos):
                entry_status = pos.get("entry_status", "filled_wallet_reconciled")
            else:
                entry_status = clob_trader.get_order_status(pos["order_id"])
            pos["entry_status"] = entry_status
            # Stale "unknown" from old CLOB orders should not block forever.
            # If the market date is in the past, try the market API instead.
            if entry_status == "unknown":
                market_date = mkt.get("date", "")
                try:
                    event_dt = datetime.strptime(market_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if now.date() > event_dt.date():
                        # Market date has passed — try market API, treat unknown as stale
                        won_fallback = check_market_resolved(market_id)
                        if won_fallback is not None:
                            print(f"  [LIVE] Stale unknown order {pos['order_id'][:16]}… for {mkt.get('city_name')} {market_date}; proceeding via market API")
                            entry_status = "filled"  # proceed to resolution below
                        else:
                            print(f"  [LIVE] Resolution blocked for {mkt.get('city_name', mkt.get('city'))} {market_date}: stale unknown order {pos['order_id'][:16]}…, market not closed yet")
                            pos["needs_reconciliation"] = True
                            save_market(mkt)
                            continue
                    else:
                        # Fresh order, legitimately unknown
                        pos["needs_reconciliation"] = True
                        print(f"  [LIVE] Resolution blocked for {mkt.get('city_name', mkt.get('city'))} {market_date}: entry order {pos['order_id']} status {entry_status!r}")
                        save_market(mkt)
                        continue
                except Exception:
                    pos["needs_reconciliation"] = True
                    print(f"  [LIVE] Resolution blocked for {mkt.get('city_name', mkt.get('city'))}: entry order {pos['order_id']} status {entry_status!r}")
                    save_market(mkt)
                    continue
            elif entry_status in ("open", "partial"):
                pos["needs_reconciliation"] = True
                print(f"  [LIVE] Resolution blocked for {mkt.get('city_name', mkt.get('city'))} {mkt.get('date')}: entry order {pos['order_id']} status {entry_status!r}")
                save_market(mkt)
                continue
            if entry_status == "cancelled":
                # Legacy/current code may have debited balance after a posted buy.
                # Confirmed cancelled means no filled position to settle; refund the
                # local reserved cost and keep it out of win/loss learning labels.
                balance += pos.get("cost", 0.0)
                pos["status"] = "closed"
                pos["exit_status"] = "buy_cancelled"
                pos["close_reason"] = "buy_cancelled"
                pos["pnl"] = 0.0
                pos["closed_at"] = now.isoformat()
                mkt["pnl"] = 0.0
                print(f"  [LIVE] Resolution skipped for cancelled entry order {pos['order_id']}; refunded reserved cost")
                save_market(mkt)
                continue

        won = check_market_resolved(market_id)
        if won is None:
            continue

        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        balance += size + pnl
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        # Capture actual temp for forensics (best-effort)
        if mkt.get("actual_temp") is None:
            mkt["actual_temp"] = get_actual_temp(mkt["city"], mkt["date"])

        record_bet_outcome(mkt["city"], pos.get("forecast_src", "ecmwf"), won)
        post_trade_forensics(mkt, won)
        nicolas_record_trade(
            city_slug=mkt["city"],
            bucket_low=pos.get("bucket_low", 0),
            bucket_high=pos.get("bucket_high", 0),
            outcome="win" if won else "loss",
            pnl=pnl,
            cost=pos.get("cost", 0.0),
            kelly=pos.get("kelly", 0.0),
            ev=pos.get("ev", 0.0),
        )

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1
        save_market(mkt)
        _auto_redeem_if_won(mkt, pos, now.isoformat())
        if pos.get("redeemed_at") or pos.get("needs_redemption"):
            save_market(mkt)
        time.sleep(0.3)

    # Ghost-resolution: locally-closed positions still need to feed the learning system.
    # Without this, stop-loss exits never feed win-rate stats or forensics, so the bot
    # cannot learn from its largest loss source.
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue
        pos = mkt.get("position")
        if not pos or pos.get("status") != "closed":
            continue
        if pos.get("exit_status") == "buy_cancelled" or pos.get("close_reason") == "buy_cancelled":
            # An unfilled/cancelled entry is not a market outcome and must not
            # feed win/loss, win-rate, or post-trade learning labels.
            continue
        if pos.get("learning_recorded"):
            continue
        market_id = pos.get("market_id")
        if not market_id:
            continue

        won = check_market_resolved(market_id)
        if won is None:
            continue

        if mkt.get("actual_temp") is None:
            mkt["actual_temp"] = get_actual_temp(mkt["city"], mkt["date"])

        # Promote realized pos.pnl to mkt.pnl so report queries include it
        if mkt.get("pnl") is None and pos.get("pnl") is not None:
            mkt["pnl"] = pos["pnl"]

        record_bet_outcome(mkt["city"], pos.get("forecast_src", "ecmwf"), won)
        post_trade_forensics(mkt, won)
        nicolas_record_trade(
            city_slug=mkt["city"],
            bucket_low=pos.get("bucket_low", 0),
            bucket_high=pos.get("bucket_high", 0),
            outcome="win" if won else "loss",
            pnl=pnl,
            cost=pos.get("cost", 0.0),
            kelly=pos.get("kelly", 0.0),
            ev=pos.get("ev", 0.0),
        )
        pos["learning_recorded"]    = True
        pos["counterfactual_won"]   = won
        mkt["status"]               = "resolved"
        mkt["resolved_outcome"]     = "win" if won else "loss"
        result = "WOULD-WIN" if won else "WOULD-LOSS"
        realized = mkt.get("pnl") if mkt.get("pnl") is not None else 0.0
        print(f"  [GHOST {result}] {mkt['city_name']} {mkt['date']} | realized PnL: {realized:+.2f} (learning recorded)")
        resolved += 1
        save_market(mkt)
        # Even though the bot locally closed at a loss, the wallet may still
        # hold winning CTF tokens (the close was local, no sell-fill). Redeem
        # them so they become USDC.e instead of accumulating as accounting drift.
        _auto_redeem_if_won(mkt, pos, now.isoformat())
        if pos.get("redeemed_at") or pos.get("needs_redemption"):
            save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    if resolved > 0:
        adapt_thresholds()

    if resolved > 0 or new_pos > 0:
        all_mkts       = load_all_markets()
        resolved_count = len([m for m in all_mkts if m.get("status") == "resolved"])
        if resolved_count >= CALIBRATION_MIN:
            global _cal
            _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses
    total_opened = state.get("total_trades", 0)

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    if total:
        print(f"  Trades:      {total_opened} opened | {total} resolved | W: {wins} | L: {losses} | WR: {wins/total:.0%}")
        # Nicolas self-learning stats
        nic = get_nicolas_learning_stats()
        print(f"  Learning:    EV floor {nic['ev_floor']} | Kelly adj {nic['kelly_adj']} | Confidence {nic['confidence']} | PnL {nic['pnl']} (last {nic['trades']} trades)")
    elif total_opened:
        print(f"  Trades:      {total_opened} open, none resolved yet")
    else:
        print(f"  Trades:      none yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m.get("unit") == "F" else "C"
            label    = f"{pos.get('bucket_low', '?')}-{pos.get('bucket_high', '?')}{unit_sym}"

            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current_price = o["price"]
                    break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"
            ens_tag = (f" ens±{pos['ensemble_std']:.1f}({pos['ensemble_n']}m)"
                       if pos.get("ensemble_std") is not None else "")

            opened = pos.get("opened_at", "")
            age_str = ""
            if opened:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(opened)
                    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    age_str = f" | {age_hours:.0f}h ago"
                except Exception:
                    pass

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}{ens_tag}{age_str}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    # Recently closed positions (last 48h, not yet resolved)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    def _is_recently_closed(m):
        if m.get("status") != "resolved" and m.get("position", {}).get("status") == "closed":
            ts = m["position"].get("closed_at", "")
            if ts:
                try:
                    return (now - datetime.fromisoformat(ts)).total_seconds() < 48 * 3600
                except Exception:
                    pass
        return False
    recent_closed = sorted([m for m in markets if _is_recently_closed(m)],
                           key=lambda x: x["position"].get("closed_at", ""), reverse=True)
    if recent_closed:
        print(f"\n  Recently closed:")
        closed_pnl = 0.0
        for m in recent_closed:
            pos      = m["position"]
            unit_sym = "F" if m.get("unit") == "F" else "C"
            label    = f"{pos.get('bucket_low', '?')}-{pos.get('bucket_high', '?')}{unit_sym}"
            reason   = pos.get("close_reason", "") or ""
            reason   = {"forecast_changed": "forecast", "take_profit": "take_prf", "stop_loss": "stop_lss",
                        "trailing_stop": "trail_st", "manual_market_exit": "manual",
                        "unfilled_buy_cancelled_reconciliation": "unfilled",
                        "ghost_closed_no_wallet": "ghost"}.get(reason, reason)
            pnl      = round(pos.get("pnl", 0.0) or 0.0, 2)
            closed_pnl += pnl
            pnl_str  = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
            age_h    = (now - datetime.fromisoformat(pos["closed_at"])).total_seconds() / 3600
            city     = LOCATIONS.get(m["city"], {}).get("name", m["city"])
            print(f"    {city:<16} {m['date']} | {label:<14} | reason: {reason:<10} | "
                  f"PnL: {pnl_str} | {age_h:.0f}h ago")
        sign = "+" if closed_pnl >= 0 else ""
        print(f"\n  Recently closed PnL: {sign}{closed_pnl:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    # Performance by forecast source
    source_stats: dict = {}
    for m in resolved:
        pos = m.get("position", {})
        src = pos.get("forecast_src", "unknown") if pos else "unknown"
        if src not in source_stats:
            source_stats[src] = {"w": 0, "l": 0, "pnl": 0.0}
        if m["resolved_outcome"] == "win":
            source_stats[src]["w"] += 1
        else:
            source_stats[src]["l"] += 1
        source_stats[src]["pnl"] += m["pnl"]

    print(f"\n  By forecast source:")
    for src, s in sorted(source_stats.items()):
        n    = s["w"] + s["l"]
        wr   = s["w"] / n if n else 0
        pnl  = s["pnl"]
        print(f"    {src.upper():<10} {s['w']}/{n} ({wr:.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = (m.get("resolved_outcome") or "unknown").upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        closed_ts = pos.get("closed_at", "")
        age_str = ""
        if closed_ts:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(closed_ts)
                age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                age_str = f"{age_h:.0f}h ago | "
            except Exception:
                pass
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {age_str}{result} {pnl_str}")

    wr_rows = win_rate_summary()
    if wr_rows:
        print(f"\n  Win-rate calibration (city|source → EV multiplier):")
        print(f"  {'city':<16} {'source':<10} {'bets':>5} {'wins':>5} {'win%':>6} {'ev_mult':>8}")
        for r in wr_rows:
            flag = " ↑" if r["ev_mult"] > 1.1 else (" ↓" if r["ev_mult"] < 0.9 else "")
            print(f"  {r['city']:<16} {r['source']:<10} {r['bets']:>5} {r['wins']:>5} "
                  f"{r['win_rate']*100:>5.1f}% {r['ev_mult']:>8.3f}{flag}")

    journal = _load_journal()
    if journal:
        print(f"\n  Trade forensics ({len(journal)} trades):")
        for label, group in [
            ("By price tier",   lambda e: e.get("price_tier", "?")),
            ("By ensemble",     lambda e: e.get("ens_bucket", "?")),
            ("By horizon",      lambda e: e.get("horizon", "?")),
        ]:
            buckets: dict = {}
            for e in journal:
                k = group(e)
                buckets.setdefault(k, {"w": 0, "n": 0, "pnl": 0.0})
                buckets[k]["n"] += 1
                buckets[k]["w"] += int(e.get("won", False))
                buckets[k]["pnl"] += e.get("pnl") or 0.0
            print(f"  {label}:")
            for k, v in sorted(buckets.items()):
                wr = v["w"] / v["n"] if v["n"] else 0
                print(f"    {k:<20} {v['w']}/{v['n']} ({wr:.0%})  PnL: {v['pnl']:+.2f}")

    learned = _load_learned()
    if learned:
        print(f"\n  Learned parameter adjustments:")
        for k, v in sorted(learned.items()):
            print(f"    {k:<35} {v}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600

def _fetch_market_bid(mid: str) -> float | None:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
        best_bid = r.json().get("bestBid")
        return float(best_bid) if best_bid is not None else None
    except Exception:
        return None


def monitor_positions():
    import concurrent.futures as _cf
    markets  = load_all_markets()
    # composite markets (e.g. highest-temperature) may have no market_id in position
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open" and m["position"].get("market_id")]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    # Fetch all prices in parallel
    mids = [m["position"]["market_id"] for m in open_pos]
    with _cf.ThreadPoolExecutor(max_workers=min(len(mids), 8)) as pool:
        prices = dict(zip(mids, pool.map(_fetch_market_bid, mids)))

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        current_price = prices.get(mid)
        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o.get("bid", o["price"])
                    break

        if current_price is None:
            continue

        entry      = pos["entry_price"]
        city_name  = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        end_date   = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        # Take-profit only — price stop-loss / trailing stop disabled 2026-04-27.
        # See scan_and_update for the rationale.
        if hours_left < 24:
            take_profit = None
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        if take_profit is not None and current_price >= take_profit:
            if not prepare_live_exit(pos, current_price):
                save_market(mkt)
                continue
            pnl = calculate_exit_pnl(pos, current_price)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            pos["close_reason"] = "take_profit"
            pos["exit_price"]   = current_price
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            closed += 1
            print(f"  [TAKE] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed

def run_loop():
    require_v3_live_confirmation()
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STARTING")
    print(f"{'='*55}")

    if LIVE_TRADE:
        try:
            print(live_startup_balance_message())
            assert_live_reconciliation_safe()
            print("  Reconcile:  local/CLOB state OK")
        except Exception as e:
            print(f"  [SAFETY] Live startup blocked: {e}")
            raise SystemExit(2)
    else:
        print(f"  Mode:       PAPER")
    _startup_state = load_state()
    print(f"  Cities:     {len(LOCATIONS)} tracked | {len(CITY_BLACKLIST)} blacklisted (no bets)")
    print(f"  Balance:    ${_startup_state['balance']:,.2f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + ICON + GFS/HRRR(US) + GEM(Americas) + METAR(D+0)")
    print(f"  EV gate:    {max(MIN_EV, get_nicolas_adjusted_ev_floor()):+.2f} <= ev <= {MAX_EV:+.2f}  | min ens_std {MIN_ENS_STD_F}°F / {MIN_ENS_STD_C}°C")
    nic = get_nicolas_learning_stats()
    print(f"  Kelly adj:  {nic['kelly_adj']} | EV floor: {nic['ev_floor']} | Confidence: {nic['confidence']} (last {nic['trades']} trades)")
    print(f"  DangerZone: skip bets when ens_std {ENSEMBLE_DANGER_LO_F}-{ENSEMBLE_DANGER_HI_F}°F / {ENSEMBLE_DANGER_LO_C}-{ENSEMBLE_DANGER_HI_C}°C")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Error: {e} — waiting 60 sec")
                time.sleep(60)
                continue
        else:
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    else:
        print("Usage: python bot_v3.py [run|status|report]")
