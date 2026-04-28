# Weatherbot v3 — Complete Code Analysis

Generated: 2026-04-28
Files analyzed: bot_v3.py (2758 lines), clob_trader.py (192), weatherbot_redeem.py (428), weatherbot_audit.py (212), position_change_notifier.py (181)

---

## 1. Architecture Overview

### Monolithic Core
`bot_v3.py` is a single 2758-line script that contains every subsystem:

| Subsystem | Lines | Key Functions |
|-----------|-------|---------------|
| Forecast fetchers | ~260 | `get_ecmwf`, `get_icon`, `get_hrrr`, `get_gem`, `get_metar` |
| Signal / probability | ~120 | `bucket_prob`, `calc_ev`, `calc_kelly`, `analyze_signal` |
| Risk gates | ~180 | `analyze_signal`, `validate_repriced_signal`, `assert_live_reconciliation_safe` |
| Order management | ~230 | `prepare_live_exit`, live buy/sell blocks in `scan_and_update` |
| Resolution / PnL | ~280 | Resolution loops, ghost resolution, zombie guards |
| Calibration / learning | ~380 | `run_calibration`, `adapt_thresholds`, Nicolas system, forensics |
| Reporting | ~220 | `print_status`, `print_report` |
| State I/O | ~140 | `atomic_json_write`, `load_market`, `save_market`, `load_state` |

**Support modules** (imported, not monolithic):
- `clob_trader.py` — thin defensive wrapper around Polymarket `py_clob_client`
- `weatherbot_redeem.py` — on-chain CTF / NegRiskAdapter redemption
- `weatherbot_audit.py` — read-only wallet/local reconciliation audit
- `position_change_notifier.py` — Telegram-style position change detection

### Data Model
All state is file-based JSON:

```
data/
  state.json               # bot accounting balance, wins/losses, peak
  calibration.json         # per-city per-source sigma (MAE-based)
  win_rates.json           # Bayesian win-rate cells
  trade_journal.json       # post-trade forensics entries
  learned_params.json      # auto-adapted thresholds
  learning/
    model.json             # Nicolas adaptive model
    trade_log.json         # rolling 30-trade window
  markets/
    {city}_{date}.json     # individual market snapshots (125 files)
  telegram_position_change_state.json
```

### Execution Model
```
run_loop()
  ├─ every SCAN_INTERVAL (3600s): scan_and_update()
  │     ├─ per city: fetch 5 forecasts in parallel
  │     ├─ match bucket → compute EV/Kelly
  │     ├─ apply 6+ risk gates
  │     ├─ fetch fresh live ask/bid
  │     ├─ if LIVE: place_buy → check status → reconcile
  │     └─ resolution pass: settle resolved markets
  └─ every MONITOR_INTERVAL (600s): monitor_positions()
        └─ take-profit exits only (price stop-loss disabled)
```

---

## 2. Data Flow & State Management

### Market Lifecycle (per city/date)
1. **Discovery** — `get_polymarket_event()` queries Gamma API by slug
2. **Creation** — `new_market()` generates template with `status: "open"`, `position: None`
3. **Polling** — every scan appends a `forecast_snapshot` and `market_snapshot`
4. **Entry** — on signal, `position` dict is populated (pending_buy → open)
5. **Exit** — forecast drift, take-profit, or resolution
6. **Resolution** — `status` → `"resolved"`, outcome known, PnL booked

### Balance Flow
```
On new paper position:    balance -= signal["cost"]
On resolution win:        balance += cost + (shares * (1 - price))
On resolution loss:       balance += cost + (-cost)  = balance -= cost
On forecast exit:         balance += cost + (current_price - entry) * shares
On cancelled buy refund:  balance += reserved cost
```

Balance is persisted to both `data/state.json` **and** `config.json` at every `save_state()` call, so restarts pick up the correct number.

### Atomic Writes
`atomic_json_write()` uses the standard pattern:
1. `tempfile.mkstemp()` in same dir
2. Write payload, `flush()`, `os.fsync()`
3. `os.replace()` into final path
4. Best-effort directory fsync

This prevents truncated JSON files on crash.

### Concern: State Drift
The `config.json` contains a `reconciliation_adjustment` record showing:
```json
{
  "old_balance": 146.68,
  "new_balance": 82.84,
  "reason": "option_A_writeoff_post_negrisk_redemption_2026-04-28"
}
```
This indicates the bot's accounting diverged from on-chain reality and required manual correction. The audit module (`weatherbot_audit.py`) exists to help detect this.

---

## 3. Probability Model & Kelly Sizing

