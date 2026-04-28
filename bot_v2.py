#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_v2.py — Weather Trading Bot for Polymarket (v2, live-capable)
==================================================================
Tracks weather forecasts from 3 sources (ECMWF, HRRR, METAR), compares with
Polymarket markets, places real CLOB orders when enabled.

v2 reads its own config (``config_v2.json``) and writes its own state
(``data/state_v2.json``) and market files (``data/markets_v2/``) so it can run
alongside bot_v3 on the same wallet without overwriting v3's accounting.
"""

import re
import sys
import json
import math
import os
import time
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# =============================================================================
# CONFIG
# =============================================================================

BASE_DIR  = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config_v2.json"

with open(CONFIG_PATH, encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 0.0)
MAX_BET          = _cfg.get("max_bet", 2.25)
MIN_EV           = _cfg.get("min_ev", 0.15)
MAX_PRICE        = _cfg.get("max_price", 0.50)
MIN_PRICE        = _cfg.get("min_price", 0.08)
MIN_VOLUME       = _cfg.get("min_volume", 300)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")

MAX_UNREALIZED_LOSS = _cfg.get("max_unrealized_loss", -5.0)
MAX_OPEN_POSITIONS  = _cfg.get("max_open_positions", 10)
BALANCE_FLOOR       = _cfg.get("balance_floor", 0.0)

RAW_LIVE_TRADE    = bool(_cfg.get("live_trade", False))
V2_LIVE_CONFIRMED = bool(_cfg.get("v2_live_confirmed", False))
LIVE_TRADE        = RAW_LIVE_TRADE and V2_LIVE_CONFIRMED


def require_v2_live_confirmation():
    """Refuse to start live without an explicit acknowledgement.

    v2 historically used a flat ``bucket_prob`` that returned p=1.0 for any
    matched bucket, which over-bet Kelly. The probability model has been
    replaced with v3's normal-CDF integration; this gate prevents an old
    ``live_trade=true`` config from quietly re-launching v2 against the
    pre-fix code without an explicit confirmation that the operator has
    reviewed the new behaviour.
    """
    if RAW_LIVE_TRADE and not V2_LIVE_CONFIRMED:
        print(
            "[SAFETY] bot_v2 live trading is blocked: config has live_trade=true "
            "but v2_live_confirmed is not true. Review the probability-model fix "
            "and bankroll setup, then add \"v2_live_confirmed\": true to "
            "config_v2.json to permit live v2 orders.",
            file=sys.stderr,
        )
        raise SystemExit(2)


if LIVE_TRADE:
    import clob_trader

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state_v2.json"
MARKETS_DIR      = DATA_DIR / "markets_v2"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration_v2.json"

# v3 markets dir — read-only; used to avoid double-buying a market v3 already
# holds an active position in (same wallet, so a duplicate buy would stack
# exposure on the same YES token).
V3_MARKETS_DIR = DATA_DIR / "markets"

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# I/O HELPERS
# =============================================================================

class StateIntegrityError(RuntimeError):
    """Persisted bot state could not be safely loaded."""


def atomic_json_write(path: Path, obj) -> None:
    """Atomic JSON write so accounting/market files are never truncated."""
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
            pass
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


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

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability the actual temperature lands in the market bucket.

    Treat forecast error as normal with stddev ``sigma``. Half-degree
    continuity window for exact integer buckets and open-ended edge buckets;
    finite Polymarket ranges are integrated exactly. This replaces v2's
    earlier flat 1.0/0.0 indicator, which over-bet Kelly on matched buckets.
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


def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)


CLOB_MIN_BET = 1.0
CLOB_MIN_SELL_SHARES = 5.0
ACTIVE_POSITION_STATUSES = {"open", "pending_buy"}


def is_sellable_share_size(shares):
    return float(shares or 0) >= CLOB_MIN_SELL_SHARES


def is_active_position(pos):
    if not pos:
        return False
    return pos.get("status") in ACTIVE_POSITION_STATUSES or bool(pos.get("needs_reconciliation"))


def clob_buy_sizing(price: float, size_usd: float) -> dict:
    price = round(float(price), 2)
    if not math.isfinite(price) or price <= 0:
        return {"price": price, "shares": 0.0, "cost": 0.0, "sellable": False}
    shares = math.ceil(float(size_usd) / price * 100) / 100
    cost = round(price * shares, 2)
    return {"price": price, "shares": shares, "cost": cost, "sellable": is_sellable_share_size(shares)}


def estimate_clob_buy_cost(price: float, size_usd: float) -> float:
    return clob_buy_sizing(price, size_usd)["cost"]


def validate_repriced_signal(signal, real_ask, real_bid, min_ev=None):
    """Rebuild signal sizing/EV against a fresh CLOB quote and reject if it no longer qualifies."""
    if min_ev is None:
        min_ev = MIN_EV
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

    signal["entry_price"]  = sizing["price"]
    signal["bid_at_entry"] = real_bid
    signal["spread"]       = real_spread
    signal["shares"]       = sizing["shares"]
    signal["cost"]         = sizing["cost"]
    signal["ev"]           = round(calc_ev(signal["p"], sizing["price"]), 4)
    signal["kelly"]        = calc_kelly(signal["p"], sizing["price"])

    if signal["ev"] < min_ev:
        return False, f"EV {signal['ev']:+.4f} below min {min_ev:+.4f} after repricing"
    if not sizing["sellable"]:
        return False, f"shares {signal['shares']:.2f} below sell minimum {CLOB_MIN_SELL_SHARES:.0f}"
    return True, None

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}


def load_cal():
    if CALIBRATION_FILE.exists():
        return _read_json_file(CALIBRATION_FILE, expected_type=dict, default_on_error={})
    return {}


def get_sigma(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C


def run_calibration(markets):
    resolved = [m for m in markets if m.get("resolved_outcome") in ("win", "loss") and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s.get(source) is not None), None)
                if snap and snap.get(source) is not None:
                    errors.append(abs(snap[source] - m["actual_temp"]))
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

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    loc  = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result


def get_hrrr(city_slug, dates):
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [HRRR] {city_slug}: {e}")
    return result


def get_metar(city_slug):
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None


def get_actual_temp(city_slug, date_str):
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
        if yes_price <= 0.05:
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
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE (separate from v3)
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"


def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return _read_json_file(p, expected_type=dict)
    return None


def save_market(market):
    atomic_json_write(market_path(market["city"], market["date"]), market)


def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        markets.append(_read_json_file(f, expected_type=dict))
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


def v3_active_market_ids() -> set[str]:
    """Market IDs v3 currently has an active position on.

    Same wallet → a v2 buy on the same YES token would simply add to v3's
    exposure. Skip those markets so the two bots never stack on the same
    bucket. v3 already de-duplicates within its own dir; this guard is
    one-directional (v2 yields to v3).
    """
    ids: set[str] = set()
    if not V3_MARKETS_DIR.exists():
        return ids
    for f in V3_MARKETS_DIR.glob("*.json"):
        try:
            mkt = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pos = mkt.get("position") or {}
        if is_active_position(pos):
            mid = pos.get("market_id")
            if mid:
                ids.add(str(mid))
    return ids

# =============================================================================
# STATE (separate from v3)
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

# =============================================================================
# LIVE-MODE SAFETY
# =============================================================================

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


def is_wallet_reconciled_held_position(pos) -> bool:
    if not pos:
        return False
    if pos.get("entry_status") != "filled_wallet_reconciled":
        return False
    if not pos.get("wallet_reconciled_at"):
        return False
    try:
        return float(pos.get("wallet_shares") or 0) > 0
    except (TypeError, ValueError):
        return False


def assert_live_reconciliation_safe() -> bool:
    """Fail closed if v2's local state and CLOB account state disagree.

    Note: v2 and v3 share a wallet, so CLOB's open-orders list will include
    v3's orders. We only flag CLOB orders that are unknown to BOTH bots.
    """
    if not LIVE_TRADE:
        return True

    v2_markets = load_all_markets()
    v2_order_ids = _local_live_order_ids(v2_markets)

    v3_order_ids: set[str] = set()
    if V3_MARKETS_DIR.exists():
        for f in V3_MARKETS_DIR.glob("*.json"):
            try:
                mkt = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            pos = mkt.get("position") or {}
            for key in ("order_id", "exit_order_id"):
                if pos.get(key):
                    v3_order_ids.add(str(pos[key]))

    known_order_ids = v2_order_ids | v3_order_ids
    issues: list[str] = []

    get_open_orders = getattr(clob_trader, "get_open_orders", lambda: [])
    for order in get_open_orders():
        oid = _order_identifier(order)
        if oid and oid not in known_order_ids:
            issues.append(f"untracked live CLOB open order {oid}")

    for market in v2_markets:
        pos = market.get("position") or {}
        if not pos:
            continue
        label = f"{market.get('city', '?')} {market.get('date', '?')}"
        if is_wallet_reconciled_held_position(pos):
            if pos.get("status") != "open" or pos.get("needs_reconciliation"):
                pos["status"] = "open"
                pos["needs_reconciliation"] = False
                save_market(market)
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

    if issues:
        joined = "; ".join(issues[:5])
        if len(issues) > 5:
            joined += f"; +{len(issues) - 5} more"
        raise RuntimeError(f"Live reconciliation failed: {joined}")
    return True


def prepare_live_exit(pos, current_price):
    """Coordinate a live close so we never orphan exposure on the CLOB.

    Paper mode: no-op. Live: cancel an unfilled buy, place a sell on a
    filled buy, monitor exit orders, fail closed on unknown statuses.
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
            pos["needs_reconciliation"] = True
            print(f"  [LIVE] Exit order partially filled: {exit_order_id}; reconciliation required")
            return False
        if exit_status == "cancelled":
            print(f"  [LIVE] Exit order cancelled: {exit_order_id}; clearing exit to retry")
            pos.pop("exit_order_id", None)
            pos.pop("exit_status", None)
            return False
        pos["exit_unknown_count"] = pos.get("exit_unknown_count", 0) + 1
        pos["needs_reconciliation"] = True
        print(f"  [LIVE] Exit order status {exit_status!r}; keeping position open ({pos['exit_unknown_count']} unknown)")
        return False

    order_id = pos.get("order_id")
    if not order_id:
        print("  [LIVE] Cannot close locally: missing order_id")
        return False

    status = clob_trader.get_order_status(order_id)
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
        print(f"  [LIVE] Buy order partial: {order_id}; reconciliation required before close")
        return False

    if status == "filled":
        token_id = pos.get("token_id")
        shares = pos.get("shares")
        if not token_id or not shares:
            print("  [LIVE] Cannot sell filled position: missing token_id/shares")
            return False
        if not is_sellable_share_size(shares):
            print(f"  [LIVE] Holding to resolution: shares {shares:.2f} below CLOB sell minimum {CLOB_MIN_SELL_SHARES}")
            return False
        sell = clob_trader.place_sell(token_id, current_price, shares)
        if sell:
            pos["exit_order_id"] = sell.get("order_id")
            pos["exit_status"] = "open"
            print(f"  [LIVE] Sell order placed: {sell.get('order_id')}")
            return False
        print("  [LIVE] Sell FAILED; keeping position open")
        return False

    if status == "cancelled":
        print(f"  [LIVE] Order already cancelled: {order_id}")
        return True

    pos["unknown_count"] = pos.get("unknown_count", 0) + 1
    pos["needs_reconciliation"] = True
    print(f"  [LIVE] Unknown order status {status!r} for {order_id} ({pos['unknown_count']} unknown — reconciliation required)")
    return False


