#!/usr/bin/env python3
"""5-minute BTC up/down Polymarket bot.

Usage:
  python btc_updown_bot.py run            -- paper mode (default)
  python btc_updown_bot.py run --live     -- live CLOB order execution
  python btc_updown_bot.py status         -- show open positions + P&L
  python btc_updown_bot.py scan           -- one-shot: find markets + score signals
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from crypto.data_sources.btc_stream import BtcPriceStream
from crypto.data_sources.btc_updown_markets import BtcUpDownMarket, find_active_markets
from crypto.strategies.btc_updown import BtcUpDownStrategy, UpDownSignal, TAKER_FEE_RATE

STATE_PATH = Path(__file__).parent / "data" / "crypto" / "btc_updown_state.json"
POLL_INTERVAL = 10          # seconds between BTC price updates
MARKET_SCAN_INTERVAL = 120  # seconds between Gamma API market scans
LOOKAHEAD_MINUTES = 30      # find markets opening within this window


# ─── state management ────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"balance": _read_balance(), "starting_balance": _read_balance(), "positions": [], "opened": 0, "closed": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _read_balance() -> float:
    try:
        cfg = json.loads(Path(__file__).parent.joinpath("config.json").read_text())
        return float(cfg.get("balance", 20.0))
    except Exception:
        return 20.0


# ─── CLOB execution ──────────────────────────────────────────────────────────

def _place_live_order(signal: UpDownSignal) -> bool:
    """Submit a market buy order via Polymarket CLOB. Returns True on success."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType

        pk = os.environ.get("PK", "")
        if not pk:
            print("  [ERROR] PK not set in .env")
            return False

        client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)
        client.set_api_creds(client.create_or_derive_api_creds())

        token_id = signal.market.up_token_id if signal.direction == "up" else signal.market.down_token_id
        price = signal.market.up_price if signal.direction == "up" else signal.market.down_price
        # round to tick size 0.01
        price = round(round(price / 0.01) * 0.01, 2)
        size = signal.bet_size

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        resp = client.create_and_post_order(order_args)
        if resp and resp.get("success"):
            print(f"  [LIVE] Order placed: {signal.direction.upper()} ${size:.2f} @ {price:.3f}")
            print(f"         order_id={resp.get('orderID', 'unknown')}")
            return True
        else:
            print(f"  [LIVE] Order failed: {resp}")
            return False
    except Exception as exc:
        print(f"  [LIVE] Exception placing order: {exc}")
        return False


# ─── position tracking ───────────────────────────────────────────────────────

def _open_position(state: dict, signal: UpDownSignal, live: bool) -> None:
    market = signal.market
    already = {p["market_id"] for p in state["positions"]}
    if market.market_id in already:
        return

    if live:
        ok = _place_live_order(signal)
        if not ok:
            return

    ask = market.up_price if signal.direction == "up" else market.down_price
    token_id = market.up_token_id if signal.direction == "up" else market.down_token_id
    shares = round(signal.bet_size / ask, 4) if ask > 0 else 0.0

    state["positions"].append({
        "market_id": market.market_id,
        "event_id": market.event_id,
        "question": market.question,
        "direction": signal.direction,
        "token_id": token_id,
        "entry_price": ask,
        "bet_size": signal.bet_size,
        "shares": shares,
        "mark_price": ask,
        "unrealized_pnl": 0.0,
        "spot_at_entry": signal.spot_price,
        "z_score": signal.z_score,
        "fair_prob_up": signal.fair_prob_up,
        "ev_after_fee": signal.ev_after_fee,
        "window_start": market.window_start.isoformat(),
        "window_end": market.window_end.isoformat(),
        "status": "open",
        "live": live,
    })
    state["opened"] = state.get("opened", 0) + 1
    state["balance"] = round(state["balance"] - signal.bet_size, 4)
    _save_state(state)

    mode_tag = "[LIVE]" if live else "[PAPER]"
    print(f"  {mode_tag} Opened {signal.direction.upper()} | {market.question}")
    print(f"         size=${signal.bet_size:.2f}  ask={ask:.3f}  z={signal.z_score:+.2f}  ev={signal.ev_after_fee:.3f}")


