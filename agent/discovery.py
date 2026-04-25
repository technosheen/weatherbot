"""Incremental Polymarket market scanner.

Fetches active markets sorted by soonest expiry. Maintains a seen-set so
repeated scans only surface new markets — callers can append to a running
deque rather than re-processing the whole API each cycle.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

import requests


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
PAGE_SIZE = 50


@dataclass
class PolyMarket:
    event_id: str
    market_id: str
    condition_id: str
    question: str
    description: str
    tags: list[str]
    yes_price: float
    no_price: float
    yes_token_id: str
    no_token_id: str
    volume: float
    liquidity: float
    end_date: datetime
    neg_risk: bool
    accepting_orders: bool
    best_bid: float = 0.0
    best_ask: float = 0.0

    @property
    def hours_to_expiry(self) -> float:
        now = datetime.now(timezone.utc)
        return max(0.0, (self.end_date - now).total_seconds() / 3600.0)

    @property
    def leader_price(self) -> float:
        return max(self.yes_price, self.no_price)

    @property
    def leader(self) -> str:
        return "yes" if self.yes_price >= self.no_price else "no"

    @property
    def leader_token_id(self) -> str:
        return self.yes_token_id if self.leader == "yes" else self.no_token_id

    @property
    def primary_tag(self) -> str:
        return self.tags[0] if self.tags else "other"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _fetch_clob_prices(token_id: str) -> tuple[float, float]:
    """Return (bid, ask) for a token. bid = what you get selling; ask = what you pay buying."""
    try:
        bid_r = requests.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "buy"}, timeout=(2, 6))
        ask_r = requests.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "sell"}, timeout=(2, 6))
        bid = float(bid_r.json().get("price", 0) or 0)
        ask = float(ask_r.json().get("price", 0) or 0)
        return bid, ask
    except Exception:
        return 0.0, 0.0


def _market_from_raw(event: dict, market: dict, fetch_clob: bool = False) -> PolyMarket | None:
    if market.get("closed") or market.get("archived"):
        return None
    if not market.get("acceptingOrders"):
        return None

    end_dt = _parse_dt(market.get("endDate") or event.get("endDate"))
    if not end_dt:
        return None

    raw_tokens = market.get("clobTokenIds", "")
    try:
        tokens = json.loads(raw_tokens) if raw_tokens else []
    except Exception:
        tokens = []
    if len(tokens) < 2:
        return None

    try:
        prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        no_price = float(prices[1]) if len(prices) > 1 else round(1.0 - yes_price, 4)
    except Exception:
        yes_price, no_price = 0.5, 0.5

    yes_tid, no_tid = str(tokens[0]), str(tokens[1])

    best_bid, best_ask = 0.0, 0.0
    if fetch_clob:
        leader_tid = yes_tid if yes_price >= no_price else no_tid
        best_bid, best_ask = _fetch_clob_prices(leader_tid)

    tags = [t.get("slug", "") for t in event.get("tags", [])]

    return PolyMarket(
        event_id=str(event.get("id", "")),
        market_id=str(market.get("id", "")),
        condition_id=market.get("conditionId", ""),
        question=market.get("question") or event.get("title") or "",
        description=(market.get("description") or event.get("description") or "")[:500],
        tags=tags,
        yes_price=yes_price,
        no_price=no_price,
        yes_token_id=yes_tid,
        no_token_id=no_tid,
        volume=float(event.get("volume") or market.get("volume") or 0),
        liquidity=float(event.get("liquidity") or market.get("liquidity") or 0),
        end_date=end_dt,
        neg_risk=bool(market.get("negRisk")),
        accepting_orders=bool(market.get("acceptingOrders")),
        best_bid=best_bid,
        best_ask=best_ask,
    )


def scan_pages(
    max_hours: float = 72.0,
    min_volume: float = 500.0,
    min_liquidity: float = 200.0,
    min_price: float = 0.65,
    max_pages: int = 30,
    fetch_clob: bool = False,
) -> Iterator[PolyMarket]:
    """Yield PolyMarket objects with end dates in [now, now+max_hours].

    Sorted by startDate descending (freshest first) so recently-opened
    short-window markets appear early. Pages until end dates exceed the window.
    """
    now = datetime.now(timezone.utc)
    seen_event_ids: set[str] = set()

    for page in range(max_pages):
        try:
            r = requests.get(GAMMA_EVENTS, params={
                "active": "true",
                "closed": "false",
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "order": "startDate",
                "ascending": "false",
            }, timeout=(5, 15))
            r.raise_for_status()
            events = r.json()
        except Exception:
            break

        if not events:
            break

        found_in_window = False
        all_too_old = True

        for event in events:
            eid = str(event.get("id", ""))
            if eid in seen_event_ids:
                continue
            seen_event_ids.add(eid)

            end_dt = _parse_dt(event.get("endDate"))
            if not end_dt:
                continue
            hours = (end_dt - now).total_seconds() / 3600.0

            if hours <= 0:      # already expired, skip
                continue
            if hours > max_hours:
                # too far out, but don't stop — some pages mix timeframes
                all_too_old = False
                continue

            all_too_old = False
            found_in_window = True

            vol = float(event.get("volume") or 0)
            liq = float(event.get("liquidity") or 0)
            if vol < min_volume and liq < min_liquidity:
                continue

            for market in event.get("markets", []):
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    leader = max(float(prices[0]), float(prices[1]) if len(prices) > 1 else 0)
                except Exception:
                    leader = 0.5
                if leader < min_price:
                    continue

                m = _market_from_raw(event, market, fetch_clob=fetch_clob)
                if m:
                    yield m

        if all_too_old:
            break


class IncrementalScanner:
    """Wraps scan_pages and tracks seen market IDs so repeated calls return only new markets."""

    def __init__(self):
        self._seen: set[str] = set()

    def scan(self, **kwargs) -> list[PolyMarket]:
        new_markets = []
        for m in scan_pages(**kwargs):
            if m.market_id not in self._seen:
                self._seen.add(m.market_id)
                new_markets.append(m)
        return new_markets

    def rescan(self, **kwargs) -> list[PolyMarket]:
        """Re-scan all markets (for price refresh), bypassing seen set."""
        return list(scan_pages(**kwargs))
