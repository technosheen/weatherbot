from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedThresholdQuestion:
    symbol: str
    direction: str
    strike: float
    expiry_label: str


@dataclass(frozen=True)
class CryptoThresholdMarket:
    event_id: str
    market_id: str
    question: str
    symbol: str
    direction: str
    strike: float
    expiry_label: str
    yes_price: float
    no_price: float
    volume: float
    url_slug: str
    end_date: str | None = None
    yes_token_id: str | None = None
    mark_price: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None


@dataclass(frozen=True)
class CryptoTradeSignal:
    market: CryptoThresholdMarket
    fair_probability: float
    edge: float
    expected_value: float
    kelly_fraction: float
    bet_size: float
    should_buy: bool
    reason: str
