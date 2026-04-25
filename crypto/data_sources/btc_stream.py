"""Fast BTC price polling with rolling history for momentum calculations."""

import time
from collections import deque
from dataclasses import dataclass

import requests


COINBASE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"


@dataclass
class PriceSample:
    price: float
    ts: float  # unix timestamp


class BtcPriceStream:
    """Poll BTC/USD price and maintain a rolling history."""

    def __init__(self, history_seconds: int = 300):
        self._history: deque[PriceSample] = deque()
        self._history_seconds = history_seconds
        self._last_price: float | None = None

    def _fetch(self) -> float:
        try:
            r = requests.get(BINANCE_URL, params={"symbol": "BTCUSDT"}, timeout=(2, 5))
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            pass
        r = requests.get(COINBASE_URL, timeout=(2, 5))
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

    def update(self) -> float:
        """Fetch current price, append to history, prune old samples. Returns current price."""
        price = self._fetch()
        now = time.time()
        self._history.append(PriceSample(price=price, ts=now))
        cutoff = now - self._history_seconds
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()
        self._last_price = price
        return price

    @property
    def current_price(self) -> float | None:
        return self._last_price

    def price_n_seconds_ago(self, seconds: float) -> float | None:
        """Return the oldest price sample within the last `seconds` window, or None."""
        if not self._history:
            return None
        target = time.time() - seconds
        for sample in self._history:
            if sample.ts >= target:
                return sample.price
        return None

    def momentum(self, lookback_seconds: float) -> float | None:
        """Return (current - past) / past as a fraction, or None if insufficient history."""
        current = self._last_price
        past = self.price_n_seconds_ago(lookback_seconds)
        if current is None or past is None or past == 0:
            return None
        return (current - past) / past

    def realized_vol_per_minute(self, window_seconds: float = 300) -> float:
        """Estimate 1-minute realized volatility from recent samples (std of log returns)."""
        import math
        samples = [s for s in self._history if s.ts >= time.time() - window_seconds]
        if len(samples) < 4:
            return 0.0017  # fallback: ~0.17% per minute (BTC historical avg)
        log_returns = []
        for i in range(1, len(samples)):
            if samples[i - 1].price > 0:
                log_returns.append(math.log(samples[i].price / samples[i - 1].price))
        if len(log_returns) < 3:
            return 0.0017
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
        sample_std = math.sqrt(variance)
        avg_dt_minutes = (window_seconds / len(samples)) / 60.0
        return sample_std / math.sqrt(avg_dt_minutes) if avg_dt_minutes > 0 else sample_std

    def has_enough_history(self, min_seconds: float) -> bool:
        if len(self._history) < 2:
            return False
        span = self._history[-1].ts - self._history[0].ts
        return span >= min_seconds
