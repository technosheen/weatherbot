#!/usr/bin/env python3
"""
backtest.py — bot_v2 vs bot_v3 on 44 resolved historical markets
=================================================================
Price model:
  Real early_avg_price from all_trade_data.json where available (5 events).
  Synthetic pricing for remaining 39: bucket prices derived via continuous
  normal CDF around market_forecast using MARKET_SIGMA.

Signal model:
  Both bots use continuous normal CDF for p (not the binary step function)
  so that EV is based on how much our forecast DIFFERS from the market's.
  This prevents the binary-p / cheap-bucket explosion.

  Bot edge = (p_bot / price_market) - 1  where price = p_market from CDF.

Usage:
    python backtest.py
"""

import sys, json, re, math, time, requests
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bot_v3 import (LOCATIONS, TIMEZONES, in_bucket, norm_cdf,
                     ENSEMBLE_AGREE_F, ENSEMBLE_AGREE_C, ENSEMBLE_SIGMA_REDUCTION,
                     CITY_BLACKLIST,
                     ENSEMBLE_DANGER_LO_F, ENSEMBLE_DANGER_HI_F,
                     ENSEMBLE_DANGER_LO_C, ENSEMBLE_DANGER_HI_C)

# ── Config ────────────────────────────────────────────────────────────────────
STARTING_BALANCE = 100.0
MAX_BET          = 5.0
MIN_EV           = 0.05
MAX_PRICE        = 0.90
KELLY_FRACTION   = 0.25

# How wide each bucket is for the prob calculation
BUCKET_HALF_F = 1.0   # ±1°F around centre (2°F buckets)
BUCKET_HALF_C = 0.5   # ±0.5°C around centre (1°C buckets)

# Sigma the market historically uses
MARKET_SIGMA_F = 3.5
MARKET_SIGMA_C = 1.8

V2_SIGMA_F = 2.0
V2_SIGMA_C = 1.2

HIST_FC_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
TODAY       = date.today()

MONTHS = {
    "january":1,"february":2,"march":3,"april":4,
    "may":5,"june":6,"july":7,"august":8,
    "september":9,"october":10,"november":11,"december":12,
}
CITY_SLUG_MAP = {
    "new york city":"nyc","nyc":"nyc","sao paulo":"sao-paulo",
    "buenos aires":"buenos-aires","tel aviv":"tel-aviv","hong kong":None,
}

def name_to_slug(city_name):
    key = city_name.lower()
    if key in CITY_SLUG_MAP: return CITY_SLUG_MAP[key]
    slug = key.replace(" ","-")
    return slug if slug in LOCATIONS else None

def parse_event_date(title):
    m = re.match(r"Highest temperature in (.+) on (\w+) (\d+)\?", title, re.IGNORECASE)
    if not m: return None, None
    month = MONTHS[m.group(2).lower()]
    day   = int(m.group(3))
    d2026 = date(2026, month, day)
    return m.group(1), (d2026 if d2026 <= TODAY else date(2025, month, day))

# ── Continuous bucket probability (no binary step) ────────────────────────────

def bucket_prob_cont(forecast, t_low, t_high, sigma, half=None):
    """
    P(actual in [t_low, t_high]) given forecast ~ N(forecast, sigma).
    For exact-match buckets (t_low==t_high) uses ±half width.
    For edge buckets uses one-sided CDF.
    """
    s = sigma or 2.0
    if t_low == -999:
        return norm_cdf((t_high + (half or 0.5) - forecast) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - (half or 0.5) - forecast) / s)
    if t_low == t_high:
        h = half or 0.5
        return norm_cdf((t_high + h - forecast) / s) - norm_cdf((t_low - h - forecast) / s)
    return norm_cdf((t_high - forecast) / s) - norm_cdf((t_low - forecast) / s)

# ── Open-Meteo fetch (cached) ─────────────────────────────────────────────────

_cache = {}

