"""Discover active 5-minute BTC up/down markets from Polymarket."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests


GAMMA_URL = "https://gamma-api.polymarket.com/public-search"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
SLUG_PATTERN = re.compile(r"^btc-updown-5m-(\d+)$")


@dataclass
class BtcUpDownMarket:
    event_id: str
    market_id: str
    question: str
    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    up_price: float       # current ask for "Up"
    down_price: float     # current ask for "Down"
    up_bid: float         # best bid for "Up"
    down_bid: float       # best bid for "Down"
    window_start: datetime
    window_end: datetime
    volume: float
    accepting_orders: bool

    @property
    def minutes_to_start(self) -> float:
        now = datetime.now(timezone.utc)
        return (self.window_start - now).total_seconds() / 60.0

    @property
    def minutes_to_end(self) -> float:
        now = datetime.now(timezone.utc)
        return (self.window_end - now).total_seconds() / 60.0


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


CLOB_PRICE_URL = "https://clob.polymarket.com/price"
CLOB_MID_URL = "https://clob.polymarket.com/midpoint"


def _fetch_clob_book(token_id: str) -> dict:
    """Fetch bid/ask from CLOB price endpoints (AMM-aware, not raw order book)."""
    try:
        bid_r = requests.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "buy"}, timeout=(3, 8))
        ask_r = requests.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "sell"}, timeout=(3, 8))
        bid_r.raise_for_status()
        ask_r.raise_for_status()
        # side=buy → best bid; side=sell → best ask
        bid = float(bid_r.json().get("price", 0) or 0)
        ask = float(ask_r.json().get("price", 0) or 0)
        return {"bid": bid, "ask": ask}
    except Exception:
        pass
    # fallback: raw order book
    try:
        r = requests.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=(3, 8))
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        return {
            "bid": float(bids[0]["price"]) if bids else 0.0,
            "ask": float(asks[0]["price"]) if asks else 0.0,
        }
    except Exception:
        return {"bid": 0.0, "ask": 0.0}


def _date_search_queries() -> list[str]:
    """Build search queries for today and tomorrow's BTC up/down markets."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    queries = []
    for delta in range(3):
        day = now + timedelta(days=delta)
        month = day.strftime("%B")
        dom = day.day
        queries.append(f"bitcoin up down {month} {dom}")
    return queries


def find_active_markets(lookahead_minutes: float = 60.0) -> list[BtcUpDownMarket]:
    """Return btc-updown-5m markets whose window starts within lookahead_minutes."""
    now = datetime.now(timezone.utc)
    results: list[BtcUpDownMarket] = []
    seen_market_ids: set[str] = set()

    for query in _date_search_queries():
        try:
            r = requests.get(GAMMA_URL, params={"q": query, "limit": 50}, timeout=(5, 15))
            r.raise_for_status()
            payload = r.json()
        except Exception:
            continue

        for event in payload.get("events", []):
            for market in event.get("markets", []):
                slug = market.get("slug", "")
                if not SLUG_PATTERN.match(slug):
                    continue
                market_id = str(market.get("id", ""))
                if market_id in seen_market_ids:
                    continue
                if market.get("closed") or not market.get("acceptingOrders"):
                    continue

                window_start = _parse_iso(market.get("eventStartTime"))
                window_end = _parse_iso(market.get("endDate"))
                if not window_start or not window_end:
                    continue

                minutes_ahead = (window_start - now).total_seconds() / 60.0
                if minutes_ahead < -5 or minutes_ahead > lookahead_minutes:
                    continue

                raw_tokens = market.get("clobTokenIds")
                if not raw_tokens:
                    continue
                try:
                    tokens = json.loads(raw_tokens)
                except Exception:
                    continue
                if len(tokens) < 2:
                    continue

                up_token_id, down_token_id = str(tokens[0]), str(tokens[1])

                try:
                    outcome_prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    gamma_up = float(outcome_prices[0])
                    gamma_down = float(outcome_prices[1]) if len(outcome_prices) > 1 else 1.0 - gamma_up
                except Exception:
                    gamma_up, gamma_down = 0.5, 0.5

                up_book = _fetch_clob_book(up_token_id)
                down_book = _fetch_clob_book(down_token_id)

                up_price = up_book["ask"] if up_book["ask"] > 0 else gamma_up
                down_price = down_book["ask"] if down_book["ask"] > 0 else gamma_down

                seen_market_ids.add(market_id)
                results.append(BtcUpDownMarket(
                    event_id=str(event.get("id", "")),
                    market_id=market_id,
                    question=market.get("question", ""),
                    slug=slug,
                    condition_id=market.get("conditionId", ""),
                    up_token_id=up_token_id,
                    down_token_id=down_token_id,
                    up_price=up_price,
                    down_price=down_price,
                    up_bid=up_book["bid"],
                    down_bid=down_book["bid"],
                    window_start=window_start,
                    window_end=window_end,
                    volume=float(market.get("volume", 0) or 0),
                    accepting_orders=bool(market.get("acceptingOrders")),
                ))

    results.sort(key=lambda m: m.window_start)
    return results
