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
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)
MIN_EV           = _cfg.get("min_ev", 0.10)
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
        print(f"  [LIVE] Exit order status {exit_status!r} for {exit_order_id}; keeping position open")
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

    print(f"  [LIVE] Cannot close locally: unknown order status {status!r} for {order_id}")
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
ENSEMBLE_SIGMA_REDUCTION = 0.80  # reduce sigma by 20% on strong consensus

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

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
WIN_RATE_FILE    = DATA_DIR / "win_rates.json"

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

    Treat forecast error as normal with standard deviation ``sigma``.  v3's
    backtester uses this continuous probability model; the live scanner must use
    the same model or every matched finite bucket is scored as p=1.0, producing
    dangerously inflated EV/Kelly sizing.
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

def is_sellable_share_size(shares):
    """Return True when a position is large enough to submit a CLOB sell."""
    return float(shares or 0) >= CLOB_MIN_SELL_SHARES


def validate_repriced_signal(signal, real_ask, real_bid, min_ev=MIN_EV):
    """Apply the live orderbook quote and reject signals that no longer qualify.

    The scanner first computes EV from the cached Gamma market quote, then fetches
    a fresh ask/bid just before placing an order. A signal must still clear the EV
    threshold after that repricing, and the resulting share count must be large
    enough to be sellable later through the CLOB.
    """
    real_ask = float(real_ask)
    real_bid = float(real_bid)
    real_spread = round(real_ask - real_bid, 4)

    signal["entry_price"] = real_ask
    signal["bid_at_entry"] = real_bid
    signal["spread"] = real_spread
    signal["shares"] = round(float(signal["cost"]) / real_ask, 2)
    signal["ev"] = round(calc_ev(signal["p"], real_ask), 4)
    signal["kelly"] = calc_kelly(signal["p"], real_ask)

    if signal["ev"] < min_ev:
        return False, f"EV {signal['ev']:+.4f} below min {min_ev:+.4f} after repricing"
    if not is_sellable_share_size(signal["shares"]):
        return False, f"shares {signal['shares']:.2f} below sell minimum {CLOB_MIN_SELL_SHARES:.0f}"
    return True, None


def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

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
    resolved = [m for m in markets if m.get("resolved") and m.get("actual_temp") is not None]
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

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal


