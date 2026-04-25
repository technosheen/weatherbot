#!/usr/bin/env python3
"""
train_v3.py — Calibrate bot_v3 from historical Open-Meteo data
================================================================
Steps:
  1. Load training_data.json (44 resolved markets with actual temps)
  2. Fetch ECMWF, ICON, GEM historical forecasts for each record date
  3. Compute per-city/per-source MAE (sigma) for existing cities
  4. For the 15 new v3 cities: fetch 90 days of ERA5 actuals + model
     forecasts, then compute baseline sigma
  5. Write calibration.json

Usage:
    python train_v3.py
"""

import json
import time
import requests
import sys
from datetime import date, timedelta
from pathlib import Path

# Pull location/timezone tables from bot_v3
sys.path.insert(0, str(Path(__file__).parent))
from bot_v3 import LOCATIONS, TIMEZONES

ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
HIST_FC_URL  = "https://historical-forecast-api.open-meteo.com/v1/forecast"
CAL_FILE     = Path("data/calibration.json")
TRAIN_FILE   = Path("data/training_data.json")
TODAY        = date.today()

MODELS = ["ecmwf_ifs025", "icon_seamless", "gem_seamless"]

MONTHS = {
    "january":1,"february":2,"march":3,"april":4,
    "may":5,"june":6,"july":7,"august":8,
    "september":9,"october":10,"november":11,"december":12,
}

# Cities present in training_data.json (original 20 v2 cities minus the 15 new ones)
TRAIN_CITIES = {
    "ankara","atlanta","buenos-aires","chicago","dallas",
    "london","lucknow","miami","munich","nyc",
    "paris","sao-paulo","seattle","seoul","shanghai",
    "singapore","tel-aviv","tokyo","toronto","wellington",
}

NEW_CITIES = set(LOCATIONS.keys()) - TRAIN_CITIES  # 15 new v3 cities

# =============================================================================

def parse_date(date_str: str) -> date:
    """'April 23' → date, assigning 2025 if the month/day is after today."""
    parts = date_str.split()
    month = MONTHS[parts[0].lower()]
    day   = int(parts[1])
    d2026 = date(2026, month, day)
    return d2026 if d2026 <= TODAY else date(2025, month, day)


def fetch_archive(city_slug: str, start: date, end: date) -> dict:
    """ERA5 reanalysis tmax — ground truth for model comparison."""
    loc       = LOCATIONS[city_slug]
    temp_unit = "fahrenheit" if loc["unit"] == "F" else "celsius"
    try:
        r = requests.get(ARCHIVE_URL, params={
            "latitude":        loc["lat"],
            "longitude":       loc["lon"],
            "start_date":      str(start),
            "end_date":        str(end),
            "daily":           "temperature_2m_max",
            "temperature_unit": temp_unit,
            "timezone":        TIMEZONES.get(city_slug, "UTC"),
        }, timeout=(10, 30))
        data = r.json()
        if "error" in data:
            return {}
        return {
            d: (round(t, 1) if loc["unit"] == "C" else round(t))
            for d, t in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"])
            if t is not None
        }
    except Exception as e:
        print(f"    [ARCHIVE-ERR] {city_slug}: {e}")
        return {}


def fetch_model(city_slug: str, model: str, start: date, end: date) -> dict:
    """Historical forecast tmax from a single Open-Meteo model."""
    loc       = LOCATIONS[city_slug]
    temp_unit = "fahrenheit" if loc["unit"] == "F" else "celsius"
    try:
        r = requests.get(HIST_FC_URL, params={
            "latitude":         loc["lat"],
            "longitude":        loc["lon"],
            "start_date":       str(start),
            "end_date":         str(end),
            "daily":            "temperature_2m_max",
            "temperature_unit": temp_unit,
            "timezone":         TIMEZONES.get(city_slug, "UTC"),
            "models":           model,
            "bias_correction":  "true",
        }, timeout=(10, 30))
        data = r.json()
        if "error" in data:
            return {}
        temps = data["daily"].get("temperature_2m_max", [])
        return {
            d: (round(t, 1) if loc["unit"] == "C" else round(t))
            for d, t in zip(data["daily"]["time"], temps)
            if t is not None
        }
    except Exception as e:
        print(f"    [HIST-FC-ERR] {city_slug} {model}: {e}")
        return {}