### Bucket Probability
`bucket_prob(forecast, t_low, t_high, sigma)` uses a normal CDF with:
- Half-degree continuity window for exact-match buckets
- Open-ended buckets (-999 / +999) get one-sided CDFs
- Default sigma: 2.0°F or 1.2°C

```python
# Example: forecast=72°F, bucket 70-75°F, sigma=2.0
p = norm_cdf((75 - 72)/2) - norm_cdf((70 - 72)/2)
  ≈ 0.933 - 0.159 = 0.774
```

### Kelly Criterion
```python
b = 1/price - 1          # decimal odds
f = (p*b - (1-p)) / b     # full Kelly
kelly = min(max(0, f) * KELLY_FRACTION, 1.0)
```

Current config: `kelly_fraction: 0.1` (very conservative — 10% of full Kelly).

### Bet Sizing
`bet_size(kelly, balance, entry_price)` caps at `MAX_BET` ($1.50 currently), but **overrides max_bet upward** if necessary to reach `CLOB_MIN_SELL_SHARES` (5 shares). This prevents creating dust positions that can't be sold on the CLOB.

```python
min_cost = ceil(5 * price * 100) / 100
# At price $0.25: min_cost = $1.25  → if MAX_BET=$1.50, no override needed
# At price $0.10: min_cost = $0.50  → but signal cost might be $0.30
```

### Ensemble Consensus
- 4 NWP models polled: ECMWF, ICON, HRRR (US only), GEM (Americas)
- METAR for D+0 only
- If ≥3 models agree within `ENSEMBLE_AGREE_F/C` threshold, `best_source = "ensemble"`
- Otherwise ECMWF-first (data shows ECMWF beats GFS/HRRR for US cities)

### Sigma Calibration
`run_calibration()` computes MAE per city/source from resolved markets with ≥`CALIBRATION_MIN` (30) samples. The sigma is updated if the change exceeds 0.05.

### Adaptive Learning (Nicolas System)
Rolling 30-trade window:
- If winrate < 45% or PnL < -$1.0: shrink Kelly by 20%, raise EV floor by 10%
- If winrate > 55% and PnL > +$2.0: grow Kelly by 10%, lower EV floor by 5%
- Adjusted Kelly capped at `KELLY_FRACTION * adjustment`

---

## 4. Safety Mechanisms & Fail-Closed Behavior

### Pre-Trade Risk Gates (in `analyze_signal`)
| Gate | Rule | Current Config |
|------|------|----------------|
| Manage-only | `NEW_ENTRIES_ENABLED == false` | true |
| Balance floor | `balance < BALANCE_FLOOR` | $50 |
| Price floor | `entry < MIN_PRICE` | $0.08 |
| Unrealized loss | `unrealized < MAX_UNREALIZED_LOSS` | -$5.00 |
| Open cap | `slot_markets >= MAX_OPEN_POSITIONS` | 10 |
| Model warning | `ens_n < 2` (flag only) | — |
| Bucket rank | `target_rank > 4` | reject |

### Hard Limits in Config
- `max_price: 0.30` — skip if ask ≥ $0.30 (was 0.45 in v2, tightened)
- `max_ev: 2.0` — cap claimed EV (was uncapped in v2; added after analysis showed high-EV bets had 0-12% win rate)
- `max_slippage: 0.03` — reject if live spread > 3¢
- `min_ensemble_std_f: 0.5` — skip when models agree too tightly (market priced in)
- `max_hours: 48` — only bet within 48h of resolution

### Live Trading Confirmation
```python
RAW_LIVE_TRADE = config.get("live_trade", False)
V3_LIVE_CONFIRMED = config.get("v3_live_confirmed", False)
LIVE_TRADE = RAW_LIVE_TRADE and V3_LIVE_CONFIRMED
```

If `live_trade=true` but `v3_live_confirmed` is missing/false → `SystemExit(2)` with safety message.

### v3 Probability Model Fix Context
The docstring says v3 "previously over-scored matched buckets as p=1.0" and the confirmation gate prevents accidentally restarting an older live config after the fix.

### CLOB Order Safety
- **Never book posted orders as filled** — After `place_buy()`, status is checked:
  - `filled` → mark open, debit balance
  - `cancelled` → mark closed, refund balance, do NOT debit
  - `open/partial/unknown` → `needs_reconciliation=True`, do NOT debit balance, persist as pending
- **Minimum sellable shares** — `CLOB_MIN_SELL_SHARES = 5.0`; positions below this are held to resolution
- **Fail closed on unknown status** — `prepare_live_exit()` returns `False` for any unknown CLOB status, keeping position open pending reconciliation
- **Balance floor projection** — Before placing live order, checks if `balance - projected_cost < BALANCE_FLOOR`