def fetch_temp(slug, edate, model):
    key = (slug, edate, model)
    if key in _cache: return _cache[key]
    loc  = LOCATIONS[slug]
    unit = "fahrenheit" if loc["unit"]=="F" else "celsius"
    try:
        r = requests.get(HIST_FC_URL, params={
            "latitude": loc["lat"], "longitude": loc["lon"],
            "start_date": str(edate), "end_date": str(edate),
            "daily": "temperature_2m_max", "temperature_unit": unit,
            "timezone": TIMEZONES.get(slug,"UTC"),
            "models": model, "bias_correction": "true",
        }, timeout=(10,20))
        data = r.json()
        if "error" in data: _cache[key]=None; return None
        temps = data["daily"].get("temperature_2m_max",[None])
        t = temps[0] if temps else None
        result = (round(t,1) if loc["unit"]=="C" else round(t)) if t is not None else None
        _cache[key] = result; return result
    except: _cache[key]=None; return None

# ── Bucket helpers ────────────────────────────────────────────────────────────

def parse_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.I):
        m = re.search(num+r'[°]?[FC] or below', question, re.I)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.I):
        m = re.search(num+r'[°]?[FC] or higher', question, re.I)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between '+num+r'-'+num+r'[°]?[FC]', question, re.I)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be '+num+r'[°]?[FC] on', question, re.I)
    if m: v=float(m.group(1)); return (v,v)
    return None

def build_buckets(market_forecast, actual, unit, market_sigma, real_buckets):
    """
    Use real buckets if any have prices. Otherwise synthesise a realistic
    range of 13 buckets centred around market_forecast.
    Price of each bucket = CDF probability from market perspective.
    """
    enriched = []
    for b in real_buckets:
        rng   = parse_range(b.get("question",""))
        price = b.get("early_avg_price") or b.get("avg_price") or 0
        if not rng or price <= 0: continue
        enriched.append({**b, "range":rng, "early_avg_price":price, "synthetic":False})
    if enriched:
        return enriched, "real"

    # Synthetic: 13 buckets
    half = BUCKET_HALF_F if unit=="F" else BUCKET_HALF_C
    if unit == "F":
        centre = round(market_forecast / 2) * 2
        vals   = list(range(centre-12, centre+14, 2))
    else:
        centre = round(market_forecast)
        vals   = list(range(centre-6, centre+7))

    buckets = []
    # edge low
    edge_lo = vals[0]
    buckets.append({"range": (-999, edge_lo-1 if unit=="F" else edge_lo-1),
                    "is_winner": in_bucket(actual, -999, edge_lo-1 if unit=="F" else edge_lo-1)})
    for v in vals:
        tl, th = (v, v+1) if unit=="F" else (v, v)
        price  = bucket_prob_cont(market_forecast, tl, th, market_sigma, half)
        price  = max(0.02, min(0.97, price))
        winner = in_bucket(actual, tl, th)
        buckets.append({"range":(tl,th), "early_avg_price":round(price,4),
                        "is_winner":winner, "synthetic":True})
    # edge high
    edge_hi = vals[-1]+2 if unit=="F" else vals[-1]+1
    buckets.append({"range": (edge_hi, 999),
                    "is_winner": in_bucket(actual, edge_hi, 999)})

    # Add price to edge buckets
    for b in buckets:
        if "early_avg_price" not in b:
            tl, th = b["range"]
            p = bucket_prob_cont(market_forecast, tl, th, market_sigma, half)
            b["early_avg_price"] = max(0.02, min(0.97, round(p,4)))
            b["synthetic"] = True

    return buckets, "synth"

# ── v3 calibration ────────────────────────────────────────────────────────────

V3_CAL = json.loads(Path("data/calibration.json").read_text()) if Path("data/calibration.json").exists() else {}

def v3_sigma(slug, src):
    loc = LOCATIONS[slug]
    default = V2_SIGMA_F if loc["unit"]=="F" else V2_SIGMA_C
    k = f"{slug}_{src}"
    if k in V3_CAL: return V3_CAL[k]["sigma"]
    if src == "ensemble":
        base = V3_CAL.get(f"{slug}_ecmwf",{}).get("sigma", default)
        return round(base * ENSEMBLE_SIGMA_REDUCTION, 3)
    return default

