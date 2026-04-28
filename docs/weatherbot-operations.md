# Weatherbot Operations

## When to use
- User says "use bot_v3", "start the weather bot", "show positions", or similar
- Managing the Polymarket weather prediction bot

## Key paths
- Repo: /home/technosheen/weatherbot/
- Venv: /home/technosheen/weatherbot/venv/
- Config: /home/technosheen/weatherbot/config.json
- Env: /home/technosheen/weatherbot/.env
- Stable operational bot: bot_v3.py
- Legacy bot: bot_v2.py exists for historical fallback only. Use bot_v3.py for established production operations unless the user explicitly asks for v2.
- Log: /tmp/bot_v3_output.log
- Wallet: 0x93a65bA4E8D02eb162B49b38093F820779f80AC9
- Portfolio: https://polymarket.com/portfolio/0x93a65bA4E8D02eb162B49b38093F820779f80AC9

## Commands
- `python3 bot_v3.py run` — continuous mode (scan every 60 min, monitor every 10 min)
- `python3 bot_v3.py status` — balance + open positions
- `python3 bot_v3.py report` — full report, if supported by current v3 code
- Does NOT support `scan` subcommand unless verified in the current code first

## Git/push safety for weatherbot
When pushing weatherbot changes, inspect `git status --short` and stage only intended code/test files. `config.json` is live runtime state and may contain API keys, wallet/accounting balances, live gates, and local strategy settings; do not commit or push it unless the user explicitly asks and secrets/accounting values have been sanitized. Before pushing code changes, run the relevant targeted pytest file(s) and `git diff --check`; if the user asks for a general push, prefer committing code/tests while leaving runtime config local and call that out in the final response.

## Retiring unused weatherbot-adjacent subsystems
Use this when the user asks to delete old experimental bots, crypto strategies, agents, docs, tests, or data while keeping Weatherbot v3 safe.

Safe cleanup flow:
1. Treat `bot_v3.py`, `config.json`, `data/markets/`, `data/state.json`, calibration files, audit/redeem scripts, notifier scripts, and the `weatherbot-v3` tmux runner as production-adjacent unless proven otherwise.
2. Identify candidate files with `git ls-files` and targeted searches, then search for references before deleting:
   ```bash
   cd /home/technosheen/weatherbot
   grep -RInE 'from crypto|import crypto|crypto_bot|btc_updown|polymarket_agent|from agent|import agent' --include='*.py' --exclude-dir=venv --exclude-dir=.git . || true
   ps aux | grep -Ei 'btc_updown|crypto_bot|polymarket_agent|updown' | grep -v grep || true
   tmux ls 2>/dev/null || true
   ```
