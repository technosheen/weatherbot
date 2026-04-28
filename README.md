# PolyWeather — Polymarket Weather Trading Bot (v3)

Automated weather prediction market bot for Polymarket. Tracks 35 cities worldwide using a 5-model weather ensemble, computes expected value with Kelly sizing, and can run fully paper-simulated or with live CLOB order execution.

Paper mode is default. Live trading requires explicit safety confirmation.

---

## What It Does

Polymarket runs markets like *"Will the highest temperature in Chicago be between 46—47°F on March 7?"* These markets are often mispriced — the forecast says 78% likely but the market is trading at 8 cents.

The bot:
1. Fetches up to 5 concurrent weather forecasts per city
2. Builds an ensemble consensus and selects the best source
3. Finds matching temperature buckets on Polymarket
4. Runs a pre-trade analysis gate (price floor, portfolio drawdown, position cap, bucket rank, ensemble confidence)
5. Sizes positions with fractional Kelly criterion
6. Refreshes live ask/bid quotes immediately before entering and re-validates EV
7. Resolves markets automatically by querying Polymarket's Gamma API
8. Runs post-trade forensics and adapts thresholds from resolved outcomes

---

## Bot Versions

| File | Status | Description |
|------|--------|-------------|
| `bot_v1.py` | Reference | 6-city base bot. Good for understanding the core idea. |
| `bot_v2.py` | Legacy | Intermediate version. Do not run in production. |
| `bot_v3.py` | Production | Current operational bot. Live/paper. |

---

## Architecture

### Forecast sources
| Source | Coverage | Notes |
|--------|----------|-------|
| ECMWF IFS 0.25° | Global | Default best source. Bias-corrected via Open-Meteo. |
| ICON seamless (DWD) | Global | Strong for Europe. |
| GFS/HRRR seamless | US only | Used for US cities via Open-Meteo; intentionally deprioritized because data shows it underperforms ECMWF for US cities. |
| GEM seamless | Americas only | Canadian model. |
| METAR | All (D+0 only) | Real-time station observations from aviationweather.gov. |

### Ensemble consensus
When 3+ models agree tightly (std < 1.0°F / 0.7°C), the ensemble mean becomes the best forecast. Otherwise ECMWF is preferred. Model disagreement is blended into sigma in quadrature.

### 35 tracked cities

US: NYC, Chicago, Miami, Dallas, Seattle, Atlanta, Los Angeles, Denver, Phoenix, Houston, Boston
Europe: London, Paris, Munich, Ankara, Amsterdam, Madrid, Rome, Stockholm
Asia / Middle East: Seoul, Tokyo, Shanghai, Singapore, Lucknow, Tel Aviv, Dubai, Mumbai, Bangkok, Jakarta
Americas / Canada: Toronto, Sao Paulo, Buenos Aires
Oceania / Africa: Wellington, Sydney, Johannesburg

### Why airport coordinates matter
Every Polymarket weather market resolves on a specific airport station. NYC → LaGuardia (KLGA), Dallas → Love Field (KDAL) — not DFW. City-center vs airport can differ 3–8°F. The bot uses exact airport lat/lon for every city.

---

## Files

| File | Purpose |
|------|---------|
| `bot_v3.py` | Main bot. `run`, `status`, `report` |
| `clob_trader.py` | Polymarket CLOB wrapper — buy, sell, cancel, order status |
| `weatherbot_redeem.py` | On-chain CTF/NegRisk redemption for resolved wins |
| `weatherbot_audit.py` | Read-only wallet vs local reconciliation report |
| `position_change_notifier.py` | Telegram notifier for position open/close events |
| `config.json` | Runtime config (balance, gates, mode) — do not commit |
| `.env` | Secrets (PK, CLOB credentials, Visual Crossing key) — NEVER commit |
| `data/markets/` | One JSON per city/date — forecasts, snapshots, positions |
| `data/state.json` | Bot accounting balance and win/loss record |
| `data/calibration.json` | Per-city per-source sigma calibration |
| `data/win_rates.json` | City+source win-rate tracking for EV multipliers |
| `data/trade_journal.json` | Resolved trade forensics |
| `data/learning/` | Nicolas adaptive model + trade log |

---

## Installation

```bash
git clone <repo>
cd weatherbot
python -m venv venv
source venv/bin/activate
pip install requests python-dotenv web3 py-clob-client
```

Create `.env`:
```
PK=your_private_key
WALLET=0xYourWalletAddress
SIG_TYPE=0
CLOB_API_KEY=your_clob_api_key
CLOB_SECRET=your_clob_secret
CLOB_PASSPHRASE=your_clob_passphrase
```

Create `config.json` from `config.example.json` and tune for your risk tolerance. For pre-calibration, use conservative gates:

```json
{
  "balance": 100.0,
  "max_bet": 1.0,
  "min_ev": 0.5,
  "max_ev": 2.0,
  "min_ensemble_std_f": 0.5,
  "min_ensemble_std_c": 0.3,
  "max_price": 0.30,
  "min_volume": 300,
  "min_hours": 4.0,
  "max_hours": 48.0,
  "kelly_fraction": 0.1,
  "scan_interval": 3600,
  "calibration_min": 30,
  "vc_key": "YOUR_VISUAL_CROSSING_API_KEY",
  "max_slippage": 0.03,
  "mode": "paper",
  "live_trade": false,
  "v3_live_confirmed": false,
  "min_price": 0.08,
  "max_unrealized_loss": -5.0,
  "max_open_positions": 10,
  "balance_floor": 50.0,
  "new_entries_enabled": true,
  "auto_redeem_on_resolve": true
}
```