### Zombie Guards
Three defensive paths prevent stale positions from corrupting state:
1. **Resolved zombie** — market.status=resolved but pos.status=open → force-close at $1 or $0
2. **Closed zombie** — market.status=closed but pos.status=open → force-close
3. **Wallet-reconciled skip** — `should_skip_zombie_close()` preserves positions operator proved are still held

### Ghost Resolution
Positions locally closed (forecast drift, take-profit) still need to feed the learning system. A separate "ghost" loop checks `check_market_resolved()` for any closed position not yet resolved, records the outcome, and marks `learning_recorded=True`. It also triggers auto-redeem if the wallet still holds winning tokens.

---

## 5. Live Trading Path & CLOB Interaction

### Order Flow
```
scan_and_update()
  ├─ compute signal
  ├─ fetch fresh Gamma API quote (bestAsk/bestBid)
  ├─ validate_repriced_signal()  ← must re-clear EV after live quote
  ├─ if LIVE:
  │    order = clob_trader.place_buy(token_id, price, cost)
  │    if order:
  │        status = clob_trader.get_order_status(order_id)
  │        if status == "filled":
  │            pos.status = "open"; debit balance
  │        elif status == "cancelled":
  │            pos.status = "closed"; refund; skip
  │        else:
  │            pos.status = "pending_buy"; needs_reconciliation=True; skip debit
  │    else:
  │        skip
  └─ if not LIVE or skip_position == False:
       debit balance; pos.status = "open"
```

### Exit Flow (`prepare_live_exit`)
```
if exit_order_id exists:
    check status → filled/open/partial/cancelled/unknown
    filled → return True (can close locally)
    open → keep open, return False
    partial → needs_reconciliation, return False
    cancelled → clear exit_order_id, retry next scan
    unknown → needs_reconciliation, return False

if no exit_order_id:
    check entry status
    open → cancel order → if ok, return True (buy cancelled)
    partial → needs_reconciliation, return False
    filled → place_sell(token_id, current_price, shares)
              if sell placed → set exit_order_id, keep open
    cancelled → return True (already dead)
    unknown → needs_reconciliation, return False
```

Key design: **local close only happens when CLOB confirms the position is gone** (filled and sold, or cancelled).

### clob_trader.py Analysis
- **Singleton pattern** — `get_client()` caches `ClobClient`
- **Input validation** — price in (0,1), shares ≥ 0.01 and ≥ 5.0 for buys
- **Order type** — GTC limit only
- **Fail-closed returns** — exceptions return safe defaults:
  - `place_buy` → `None`
  - `place_sell` → `None`
  - `cancel_order` → `False`
  - `get_order_status` → `"unknown"`
  - `get_open_orders` → `[]`

### Risk: Immediate Status Check Race
After placing a GTC buy, the bot calls `get_order_status()` immediately. In a fast-moving market, the order could fill between `post_order` and `get_order_status`. The logic handles this because:
- If status="filled" → books as open (correct)
- If status="unknown" → marks pending (safe, reconciles later)

No race condition can cause double-booking because `skip_position` blocks balance debit on anything not "filled".

---

## 6. Reconciliation & Audit

### `assert_live_reconciliation_safe()`
Runs at every `scan_and_update()` and `run_loop()` startup. Blocks new live bets if:
1. Any CLOB open order is not tracked locally
2. Any local position is marked `needs_reconciliation`
3. Any local open position has CLOB entry status ≠ "filled"

For wallet-reconciled positions (operator proved held via balance API), it:
- Sets `entry_status = "filled_wallet_reconciled"`
- Clears `needs_reconciliation`
- Reopens market status if it was prematurely closed

### `weatherbot_audit.py` (Read-Only)
**Critical separation**: this module never trades, cancels, or mutates state.

Classifies wallet positions into buckets:
- `active_open` — matches local open position
- `claimable_or_resolved` — local closed/resolved, wallet still shows value
- `wallet_only_unexplained` — wallet has position bot doesn't know about
- `closed_held_active_ghost` — local says closed, wallet shows active
- `auxiliary_duplicate` — extra token in same market file

Computes `accounting_minus_wallet_economic` drift metric.

### Redemption (`weatherbot_redeem.py`)
- Supports both standard CTF and NegRiskAdapter markets
- **Idempotent** — skips if `redeemed_at` already set
- **Pre-flight simulation** — `fn.call()` + `estimate_gas()` before broadcasting
- **Zero-payout guard** — checks `payoutNumerators` to avoid burning losing tokens for gas
- **Gas floor alert** — warns if post-redeem MATIC drops below $2 USD
- **Broadcast mode** — only runs with explicit `--broadcast` flag; default is dry-run
- **On-chain payout verification** — parses USDC.e `Transfer` event logs from receipt