def compute_sigma(errors: list) -> float:
    """Mean absolute error → sigma."""
    if not errors:
        return None
    return round(sum(errors) / len(errors), 3)


# =============================================================================
# PHASE 1: calibrate existing cities from training_data.json
# =============================================================================

def calibrate_from_training_data() -> dict:
    """Returns {city_source: {sigma, n, errors[]}}."""
    records = json.loads(TRAIN_FILE.read_text())
    print(f"Phase 1 — {len(records)} training records across {len(TRAIN_CITIES)} cities")

    # Group records by city
    by_city: dict = {}
    for r in records:
        city = r["slug"]
        d    = parse_date(r["date"])
        if city not in by_city:
            by_city[city] = []
        by_city[city].append({"date": d, "actual": float(r["actual_temp"]), "unit": r.get("unit", "")})

    errors_by_key: dict = {}   # {f"{city}_{source}": [abs_error, ...]}

    for city, recs in by_city.items():
        if city not in LOCATIONS:
            print(f"  skip {city} — not in LOCATIONS")
            continue

        print(f"  {LOCATIONS[city]['name']} ({len(recs)} records)...")
        dates_sorted = sorted(set(r["date"] for r in recs))
        start, end   = dates_sorted[0], dates_sorted[-1]

        # For GEM, only fetch Americas cities
        loc      = LOCATIONS[city]
        models   = [m for m in MODELS if not (m == "gem_seamless" and loc["region"] not in ("us","ca","sa"))]
        hrrr_ok  = loc["region"] == "us"

        for model in models:
            short = model.replace("_seamless","").replace("_ifs025","")
            fc    = fetch_model(city, model, start, end)
            time.sleep(0.5)

            key = f"{city}_{short}"
            if key not in errors_by_key:
                errors_by_key[key] = []

            for rec in recs:
                pred = fc.get(str(rec["date"]))
                if pred is None:
                    continue
                actual = float(rec["actual"])
                # Normalize to the city's native unit (training data may differ)
                train_unit = rec.get("unit", loc["unit"])
                if train_unit == "F" and loc["unit"] == "C":
                    actual = round((actual - 32) * 5 / 9, 1)
                elif train_unit == "C" and loc["unit"] == "F":
                    actual = round(actual * 9 / 5 + 32)
                errors_by_key[key].append(abs(pred - actual))

        # HRRR uses gfs_seamless — we already fetched it above as model ecmwf_ifs025 etc.
        # Re-alias gfs → hrrr for US cities using the gfs_seamless fetch
        if hrrr_ok:
            gfs_key  = f"{city}_gfs"
            hrrr_key = f"{city}_hrrr"
            if gfs_key in errors_by_key:
                errors_by_key[hrrr_key] = errors_by_key.pop(gfs_key)

    return errors_by_key


# =============================================================================
# PHASE 2: calibrate 15 new v3 cities from ERA5 + historical forecasts
# =============================================================================