def _resolve_positions(state: dict, markets_by_id: dict[str, BtcUpDownMarket]) -> int:
    """Resolve positions for markets that have closed (price = 0 or 1)."""
    now = datetime.now(timezone.utc)
    resolved = 0
    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        window_end_str = pos.get("window_end", "")
        if not window_end_str:
            continue
        window_end = datetime.fromisoformat(window_end_str.replace("Z", "+00:00"))
        if now < window_end:
            continue

        # Market has expired — fetch resolution price from CLOB
        token_id = pos.get("token_id", "")
        if not token_id:
            continue
        try:
            import requests
            r = requests.get("https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=(3, 8))
            data = r.json()
            # resolved markets return outcomePrices near 0 or 1
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            # if no bids and no asks, market may be resolved
            if not bids and not asks:
                # try gamma API for resolution price
                exit_price = _fetch_resolution_price(pos["market_id"])
            else:
                best_bid = float(bids[0]["price"]) if bids else 0.0
                if best_bid >= 0.95:
                    exit_price = 1.0
                elif best_bid <= 0.05:
                    exit_price = 0.0
                else:
                    continue  # not resolved yet
        except Exception:
            continue

        entry = pos["entry_price"]
        shares = pos["shares"]
        fee = entry * shares * TAKER_FEE_RATE
        realized = round((exit_price - entry) * shares - fee, 4)
        pos["status"] = "resolved"
        pos["exit_price"] = exit_price
        pos["realized_pnl"] = realized
        pos["unrealized_pnl"] = 0.0
        state["balance"] = round(state["balance"] + exit_price * shares, 4)
        state["closed"] = state.get("closed", 0) + 1
        outcome = "WIN" if exit_price >= 0.95 else "LOSS"
        print(f"  [RESOLVED] {outcome} {pos['direction'].upper()} | {pos['question']}")
        print(f"             pnl={realized:+.4f}  exit={exit_price}")
        resolved += 1

    if resolved:
        _save_state(state)
    return resolved


def _fetch_resolution_price(market_id: str) -> float:
    try:
        import requests
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 8))
        data = r.json()
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return 0.5


def _mark_positions(state: dict, markets_by_id: dict[str, BtcUpDownMarket]) -> None:
    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        m = markets_by_id.get(pos["market_id"])
        if not m:
            continue
        direction = pos["direction"]
        mark = m.up_bid if direction == "up" else m.down_bid
        if mark <= 0:
            continue
        pos["mark_price"] = mark
        pos["unrealized_pnl"] = round((mark - pos["entry_price"]) * pos["shares"], 4)


# ─── display helpers ─────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _print_status(state: dict) -> None:
    open_positions = [p for p in state["positions"] if p["status"] == "open"]
    closed_positions = [p for p in state["positions"] if p["status"] == "resolved"]
    start = state.get("starting_balance", state["balance"])
    pct = (state["balance"] - start) / start * 100 if start else 0

    print("=" * 60)
    print("  BTC UP/DOWN BOT — STATUS")
    print("=" * 60)
    print(f"  Balance:  ${state['balance']:.2f}  (start ${start:.2f}, {pct:+.1f}%)")
    print(f"  Open: {len(open_positions)}  |  Resolved: {len(closed_positions)}")

    if open_positions:
        print("\n  Open positions:")
        for p in open_positions:
            unr = p.get("unrealized_pnl", 0.0)
            sign = "+" if unr >= 0 else ""
            print(f"    {p['direction'].upper():<4}  entry={p['entry_price']:.3f}  "
                  f"mark={p.get('mark_price', p['entry_price']):.3f}  "
                  f"pnl={sign}{unr:.4f}  |  {p['question'][:55]}")

    if closed_positions:
        total_pnl = sum(p.get("realized_pnl", 0.0) for p in closed_positions)
        wins = sum(1 for p in closed_positions if p.get("realized_pnl", 0.0) > 0)
        print(f"\n  Resolved positions ({wins}/{len(closed_positions)} wins, total pnl={total_pnl:+.4f}):")
        for p in closed_positions:
            pnl = p.get("realized_pnl", 0.0)
            print(f"    {p['direction'].upper():<4}  pnl={pnl:+.4f}  |  {p['question'][:55]}")

    print("=" * 60)


