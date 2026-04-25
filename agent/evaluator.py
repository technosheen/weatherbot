"""Quick-win opportunity scorer.

Scores each market on its "quick win potential" — a composite of:
  - expected return if correct (1/price - 1)
  - urgency (prefer sooner expiry)
  - calibration factor (are we historically good at this category?)
  - liquidity / volume quality

Markets are scored on the LEADING side (whichever of Yes/No is priced higher).
The assumption is that high-price markets near expiry often represent
near-certain outcomes where the market has already made up its mind.
The edge comes from: (a) the market is efficiently pricing a real 85%+ probability,
and (b) our calibration shows we win at this price/category/time combination
at a rate that produces positive EV after fees.
"""

import math
from dataclasses import dataclass

from agent.discovery import PolyMarket
from agent import calibrator


# Taker fee for regular Polymarket binary markets (~2%)
TAKER_FEE = 0.02

# Minimum thresholds
MIN_HOURS = 0.25        # don't enter < 15 minutes before resolution
MAX_HOURS = 72.0
MIN_PRICE = 0.65
MIN_VOLUME = 500.0
MIN_LIQUIDITY = 200.0
DEFAULT_MIN_SCORE = 0.05


@dataclass
class Opportunity:
    market: PolyMarket
    direction: str          # "yes" or "no"
    entry_price: float      # ask price to enter
    fair_prob: float        # our estimated P(win) after calibration
    expected_return: float  # (1 - price) / price before fees
    ev_after_fee: float
    quick_win_score: float  # composite ranking score
    kelly: float
    bet_size: float
    category: str
    reason: str             # why selected / why skipped


def _categorize(market: PolyMarket) -> str:
    """Map to a broad category from tags."""
    tag_map = {
        "sports": ["sports", "basketball", "football", "soccer", "baseball", "hockey",
                   "tennis", "golf", "mma", "boxing", "nfl", "nba", "mlb", "nhl",
                   "ncaa", "cricket", "rugby", "formula-1", "esports"],
        "politics": ["politics", "elections", "us-politics", "geopolitics", "government",
                     "federal-government", "senate", "house-races", "president"],
        "crypto": ["crypto", "bitcoin", "ethereum", "altcoins", "defi", "nft",
                   "crypto-prices", "up-or-down"],
        "economics": ["economics", "finance", "markets", "stocks", "sp500",
                      "fed", "interest-rates", "gdp"],
        "science": ["science", "space", "ai", "technology", "health", "climate"],
        "entertainment": ["entertainment", "tv", "movies", "music", "awards",
                          "oscars", "grammys", "celebrity"],
    }
    tags = set(t.lower() for t in market.tags)
    for cat, keywords in tag_map.items():
        if tags & set(keywords):
            return cat
    return "other"


def _ev_after_fee(prob: float, price: float, fee: float = TAKER_FEE) -> float:
    if price <= 0 or price >= 1:
        return -1.0
    net_win = 1.0 - price - price * fee
    return prob * net_win - (1.0 - prob) * price


def _kelly(prob: float, price: float, kelly_fraction: float, fee: float = TAKER_FEE) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price - price * fee) / price
    raw = (prob * b - (1.0 - prob)) / b
    return max(0.0, raw) * kelly_fraction


def score_market(
    market: PolyMarket,
    balance: float,
    kelly_fraction: float = 0.25,
    max_bet: float = 5.0,
    min_score: float | None = None,
    base_edge: float = 0.025,
) -> Opportunity:
    category = _categorize(market)
    hours = market.hours_to_expiry

    # Basic eligibility filters
    if hours < MIN_HOURS:
        return _skip(market, category, "too_close_to_expiry")
    if hours > MAX_HOURS:
        return _skip(market, category, "too_far_away")
    if not market.accepting_orders:
        return _skip(market, category, "not_accepting_orders")
    if market.volume < MIN_VOLUME and market.liquidity < MIN_LIQUIDITY:
        return _skip(market, category, "insufficient_liquidity")

    direction = market.leader
    entry_price = market.leader_price

    if entry_price < MIN_PRICE:
        return _skip(market, category, "price_too_low")
    if entry_price >= 0.99:
        return _skip(market, category, "already_resolved")

    # Calibration factor: how well do we perform in this bucket historically?
    cal_factor = calibrator.get_calibration_factor(category, entry_price, hours)

    # Fair probability: market price + base_edge (near-expiry underpricing premium)
    # scaled by calibration factor. base_edge reflects that high-confidence markets
    # near resolution are systematically slightly under-priced by market makers.
    fair_prob = min(0.99, (entry_price + base_edge) * cal_factor)

    # Expected return and EV
    expected_return = (1.0 - entry_price) / entry_price
    ev = _ev_after_fee(fair_prob, entry_price)

    if ev <= 0:
        return _skip(market, category, "ev_negative", fair_prob, entry_price, ev)

    # Quick-win score: EV * urgency factor * log-volume quality
    # Urgency: markets expiring in 1h score 4x higher than 24h
    urgency = math.log(max(1.0, 48.0 / max(0.5, hours))) / math.log(48.0)
    vol_quality = min(1.0, math.log10(max(10, market.volume)) / 5.0)
    quick_win_score = ev * urgency * vol_quality * cal_factor

    threshold = min_score if min_score is not None else calibrator.get_min_score_threshold(category)
    if quick_win_score < threshold:
        return _skip(market, category, f"score_too_low({quick_win_score:.4f}<{threshold:.4f})",
                     fair_prob, entry_price, ev, quick_win_score)

    kelly = _kelly(fair_prob, entry_price, kelly_fraction)
    bet_size = round(min(balance * kelly, max_bet), 2)

    if bet_size < 0.25:
        return _skip(market, category, "bet_size_too_small", fair_prob, entry_price, ev, quick_win_score)

    return Opportunity(
        market=market,
        direction=direction,
        entry_price=entry_price,
        fair_prob=round(fair_prob, 4),
        expected_return=round(expected_return, 4),
        ev_after_fee=round(ev, 4),
        quick_win_score=round(quick_win_score, 4),
        kelly=round(kelly, 4),
        bet_size=bet_size,
        category=category,
        reason="buy",
    )


def rank_opportunities(markets: list[PolyMarket], balance: float, base_edge: float = 0.025, **kwargs) -> list[Opportunity]:
    opps = [score_market(m, balance, base_edge=base_edge, **kwargs) for m in markets]
    buyable = [o for o in opps if o.reason == "buy"]
    buyable.sort(key=lambda o: o.quick_win_score, reverse=True)
    return buyable


def _skip(market, category, reason, fair_prob=0.0, price=0.0, ev=0.0, score=0.0) -> Opportunity:
    return Opportunity(
        market=market,
        direction="none",
        entry_price=price or market.leader_price,
        fair_prob=fair_prob,
        expected_return=(1 - price) / price if 0 < price < 1 else 0.0,
        ev_after_fee=ev,
        quick_win_score=score,
        kelly=0.0,
        bet_size=0.0,
        category=category,
        reason=reason,
    )
