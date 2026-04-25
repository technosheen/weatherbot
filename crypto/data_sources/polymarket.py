import json
import re
from datetime import datetime, timezone
from typing import Iterable

import requests

from crypto.models import CryptoThresholdMarket, ParsedThresholdQuestion


SYMBOL_ALIASES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
}


class GammaPolymarketClient:
    BASE_URL = "https://gamma-api.polymarket.com/public-search"

    def __init__(self, symbols: Iterable[str] | None = None, base_queries: Iterable[str] | None = None):
        self.symbols = list(symbols or ["BTC", "ETH"])
        self.base_queries = list(base_queries) if base_queries is not None else build_threshold_search_queries(self.symbols)

    def search_threshold_markets(self, queries: Iterable[str] | None = None) -> list[CryptoThresholdMarket]:
        query_list = list(queries) if queries is not None else self.base_queries
        raw_payloads: list[dict] = []
        token_ids: list[str] = []
        for query in query_list:
            response = requests.get(self.BASE_URL, params={"q": query}, timeout=(3, 8))
            response.raise_for_status()
            payload = response.json()
            raw_payloads.append(payload)
            token_ids.extend(extract_yes_token_ids(payload))

        clob_prices = fetch_clob_prices(token_ids)
        seen: set[str] = set()
        results: list[CryptoThresholdMarket] = []
        for payload in raw_payloads:
            for market in extract_threshold_markets_from_search_response(payload, clob_prices=clob_prices):
                if market.market_id in seen:
                    continue
                seen.add(market.market_id)
                results.append(market)
        return results


def _normalize_symbol(asset_text: str) -> str | None:
    lowered = asset_text.strip().lower()
    for symbol, aliases in SYMBOL_ALIASES.items():
        if lowered in aliases:
            return symbol
    return None


def build_threshold_search_queries(symbols: Iterable[str]) -> list[str]:
    queries: list[str] = []
    for symbol in symbols:
        aliases = SYMBOL_ALIASES.get(symbol.upper(), [symbol.lower()])
        for alias in aliases:
            queries.extend([
                f"{alias} above",
                f"will {alias} be above",
                f"{alias} below",
            ])
    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        deduped.append(query)
    return deduped


def parse_threshold_question(question: str) -> ParsedThresholdQuestion | None:
    match = re.search(
        r"Will\s+(?:the\s+price\s+of\s+)?(Bitcoin|BTC|Ethereum|ETH)\s+be\s+(above|below)\s+\$?([\d,]+(?:\.\d+)?)\s+on\s+(.+?)\?*$",
        question,
        re.IGNORECASE,
    )
    if not match:
        return None
    symbol = _normalize_symbol(match.group(1))
    if not symbol:
        return None
    return ParsedThresholdQuestion(
        symbol=symbol,
        direction=match.group(2).lower(),
        strike=float(match.group(3).replace(",", "")),
        expiry_label=match.group(4).strip(),
    )


def hours_to_expiry(end_date: str | None, now: datetime | None = None) -> float | None:
    if not end_date:
        return None
    now = now or datetime.now(timezone.utc)
    normalized = end_date.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - now).total_seconds() / 3600.0


def extract_yes_token_ids(payload: dict) -> list[str]:
    token_ids: list[str] = []
    for event in payload.get("events", []):
        for market in event.get("markets", []):
            raw = market.get("clobTokenIds")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if parsed:
                token_ids.append(str(parsed[0]))
    return token_ids


def fetch_clob_prices(token_ids: Iterable[str]) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for token_id in token_ids:
        if token_id in seen:
            continue
        seen.add(token_id)
        try:
            response = requests.get("https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=(3, 8))
            response.raise_for_status()
            payload = response.json()
            bids = payload.get("bids", [])
            asks = payload.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            best_bid_size = float(bids[0]["size"]) if bids else 0.0
            best_ask_size = float(asks[0]["size"]) if asks else 0.0
            results[token_id] = {
                "bid": best_bid if best_bid is not None else 0.0,
                "ask": best_ask if best_ask is not None else 0.0,
                "bid_size": best_bid_size,
                "ask_size": best_ask_size,
            }
        except Exception:
            continue
    return results


def extract_threshold_markets_from_search_response(payload: dict, clob_prices: dict[str, dict[str, float]] | None = None) -> list[CryptoThresholdMarket]:
    markets: list[CryptoThresholdMarket] = []
    seen: set[str] = set()
    clob_prices = clob_prices or {}
    for event in payload.get("events", []):
        event_id = str(event.get("id", ""))
        for market in event.get("markets", []):
            market_id = str(market.get("id", ""))
            if market_id in seen:
                continue
            if market.get("closed") or market.get("active") is False:
                continue
            parsed = parse_threshold_question(market.get("question", ""))
            if not parsed:
                continue
            yes_token_id = None
            raw_tokens = market.get("clobTokenIds")
            if raw_tokens:
                try:
                    parsed_tokens = json.loads(raw_tokens)
                    if parsed_tokens:
                        yes_token_id = str(parsed_tokens[0])
                except Exception:
                    yes_token_id = None
            try:
                outcome_prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
                gamma_yes = float(outcome_prices[0])
                no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else round(1.0 - gamma_yes, 4)
            except Exception:
                continue
            bid = clob_prices.get(yes_token_id, {}).get("bid") if yes_token_id else None
            ask = clob_prices.get(yes_token_id, {}).get("ask") if yes_token_id else None
            bid_size = clob_prices.get(yes_token_id, {}).get("bid_size") if yes_token_id else None
            ask_size = clob_prices.get(yes_token_id, {}).get("ask_size") if yes_token_id else None
            yes_price = ask if ask and ask > 0 else gamma_yes
            mark_price = bid if bid and bid > 0 else gamma_yes
            seen.add(market_id)
            markets.append(
                CryptoThresholdMarket(
                    event_id=event_id,
                    market_id=market_id,
                    question=market.get("question", ""),
                    symbol=parsed.symbol,
                    direction=parsed.direction,
                    strike=parsed.strike,
                    expiry_label=parsed.expiry_label,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=float(market.get("volume", 0) or 0),
                    url_slug=market.get("slug", ""),
                    end_date=market.get("endDate") or event.get("endDate"),
                    yes_token_id=yes_token_id,
                    mark_price=mark_price,
                    bid_size=bid_size,
                    ask_size=ask_size,
                )
            )
    return markets
