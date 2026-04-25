#!/usr/bin/env python3
"""General Polymarket quick-win agent.

Incrementally scans all active markets, scores them for "quick win potential"
(high-confidence markets near resolution), places bets, and self-improves via
Bayesian calibration of win rates per category/price/time bucket.

Usage:
  python polymarket_agent.py run            -- paper mode (safe)
  python polymarket_agent.py run --live     -- live CLOB orders
  python polymarket_agent.py status         -- positions + P&L
  python polymarket_agent.py report         -- full calibration report
  python polymarket_agent.py scan           -- one-shot scan, no bets
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from agent.discovery import IncrementalScanner, PolyMarket
from agent.evaluator import score_market, rank_opportunities, Opportunity
from agent import calibrator, state as st, executor

CONFIG_PATH = Path(__file__).parent / "config.json"
SCAN_INTERVAL   = 300    # seconds between full rescans
RESOLVE_INTERVAL = 120   # seconds between resolution checks
MAX_OPEN = 5             # max concurrent positions
MAX_HOURS = 72.0
MIN_VOLUME = 500.0
MIN_PRICE = 0.65
KELLY_FRACTION = 0.20
MAX_BET = 5.0


def _read_balance() -> float:
    try:
        return float(json.loads(CONFIG_PATH.read_text()).get("balance", 17.31))
    except Exception:
        return 17.31


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ─── display ─────────────────────────────────────────────────────────────────

def _print_opportunity(opp: Opportunity, prefix: str = "") -> None:
    h = opp.market.hours_to_expiry
    print(f"{prefix}[{opp.category}] {opp.market.question[:70]}")
    print(f"{prefix}  expires in {h:.1f}h | {opp.direction.upper()} @ {opp.entry_price:.3f} "
          f"| fair={opp.fair_prob:.3f} | ev={opp.ev_after_fee:+.4f} "
          f"| score={opp.quick_win_score:.4f} | size=${opp.bet_size:.2f}")


def _print_status(state: dict) -> None:
    open_pos = st.get_open(state)
    resolved = st.get_resolved(state)
    start = state.get("starting_balance", state["balance"])
    pct = (state["balance"] - start) / start * 100 if start else 0

    print("=" * 65)
    print("  POLYMARKET AGENT — STATUS")
    print("=" * 65)
    print(f"  Balance:  ${state['balance']:.2f}  (start ${start:.2f}, {pct:+.1f}%)")
    print(f"  Open: {len(open_pos)}  |  Resolved: {len(resolved)}")

    if open_pos:
        print("\n  Open positions:")
        for p in open_pos:
            unr = p.get("unrealized_pnl", 0.0)
            sign = "+" if unr >= 0 else ""
            h_left = ""
            try:
                end = datetime.fromisoformat(p["end_date"].replace("Z", "+00:00"))
                h_left = f"  {(end - datetime.now(timezone.utc)).total_seconds()/3600:.1f}h left"
            except Exception:
                pass
            print(f"    {p['direction'].upper():<3} {p['category']:<12} "
                  f"entry={p['entry_price']:.3f}  mark={p.get('mark_price', p['entry_price']):.3f}  "
                  f"pnl={sign}{unr:.3f}{h_left}")
            print(f"         {p['question'][:65]}")

    if resolved:
        wins = sum(1 for p in resolved if p.get("realized_pnl", 0) > 0)
        total_pnl = sum(p.get("realized_pnl", 0) for p in resolved)
        print(f"\n  Resolved ({wins}/{len(resolved)} wins, total pnl={total_pnl:+.3f}):")
        for p in resolved[-10:]:
            pnl = p.get("realized_pnl", 0)
            tag = "WIN " if pnl > 0 else "LOSS"
            print(f"    {tag} {p['direction'].upper():<3} {p['category']:<12} "
                  f"pnl={pnl:+.4f}  |  {p['question'][:55]}")
    print("=" * 65)


def _print_report(state: dict) -> None:
    _print_status(state)
    rows = calibrator.summary()
    if not rows:
        print("\n  No calibration data yet.")
        return
    print(f"\n  Calibration table ({len(rows)} cells with data):")
    print(f"  {'category':<12} {'price':<12} {'time':<10} {'bets':>5} {'wins':>5} {'win%':>6} {'factor':>7}")
    for row in rows:
        print(f"  {row['category']:<12} {row['price_bucket']:<12} {row['time_bucket']:<10} "
              f"{row['bets']:>5} {row['wins']:>5} {row['win_rate']*100:>5.1f}% {row['factor']:>7.3f}")


# ─── main run loop ────────────────────────────────────────────────────────────

def cmd_run(live: bool = False) -> None:
    mode = "LIVE" if live else "PAPER"
    print(f"[{_ts()}] Polymarket agent starting ({mode} mode)")
    if live:
        print("  WARNING: live mode submits real orders via CLOB")

    balance = _read_balance()
    state = st.init(balance)
    scanner = IncrementalScanner()

    bet_market_ids: set[str] = set()
    last_scan = 0.0
    last_resolve = 0.0
    cycle = 0

    while True:
        now = time.time()
        cycle += 1

        # ── resolve expired positions ────────────────────────────────────
        if now - last_resolve > RESOLVE_INTERVAL:
            resolved = st.check_and_resolve_expired(state)
            for pos in resolved:
                won = pos.get("exit_price", 0) >= 0.95
                tag = "WIN " if won else "LOSS"
                print(f"[{_ts()}] {tag} resolved: {pos['question'][:60]}")
                print(f"         pnl={pos['realized_pnl']:+.4f}  "
                      f"direction={pos['direction'].upper()}  "
                      f"category={pos['category']}")
            last_resolve = now

        # ── incremental market scan ──────────────────────────────────────
        if now - last_scan > SCAN_INTERVAL:
            print(f"[{_ts()}] Scanning markets...")
            new_markets = scanner.scan(
                max_hours=MAX_HOURS,
                min_volume=MIN_VOLUME,
                min_price=MIN_PRICE,
            )

            # Also rescan already-seen markets to get updated prices
            all_markets = scanner.rescan(
                max_hours=MAX_HOURS,
                min_volume=MIN_VOLUME,
                min_price=MIN_PRICE,
            )

            print(f"         {len(new_markets)} new | {len(all_markets)} total in window")

            open_count = len(st.get_open(state))
            slots = MAX_OPEN - open_count

            if slots > 0:
                opps = rank_opportunities(
                    all_markets,
                    balance=state["balance"],
                    kelly_fraction=KELLY_FRACTION,
                    max_bet=MAX_BET,
                )

                placed = 0
                for opp in opps:
                    if placed >= slots:
                        break
                    if opp.market.market_id in bet_market_ids:
                        continue
                    if state["balance"] < opp.bet_size:
                        continue

                    print(f"[{_ts()}] OPPORTUNITY:")
                    _print_opportunity(opp, prefix="  ")

                    if live:
                        ok = executor.place_order(opp)
                        if not ok:
                            continue

                    opened = st.open_position(state, opp, live=live)
                    if opened:
                        bet_market_ids.add(opp.market.market_id)
                        placed += 1
                        mode_tag = "[LIVE]" if live else "[PAPER]"
                        print(f"  {mode_tag} Placed bet #{state['opened']}")
            else:
                print(f"         {open_count}/{MAX_OPEN} positions open, no new slots")

            # Print top 5 opportunities even when full
            if cycle == 1 or cycle % 6 == 0:
                all_opps = rank_opportunities(all_markets, balance=state["balance"],
                                              kelly_fraction=KELLY_FRACTION, max_bet=MAX_BET)
                if all_opps:
                    print(f"\n  Top opportunities right now:")
                    for o in all_opps[:5]:
                        _print_opportunity(o, prefix="    ")
                    print()

            last_scan = now

        time.sleep(30)


def cmd_scan() -> None:
    print(f"[{_ts()}] One-shot scan (next {int(MAX_HOURS)}h, no bets placed)...")
    balance = _read_balance()
    markets = list(__import__("agent.discovery", fromlist=["scan_pages"]).scan_pages(
        max_hours=MAX_HOURS, min_volume=MIN_VOLUME, min_price=MIN_PRICE,
    ))
    print(f"  Found {len(markets)} eligible markets\n")

    opps = rank_opportunities(markets, balance=balance, kelly_fraction=KELLY_FRACTION, max_bet=MAX_BET)

    if not opps:
        print("  No actionable opportunities found.")
    else:
        print(f"  Top {min(10, len(opps))} opportunities:")
        for opp in opps[:10]:
            _print_opportunity(opp, prefix="  ")
            print()


def cmd_status() -> None:
    balance = _read_balance()
    state = st.init(balance)
    _print_status(state)


def cmd_report() -> None:
    balance = _read_balance()
    state = st.init(balance)
    _print_report(state)


# ─── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    command = args[0] if args else "status"
    live_flag = "--live" in args

    if command == "run":
        cmd_run(live=live_flag)
    elif command == "scan":
        cmd_scan()
    elif command == "status":
        cmd_status()
    elif command == "report":
        cmd_report()
    else:
        print(__doc__)