3. If deleting recursively, remove only the confirmed isolated subsystem paths. Do not delete live runtime files or `config.json`.
4. Verify immediately after deletion:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate
   grep -RInE 'from crypto|import crypto|crypto_bot|btc_updown|polymarket_agent|from agent|import agent' --include='*.py' --exclude-dir=venv --exclude-dir=.git . || true
   python3 -m pytest -q
   python3 -u bot_v3.py status 2>&1
   tmux ls 2>/dev/null || true
   ```
5. If notifier/cron functionality exists, run its check too, e.g. `python3 position_change_notifier.py`, to ensure the cleanup did not break operational alerts.
6. Before commit/push, inspect `git status --short` and stage only intended deletions/code. Leave live `config.json`, runtime state markers, backups, and secrets uncommitted unless explicitly requested and sanitized.

## bot_v3 safety/debugging
Use this for normal weatherbot operations.

Key lessons from debugging bot_v3:
- bot_v3 is the primary operational bot in `/home/technosheen/weatherbot/bot_v3.py`.
- Treat v3 as live-dangerous even if tests pass. It can read `config.json` and may have `live_trade: true`.
- Before running any v3 scan/run, inspect `config.json` for `live_trade` and `v3_live_confirmed`.
- bot_v3 should fail closed unless both are true: `live_trade: true` and `v3_live_confirmed: true`. If `live_trade` is true but `v3_live_confirmed` is false, `bot_v3.py run` and `scan_and_update()` should exit with code 2 before placing orders.
- `python3 -u bot_v3.py status` is safe for read-only inspection and should still work when the live gate blocks run/scan.
- If debugging v3 scans, clone/copy to `/tmp` and force paper mode (`live_trade=false`) before calling `scan_and_update()`; never test live scanning directly in the real repo.

Critical v3 probability bug fixed once:
- The live scanner's `bucket_prob()` incorrectly returned `1.0` for any matched finite temperature bucket, while the backtester used a continuous normal probability model.
- This inflated EV/Kelly and caused live orders with bogus `p=1.0`.
- Regression tests belong in `/home/technosheen/weatherbot/tests/test_bot_v3_probability.py` and should check finite Fahrenheit ranges, single-degree Celsius buckets, and edge buckets.
- The live scanner and backtester must use the same continuous probability model; if they diverge, trust the tested backtest model and add tests before changing trading logic.
- After probability fixes, recompute metadata for any open v3 positions (`p`, `ev`, `kelly`) but do not place/cancel/sell orders unless the user explicitly asks.
  - v3 live-readiness fixes added later:
    - `bot_v3.prepare_live_exit()` must not mark a filled live position locally closed immediately after submitting a sell order. It should store `exit_order_id`/`exit_status` and keep the local position open until the exit order reports `filled`.
    - For wallet-reconciled held positions, `prepare_live_exit()` must treat `entry_status="filled_wallet_reconciled"` as effectively filled, must not hard-fail on missing stale `order_id`, and must not locally close an unsellable `<5` share position just because the forecast changed. Unsellable wallet-held positions should stay open and hold to resolution; sellable wallet-reconciled positions may place a live sell but remain locally open until an actual fill is confirmed.
    - If an unfilled buy order is cancelled, mark `exit_status="buy_cancelled"` and use zero realized PnL; do not calculate fake PnL on an order that never filled.
    - `clob_trader.cancel_order()` must validate the CLOB response and return `False` for explicit failure / `not_canceled`; do not assume no exception means success.
    - v3 startup must not overwrite bot accounting balance with raw wallet USDC.e. It may display wallet balance, but state/config accounting should only change via net-new-funds accounting corrections. If `bot_v3.py` still contains startup code like `state["balance"] = round(real_bal, 2)` when `abs(real_bal - acct_bal) > 0.50`, treat that as a bug: wallet cash excludes collateral deployed into open positions and can falsely reduce accounting balance.
    - Balance floor nuance: checking only `balance < balance_floor` before opening a position is insufficient. A trade can start above the floor and push cash below it. New-buy logic should also block when `balance - planned_trade_cost < balance_floor` if the floor is meant to preserve idle cash.
    - Polymarket CLOB rejects SELL orders below 5 shares. v3 should avoid opening positions with fewer than 5 shares because those cannot be exited early through the CLOB. bot_v3 now validates repriced signals with `validate_repriced_signal()` and clob_trader.place_buy has a hard guard against buy orders that would create <5 shares.
    - Existing wallet-held open positions below 5 shares are hold-to-resolution exposure: keep them active so they resolve normally and still block duplicate exposure for their own city/date, but do not count them toward `max_open_positions` / new unrelated trading capacity. In bot_v3 this belongs in separate helpers (e.g. `is_active_position()` for duplicate/reconciliation exposure vs `counts_toward_trading_slots()` / `active_trading_slot_markets()` for the trading cap). Regression coverage belongs in `tests/test_bot_v3_new_entries_gate.py` and should prove 14 sub-5-share positions plus 4 sellable positions count as 4/10 trading slots, not 18/10.
    - If a fresh real ask/bid quote changes the signal, re-run EV validation after repricing. Do not open a position if repriced EV falls below `MIN_EV`, even if the stale/cached quote passed the first scan.
    - If a filled/wallet-reconciled position wants to exit but the current sell bid is non-actionable (`None`, nonnumeric, NaN/inf, `<= 0`, or `>= 1`), `prepare_live_exit()` should not call `clob_trader.place_sell()` and should not mark reconciliation required. Keep the position open/held to resolution and log that there is no actionable sell bid. Regression coverage belongs in `tests/test_bot_v3_live_exit_safety.py`.
    - If a sell fails with conditional-token allowance errors, check ERC1155 `setApprovalForAll` on Conditional Tokens `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` for Polymarket spender addresses; ERC20 USDC.e approval alone is not enough for selling conditional tokens.

Useful verification commands:
```bash
cd /home/technosheen/weatherbot && source venv/bin/activate
python3 -m pytest tests/test_bot_v3_probability.py tests/test_bot_v3_live_gate.py -q
python3 -m pytest -q
python3 -u bot_v3.py status
python3 -u bot_v3.py run; echo EXIT_CODE=$?
```

## Production-grade code review / hardening workflow
When asked to rigorously review or repair weatherbot code, use parallel subagents and then implement only safe, high-leverage fixes locally:
1. Delegate independent read-only reviews for: architecture/OOP boundaries, Polymarket CLOB execution/risk logic, and fault-tolerance/persistence/testing. Tell subagents not to run `bot_v3.py run` or any live trading path.
2. Run targeted tests before changing code, then full `/home/technosheen/weatherbot/venv/bin/python -m pytest -q` after changes.
3. Production safety issues discovered in April 2026 review:
   - Never let unknown live CLOB order status become an assumed fill/cancel after a retry count. Unknown status must fail closed, keep the position local, and set a reconciliation-required flag.
   - In live mode, fresh executable quote refresh must be mandatory. HTTP errors or missing `bestAsk`/`bestBid` should skip the trade rather than falling back to stale Gamma proxy prices.
   - `run_loop` must not overwrite bot accounting balance with raw wallet balance. Display wallet/accounting separately; adjust accounting only through explicit net-new-funds or reconciliation logic.
   - Critical JSON writes should be atomic (`tempfile.mkstemp` in same dir, flush/fsync, `os.replace`, best-effort parent fsync) for state, market files, calibration, win rates, journal, and learned params.
   - Data paths should be repo-relative (`Path(__file__).resolve().parent / "data"`), not cwd-relative.
   - Before enforcing `balance_floor`, estimate the actual submitted CLOB buy notional using the same price rounding/share-ceiling behavior as `clob_trader.place_buy`.
   - Fetch `actual_temp` before `post_trade_forensics()` during resolution so journal entries include forecast error and edge diagnostics.
   - Live GTC buy submission must be modeled as an order first, not a filled position. A successful `place_buy()` should immediately persist `position.status="pending_buy"`, `entry_status="submitted"`, and `needs_reconciliation=true`; only after `clob_trader.get_order_status(order_id)` returns confirmed `filled` should bot_v3 mark `status="open"` and debit accounting. `open`, `partial`, or `unknown` must remain reconciliation-required and block duplicate exposure.
   - `clob_trader.get_order_status()` must use an explicit status allowlist: known live statuses -> `open`, terminal filled statuses -> `filled`, `expired/cancelled/canceled` -> `cancelled`, recognized partial fill states or fill-size mismatches -> `partial`, anything unmapped/errored -> `unknown`. Never collapse unknown future statuses to `open`.
   - Live auto-resolution must check entry order status before settling. If status is `open`, `partial`, or `unknown`, mark reconciliation required and skip win/loss/PnL. If status is confirmed `cancelled`, refund any locally reserved cost, mark `exit_status="buy_cancelled"`, and exclude it from ghost-resolution learning/win-rate labels.
   - `validate_repriced_signal()` should reject malformed live quotes before EV math: nonnumeric, NaN/inf, `bid > ask`, `bid <= 0`, or `ask >= 1`. Use a single CLOB buy sizing helper for repriced shares/cost and balance-floor projection.
4. Larger unresolved design risks: startup reconciliation still needs a first-class gate comparing local market JSONs to live CLOB open orders/token balances before new live trading. Cross-file accounting transitions still need a journal/transaction layer before this is truly institutional production-grade.
5. When checking a live bot for errors, do not stop at tmux/logs/tests. Also query live CLOB open orders and compare them to local market JSONs. A real issue found once: a Sao Paulo BUY order was `LIVE` with `size_matched=0`, but `/data/markets/sao-paulo_2026-04-27.json` was locally recorded as `position.status="open"`, `shares=5`, `cost=1.0`. Treat this as P0: unfilled submitted orders must be `pending_buy`/`needs_reconciliation`, not filled/open positions. Do not cancel or edit live state without explicit user authorization.
6. `clob_trader.get_order_status()` may return `unknown` for many older local order IDs. Do not infer filled/cancelled from `unknown`; use open-order queries, token balances/fills, and explicit reconciliation reports. New-live trading should be blocked if local/CLOB state disagrees.
7. Operational health checks should flag: CLOB open orders not reflected locally, local positions tied to unmatched live CLOB orders, `open_count > max_open_positions`, positions with `<5` shares that cannot be normally sold, and config/state balance divergence.
8. A deeper reconciliation pass found a failure mode where market JSONs were locally marked `position.status="closed"` after stop-loss / forecast-change / trailing-stop logic, but the wallet still held the ERC1155 YES tokens. Do not trust local `closed` status as proof of a live exit. For live reconciliation, compare every local token_id — open AND closed — against Polymarket data-api positions and, when possible, Polygon Conditional Tokens `balanceOf`.

## Live reconciliation / ghost-position workflow
Use this when the user asks to reconcile weatherbot v3, investigate unknown CLOB statuses, or decide whether it is safe to restart live trading.

Safety rules:
- Read-only first. Do not place/cancel/sell live orders or edit market JSON unless explicitly authorized.
- If `weatherbot-v3` is running and live-state integrity is suspect, pause tmux before further debugging.
- Do not restart live trading while wallet-held positions are missing from local open/reconciliation state.

Read-only evidence collection:
1. Check process/state/tests:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate
   tmux ls 2>/dev/null || true
   python3 -u bot_v3.py status 2>&1
   python3 -m pytest -q
   ```