**Important config fields:**
- `live_trade`: enables CLOB order submission
- `v3_live_confirmed`: explicit safety ack required before live trading
- `new_entries_enabled`: set `false` for manage-only mode (monitor + resolve, no new buys)
- `balance_floor`: hard stop — no new bets when bot accounting balance drops below this

---

## Usage

### Paper mode (default)
```bash
python3 bot_v3.py status    # balance, open positions, unrealized PnL
python3 bot_v3.py report    # full breakdown of resolved markets
python3 bot_v3.py run       # continuous scan every hour + position monitor every 10 min
```

### Live mode
Both `live_trade` and `v3_live_confirmed` must be `true` in `config.json`. The bot exits with code 2 if `live_trade` is true but `v3_live_confirmed` is false.

```bash
# Start in a tmux session (persistent)
tmux new-session -d -s weatherbot-v3 \
  "cd weatherbot && source venv/bin/activate && PYTHONUNBUFFERED=1 python3 bot_v3.py run 2>&1 | tee -a /tmp/bot_v3_output.log"

# Attach
 tmux attach -t weatherbot-v3

# Stop
 tmux send-keys -t weatherbot-v3 C-c
```

### Redeem resolved wins
```bash
# Dry run first — prints expected payouts, gas, candidate markets
python3 weatherbot_redeem.py

# After reviewing, broadcast
python3 weatherbot_redeem.py --broadcast
```

### Wallet audit (read-only)
```bash
python3 weatherbot_audit.py
```
---

## Data Storage

Every market gets a JSON file in `data/markets/{city}_{date}.json` containing:
- Hourly forecast snapshots from all models
- Market price history
- Position details (entry, exit, PnL, order IDs)
- Final resolution outcome and actual temperature

This data feeds:
- **Self-calibration** — per-city per-source sigma updates after minimum resolved bets
- **Win-rate tracking** — Bayesian-smoothed multipliers applied to EV thresholds
- **Nicolas learning** — adaptive Kelly adjustment and EV floor from last N trades
- **Post-trade forensics** — price tier, ensemble bucket, horizon, forecast drift, edge proximity

---

## Pre-Trade Safety Gates (v3)

1. **Manage-only gate** — if `new_entries_enabled` is false, no new positions ever open
2. **Balance floor** — hard stop when accounting balance < floor
3. **Price floor** — skip very-low-price bets (default 8¢, can be learned upward from forensics)
4. **Portfolio drawdown gate** — pauses new bets when unrealized PnL < threshold
5. **Open position cap** — max concurrent trading-slot positions (sub-5-share positions are held to resolution and do not consume slots)
6. **Model consensus warning** — flagged when only 1 model supports the forecast
7. **Bucket rank gate** — skips if target bucket is ranked > #4 by market price
8. **Ensemble danger zone** — skips bets when ensemble std falls in a historically poor range
9. **Exact-match centering** — for single-degree buckets, skips if forecast is too close to the bucket edge
10. **EV cap** — skips bets with EV above a max threshold (historically correlated with losses)
11. **Live quote re-validation** — fetches fresh ask/bid immediately before ordering; repriced EV must still clear gate
12. **Sell-minimum sizing** — refuses to open positions below 5 shares because CLOB sell minimum is 5
13. **City blacklist** — some cities are tracked but never bet due to negative historical edge
14. **Per-city position limit** — only one open bet per city at any time

---

## Live Reconciliation Safety

v3 has first-class reconciliation helpers:
- `assert_live_reconciliation_safe()` blocks new live trading if CLOB open orders or local pending states are untracked
- Wallet-reconciled positions (proven by data-api + ERC1155 balances) survive stale CLOB order lookups
- `prepare_live_exit()` handles unsellable positions by holding to resolution instead of creating orphan sells
- Zombie guards prevent reopened markets from hiding resolved/closed positions
- Ghost-resolution feeds forensics and learning even for locally-closed positions

If `clob_trader.get_order_status()` returns `unknown` for older orders, do not infer filled/cancelled. Use `weatherbot_audit.py` or manual data-api/ERC1155 checks.

---

## APIs Used

| API | Auth | Purpose |
|-----|------|---------|
| Open-Meteo | None | ECMWF, ICON, GFS/HRRR, GEM forecasts |
| Aviation Weather (METAR) | None | Real-time station observations |
| Polymarket Gamma API | None | Market data, event search, resolution prices |
| Polymarket CLOB | API key + secret + passphrase | Order placement, cancellation, status |
| Visual Crossing | Free key | Historical temps for resolution |
| Polygon RPC | None | On-chain redemption, ERC1155 balance checks |

---

## Testing

```bash
cd weatherbot
source venv/bin/activate
python3 -m pytest tests/ -q
```

Tests cover:
- Probability / EV math consistency between paper and live modes
- Live safety gates (reconciliation, new-entries, state integrity)
- CLOB buy/sell sizing and minimum-share guards
- Persistence and atomic JSON writes

---

## Disclaimer

This is not financial advice. Prediction markets carry real risk. Start in paper mode, run extensive backtests, and only enable live mode after understanding the full reconciliation and safety model.