def calibrate_new_cities(window_days: int = 90) -> dict:
    """Fetch 90 days of ERA5 + model forecast data for each new city."""
    end   = TODAY - timedelta(days=1)        # yesterday (ERA5 available)
    start = end - timedelta(days=window_days - 1)

    print(f"\nPhase 2 — {len(NEW_CITIES)} new cities | {start} → {end}")
    errors_by_key: dict = {}

    for city in sorted(NEW_CITIES):
        loc     = LOCATIONS[city]
        print(f"  {loc['name']}...")
        actuals = fetch_archive(city, start, end)
        time.sleep(0.5)
        if not actuals:
            print(f"    no archive data, skipping")
            continue

        models = [m for m in MODELS if not (m == "gem_seamless" and loc["region"] not in ("us","ca","sa"))]
        hrrr_ok = loc["region"] == "us"

        for model in models:
            short = model.replace("_seamless","").replace("_ifs025","")
            fc    = fetch_model(city, model, start, end)
            time.sleep(0.5)

            key = f"{city}_{short}"
            if key not in errors_by_key:
                errors_by_key[key] = []

            for d_str, actual in actuals.items():
                pred = fc.get(d_str)
                if pred is not None:
                    errors_by_key[key].append(abs(pred - actual))

        if hrrr_ok:
            gfs_key  = f"{city}_gfs"
            hrrr_key = f"{city}_hrrr"
            if gfs_key in errors_by_key:
                errors_by_key[hrrr_key] = errors_by_key.pop(gfs_key)

    return errors_by_key


# =============================================================================
# PHASE 3: compute ensemble sigma from model agreement in historical data
# =============================================================================

def calibrate_ensemble(all_errors: dict, all_cities: set) -> dict:
    """
    Ensemble sigma = mean of available per-source sigmas, reduced by
    ENSEMBLE_SIGMA_REDUCTION. We store it explicitly so it's visible in
    the calibration file.
    """
    ensemble_errors: dict = {}
    for city in all_cities:
        if city not in LOCATIONS:
            continue
        loc      = LOCATIONS[city]
        default  = 2.0 if loc["unit"] == "F" else 1.2
        sources  = ["ecmwf", "icon"]
        if loc["region"] in ("us","ca","sa"):
            sources.append("hrrr")

        sigmas = []
        for src in sources:
            key = f"{city}_{src}"
            if key in all_errors and all_errors[key]:
                s = compute_sigma(all_errors[key])
                if s:
                    sigmas.append(s)

        if sigmas:
            # Ensemble tightens the sigma by 20% vs mean of individual models
            ens_sigma = round(sum(sigmas) / len(sigmas) * 0.80, 3)
            ensemble_errors[f"{city}_ensemble"] = {"_precomputed": True, "sigma": ens_sigma, "n": len(sigmas)}

    return ensemble_errors


# =============================================================================
# WRITE calibration.json
# =============================================================================

def write_calibration(phase1: dict, phase2: dict, ensemble: dict):
    now_str  = str(date.today())
    cal      = {}
    all_errs = {**phase1, **phase2}

    for key, errs in all_errs.items():
        sigma = compute_sigma(errs)
        if sigma:
            cal[key] = {"sigma": sigma, "n": len(errs), "updated_at": now_str}

    # Add ensemble entries
    for key, entry in ensemble.items():
        cal[key] = {"sigma": entry["sigma"], "n": entry["n"], "updated_at": now_str}

    CAL_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    return cal


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print("  bot_v3 calibration")
    print("=" * 60)

    phase1 = calibrate_from_training_data()
    phase2 = calibrate_new_cities(window_days=90)

    all_cities = TRAIN_CITIES | NEW_CITIES
    ensemble   = calibrate_ensemble({**phase1, **phase2}, all_cities)
    cal        = write_calibration(phase1, phase2, ensemble)

    print(f"\n{'='*60}")
    print(f"  Calibration complete — {len(cal)} entries written to {CAL_FILE}")
    print(f"{'='*60}")

    # Summary table
    print(f"\n  {'City':<18} {'ECMWF':>7} {'ICON':>7} {'GEM':>7} {'ENS':>7}  unit")
    print(f"  {'-'*60}")
    for city in sorted(LOCATIONS.keys()):
        loc  = LOCATIONS[city]
        unit = loc["unit"]
        def s(src):
            k = f"{city}_{src}"
            return f"{cal[k]['sigma']:>6.2f}" if k in cal else "     -"
        print(f"  {loc['name']:<18} {s('ecmwf')} {s('icon')} {s('gem' if loc['region'] in ('us','ca','sa') else 'gem'):>7} {s('ensemble')}  {unit}")


if __name__ == "__main__":
    main()