def _load_win_rates() -> dict:
    if WIN_RATE_FILE.exists():
        return json.loads(WIN_RATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_win_rates(data: dict) -> None:
    WIN_RATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    """GFS seamless (HRRR+GFS) via Open-Meteo. US cities, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    unit = loc["unit"]
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

def get_gem(city_slug, dates):
    """GEM seamless (Canadian) via Open-Meteo. Americas only."""
    loc = LOCATIONS[city_slug]
    if loc["region"] not in ("us", "ca", "sa"):
        return {}
    return _open_meteo_fetch(city_slug, dates, "gem_seamless", "GEM")

def _metar_fetch(station, unit):
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
    data = requests.get(url, timeout=(3, 5)).json()
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
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
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
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches all model forecasts, computes ensemble consensus, returns snapshots."""
    now_str = datetime.now(timezone.utc).isoformat()
    loc     = LOCATIONS[city_slug]
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ecmwf = get_ecmwf(city_slug, dates)
    icon  = get_icon(city_slug, dates)
    hrrr  = get_hrrr(city_slug, dates)   # US only
    gem   = get_gem(city_slug, dates)    # Americas only

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
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    # Cities that already have an open position on any date — no double-dipping
    cities_with_open = {
        m["city"] for m in load_all_markets()
        if m.get("position") and m["position"].get("status") == "open"
    }

    for city_slug, loc in LOCATIONS.items():
        unit     = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates     = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
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
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    bid = float(prices[0])
                    ask = float(prices[1]) if len(prices) > 1 else bid
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

            # Stop-loss and trailing stop
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o.get("bid", o["price"])
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * 0.80)

                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"]         = entry
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
                        closed += 1
                        reason = "STOP" if current_price < entry else "TRAILING BE"
                        print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

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
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                # Blacklisted cities: track forecast but never bet
                if city_slug in CITY_BLACKLIST:
                    save_market(mkt)
                    continue

                # Per-city position limit: only one open bet per city at a time
                if city_slug in cities_with_open:
                    save_market(mkt)
                    continue

                # Danger zone: ensemble std in a range where models look close but
                # historical error is anomalously high (MAE 4.4° vs 2.5° baseline)
                ens_std = snap.get("ensemble_std")
                if ens_std is not None:
                    _dlo = ENSEMBLE_DANGER_LO_F if loc["unit"] == "F" else ENSEMBLE_DANGER_LO_C
                    _dhi = ENSEMBLE_DANGER_HI_F if loc["unit"] == "F" else ENSEMBLE_DANGER_HI_C
                    if _dlo <= ens_std <= _dhi:
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
                    o      = matched_bucket
                    t_low, t_high = o["range"]
                    volume = o["volume"]
                    bid    = o.get("bid", o["price"])
                    ask    = o.get("ask", o["price"])
                    spread = o.get("spread", 0)

                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, ask)
                        ev_mult    = get_ev_multiplier(city_slug, best_source or "ecmwf")
                        # Time-scaled EV floor: require more EV for short-window bets
                        # where there's less time for price to drift in our favour.
                        # At 2h: +0.05 bonus; at 72h: no bonus.
                        time_premium = 0.05 * max(0.0, 1.0 - hours / MAX_HOURS)
                        ev_min_adj = round((MIN_EV + time_premium) * ev_mult, 4)
                        if ev >= ev_min_adj:
                            kelly = calc_kelly(p, ask)
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
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        mdata    = r.json()
                        real_ask = float(mdata.get("bestAsk", best_signal["entry_price"]))
                        real_bid = float(mdata.get("bestBid", best_signal["bid_at_entry"]))
                        real_spread = round(real_ask - real_bid, 4)
                        if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE:
                            print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            ok, reason = validate_repriced_signal(best_signal, real_ask, real_bid, MIN_EV)
                            if not ok:
                                print(f"  [SKIP] {loc['name']} {date} — {reason}")
                                skip_position = True
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")

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
                                    print(f"  [LIVE] Order submitted: {order['order_id']}")
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

        record_bet_outcome(mkt["city"], pos.get("forecast_src", "ecmwf"), won)

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1
        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    all_mkts       = load_all_markets()
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
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
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
            ens_tag = (f" ens±{pos['ensemble_std']:.1f}({pos['ensemble_n']}m)"
                       if pos.get("ensemble_std") is not None else "")

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}{ens_tag}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

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
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    wr_rows = win_rate_summary()
    if wr_rows:
        print(f"\n  Win-rate calibration (city|source → EV multiplier):")
        print(f"  {'city':<16} {'source':<10} {'bets':>5} {'wins':>5} {'win%':>6} {'ev_mult':>8}")
        for r in wr_rows:
            flag = " ↑" if r["ev_mult"] > 1.1 else (" ↓" if r["ev_mult"] < 0.9 else "")
            print(f"  {r['city']:<16} {r['source']:<10} {r['bets']:>5} {r['wins']:>5} "
                  f"{r['win_rate']*100:>5.1f}% {r['ev_mult']:>8.3f}{flag}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600

def monitor_positions():
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata    = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o.get("bid", o["price"])
                    break

        if current_price is None:
            continue

        entry      = pos["entry_price"]
        stop       = pos.get("stop_price", entry * 0.80)
        city_name  = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        end_date   = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        if hours_left < 24:
            take_profit = None
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"]         = entry
            pos["trailing_activated"] = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

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
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
            else:
                pos["close_reason"] = "trailing_stop"
                reason = "TRAILING BE"
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

def run_loop():
    require_v3_live_confirmation()
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STARTING")
    print(f"{'='*55}")

    if LIVE_TRADE:
        try:
            real_bal = clob_trader.get_balance()
            state    = load_state()
            acct_bal = state.get("balance", 0.0)
            # Sync accounting balance to live wallet so Kelly uses real capital
            if abs(real_bal - acct_bal) > 0.50:
                state["balance"] = round(real_bal, 2)
                save_state(state)
                print(f"  Mode:       LIVE  (wallet ${real_bal:.2f} USDC; accounting synced from ${acct_bal:.2f})")
            else:
                print(f"  Mode:       LIVE  (wallet ${real_bal:.2f} USDC; accounting ${acct_bal:.2f})")
        except Exception as e:
            print(f"  [WARN] Could not fetch real balance: {e} — using saved accounting balance")
    else:
        print(f"  Mode:       PAPER")
    _startup_state = load_state()
    print(f"  Cities:     {len(LOCATIONS)} tracked | {len(CITY_BLACKLIST)} blacklisted (no bets)")
    print(f"  Balance:    ${_startup_state['balance']:,.2f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + ICON + GFS/HRRR(US) + GEM(Americas) + METAR(D+0)")
    print(f"  Ensemble:   agree<{ENSEMBLE_AGREE_F}°F/{ENSEMBLE_AGREE_C}°C tightens sigma {int((1-ENSEMBLE_SIGMA_REDUCTION)*100)}%")
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