2. Confirm no resting CLOB orders:
   ```python
   import clob_trader
   orders = clob_trader.get_open_orders()
   print(f"open_orders_count={len(orders)}")
   ```
3. Query Polymarket data-api wallet positions; this is the fastest way to detect ghost positions still held despite local `closed` status:
   ```python
   import requests
   wallet = "0x93a65ba4e8d02eb162b49b38093f820779f80ac9"
   positions = requests.get(
       "https://data-api.polymarket.com/positions",
       params={"user": wallet, "limit": 200},
       timeout=15,
   ).json()
   ```
4. For exact on-chain local-open balances, call ERC1155 `balanceOf(address,uint256)` on Conditional Tokens `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`. A public Polygon RPC that worked once: `https://polygon-bor-rpc.publicnode.com`. Balance values are 6-decimal fixed point:
   ```python
   import requests
   wallet = "0x93a65ba4e8d02eb162b49b38093f820779f80ac9"
   ctf = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
   token_id = "..."
   data = "0x00fdd58e" + wallet[2:].rjust(64, "0") + hex(int(token_id))[2:].rjust(64, "0")
   j = requests.post("https://polygon-bor-rpc.publicnode.com", json={
       "jsonrpc": "2.0", "id": 1, "method": "eth_call",
       "params": [{"to": ctf, "data": data}, "latest"],
   }, timeout=8).json()
   shares = int(j["result"], 16) / 1_000_000
   ```
5. Build a report by matching `data/markets/*.json` `position.token_id` to data-api `asset`, separating:
   - local open positions still held
   - local closed positions still held (ghost exposure)
   - sellable positions with actual wallet size >= 5
   - unsellable positions with actual wallet size < 5

