import math

from crypto.models import CryptoThresholdMarket, CryptoTradeSignal
from crypto.strategies.base import BaseStrategy


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_probability_above(spot: float, strike: float, hours_to_expiry: float, annualized_vol: float) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if hours_to_expiry <= 0:
        return 1.0 if spot > strike else 0.0
    sigma_t = annualized_vol * math.sqrt(hours_to_expiry / (24.0 * 365.0))
    if sigma_t <= 0:
        return 1.0 if spot > strike else 0.0
    z = math.log(strike / spot) / sigma_t
    return max(0.0, min(1.0, 1.0 - norm_cdf(z)))


def calc_ev(probability: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return probability * (1.0 / price - 1.0) - (1.0 - probability)


def calc_kelly(probability: float, price: float, kelly_fraction: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    raw = (probability * b - (1.0 - probability)) / b
    return max(0.0, raw) * kelly_fraction


def market_spread(market: CryptoThresholdMarket) -> float:
    if market.mark_price is None:
        return 0.0
    return max(0.0, market.yes_price - market.mark_price)


class CryptoThresholdStrategy(BaseStrategy):
    name = "crypto-threshold"

    def __init__(
        self,
        min_edge: float = 0.05,
        max_price: float = 0.45,
        kelly_fraction: float = 0.25,
        max_bet: float = 5.0,
        min_price: float = 0.02,
        min_volume: float = 5000.0,
        max_spread: float = 0.10,
        min_top_book_size: float = 50.0,
    ):
        self.min_edge = min_edge
        self.max_price = max_price
        self.kelly_fraction = kelly_fraction
        self.max_bet = max_bet
        self.min_price = min_price
        self.min_volume = min_volume
        self.max_spread = max_spread
        self.min_top_book_size = min_top_book_size

    def score_market(
        self,
        market: CryptoThresholdMarket,
        spot_price: float,
        hours_to_expiry: float,
        annualized_vol: float,
        balance: float,
    ) -> CryptoTradeSignal:
        fair_probability = estimate_probability_above(
            spot=spot_price,
            strike=market.strike,
            hours_to_expiry=hours_to_expiry,
            annualized_vol=annualized_vol,
        )
        spread = market_spread(market)
        edge = fair_probability - market.yes_price
        expected_value = calc_ev(fair_probability, market.yes_price)
        kelly = calc_kelly(fair_probability, market.yes_price, self.kelly_fraction)
        ask_size = market.ask_size if market.ask_size is not None else float("inf")
        should_buy = (
            market.yes_price >= self.min_price
            and market.yes_price <= self.max_price
            and market.volume >= self.min_volume
            and spread <= self.max_spread
            and ask_size >= self.min_top_book_size
            and edge >= self.min_edge
            and expected_value > 0
        )
        bet_size = 0.0 if not should_buy else round(min(balance * kelly, self.max_bet), 2)
        if market.yes_price < self.min_price:
            reason = "price_too_low"
        elif market.yes_price > self.max_price:
            reason = "price_too_high"
        elif market.volume < self.min_volume:
            reason = "volume_too_low"
        elif spread > self.max_spread:
            reason = "spread_too_wide"
        elif ask_size < self.min_top_book_size:
            reason = "top_book_too_thin"
        elif expected_value <= 0:
            reason = "ev_non_positive"
        elif edge < self.min_edge:
            reason = "edge_too_small"
        elif bet_size <= 0:
            reason = "bet_size_zero"
        else:
            reason = "buy"
        return CryptoTradeSignal(
            market=market,
            fair_probability=round(fair_probability, 4),
            edge=round(edge, 4),
            expected_value=round(expected_value, 4),
            kelly_fraction=round(kelly, 4),
            bet_size=bet_size,
            should_buy=should_buy,
            reason=reason,
        )
