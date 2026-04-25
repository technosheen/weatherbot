#!/usr/bin/env python3
import sys
from pathlib import Path

from crypto.config import load_crypto_config
from crypto.data_sources.polymarket import GammaPolymarketClient, hours_to_expiry
from crypto.data_sources.spot import CoinbaseSpotClient
from crypto.portfolio import (
    choose_best_signals,
    default_crypto_state,
    load_crypto_state,
    mark_open_positions,
    open_paper_positions,
    resolve_positions,
    save_crypto_state,
)
from crypto.selection import filter_near_atm_markets
from crypto.strategies.crypto_threshold import CryptoThresholdStrategy

CRYPTO_STATE_FILE = Path("data/crypto/state.json")


def format_signal_line(signal) -> str:
    tag = "BUY" if signal.should_buy else "SKIP"
    return (
        f"[{tag}] {signal.market.symbol} {signal.market.expiry_label} | "
        f"strike ${signal.market.strike:,.0f} | ask ${signal.market.yes_price:.3f} | "
        f"fair {signal.fair_probability:.1%} | edge {signal.edge:+.1%} | "
        f"EV {signal.expected_value:+.2f} | bet ${signal.bet_size:.2f}"
    )


def discover_and_score_signals(config: dict):
    gamma = GammaPolymarketClient()
    spot = CoinbaseSpotClient()
    strategy = CryptoThresholdStrategy(
        min_edge=config["min_edge"],
        min_price=config["min_price"],
        max_price=config["max_price"],
        min_volume=config["min_volume"],
        max_spread=config["max_spread"],
        min_top_book_size=config["min_top_book_size"],
        kelly_fraction=config["kelly_fraction"],
        max_bet=config["max_bet"],
    )

    markets = gamma.search_threshold_markets(config["queries"])
    spot_cache = {}
    for market in markets:
        if market.symbol not in spot_cache:
            spot_cache[market.symbol] = spot.get_spot_price(market.symbol)

    near_atm_markets = filter_near_atm_markets(
        markets,
        spot_cache,
        max_markets_per_event=config.get("max_markets_per_event", 2),
    )

    signals = []
    for market in sorted(near_atm_markets, key=lambda m: m.volume, reverse=True):
        signals.append(
            strategy.score_market(
                market=market,
                spot_price=spot_cache[market.symbol],
                hours_to_expiry=hours_to_expiry(market.end_date) or config["default_hours_to_expiry"],
                annualized_vol=config["annualized_vol"][market.symbol],
                balance=config["paper_balance"],
            )
        )
    return signals


def apply_lifecycle(state: dict, signals) -> tuple[int, int]:
    marks = {
        signal.market.market_id: (
            signal.market.mark_price if signal.market.mark_price is not None else signal.market.yes_price
        )
        for signal in signals
    }
    marked = mark_open_positions(state, marks)
    resolutions = {
        signal.market.market_id: (
            signal.market.mark_price if signal.market.mark_price is not None else signal.market.yes_price
        )
        for signal in signals
        if (signal.market.mark_price if signal.market.mark_price is not None else signal.market.yes_price) in (0.0, 1.0)
    }
    closed = resolve_positions(state, resolutions)
    return marked, closed


def run_scan() -> int:
    config = load_crypto_config()
    state = load_crypto_state(CRYPTO_STATE_FILE, starting_balance=config["paper_balance"])
    signals = discover_and_score_signals(config)
    if not signals:
        print("No crypto threshold candidates found.")
        return 0

    marked, closed = apply_lifecycle(state, signals)
    for signal in signals:
        print(format_signal_line(signal))

    selected = choose_best_signals(signals)
    opened = open_paper_positions(state, selected)
    save_crypto_state(CRYPTO_STATE_FILE, state)
    print(f"\nMarked {marked}, closed {closed}, opened {opened} crypto paper positions.")
    return 0


def print_status() -> int:
    config = load_crypto_config()
    state = load_crypto_state(CRYPTO_STATE_FILE, starting_balance=config["paper_balance"])
    signals = discover_and_score_signals(config)
    apply_lifecycle(state, signals)
    save_crypto_state(CRYPTO_STATE_FILE, state)

    open_positions = [p for p in state.get("positions", []) if p.get("status") == "open"]
    total_unrealized = sum(float(p.get("unrealized_pnl", 0.0)) for p in open_positions)
    print("\nCRYPTO PAPER BOT — STATUS")
    print("=" * 55)
    print(f"Balance:     ${state.get('balance', config['paper_balance']):,.2f}")
    print(f"Open:        {len(open_positions)}")
    print(f"Opened:      {state.get('opened', 0)}")
    print(f"Closed:      {state.get('closed', 0)}")
    if open_positions:
        print("\nOpen positions:")
        for pos in open_positions:
            print(
                f"  {pos['symbol']} {pos['expiry_label']} | strike ${pos['strike']:,.0f} | "
                f"entry ${pos['entry_price']:.3f} -> ${pos.get('mark_price', pos['entry_price']):.3f} | "
                f"uPnL {pos.get('unrealized_pnl', 0.0):+.2f} | size ${pos['bet_size']:.2f}"
            )
        print(f"\nUnrealized PnL: {total_unrealized:+.2f}")
    print("=" * 55)
    return 0


def print_report() -> int:
    config = load_crypto_config()
    state = load_crypto_state(CRYPTO_STATE_FILE, starting_balance=config["paper_balance"])
    signals = discover_and_score_signals(config)
    apply_lifecycle(state, signals)
    save_crypto_state(CRYPTO_STATE_FILE, state)

    positions = state.get("positions", [])
    resolved = [p for p in positions if p.get("status") == "resolved"]
    print("\nCRYPTO PAPER BOT — REPORT")
    print("=" * 55)
    print(f"Total positions: {len(positions)}")
    print(f"Resolved:       {len(resolved)}")
    total_realized = sum(float(p.get("realized_pnl", 0.0)) for p in resolved)
    print(f"Realized PnL:   {total_realized:+.2f}")
    by_symbol = {}
    for pos in positions:
        by_symbol[pos["symbol"]] = by_symbol.get(pos["symbol"], 0) + 1
    for symbol, count in sorted(by_symbol.items()):
        print(f"  {symbol}: {count}")
    print("=" * 55)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "scan"
    if cmd == "scan":
        return run_scan()
    if cmd == "status":
        return print_status()
    if cmd == "report":
        return print_report()
    print("Usage: python3 crypto_bot.py [scan|status|report]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
