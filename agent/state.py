"""Persistent agent state: open positions, resolved bets, running P&L."""

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.discovery import PolyMarket
from agent.evaluator import Opportunity

STATE_PATH = Path(__file__).parent.parent / "data" / "agent" / "state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "starting_balance": None,
        "balance": None,
        "positions": [],
        "opened": 0,
        "closed": 0,
    }


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def init(starting_balance: float) -> dict:
    state = _load()
    if state["starting_balance"] is None:
        state["starting_balance"] = starting_balance
        state["balance"] = starting_balance
        _save(state)
    return state


def open_position(state: dict, opp: Opportunity, live: bool) -> bool:
    """Record a new bet. Returns False if already tracking this market."""
    existing = {p["market_id"] for p in state["positions"]}
    if opp.market.market_id in existing:
        return False

    token_id = opp.market.yes_token_id if opp.direction == "yes" else opp.market.no_token_id
    shares = round(opp.bet_size / opp.entry_price, 6) if opp.entry_price > 0 else 0.0

    state["positions"].append({
        "market_id": opp.market.market_id,
        "event_id": opp.market.event_id,
        "condition_id": opp.market.condition_id,
        "question": opp.market.question,
        "category": opp.category,
        "direction": opp.direction,
        "token_id": token_id,
        "entry_price": opp.entry_price,
        "fair_prob": opp.fair_prob,
        "bet_size": opp.bet_size,
        "shares": shares,
        "mark_price": opp.entry_price,
        "unrealized_pnl": 0.0,
        "ev_after_fee": opp.ev_after_fee,
        "quick_win_score": opp.quick_win_score,
        "hours_at_entry": round(opp.market.hours_to_expiry, 2),
        "end_date": opp.market.end_date.isoformat(),
        "opened_at": _now_iso(),
        "status": "open",
        "live": live,
    })
    state["balance"] = round(state["balance"] - opp.bet_size, 4)
    state["opened"] = state.get("opened", 0) + 1
    _save(state)
    return True


def mark_positions(state: dict, price_map: dict[str, float]) -> None:
    """Update mark-to-market prices. price_map: market_id → current leader bid."""
    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        mid = pos["market_id"]
        if mid not in price_map:
            continue
        mark = price_map[mid]
        pos["mark_price"] = mark
        pos["unrealized_pnl"] = round((mark - pos["entry_price"]) * pos["shares"], 4)
    _save(state)


def resolve_position(state: dict, market_id: str, exit_price: float) -> dict | None:
    """Resolve an open position. Returns the position dict or None if not found."""
    from agent import calibrator
    for pos in state["positions"]:
        if pos["market_id"] != market_id or pos["status"] != "open":
            continue
        shares = pos["shares"]
        entry = pos["entry_price"]
        fee = entry * shares * 0.02  # 2% taker fee on original cost
        # exit_price = 1.0 if won, 0.0 if lost
        realized = round(exit_price * shares - entry * shares - fee, 4)
        pos["status"] = "resolved"
        pos["exit_price"] = exit_price
        pos["realized_pnl"] = realized
        pos["unrealized_pnl"] = 0.0
        pos["resolved_at"] = _now_iso()
        won = exit_price >= 0.95
        state["balance"] = round(state["balance"] + exit_price * shares, 4)
        state["closed"] = state.get("closed", 0) + 1
        calibrator.record_outcome(
            category=pos["category"],
            entry_price=pos["entry_price"],
            hours_at_entry=pos.get("hours_at_entry", 24.0),
            won=won,
        )
        _save(state)
        return pos
    return None


def check_and_resolve_expired(state: dict) -> list[dict]:
    """Poll Polymarket for resolution prices on expired open positions."""
    import requests
    now = datetime.now(timezone.utc)
    resolved = []

    for pos in state["positions"]:
        if pos["status"] != "open":
            continue
        end_dt = datetime.fromisoformat(pos["end_date"].replace("Z", "+00:00"))
        if now < end_dt:
            continue

        # Fetch current price from Gamma API
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{pos['market_id']}",
                timeout=(3, 8)
            )
            data = r.json()
            prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
            direction = pos["direction"]
            raw_price = float(prices[0] if direction == "yes" else prices[1])
            # Only resolve if clearly settled (near 0 or 1)
            if raw_price >= 0.95:
                exit_price = 1.0
            elif raw_price <= 0.05:
                exit_price = 0.0
            else:
                continue  # still resolving
        except Exception:
            continue

        r = resolve_position(state, pos["market_id"], exit_price)
        if r:
            resolved.append(r)

    return resolved


def get_open(state: dict) -> list[dict]:
    return [p for p in state["positions"] if p["status"] == "open"]


def get_resolved(state: dict) -> list[dict]:
    return [p for p in state["positions"] if p["status"] == "resolved"]
