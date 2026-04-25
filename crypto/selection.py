from collections import defaultdict

from crypto.models import CryptoThresholdMarket


def filter_near_atm_markets(
    markets: list[CryptoThresholdMarket],
    spot_by_symbol: dict[str, float],
    max_markets_per_event: int = 2,
) -> list[CryptoThresholdMarket]:
    grouped: dict[str, list[CryptoThresholdMarket]] = defaultdict(list)
    for market in markets:
        grouped[market.event_id].append(market)

    filtered: list[CryptoThresholdMarket] = []
    for event_id, event_markets in grouped.items():
        if not event_markets:
            continue
        symbol = event_markets[0].symbol
        spot = spot_by_symbol.get(symbol)
        if spot is None:
            filtered.extend(event_markets)
            continue
        ranked = sorted(
            event_markets,
            key=lambda market: (abs(market.strike - spot), market.strike),
        )
        filtered.extend(ranked[:max_markets_per_event])
    return filtered
