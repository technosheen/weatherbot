import json
from pathlib import Path

from crypto.models import CryptoTradeSignal


def default_crypto_state(starting_balance: float) -> dict:
    return {
        "starting_balance": starting_balance,
        "balance": starting_balance,
        "positions": [],
        "opened": 0,
        "closed": 0,
    }


def load_crypto_state(path: Path, starting_balance: float) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default_crypto_state(starting_balance)


def save_crypto_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def choose_best_signals(signals: list[CryptoTradeSignal]) -> list[CryptoTradeSignal]:
    buys = [signal for signal in signals if signal.should_buy]
    best_by_event: dict[str, CryptoTradeSignal] = {}
    for signal in buys:
        current = best_by_event.get(signal.market.event_id)
        if current is None or (signal.expected_value, signal.edge) > (current.expected_value, current.edge):
            best_by_event[signal.market.event_id] = signal
    return list(best_by_event.values())


def open_paper_positions(state: dict, selected_signals: list[CryptoTradeSignal]) -> int:
    existing_market_ids = {position["market_id"] for position in state.get("positions", [])}
    opened = 0
    for signal in selected_signals:
        if signal.market.market_id in existing_market_ids or signal.bet_size <= 0:
            continue
        shares = round(signal.bet_size / signal.market.yes_price, 8) if signal.market.yes_price > 0 else 0.0
        state.setdefault("positions", []).append(
            {
                "event_id": signal.market.event_id,
                "market_id": signal.market.market_id,
                "symbol": signal.market.symbol,
                "expiry_label": signal.market.expiry_label,
                "strike": signal.market.strike,
                "entry_price": signal.market.yes_price,
                "bet_size": signal.bet_size,
                "cost": signal.bet_size,
                "shares": shares,
                "mark_price": signal.market.yes_price,
                "unrealized_pnl": 0.0,
                "expected_value": signal.expected_value,
                "edge": signal.edge,
                "status": "open",
            }
        )
        state["opened"] = state.get("opened", 0) + 1
        opened += 1
    return opened


def mark_open_positions(state: dict, marks: dict[str, float]) -> int:
    marked = 0
    for position in state.get("positions", []):
        if position.get("status") != "open":
            continue
        market_id = position["market_id"]
        if market_id not in marks:
            continue
        mark_price = float(marks[market_id])
        position["mark_price"] = mark_price
        shares = float(position.get("shares", 0.0))
        entry = float(position.get("entry_price", 0.0))
        position["unrealized_pnl"] = round((mark_price - entry) * shares, 8)
        marked += 1
    return marked


def resolve_positions(state: dict, resolutions: dict[str, float]) -> int:
    closed = 0
    for position in state.get("positions", []):
        if position.get("status") != "open":
            continue
        market_id = position["market_id"]
        if market_id not in resolutions:
            continue
        exit_price = float(resolutions[market_id])
        shares = float(position.get("shares", 0.0))
        entry = float(position.get("entry_price", 0.0))
        realized = round((exit_price - entry) * shares, 8)
        position["status"] = "resolved"
        position["exit_price"] = exit_price
        position["realized_pnl"] = realized
        position["unrealized_pnl"] = 0.0
        position["mark_price"] = exit_price
        state["balance"] = round(float(state.get("balance", 0.0)) + realized, 8)
        state["closed"] = state.get("closed", 0) + 1
        closed += 1
    return closed