# ── Simulate one decision ─────────────────────────────────────────────────────

def simulate_bet(forecast, source, bot_sigma, buckets, unit):
    half = BUCKET_HALF_F if unit=="F" else BUCKET_HALF_C
    best = None
    for b in buckets:
        rng = b.get("range") or parse_range(b.get("question",""))
        if not rng: continue
        tl, th = rng

        # Bot only bets if forecast falls in this bucket
        if not in_bucket(forecast, tl, th): continue

        price = b.get("early_avg_price",0)
        if not price or price <= 0 or price >= MAX_PRICE: continue

        p_bot = bucket_prob_cont(forecast, tl, th, bot_sigma, half)
        if p_bot <= 0: continue

        ev    = p_bot / price - 1.0    # EV = p/price - 1
        if ev < MIN_EV: continue

        # Fractional Kelly
        b_odds = 1.0 / price - 1.0
        f_kelly = (p_bot * b_odds - (1-p_bot)) / b_odds if b_odds > 0 else 0
        f_kelly = max(0, f_kelly) * KELLY_FRACTION
        size    = round(min(f_kelly * STARTING_BALANCE, MAX_BET), 2)
        if size < 0.05: continue

        if best is None or ev > best["ev"]:
            best = {
                "range": (tl,th), "entry_price": price,
                "is_winner": b["is_winner"],
                "p": round(p_bot,4), "ev": round(ev,4),
                "kelly": round(f_kelly,4), "size": size,
                "shares": round(size/price,3),
                "source": source, "sigma": bot_sigma, "forecast": forecast,
                "synthetic": b.get("synthetic", False),
            }
    return best