Interpretation:
- `clob_trader.get_order_status(order_id) == "unknown"` for older orders is not enough to infer anything. Use data-api positions and ERC1155 balances as authoritative evidence of held tokens.
- Polymarket data-api `size`/ERC1155 balance may be slightly lower than bot local `shares` due to fee/rounding effects; use actual wallet size for sellability and reconciliation.
- Local `closed` + wallet still holding tokens means prior bot accounting likely booked a close that did not actually execute. Treat this as live exposure that must be represented locally before restart.
- Positions below 5 shares cannot normally be sold through CLOB; default recommendation is hold-to-resolution unless the user explicitly chooses another strategy.
- If a position appears in Polymarket data-api `positions` but has no matching local market JSON at all, treat it as hidden live exposure (`wallet_only`). Build or restore a local record before allowing new live trading.
- If a market JSON is still `status="open"` but also has `closed_at` populated, treat that as corrupt local state needing repair before trusting summaries/open-count gates.

## Full wallet audit workflow (read-only)
Use this when the user asks for a full audit from Polygon wallet transactions, trades, and reconciliations.

Goal: reconcile four views without mutating anything:
1. local bot files (`config.json`, `data/state.json`, `data/markets/*.json`)
2. live bot status/tmux logs
3. Polymarket indexed APIs (`positions`, `trades`)
4. on-chain wallet cash (USDC.e) plus optional tx receipt spot-checks