def calculate_exit_pnl(pos, current_price):
    if pos.get("exit_status") == "buy_cancelled":
        return 0.0
    return round((current_price - pos["entry_price"]) * pos["shares"], 2)


def live_startup_balance_message():
    real_bal = clob_trader.get_balance()
    state = load_state()
    return f"  Mode:       LIVE  (wallet ${real_bal:.2f} USDC; v2 accounting ${state.get('balance', 0.0):.2f})"

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    hrrr    = get_hrrr(city_slug, dates)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hrrr_cutoff = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= hrrr_cutoff else None,
            "metar": get_metar(city_slug) if date == today else None,
        }
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"] = snap["hrrr"]; snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]; snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None; snap["best_source"] = None
        snapshots[date] = snap
    return snapshots


def scan_and_update():
    require_v2_live_confirmation()

    live_reconciliation_ok = True
    try:
        assert_live_reconciliation_safe()
    except RuntimeError as e:
        live_reconciliation_ok = False
        print(f"  [LIVE] New bets blocked: {e}")

    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    if BALANCE_FLOOR > 0 and balance < BALANCE_FLOOR:
        print(f"  [FLOOR] Balance ${balance:.2f} below floor ${BALANCE_FLOOR:.2f} — skipping new bets")

    cities_with_open = {
        m["city"] for m in load_all_markets()
        if is_active_position(m.get("position"))
    }
    open_position_count = sum(
        1 for m in load_all_markets()
        if is_active_position(m.get("position"))
    )

    blocked_market_ids = v3_active_market_ids()

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
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

            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
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
            mkt["forecast_snapshots"].append({
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "hrrr":        snap.get("hrrr"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            })

            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            mkt["market_snapshots"].append({
                "ts":         snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            })

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # Pending-buy reconciliation: if a previous scan submitted an order,
            # poll its status before doing anything else with this market.
            if LIVE_TRADE and mkt.get("position") and mkt["position"].get("status") == "pending_buy":
                pos = mkt["position"]
                order_id = pos.get("order_id")
                if order_id:
                    st = clob_trader.get_order_status(order_id)
                    pos["entry_status"] = st
                    if st == "filled":
                        balance -= pos["cost"]
                        pos["status"] = "open"
                        pos["needs_reconciliation"] = False
                        cities_with_open.add(city_slug)
                        open_position_count += 1
                        state["total_trades"] += 1
                        new_pos += 1
                        print(f"  [LIVE] Pending fill confirmed: {order_id}")
                    elif st == "cancelled":
                        pos["status"] = "closed"
                        pos["exit_status"] = "buy_cancelled"
                        pos["close_reason"] = "buy_cancelled"
                        pos["closed_at"] = now.isoformat()
                        pos["pnl"] = 0.0
                        mkt["pnl"] = 0.0
                        print(f"  [LIVE] Pending order cancelled: {order_id}")
                save_market(mkt)

            # Stop-loss / trailing stop on bid
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o.get("bid", o["price"])
                        break

                if current_price is not None:
                    entry     = pos["entry_price"]
                    entry_ref = pos.get("bid_at_entry") or (entry * 0.92)
                    stop      = pos.get("stop_price", entry_ref * 0.65)

                    if current_price >= entry and stop < entry_ref:
                        pos["stop_price"] = entry_ref
                        pos["trailing_activated"] = True

                    if current_price <= stop:
                        if not prepare_live_exit(pos, current_price):
                            save_market(mkt)
                            continue
                        pnl = calculate_exit_pnl(pos, current_price)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        cities_with_open.discard(city_slug)
                        open_position_count -= 1
                        closed += 1
                        reason = "STOP" if current_price < entry else "TRAILING BE"
                        print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # Forecast-changed exit (2-scan drift confirmation)
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
                            pos["status"]       = "closed"
                            cities_with_open.discard(city_slug)
                            open_position_count -= 1
                            closed += 1
                            print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # Open new position
            if (not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS
                    and live_reconciliation_ok
                    and not (BALANCE_FLOOR > 0 and balance < BALANCE_FLOOR)
                    and open_position_count < MAX_OPEN_POSITIONS):
                if city_slug in cities_with_open:
                    save_market(mkt)
                    continue

                sigma = get_sigma(city_slug, best_source or "ecmwf")
                best_signal    = None
                matched_bucket = None

                for o in outcomes:
                    t_low, t_high = o["range"]
                    if in_bucket(forecast_temp, t_low, t_high):
                        matched_bucket = o
                        break

                if matched_bucket:
                    o = matched_bucket
                    if str(o["market_id"]) in blocked_market_ids:
                        print(f"  [SKIP] {loc['name']} {date} — v3 already holds {o['market_id']}")
                        save_market(mkt)
                        continue

                    t_low, t_high = o["range"]
                    volume = o["volume"]
                    bid    = o.get("bid", o["price"])
                    ask    = o.get("ask", o["price"])
                    spread = o.get("spread", 0)

                    if ask >= MAX_PRICE or ask < MIN_PRICE:
                        save_market(mkt)
                        continue

                    if t_low == t_high:
                        center_dist = abs(forecast_temp - t_low)
                        max_dist = 0.30 if unit == "F" else 0.20
                        if center_dist > max_dist:
                            save_market(mkt)
                            continue

                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, ask)
                        if ev >= MIN_EV:
                            kelly = calc_kelly(p, ask)
                            size  = bet_size(kelly, balance)
                            if size >= CLOB_MIN_BET:
                                best_signal = {
                                    "market_id":     o["market_id"],
                                    "question":      o["question"],
                                    "bucket_low":    t_low,
                                    "bucket_high":   t_high,
                                    "entry_price":   ask,
                                    "bid_at_entry":  bid,
                                    "spread":        spread,
                                    "shares":        round(size / ask, 2),
                                    "cost":          size,
                                    "p":             round(p, 4),
                                    "ev":            round(ev, 4),
                                    "kelly":         round(kelly, 4),
                                    "forecast_temp": forecast_temp,
                                    "forecast_src":  best_source,
                                    "sigma":         sigma,
                                    "opened_at":     snap.get("ts"),
                                    "status":        "open",
                                    "pnl":           None,
                                    "exit_price":    None,
                                    "close_reason":  None,
                                    "closed_at":     None,
                                }

                if best_signal:
                    skip_position = False
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        r.raise_for_status()
                        mdata = r.json()
                        if LIVE_TRADE and (mdata.get("bestAsk") is None or mdata.get("bestBid") is None):
                            raise ValueError("live quote missing bestAsk/bestBid")
                        real_ask = float(mdata.get("bestAsk") if mdata.get("bestAsk") is not None else best_signal["entry_price"])
                        real_bid = float(mdata.get("bestBid") if mdata.get("bestBid") is not None else best_signal["bid_at_entry"])
                        real_spread = round(real_ask - real_bid, 4)
                        if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE or real_ask < MIN_PRICE:
                            print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            ok, reason = validate_repriced_signal(best_signal, real_ask, real_bid)
                            if not ok:
                                print(f"  [SKIP] {loc['name']} {date} — {reason}")
                                skip_position = True
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")
                        if LIVE_TRADE:
                            print("  [SKIP] live quote refresh failed — refusing stale Gamma proxy prices")
                            skip_position = True

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        projected_cost = estimate_clob_buy_cost(best_signal["entry_price"], best_signal["cost"])
                        if BALANCE_FLOOR > 0 and balance - projected_cost < BALANCE_FLOOR:
                            print(f"  [SKIP] {loc['name']} {date} — submitted cost ${projected_cost:.2f} would breach floor ${BALANCE_FLOOR:.2f}")
                            skip_position = True

                    if not skip_position and LIVE_TRADE:
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
                                open_position_count += 1
                                blocked_market_ids.add(str(best_signal["market_id"]))
                                save_market(mkt)
                                print(f"  [LIVE] Order submitted: {order['order_id']} (pending fill)")

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
                                    cities_with_open.discard(city_slug)
                                    open_position_count -= 1
                                    print(f"  [LIVE] Order cancelled before fill: {order['order_id']}")
                                    skip_position = True
                                else:
                                    # Pending fill: persist as pending_buy, don't debit yet
                                    print(f"  [LIVE] Order status {order_status!r}; held pending")
                                    skip_position = True
                                save_market(mkt)
                            else:
                                print(f"  [LIVE] Order FAILED — skipping {loc['name']} {date}")
                                skip_position = True

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        balance -= best_signal["cost"]
                        mkt["position"] = best_signal
                        cities_with_open.add(city_slug)
                        open_position_count += 1
                        blocked_market_ids.add(str(best_signal["market_id"]))
                        state["total_trades"] += 1
                        new_pos += 1
                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        live_tag = " [LIVE]" if LIVE_TRADE else ""
                        print(f"  [BUY{live_tag}]  {loc['name']} {horizon} {date} | {bucket_label} | "
                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()})")

            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # Auto-resolution
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue
        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue
        market_id = pos.get("market_id")
        if not market_id:
            continue

        if LIVE_TRADE and pos.get("order_id"):
            entry_status = clob_trader.get_order_status(pos["order_id"])
            pos["entry_status"] = entry_status
            if entry_status in ("open", "partial", "unknown"):
                pos["needs_reconciliation"] = True
                print(f"  [LIVE] Resolution blocked for {mkt.get('city_name', mkt.get('city'))} {mkt.get('date')}: entry order {pos['order_id']} status {entry_status!r}")
                save_market(mkt)
                continue
            if entry_status == "cancelled":
                balance += pos.get("cost", 0.0)
                pos["status"] = "closed"
                pos["exit_status"] = "buy_cancelled"
                pos["close_reason"] = "buy_cancelled"
                pos["pnl"] = 0.0
                pos["closed_at"] = now.isoformat()
                mkt["pnl"] = 0.0
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

        if won: state["wins"] += 1
        else:   state["losses"] += 1

        try:
            actual = get_actual_temp(mkt["city"], mkt["date"])
            if actual is not None:
                mkt["actual_temp"] = actual
        except Exception:
            pass

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
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
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = ((bal - start) / start * 100) if start else 0.0
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v2 — STATUS")
    print(f"{'='*55}")
    print(f"  Mode:        {'LIVE' if LIVE_TRADE else 'PAPER'}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    if total:
        print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}")
    else:
        print(f"  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"
            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current_price = o["price"]
                    break
            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"
            src = (pos.get('forecast_src') or '').upper()
            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {src}")
        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")


def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v2 — FULL REPORT")
    print(f"{'='*55}")
    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins   = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses = [m for m in resolved if m["resolved_outcome"] == "loss"]

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

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m.get("actual_temp") else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")
    print(f"{'='*55}\n")

# =============================================================================
# MONITOR
# =============================================================================

MONITOR_INTERVAL = 600


def _fetch_market_bid(mid: str):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
        best_bid = r.json().get("bestBid")
        return float(best_bid) if best_bid is not None else None
    except Exception:
        return None


def monitor_positions():
    import concurrent.futures as _cf
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

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
        entry_ref  = pos.get("bid_at_entry") or (entry * 0.92)
        stop       = pos.get("stop_price", entry_ref * 0.65)
        city_name  = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        end_date   = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        if hours_left < 24:
            take_profit = None
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        if current_price >= entry and stop < entry_ref:
            pos["stop_price"] = entry_ref
            pos["trailing_activated"] = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to entry-bid ${entry_ref:.3f}")

        take_triggered = take_profit is not None and current_price >= take_profit
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            if not prepare_live_exit(pos, current_price):
                save_market(mkt)
                continue
            pnl = calculate_exit_pnl(pos, current_price)
            balance += pos["cost"] + pnl
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()
            if take_triggered:
                pos["close_reason"] = "take_profit"; reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss";   reason = "STOP"
            else:
                pos["close_reason"] = "trailing_stop"; reason = "TRAILING BE"
            pos["exit_price"] = current_price
            pos["pnl"]        = pnl
            pos["status"]     = "closed"
            closed += 1
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)
    return closed

# =============================================================================
# MAIN LOOP
# =============================================================================

def run_loop():
    require_v2_live_confirmation()
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v2 — STARTING")
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
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${_startup_state['balance']:,.2f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + HRRR(US) + METAR(D+0)")
    print(f"  Data:       {DATA_DIR.resolve()}  (state_v2.json, markets_v2/)")
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
        print("Usage: python bot_v2.py [run|status|report]")