def ens_std(vals):
    if len(vals)<2: return 999.0
    m = sum(vals)/len(vals)
    return math.sqrt(sum((v-m)**2 for v in vals)/len(vals))

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    td_raw  = json.loads(Path("data/training_data.json").read_text())
    atd_raw = json.loads(Path("data/all_trade_data.json").read_text())

    atd_lookup = {}
    for e in atd_raw:
        cn, ed = parse_event_date(e["event_title"])
        if cn and ed: atd_lookup[(cn.lower(), ed)] = e["buckets"]

    events = []
    for r in td_raw:
        parts = r["date"].split()
        month = MONTHS[parts[0].lower()]; day = int(parts[1])
        d2026 = date(2026,month,day)
        edate = d2026 if d2026<=TODAY else date(2025,month,day)
        slug  = name_to_slug(r["city"])
        if not slug: continue
        events.append({"city":r["city"],"slug":slug,"date":edate,
                       "actual":float(r["actual_temp"]),"mf":float(r["market_forecast"]),
                       "train_unit":r["unit"]})

    print(f"{'='*68}")
    print(f"  bot_v2 vs bot_v3 backtest  —  {len(events)} resolved events")
    print(f"  Balance: ${STARTING_BALANCE}  MaxBet: ${MAX_BET}  MinEV: {MIN_EV}  Kelly: {KELLY_FRACTION}")
    print(f"  Price model: real (5 events) + synthetic CDF (39 events)")
    print(f"{'='*68}\n")

    v2 = {"bal":STARTING_BALANCE, "trades":[], "no_bet":0}
    v3 = {"bal":STARTING_BALANCE, "trades":[], "no_bet":0}

    for ev in events:
        slug  = ev["slug"]; edate = ev["date"]; loc = LOCATIONS[slug]
        unit  = loc["unit"]; is_us = loc["region"]=="us"

        # Normalise to native unit
        actual, mf = ev["actual"], ev["mf"]
        tu = ev["train_unit"]
        if tu=="F" and unit=="C": actual=round((actual-32)*5/9,1); mf=round((mf-32)*5/9,1)
        elif tu=="C" and unit=="F": actual=round(actual*9/5+32); mf=round(mf*9/5+32)

        print(f"  {ev['city']:<18} {edate}  actual={actual}{unit}  mkt={mf}{unit}", end="", flush=True)

        ecmwf = fetch_temp(slug,edate,"ecmwf_ifs025"); time.sleep(0.2)
        icon  = fetch_temp(slug,edate,"icon_seamless"); time.sleep(0.2)
        hrrr  = fetch_temp(slug,edate,"gfs_seamless") if is_us else None
        if is_us: time.sleep(0.2)
        gem   = fetch_temp(slug,edate,"gem_seamless") if loc["region"] in ("us","ca","sa") else None
        if loc["region"] in ("us","ca","sa"): time.sleep(0.2)

        model_map = {k:v for k,v in [("ecmwf",ecmwf),("icon",icon),("hrrr",hrrr),("gem",gem)] if v is not None}
        if not model_map:
            v2["no_bet"]+=1; v3["no_bet"]+=1; print("  [no forecast]"); continue

        ms = MARKET_SIGMA_F if unit=="F" else MARKET_SIGMA_C
        real_bkts = atd_lookup.get((ev["city"].lower(), edate), [])
        buckets, price_src = build_buckets(mf, actual, unit, ms, real_bkts)

        # ── v2 ──────────────────────────────────────────────────────────────
        v2_fc  = (hrrr if is_us and hrrr else ecmwf)
        v2_src = "hrrr" if (is_us and hrrr) else "ecmwf"
        v2_sig = V2_SIGMA_F if unit=="F" else V2_SIGMA_C
        v2_trade = simulate_bet(v2_fc, v2_src, v2_sig, buckets, unit) if v2_fc else None

        # ── v3 ──────────────────────────────────────────────────────────────
        n = len(model_map)
        at = ENSEMBLE_AGREE_F if unit=="F" else ENSEMBLE_AGREE_C
        if n>=3:
            vals=list(model_map.values()); em=sum(vals)/n
            em=round(em,1) if unit=="C" else round(em)
            if ens_std(vals)<at: v3_fc,v3_src=em,"ensemble"
            elif is_us and hrrr: v3_fc,v3_src=hrrr,"hrrr"
            else: v3_fc,v3_src=(ecmwf,"ecmwf") if ecmwf else (icon,"icon")
        elif is_us and hrrr: v3_fc,v3_src=hrrr,"hrrr"
        elif ecmwf: v3_fc,v3_src=ecmwf,"ecmwf"
        else: v3_fc,v3_src=icon,"icon"
        # v3 filters: blacklist + danger zone
        if slug in CITY_BLACKLIST:
            v3_trade = None
            v3["no_bet"] += 1
        else:
            _dlo  = ENSEMBLE_DANGER_LO_F if unit=="F" else ENSEMBLE_DANGER_LO_C
            _dhi  = ENSEMBLE_DANGER_HI_F if unit=="F" else ENSEMBLE_DANGER_HI_C
            _estd = ens_std(list(model_map.values())) if len(model_map)>=2 else 0.0
            if _dlo <= _estd <= _dhi:
                v3_trade = None
                v3["no_bet"] += 1
            else:
                v3_sig   = v3_sigma(slug, v3_src)
                v3_trade = simulate_bet(v3_fc, v3_src, v3_sig, buckets, unit)

        # ── Record ───────────────────────────────────────────────────────────
        def record(state, trade):
            if not trade: state["no_bet"]+=1; return "NO BET"
            won = trade["is_winner"]
            pnl = round(trade["shares"]*(1-trade["entry_price"]),2) if won else round(-trade["size"],2)
            state["bal"]+=pnl
            state["trades"].append({**trade,"won":won,"pnl":pnl,
                "city":ev["city"],"date":str(edate),"actual":actual,
                "market_forecast":mf,"model_err":round(abs(trade["forecast"]-actual),2),
                "market_err":round(abs(mf-actual),2),"price_src":price_src})
            s=f"{'WIN' if won else 'LOSS'} {'+' if pnl>=0 else ''}{pnl:.2f}"
            return f"{s} ({trade['source'].upper()} σ{trade['sigma']:.2f} p={trade['p']:.2f} EV={trade['ev']:+.2f} fc={trade['forecast']}{unit} [{price_src}])"

        model_errs = "  ".join(f"{k}±{round(abs(v-actual),1)}" for k,v in model_map.items())
        print(f"\n    {model_errs}")
        print(f"    v2: {record(v2, v2_trade)}")
        print(f"    v3: {record(v3, v3_trade)}")

    # ── Results ───────────────────────────────────────────────────────────────
    def summarise(label, state):
        trades  = state["trades"]
        n_t     = len(trades)
        wins    = sum(1 for t in trades if t["won"])
        pnl_tot = sum(t["pnl"] for t in trades)
        ret_pct = (state["bal"]-STARTING_BALANCE)/STARTING_BALANCE*100
        wr      = wins/n_t*100 if n_t else 0
        avg_ev  = sum(t["ev"] for t in trades)/n_t if n_t else 0

        print(f"\n  {label}")
        print(f"    Balance:    ${state['bal']:>8.2f}  ({'+' if ret_pct>=0 else ''}{ret_pct:.1f}%)")
        print(f"    Trades:     {n_t}  (W:{wins} L:{n_t-wins} no-bet:{state['no_bet']})")
        print(f"    Win rate:   {wr:.0f}%")
        print(f"    Total PnL:  {'+' if pnl_tot>=0 else ''}{pnl_tot:.2f}")
        print(f"    Avg EV:     {avg_ev:+.3f}")

        src_stats: dict = {}
        for t in trades:
            s = t["source"]
            src_stats.setdefault(s,{"w":0,"n":0,"pnl":0.0,"model_errs":[]})
            src_stats[s]["n"]+=1
            if t["won"]: src_stats[s]["w"]+=1
            src_stats[s]["pnl"]+=t["pnl"]
            src_stats[s]["model_errs"].append(t["model_err"])
        print(f"    By source:")
        for src,ss in sorted(src_stats.items()):
            mae = sum(ss["model_errs"])/len(ss["model_errs"]) if ss["model_errs"] else 0
            wr_s = ss["w"]/ss["n"]*100
            print(f"      {src.upper():<10} {ss['w']}/{ss['n']} ({wr_s:.0f}%)  "
                  f"PnL {'+' if ss['pnl']>=0 else ''}{ss['pnl']:.2f}  model_MAE={mae:.2f}")

    print(f"\n{'='*68}")
    print(f"  RESULTS")
    print(f"{'='*68}")
    summarise("bot_v2", v2)
    summarise("bot_v3", v3)

    # ── Model accuracy ────────────────────────────────────────────────────────
    print(f"\n{'='*68}")
    print(f"  MODEL ACCURACY  (all {len(events)} events, actual temp vs each model)")
    print(f"{'='*68}")

    all_errs:   dict = {s:[] for s in ("market","ecmwf","icon","hrrr","gem","ensemble")}
    bucket_ok:  dict = {s:{"hit":0,"n":0} for s in ("market","ecmwf","icon","hrrr","gem","ensemble")}
    correct_bets: dict = {"v2":{"hit":0,"n":0},"v3":{"hit":0,"n":0}}

    for ev in events:
        slug  = ev["slug"]; edate = ev["date"]; loc = LOCATIONS[slug]
        unit  = loc["unit"]; is_us = loc["region"]=="us"
        actual, mf = ev["actual"], ev["mf"]
        tu = ev["train_unit"]
        if tu=="F" and unit=="C": actual=round((actual-32)*5/9,1); mf=round((mf-32)*5/9,1)
        elif tu=="C" and unit=="F": actual=round(actual*9/5+32); mf=round(mf*9/5+32)

        model_map = {k:_cache.get((slug,edate,{"ecmwf":"ecmwf_ifs025","icon":"icon_seamless",
                     "hrrr":"gfs_seamless","gem":"gem_seamless"}[k]))
                     for k in ("ecmwf","icon","hrrr","gem")}
        model_map = {k:v for k,v in model_map.items() if v is not None}
        vals = list(model_map.values())
        if len(vals)>=2: model_map["ensemble"] = round(sum(vals)/len(vals),1) if unit=="C" else round(sum(vals)/len(vals))
        model_map["market"] = mf

        half = BUCKET_HALF_F if unit=="F" else BUCKET_HALF_C

        for src, pred in model_map.items():
            err = abs(pred-actual)
            all_errs[src].append(err)
            # "correct bucket" = does the forecast round to the actual?
            hit = in_bucket(actual, round(pred)-half, round(pred)+half) if unit=="F" \
                  else in_bucket(actual, round(pred), round(pred))
            bucket_ok[src]["hit"]+=int(hit)
            bucket_ok[src]["n"]+=1

        # v2 and v3 correct-bucket rate
        v2_fc = (model_map.get("hrrr") or model_map.get("ecmwf"))
        v3_fc_val = model_map.get("ensemble") or (model_map.get("hrrr") if is_us else model_map.get("ecmwf"))

        for bname, fc in [("v2",v2_fc),("v3",v3_fc_val)]:
            if fc is None: continue
            hit = in_bucket(actual, round(fc)-half, round(fc)+half) if unit=="F" \
                  else in_bucket(actual, round(fc), round(fc))
            correct_bets[bname]["hit"]+=int(hit)
            correct_bets[bname]["n"]+=1

    print(f"\n  {'Source':<12} {'MAE':>7}  {'Bucket%':>8}  {'Improvement vs market':>22}")
    print(f"  {'-'*55}")
    market_mae = sum(all_errs["market"])/len(all_errs["market"])
    for src in ("market","ecmwf","hrrr","gem","icon","ensemble"):
        errs = all_errs[src]
        if not errs: continue
        mae  = sum(errs)/len(errs)
        bh   = bucket_ok[src]
        hr   = bh["hit"]/bh["n"]*100 if bh["n"] else 0
        impr = (market_mae-mae)/market_mae*100
        marker = f"  {impr:+.1f}% vs market" if src!="market" else ""
        best_marker = " ◀ best" if src!="market" and mae==min(
            sum(all_errs[s])/len(all_errs[s]) for s in ("ecmwf","icon","hrrr","gem","ensemble") if all_errs[s]) else ""
        print(f"  {src.upper():<12} {mae:>7.3f}  {hr:>7.0f}%{marker}{best_marker}")

    print(f"\n  Bot correct-bucket rate (forecast points to winning bucket):")
    for bname, stats in correct_bets.items():
        hr = stats["hit"]/stats["n"]*100 if stats["n"] else 0
        print(f"    {bname}:  {stats['hit']}/{stats['n']}  ({hr:.0f}%)")

    # ── Head-to-head ──────────────────────────────────────────────────────────
    v2m = {(t["city"],t["date"]):t for t in v2["trades"]}
    v3m = {(t["city"],t["date"]):t for t in v3["trades"]}
    shared = set(v2m)&set(v3m)
    both_win=both_lose=v3_better=v2_better=0
    for k in shared:
        t2,t3=v2m[k],v3m[k]
        if t2["won"] and t3["won"]: both_win+=1
        elif not t2["won"] and not t3["won"]: both_lose+=1
        elif t3["pnl"]>t2["pnl"]: v3_better+=1
        else: v2_better+=1
    print(f"\n  Head-to-head ({len(shared)} events both bots bet):")
    print(f"    Both WIN: {both_win}  |  Both LOSE: {both_lose}  |  v3>v2: {v3_better}  |  v2>v3: {v2_better}")

    only_v3={k:v3m[k] for k in v3m if k not in v2m}
    only_v2={k:v2m[k] for k in v2m if k not in v3m}
    for label,d in [("v3-only",only_v3),("v2-only",only_v2)]:
        pnl=sum(t["pnl"] for t in d.values())
        print(f"\n  {label} bets: {len(d)}  PnL {'+' if pnl>=0 else ''}{pnl:.2f}")
        for k,t in sorted(d.items()):
            print(f"    {k[0]:<18} {k[1]}  {'WIN' if t['won'] else 'LOSS'} {'+' if t['pnl']>=0 else ''}{t['pnl']:.2f}"
                  f"  src={t['source']}  fc={t['forecast']}  σ={t['sigma']:.2f}  p={t['p']:.2f}")

    print(f"\n{'='*68}\n")

if __name__ == "__main__":
    run()