Recommended flow:
1. Run safe local checks first:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate
   python3 -u bot_v3.py status 2>&1
   python3 -m pytest -q
   tmux capture-pane -t weatherbot-v3 -p -S -120 2>/dev/null | tail -120
   ```
2. Read `config.json` and `data/state.json` and compare balances.
3. Build a local market inventory from `data/markets/*.json` capturing at least:
   - slug / city / date
   - `position.token_id`
   - `position.status`
   - `shares`, `cost`, `entry_price`
   - `order_id`, `entry_status`, `exit_status`
   - `needs_reconciliation`, `closed_at`, `closed_reason`
4. Query live Polymarket wallet state:
   - `https://data-api.polymarket.com/positions?user=<wallet>&limit=500`
   - `https://data-api.polymarket.com/trades?user=<wallet>&limit=500`
   Notes:
   - The `trades?user=` filter works for this wallet and is the fastest audit source for recent actual BUY/SELL executions.
   - A recent audit showed `trades` can reveal many BUYs even when local files imply positions were closed; use this to challenge local bookkeeping.
5. Query live CLOB open orders via `clob_trader.get_open_orders()`.
6. Query on-chain bridged USDC.e balance directly from Polygon via ERC20 `balanceOf` on `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`.
7. Reconcile by token ID / asset ID and classify:
   - local open + wallet held
   - local closed + wallet held (`closed_held`)
   - wallet held but no local record at all (`wallet_only`)
   - local open missing from wallet
   - local open with `closed_at` populated (corrupt local state)
   - open positions with `<5` shares (normally unsellable on CLOB)
8. Sum wallet values into buckets:
   - raw wallet USDC.e
   - live unresolved mark-to-market value
   - redeemable resolved claims
   - wallet-only hidden live exposure
9. Spot-check recent trade transaction hashes with Polygon `eth_getTransactionByHash` / `eth_getTransactionReceipt` if you want proof the indexed Polymarket trades correspond to successful on-chain settlement. Expect `tx.from` to often be a relayer/proxy address rather than the wallet EOA.

Key findings/pitfalls from a real audit:
- Public Polygon RPCs available in-agent may be pruned and enforce small `eth_getLogs` block ranges. Do not rely on generic full-history log scraping as the primary audit method.
- For this environment, Polymarket data-api `positions` + `trades`, live CLOB open-order checks, and on-chain USDC.e `balanceOf` gave the most reliable audit picture.
- A wallet can show materially more economic value than bot accounting because value may be split across idle USDC.e, unresolved positions, and redeemable resolved claims.
- If recent `trades` show many BUYs but almost no SELLs while local files show many `closed` positions, assume local closes may be bookkeeping-only until proven by wallet state.
- `needs_reconciliation=false` everywhere does not prove safety; compare against actual wallet holdings.

Safe next step after finding ghost positions:
- Recommend a local-only reconciliation patch: mark ghost-held positions back to `open`, update local shares to actual wallet/data-api size, preserve old local-close metadata, and initially block new entries until all wallet-held positions are represented locally. After the wallet/data-api reconciliation is applied, clear `needs_reconciliation` only for positions with durable proof fields such as `entry_status="filled_wallet_reconciled"`, positive `wallet_shares`, `wallet_reconciled_at`, or equivalent helper detection.
- Before editing any market JSONs, create a timestamped backup directory under `data/reconciliation_backups/` and copy every file you will touch.
- If reconciliation discovers multiple live wallet positions for the same `city/date` file slug (real example: Toronto 2026-04-28 had both a 19°C-or-higher position and a separate 17°C position), do NOT fake a full repair by merging or overwriting one into the other. `bot_v3.py` currently models only one primary `position` per market file. In that case, restore the primary wallet-held position you can represent, preserve the displaced/conflicting one under an auxiliary field such as `wallet_reconciliation_extra_positions`, add explicit warning metadata, and decide the gate based on user preference:
  - conservative default: leave `needs_reconciliation=true` with a clear blocked status if the user wants the bot halted until the schema is improved
  - operational workaround used successfully in April 2026: keep the primary position live, preserve the second wallet-held position in auxiliary metadata, set the primary `entry_status="filled_wallet_reconciled"`, clear `needs_reconciliation=false`, and rely on post-restart wallet-vs-local sanity checks while accepting that `status` open-count will understate exposure by one
- A good post-repair verification script should prove four things separately: (1) no open positions still carry stale close metadata, (2) no locally closed positions are still wallet-live by token_id match, (3) any remaining wallet-vs-local mismatch is explicitly explained by preserved auxiliary conflict metadata rather than silent drift, and (4) after restart, previously restored wallet-held positions did not get reclosed by scan logic.
- Critical April 2026 finding: bot restart can silently undo a successful wallet reconciliation. In `bot_v3.py`, the zombie guard path `if mkt.get("status") == "closed" and mkt.get("position", {}).get("status") == "open":` force-flips positions back to closed and logs `[ZCLOSE] ... (zombie fix)`. This incorrectly recloses wallet-held reconciled positions on startup/full scan, recreating ghost exposure immediately after a repair. After any reconciliation-based restart, always run a fresh wallet-vs-local sanity check instead of trusting `bot_v3.py status` alone.
- Durable fix requirements discovered in April 2026:
  - the zombie-close path must skip wallet-reconciled held positions, including markets whose `market.status` is `closed` or `resolved`, when local proof fields show real held exposure (`entry_status="filled_wallet_reconciled"`, positive `wallet_shares`, `wallet_reconciled_at`, or equivalent helper detection)
  - `assert_live_reconciliation_safe()` must process wallet-reconciled held positions before skipping top-level `market.status in ("closed", "resolved")`. If the wallet-proof position is open while the wrapper market is closed/resolved, reopen the wrapper to `status="open"`, clear `needs_reconciliation`, and save. A real re-audit found 29 locally closed/wallet-held positions plus 11 `position.status="open"` records hidden under top-level closed/resolved wrappers; the repair moved status from 19 open/54 closed to 48 open/25 closed with zero closed-held ghosts.
  - `prepare_live_exit()` must not locally close wallet-reconciled held positions solely because forecast/stop logic wants an exit; it must either submit a real sell and keep the position open pending fill, or hold the position open to resolution if the wallet size is below the CLOB minimum
- Operational symptoms of this bug: status/open-count can drop sharply after restart (for example 19 -> 7 open), or `bot_v3.py status` can show far fewer open positions than Polymarket data-api/Polygon ERC1155 balances; tmux may show a burst of `[ZCLOSE] ... (zombie fix)` lines.
- Regression tests that call `assert_live_reconciliation_safe()` with synthetic markets must monkeypatch `save_market` to a no-op or temp path. The real helper writes by `city/date`, so a test market like `nyc_2099-01-01` can otherwise pollute `/home/technosheen/weatherbot/data/markets/`. Add/keep tests for both: (1) wallet-reconciled positions are not zombie-closed when the top-level market is closed, and (2) `assert_live_reconciliation_safe()` reopens a top-level closed wallet-reconciled held position.
- Full re-audit workflow refinement: after matching data-api positions to primary `position.token_id`, also scan `wallet_reconciliation_extra_positions` so known city/date duplicate exposure is classified as explained auxiliary rather than unexplained wallet-only. Conversely, if a local open primary token is absent from data-api positions, check recent data-api trades and Polygon receipts; a confirmed SELL plus absent ERC1155/data-api position means close the local position as wallet-sold instead of reopening it. Preserve old PnL/accounting unless doing a separate explicit ledger adjustment.
- Manage-only/no-new-buys restart workflow used successfully after a dirty reconciliation:
  1. Add or verify an explicit config gate such as `"new_entries_enabled": false`; do not rely only on `open_count > max_open_positions` because that protection disappears as positions resolve.
  2. Gate only fresh-entry/buy paths in `bot_v3.py`; monitoring, resolving, holding, and live exit/reconciliation paths should still run.
  3. Add a regression test proving `analyze_signal()`/fresh-entry logic does not call `place_buy()` when `NEW_ENTRIES_ENABLED` is false, then run the full pytest suite.
  4. Update `config.json` atomically or via a small Python JSON rewrite; avoid exposing or rewriting credentials and do not change accounting balance as part of this safety gate.
  5. Restart with the normal tmux command and verify all of: tmux process exists, config reads `False`, startup/full-scan log says new entries are blocked or `new: 0`, CLOB open orders are still zero, and `python3 -u bot_v3.py status` is healthy.
  6. Immediately run a fresh wallet-vs-local re-audit after the first full scan. The scan/resolution code can legitimately change balance/open/resolved counts and can also reintroduce dirty state (for example locally closed but wallet-held resolved tokens, top-level closed wrappers hiding open positions, or already-sold positions being reopened). Do not assume a pre-restart clean audit remains clean after the bot processes resolutions.
- Manage-only re-audit interpretation: local-closed positions that still appear in Polymarket data-api may be resolved/redeemable tokens rather than actionable live exposure. Keep them separate from unresolved open risk, and plan a claim/redeem plus accounting reconciliation step instead of force-reopening every closed-held token.
- A concrete fix from April 2026: `is_wallet_reconciled_held_position()` must exclude terminal local closes (`exit_status` filled/filled_wallet_sell_confirmed/buy_cancelled, or `close_reason` resolved/buy_cancelled) and require positive `wallet_shares`; otherwise `assert_live_reconciliation_safe()` can resurrect already-sold positions like Buenos Aires or resolved claimable tokens like Tokyo/Wellington.
- Another concrete fix from April 2026: the D+0 cutoff path that sets `market.status="closed"` when `hours < 0.5` must not do so while `position.status="open"`; otherwise active wallet-held exposure is hidden under a top-level closed wrapper on every manage-only scan. Keep top-level `status="open"` until actual market resolution closes the position.
- A reusable read-only audit helper now exists at `/home/technosheen/weatherbot/weatherbot_audit.py`. Run it with `cd /home/technosheen/weatherbot && source venv/bin/activate && python3 weatherbot_audit.py`. It classifies wallet positions into active open exposure, resolved/claimable tokens, auxiliary duplicate exposure, and unexplained wallet-only rows, and reports wallet USDC.e, active position value, claimable/resolved value, total wallet economic value, bot accounting balance, and accounting-vs-wallet gap. Use this before any redeem/accounting work; it sends no transactions and mutates nothing.

## Redemption workflow
Use this when the user asks to run redemption, redeem resolved wins, claim Polymarket payouts, or settle claimable weatherbot positions.

Safety and flow:
1. Always run the dry-run plan first:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 weatherbot_redeem.py
   ```
   This prints `tx_count`, expected claimable total, contracts, gas estimate, and candidate files without signing or broadcasting.
2. If `tx_count=0`, there is nothing to redeem. Do not expect transactions; a subsequent `--broadcast` will return `receipts: []`.
3. If the plan has positive `tx_count` and looks sane, broadcast only after the user has requested redemption:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 weatherbot_redeem.py --broadcast
   ```
   The script signs with `PK` from `.env`, verifies the private key matches the configured wallet, waits for receipts, and errors if a receipt fails.
4. Verify after any redemption attempt with:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 weatherbot_audit.py | tail -80
   ```
   Report `claimable_positive`, `claimable_or_resolved_value`, wallet USDC.e, active position value, wallet economic total, open count, and `needs_reconciliation`.
5. Redemption is on-chain settlement only. It should not mutate bot accounting balance; bot accounting should already book wins during resolution. Handle any accounting reconciliation as a separate explicit task.

## Live error-check workflow
When the user asks to "check bot 3 for errors" or similar:
1. Verify process/log health:
   ```bash
   tmux ls 2>/dev/null || true
   tmux capture-pane -t weatherbot-v3 -p -S -200 2>/dev/null | tail -200
   tail -800 /tmp/bot_v3_output.log | grep -Ei 'error|warn|traceback|exception|failed|timeout|expecting value|connection|skipping|blocked|reconciliation' | tail -120
   ```
2. Run safe commands only:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate
   python3 -u bot_v3.py status 2>&1
   python3 -m pytest -q
   ```
3. Compare local state to live CLOB open orders without mutating anything:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate
   python3 - <<'PY'
   import json, glob, os, clob_trader
   orders = clob_trader.get_open_orders()
   print(f"open_orders_count={len(orders)}")
   for o in orders:
       print({k:o.get(k) for k in ['id','status','side','original_size','size_matched','price','outcome','asset_id']})
   order_ids = {o.get('id') for o in orders}
   for p in sorted(glob.glob('data/markets/*.json')):
       m=json.load(open(p)); pos=m.get('position') or {}
       if pos.get('order_id') in order_ids:
           print('LOCAL_MATCH', p, pos.get('status'), pos.get('shares'), pos.get('cost'), pos.get('order_id'))
   PY
   ```
4. Scan local market JSONs for open count, exposure, unsellable `<5` share positions, and `needs_reconciliation` flags. Report these as operational risks even if the bot is not crashing.
5. If the live bot is currently running while code/tests show unresolved live-trading safety failures, pause the tmux runner before editing/retesting to prevent new orders during debugging:
   ```bash
   tmux send-keys -t weatherbot-v3 C-c
   ```
   This stops local automation only; it does not cancel/modify live Polymarket orders or market JSON state.
6. Report findings in priority order: fatal crash/traceback, live CLOB/local mismatch, risk-limit/accounting issues, recoverable data-source warnings (e.g. METAR timeout/JSON parse), then improvements. Avoid changing live orders/state unless the user explicitly authorizes remediation.

## Starting the bot (persistent)
Use tmux, NOT Hermes background processes. Hermes background procs die with the session.

```bash
tmux kill-session -t weatherbot-v3 2>/dev/null
tmux new-session -d -s weatherbot-v3 \
  "cd /home/technosheen/weatherbot && source venv/bin/activate && PYTHONUNBUFFERED=1 python3 bot_v3.py run 2>&1 | tee -a /tmp/bot_v3_output.log"
```

Verify it started:
```bash
sleep 5 && tmux capture-pane -t weatherbot-v3 -p | head -20
```

Attach/stop:
- `tmux attach -t weatherbot-v3`
- `tmux send-keys -t weatherbot-v3 C-c`

## Pitfall: Python stdout buffering
bot_v3.py can produce ZERO output in Hermes background mode due to Python stdout buffering. Use `PYTHONUNBUFFERED=1` plus `tee` when starting it in tmux.

## Checking positions
Just run status — it's fast and doesn't interfere with the running bot:
```bash
cd /home/technosheen/weatherbot && source venv/bin/activate && python3 -u bot_v3.py status 2>&1
```

## Self-learning
The bot auto-calibrates sigma values from resolved markets via `run_calibration()`. No manual intervention needed — it adjusts forecast confidence based on past accuracy. Calibration only kicks in after markets resolve — until then it uses default sigma (2.0°F / 1.2°C) which can be too aggressive.

## Config tuning
Config lives in config.json. Bot reads it at startup — restart tmux session after changes.

Key levers for risk management:
- `balance`: should reflect actual USDC.e in wallet, not starting amount
- `max_bet`: cap per trade ($1.0 conservative, $2.0 default)
- `min_ev`: minimum expected value to enter (0.5 conservative, 0.1 default — 0.1 is too loose pre-calibration)
- `max_price`: max ask price to buy (0.30 conservative, 0.45 default)
- `kelly_fraction`: sizing multiplier (0.25 default)
- `max_slippage`: max bid-ask spread (0.03 default)

Because v3 avoids opening positions below the 5-share CLOB sell minimum, `max_bet` and `max_price` interact:
- Required notional for sellable buys is roughly `5 * ask_price`.
- At `max_price=0.30`, `max_bet` must be at least `$1.50` to allow 5 shares.
- At `max_price=0.45`, `max_bet` must be at least `$2.25` to allow 5 shares.
- Raising `max_bet` without lowering `min_ev` is the safer way to restore eligible trade flow after the 5-share guard.

Conservative config for early/pre-calibration phase:
```json
"max_bet": 1.0, "min_ev": 0.5, "max_price": 0.30
```

Default config was too aggressive before calibration data exists — bot went -65% in first day on defaults. Recommend conservative settings until at least 10+ markets have resolved and calibration has real data.

## Restarting / forcing an immediate re-scan after config change
bot_v3 does not have a verified standalone `scan` subcommand. To force an immediate scan after changing config, restart the tmux runner; `bot_v3.py run` performs a full scan on startup.

```bash
tmux kill-session -t weatherbot-v3 2>/dev/null
tmux new-session -d -s weatherbot-v3 \
  "cd /home/technosheen/weatherbot && source venv/bin/activate && PYTHONUNBUFFERED=1 python3 bot_v3.py run 2>&1 | tee -a /tmp/bot_v3_output.log"
sleep 5 && tmux capture-pane -t weatherbot-v3 -p | head -15
```

Verification pattern for a forced re-scan:
1. Capture the pane/log and wait until the scan prints a completed summary line like `balance: $... | new: N | closed: N | resolved: N`.
2. Run `python3 -u bot_v3.py status` to verify open positions after the scan.
3. Report notable skips from the pane/log, especially repricing skips (`real ask`, `spread`, `EV below min`, `shares below sell minimum`).

## Syncing config balance to live balance
When the user asks to "update the config with the new balance", do this exact flow:
1. Run `python3 -u bot_v3.py status` from the repo venv to get the current live balance.
2. Read `config.json` and update only the `balance` field to the status value.
3. Restart the tmux session, because the bot only reads config at startup.
4. Verify both:
   - `config.json` shows the new balance
   - `tmux capture-pane -t weatherbot-v3 -p` shows the bot restarted

Important: the live balance can move again almost immediately after restart because open positions are mark-to-market. So config may become stale again right away; that's expected.

## Advising on adding more bankroll
When the user asks whether to add funds, ground the recommendation in current risk state instead of just using headline PnL:
1. Run `python3 -u bot_v3.py status` for live balance, open count, and unrealized PnL.
2. Read `config.json` for `max_bet`, `min_ev`, `max_price`, and `kelly_fraction`.
3. Estimate current open exposure by scanning `/home/technosheen/weatherbot/data/markets/*.json` for `position.status == "open"`; cost basis is `shares * entry_price` because old positions may not store a `size` field.
4. Check `/tmp/bot_v3_output.log` or tmux pane for recent closes/skips and whether the process is healthy.
5. Recommendation heuristic:
   - Adding funds is reasonable if open exposure is small relative to bankroll and config remains conservative (`max_bet` around `$1`, `min_ev` around `0.5`).
   - Do NOT suggest increasing `max_bet` or lowering `min_ev` until there is a meaningful live resolved weather record.
   - Treat mark-to-market gains as encouraging but not statistically meaningful when `Resolved: 0`.
6. If the user adds/swaps funds while the bot already has an accounting balance in `data/state.json`, do **not** overwrite the bot state with raw wallet USDC.e. Add only the net new USDC.e received/deposited to the existing `data/state.json` balance, then set `config.json` to the same corrected accounting balance and restart tmux. The wallet's raw USDC.e can be lower than bot accounting balance because prior bot gains/open positions are tracked in bot state, not necessarily as idle USDC.e.

## Tmux session can die between Hermes sessions
Always check `tmux ls` before assuming the bot is running. If the tmux server is gone, just recreate it.

## Telegram position open/close alerts
If the user wants Telegram alerts whenever a new position opens or an existing position closes/resolves, use the local notifier script plus a Hermes cron job.

Current working setup:
- Script: `/home/technosheen/weatherbot/position_change_notifier.py`
- State marker: `/home/technosheen/weatherbot/data/telegram_position_change_state.json`
- Cron job name: `weatherbot-telegram-position-change-alerts`
- Cron job id seen when created: `9c4e465ef27c` (always `cronjob(action="list")` before managing it; do not assume this ID forever)
- Schedule: every 5 minutes
- Delivery pattern: cron `deliver="local"`; the cron prompt uses `send_message(target="telegram")` only when the notifier reports a real change, then commits the marker state. This avoids "no changes" spam.

Safe setup/repair flow:
1. Create or inspect the script. It should read `data/markets/*.json`, compare current position statuses to the marker JSON, and emit a single-line JSON object:
   - `{"changed": false, ...}` when no alert is needed
   - `{"changed": true, "message": "..."}` when a Telegram alert should be sent
   It must not trade, redeem, cancel, or edit market files.
2. Initialize the marker once to prevent a flood of alerts for existing positions:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 position_change_notifier.py --init
   ```
3. Verify no immediate false positive:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 position_change_notifier.py
   ```
4. Create/update a recurring Hermes cron job whose self-contained prompt runs the notifier, sends `message` to Telegram only when `changed=true`, and only then commits state with:
   ```bash
   cd /home/technosheen/weatherbot && source venv/bin/activate && python3 position_change_notifier.py --commit
   ```
5. Use `cronjob(action="list")` to verify it is scheduled. If updating/removing, list first and use the actual job ID.

## Telegram updates after hourly full scans
If the user wants Telegram status updates after each hourly full scan, do **not** rely on a one-off assistant message. Set up recurring automation with duplicate protection.

Known working pattern:
1. Mark the latest already-notified scan timestamp so old scans do not resend:
   - Marker file: `/home/technosheen/weatherbot/data/last_telegram_scan_update.txt`
   - Content format: `YYYY-MM-DD HH:MM:SS`
2. Create a recurring cron job (Hermes cron is acceptable for this notification layer) that runs every 10 minutes and:
   - Parses `/tmp/bot_v3_output.log` for the latest completed pair:
     - `[YYYY-MM-DD HH:MM:SS] full scan...`
     - followed by `balance: $... | new: N | closed: N | resolved: N`
   - Compares the scan timestamp to the marker file.
   - If no newer completed full scan exists, sends nothing.
   - If a newer completed scan exists, runs:
     ```bash
     cd /home/technosheen/weatherbot && source venv/bin/activate && python3 -u bot_v3.py status 2>&1
     ```
   - Sends a concise Telegram home-channel message with scan timestamp, scan summary, balance, open/resolved counts, unrealized PnL, and open positions.
   - Updates the marker file only after the Telegram send succeeds.

Reasoning: full scans are hourly but monitor ticks are every 10 minutes; polling every 10 minutes catches the scan soon after completion without sending monitor noise. The marker prevents duplicates across restarts or repeated cron executions.

Example cron job name: `weatherbot-telegram-hourly-scan-update`.

## Training calibration from Polymarket historical data

The bot's sigma values (forecast uncertainty) can be trained from resolved Polymarket weather markets. This dramatically improves bet selection — defaults were wrong by 2-5x for some cities.

### Data source
Use the Gamma API, NOT the 6GB S3 archive (`orderFilled_complete.csv.xz` — too large, 99% irrelevant non-weather data).

### Workflow
1. **Search resolved weather events** per city via Gamma API:
   ```
   GET https://gamma-api.polymarket.com/public-search?q=highest+temperature+{city}
   ```
   Filter for `closed=True` events with "temperature" in title. Yields ~2-3 resolved events per city.

2. **Fetch event details** with full market data:
   ```
   GET https://gamma-api.polymarket.com/events?id={event_id}
   ```
   Each event has ~11 markets (temperature buckets). The winner has `outcomePrices[0] > 0.95`.

3. **Parse winning bucket** to get actual temperature. Handle all question formats:
   - Single degree: `"be 18°C on April 23"` → 18°C
   - Range: `"between 70-71°F"` → 70.5°F
   - Upper edge: `"22°C or higher"` → ~24°C
   - Lower edge: `"12°C or below"` → ~10°C

4. **Compute market-implied forecast** = volume-weighted average temp across all buckets.

5. **Compute optimal sigma per city** = RMSE of (actual - market_forecast) errors, floored at 0.5.

6. **Write calibration.json** with entries for each `{city}_{source}` key (ecmwf, hrrr, metar).

### Pitfalls discovered
- **Price history API returns empty** for resolved markets — don't bother with `clob.polymarket.com/prices-history`.
- **Trade data API** (`data-api.polymarket.com/trades`) works but only keeps recent trades. Use `json_parse()` not `json.loads()` — responses contain control characters that break strict JSON parsing.
- **Volume field is a string** in Gamma API market objects — cast with `float(m.get('volume', 0) or 0)`.
- No "weather" tag exists in Polymarket tags — must search by city name + "highest temperature".

### Results from 44 resolved markets
Key corrections to defaults (2.0°F / 1.2°C):
- **NYC 2.0→0.63°F, Miami 2.0→0.76°F**: Very predictable, bot was overpaying
- **Chicago 2.0→5.94°F, Atlanta 2.0→6.15°F**: High variability, bot was underestimating uncertainty
- **Wellington 1.2→3.54°C, Ankara 1.2→2.56°C**: Similar underestimate
- **Singapore 1.2→0.93°C, São Paulo 1.2→0.58°C**: Tropical cities very stable

### Data files
- `/home/technosheen/weatherbot/data/calibration.json` — loaded by bot on startup
- `/home/technosheen/weatherbot/data/training_data.json` — 44 resolved records
- `/home/technosheen/weatherbot/data/all_trade_data.json` — trade-level data
- `/tmp/weather_markets_raw.json` — raw Gamma API market dump (494 markets)