---

## 7. Configuration & Current State

```json
{
  "balance": 82.84,
  "max_bet": 1.50,
  "min_ev": 0.60,
  "max_ev": 2.00,
  "kelly_fraction": 0.10,
  "max_price": 0.30,
  "min_volume": 300,
  "min_hours": 4.0,
  "max_hours": 48.0,
  "max_open_positions": 10,
  "balance_floor": 50.0,
  "max_unrealized_loss": -5.0,
  "live_trade": true,
  "v3_live_confirmed": true,
  "auto_redeem_on_resolve": true
}
```

**Current firepower**: $82.84 balance - $50 floor = ~$32.84 available for new bets.
At max $1.50 per bet, that's ~21 bets before hitting floor.

---

## 8. Code Quality Observations

### Strengths
1. **Fail-closed everywhere** — unknown CLOB status, reconciliation divergence, missing quotes all block rather than proceed
2. **Atomic file I/O** — all state writes are crash-safe
3. **Self-learning** — multiple adaptive systems (calibration, Nicolas, win-rates, learned thresholds)
4. **Comprehensive forensics** — every resolved trade is journaled with price tier, ensemble std, drift, etc.
5. **Clear audit trail** — reconciliation adjustment was documented in config with timestamp and reason
6. **Modular redemption** — auto-redeem is separate, idempotent, and gas-aware

### Weaknesses
1. **Monolith** — 2758 lines in one file makes reasoning, testing, and review difficult
2. **No test coverage** — `.pytest_cache` exists but no test files were found in `tests/`
3. **Global mutable state** — `_cal`, `_win_rates_cache`, `_NICOLAS_DEFAULT_MODEL`, `_AUTO_CLIENTS` create hidden dependencies
4. **Magic numbers** — ENSEMBLE_AGREE thresholds, danger zones, buffer distances scattered without constants module
5. **Error handling style** — mostly `print()` rather than structured logging; makes production debugging harder
6. **Ghost resolution complexity** — three resolution paths (normal, zombie, ghost) with overlapping logic is error-prone
7. **Network fragility** — 8+ external APIs (Open-Meteo, Gamma, Visual Crossing, METAR, Polygon RPC, Coingecko, data-api); any one can degrade scan performance or cause skips
8. **No circuit breaker** — If Open-Meteo is down, the scan still tries 3 attempts per city per model = 35 cities × 4 models × 3 retries = 420 requests per scan
9. **Position change notifier** — writes state atomically but doesn't handle concurrent writes from the bot process

### Refactoring Opportunities (low priority given operational state)
- Extract probability/sizing into `signals.py`
- Extract resolution logic into `resolution.py`
- Extract forecast fetchers into `forecasts.py`
- Add pytest coverage for `bucket_prob`, `calc_ev`, `bet_size`, `analyze_signal`
- Add circuit breaker for external API failures

---

## 9. Risks Summary

| Risk | Severity | Mitigation |
|------|----------|------------|
| Accounting drift | Medium | Audit module, reconciliation backups, atomic writes |
| CLOB order status race | Low | skip_position on non-filled; immediate status check |
| Network/API failures | Medium | 3 retries per endpoint; bot continues on exception |
| Ghost position proliferation | Low | Zombie guards + auto-redeem + ghost resolution |
| Balance floor breach | Low | Hard gate at $50; projected cost check before live order |
| Dust positions (<5 shares) | Low | is_sellable_share_size guards; held to resolution |
| High-EV trap bets | Low | MAX_EV cap at 2.0 |
| Tight consensus trap | Low | MIN_ENS_STD gates; danger zone filtering |
| Manual balance correction needed again | Medium | Operator should run audit before any top-up |

---

## 10. Key Invariants Maintained

These are the properties the code actively preserves:

1. **Balance never decreases without a corresponding position** — cancelled buys are refunded
2. **A position is only "open" if either CLOB says filled or wallet reconciliation proves it**
3. **No new live bets while reconciliation has unresolved divergences**
4. **No position below 5 shares is marked as requiring active CLOB exit management**
5. **Every resolved trade feeds calibration, win-rates, Nicolas model, forensics, and learned params**
6. **Auto-redeem is idempotent and never mutates bot accounting**
7. **Atomic writes prevent truncated state files**
8. **v3 live trading requires explicit operator confirmation post probability-model fix**

---

*Analysis complete. This document should be updated when significant config changes, balance corrections, or code changes occur.*