# ─── main run loop ───────────────────────────────────────────────────────────

def cmd_run(live: bool = False) -> None:
    mode = "LIVE" if live else "PAPER"
    print(f"[{_ts()}] BTC up/down bot starting ({mode} mode)")
    if live:
        print("  WARNING: live mode will submit real orders via CLOB")

    state = _load_state()
    stream = BtcPriceStream(history_seconds=300)
    strategy = BtcUpDownStrategy()

    markets: list[BtcUpDownMarket] = []
    last_market_scan = 0.0
    bet_market_ids: set[str] = set()

    print(f"[{_ts()}] Warming up price stream (need {90}s history)...")

    while True:
        now = time.time()

        # Update BTC price
        try:
            price = stream.update()
            print(f"[{_ts()}] BTC=${price:,.2f}", end="")
        except Exception as exc:
            print(f"[{_ts()}] Price fetch error: {exc}")
            time.sleep(POLL_INTERVAL)
            continue

        # Refresh market list periodically
        if now - last_market_scan > MARKET_SCAN_INTERVAL:
            markets = find_active_markets(lookahead_minutes=LOOKAHEAD_MINUTES)
            last_market_scan = now
            if markets:
                print(f"  | {len(markets)} market(s) found")
                for m in markets:
                    print(f"    {m.question[:60]}  start_in={m.minutes_to_start:.1f}min  up={m.up_price:.3f}")
            else:
                print(f"  | no markets in next {LOOKAHEAD_MINUTES}min")
        else:
            print()

        markets_by_id = {m.market_id: m for m in markets}

        # Resolve expired positions
        _resolve_positions(state, markets_by_id)
        _mark_positions(state, markets_by_id)

        # Score and bet
        if stream.has_enough_history(90):
            for market in markets:
                if market.market_id in bet_market_ids:
                    continue
                signal = strategy.score(market, stream, state["balance"])
                if signal.bet_size > 0:
                    print(f"[{_ts()}] SIGNAL {signal.direction.upper()} | z={signal.z_score:+.2f} "
                          f"ev={signal.ev_after_fee:.3f} size=${signal.bet_size:.2f}")
                    _open_position(state, signal, live=live)
                    bet_market_ids.add(market.market_id)
                elif signal.reason not in ("too_early", "insufficient_history", "no_price_data"):
                    print(f"  skip {market.question[:50]} ({signal.reason}  z={signal.z_score:+.2f})")

        time.sleep(POLL_INTERVAL)


def cmd_scan() -> None:
    print("Fetching active 5-minute BTC up/down markets (next 24h)...")
    markets = find_active_markets(lookahead_minutes=1440)
    if not markets:
        print("  No markets found opening in the next 60 minutes.")
        return

    stream = BtcPriceStream()
    try:
        btc = stream.update()
        print(f"  BTC spot: ${btc:,.2f}\n")
    except Exception:
        btc = None
        print("  (could not fetch BTC price)\n")

    strategy = BtcUpDownStrategy()
    for m in markets:
        print(f"  {m.question}")
        print(f"    window: {m.window_start.strftime('%H:%M')} → {m.window_end.strftime('%H:%M')} UTC")
        print(f"    start_in: {m.minutes_to_start:.1f} min  |  up={m.up_price:.3f}  down={m.down_price:.3f}")
        print(f"    volume: ${m.volume:,.0f}  |  accepting_orders: {m.accepting_orders}")
        if btc:
            signal = strategy.score(m, stream, 20.0)
            print(f"    signal: {signal.reason}  z={signal.z_score:.2f}  ev={signal.ev_after_fee:.3f}")
        print()


def cmd_status() -> None:
    state = _load_state()
    _print_status(state)


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
    else:
        print(__doc__)
