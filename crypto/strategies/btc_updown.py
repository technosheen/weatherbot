"""Momentum strategy for 5-minute BTC up/down Polymarket markets."""

import math
from dataclasses import dataclass

from crypto.data_sources.btc_updown_markets import BtcUpDownMarket
from crypto.data_sources.btc_stream import BtcPriceStream


TAKER_FEE_RATE = 0.072  # 7.2% of bet size
MIN_HISTORY_SECONDS = 90  # need at least 90s of price history before betting
MOMENTUM_LOOKBACK_SECONDS = 120  # 2-minute lookback for signal


@dataclass
class UpDownSignal:
    market: BtcUpDownMarket
    spot_price: float
    momentum: float            # raw (current - past) / past
    z_score: float             # momentum / expected_vol_for_period
    fair_prob_up: float        # model P(Up wins)
    edge: float                # fair_prob - market_ask (positive = bet up)
    ev_after_fee: float        # expected value after taker fee
    kelly_fraction: float      # raw Kelly allocation
    bet_size: float            # USD to bet (0 = no bet)
    direction: str             # "up", "down", or "none"
    reason: str


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _logistic(x: float, k: float = 1.0) -> float:
    return 1.0 / (1.0 + math.exp(-k * x))


def _ev_after_fee(prob: float, ask_price: float) -> float:
    """EV = prob * (1 - ask - ask*fee) - (1-prob) * ask"""
    if ask_price <= 0 or ask_price >= 1:
        return -1.0
    net_win = 1.0 - ask_price - ask_price * TAKER_FEE_RATE
    return prob * net_win - (1.0 - prob) * ask_price


def _kelly(prob: float, ask_price: float, kelly_fraction: float) -> float:
    if ask_price <= 0 or ask_price >= 1:
        return 0.0
    b = (1.0 - ask_price) / ask_price
    raw = (prob * b - (1.0 - prob)) / b
    return max(0.0, raw) * kelly_fraction


CALIBRATION_PATH = (
    __import__("pathlib").Path(__file__).parent.parent.parent
    / "data" / "crypto" / "btc_updown_calibration.json"
)


def load_calibration() -> dict:
    if CALIBRATION_PATH.exists():
        import json
        return json.loads(CALIBRATION_PATH.read_text())
    return {}


class BtcUpDownStrategy:
    def __init__(
        self,
        min_ev: float = 0.03,
        min_z: float | None = None,          # loaded from calibration if None
        kelly_fraction: float = 0.20,
        max_bet: float = 5.0,
        momentum_alpha: float | None = None,  # loaded from calibration if None
        entry_window_minutes: float = 4.0,
        min_entry_minutes: float = 0.5,
        direction_bias: str | None = None,    # "down_only", "up_only", or None (both)
    ):
        cal = load_calibration()
        self.min_ev = min_ev
        self.min_z = min_z if min_z is not None else cal.get("min_z", 1.2)
        self.momentum_alpha = momentum_alpha if momentum_alpha is not None else cal.get("momentum_alpha", 0.55)
        self.direction_bias = direction_bias if direction_bias is not None else cal.get("direction_bias")
        self.kelly_fraction = kelly_fraction
        self.max_bet = max_bet
        self.entry_window_minutes = entry_window_minutes
        self.min_entry_minutes = min_entry_minutes

    def score(
        self,
        market: BtcUpDownMarket,
        stream: BtcPriceStream,
        balance: float,
    ) -> UpDownSignal:
        spot = stream.current_price
        if spot is None:
            return self._no_signal(market, 0.0, 0.0, "no_price_data")

        minutes_to_start = market.minutes_to_start

        if minutes_to_start > self.entry_window_minutes:
            return self._no_signal(market, spot, 0.0, "too_early")

        if minutes_to_start < self.min_entry_minutes:
            return self._no_signal(market, spot, 0.0, "too_late")

        if not stream.has_enough_history(MIN_HISTORY_SECONDS):
            return self._no_signal(market, spot, 0.0, "insufficient_history")

        raw_momentum = stream.momentum(MOMENTUM_LOOKBACK_SECONDS)
        if raw_momentum is None:
            return self._no_signal(market, spot, 0.0, "no_momentum")

        vol_per_min = stream.realized_vol_per_minute()
        vol_for_period = vol_per_min * math.sqrt(MOMENTUM_LOOKBACK_SECONDS / 60.0) if vol_per_min > 0 else 0.0017
        z_score = raw_momentum / vol_for_period if vol_for_period > 0 else 0.0

        # Convert z-score to directional probability with dampening
        # alpha < 1 reflects that market already partially prices momentum
        scaled_z = z_score * self.momentum_alpha
        fair_prob_up = _logistic(scaled_z, k=1.0)

        # Decide direction (respects calibrated direction_bias)
        if z_score > 0:
            bullish_direction = "up"
        else:
            bullish_direction = "down"

        if self.direction_bias == "down_only" and bullish_direction == "up":
            return self._no_signal(market, spot, z_score, "direction_filtered", raw_momentum, z_score, fair_prob_up)
        if self.direction_bias == "up_only" and bullish_direction == "down":
            return self._no_signal(market, spot, z_score, "direction_filtered", raw_momentum, z_score, fair_prob_up)

        direction = bullish_direction
        if direction == "up":
            ask_price = market.up_price
            edge = fair_prob_up - ask_price
            ev = _ev_after_fee(fair_prob_up, ask_price)
        else:
            fair_prob_down = 1.0 - fair_prob_up
            ask_price = market.down_price
            edge = fair_prob_down - ask_price
            ev = _ev_after_fee(fair_prob_down, ask_price)

        if abs(z_score) < self.min_z:
            return self._no_signal(market, spot, z_score, "z_too_small", raw_momentum, z_score, fair_prob_up)

        if ev < self.min_ev:
            reason = "ev_too_low"
            return UpDownSignal(
                market=market,
                spot_price=spot,
                momentum=raw_momentum,
                z_score=z_score,
                fair_prob_up=round(fair_prob_up, 4),
                edge=round(edge, 4),
                ev_after_fee=round(ev, 4),
                kelly_fraction=0.0,
                bet_size=0.0,
                direction="none",
                reason=reason,
            )

        prob = fair_prob_up if direction == "up" else 1.0 - fair_prob_up
        kelly = _kelly(prob, ask_price, self.kelly_fraction)
        bet_size = round(min(balance * kelly, self.max_bet), 2)

        if bet_size < 5.0:
            bet_size = 0.0
            reason = "bet_below_minimum"
        else:
            reason = direction

        return UpDownSignal(
            market=market,
            spot_price=spot,
            momentum=raw_momentum,
            z_score=round(z_score, 3),
            fair_prob_up=round(fair_prob_up, 4),
            edge=round(edge, 4),
            ev_after_fee=round(ev, 4),
            kelly_fraction=round(kelly, 4),
            bet_size=bet_size,
            direction=direction if bet_size > 0 else "none",
            reason=reason,
        )

    def _no_signal(
        self,
        market: BtcUpDownMarket,
        spot: float,
        z_score: float,
        reason: str,
        momentum: float = 0.0,
        z: float = 0.0,
        fair_up: float = 0.5,
    ) -> UpDownSignal:
        return UpDownSignal(
            market=market,
            spot_price=spot,
            momentum=momentum,
            z_score=z,
            fair_prob_up=fair_up,
            edge=0.0,
            ev_after_fee=0.0,
            kelly_fraction=0.0,
            bet_size=0.0,
            direction="none",
            reason=reason,
        )
