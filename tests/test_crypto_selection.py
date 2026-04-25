import unittest

from crypto.models import CryptoThresholdMarket
from crypto.selection import filter_near_atm_markets


class TestCryptoSelection(unittest.TestCase):
    def _market(self, event_id: str, market_id: str, strike: float, symbol: str = "BTC"):
        return CryptoThresholdMarket(
            event_id=event_id,
            market_id=market_id,
            question=f"Will the price of {symbol} be above ${strike:,.0f} on April 25?",
            symbol=symbol,
            direction="above",
            strike=strike,
            expiry_label="April 25",
            yes_price=0.25,
            no_price=0.75,
            volume=10000.0,
            url_slug=f"slug-{market_id}",
            end_date="2026-04-25T23:59:00Z",
            mark_price=0.20,
        )

    def test_keeps_only_closest_strikes_per_event(self):
        markets = [
            self._market("e1", "m1", 70000),
            self._market("e1", "m2", 74000),
            self._market("e1", "m3", 78000),
            self._market("e1", "m4", 82000),
            self._market("e1", "m5", 86000),
        ]
        filtered = filter_near_atm_markets(markets, {"BTC": 79000}, max_markets_per_event=2)
        self.assertEqual({m.market_id for m in filtered}, {"m3", "m4"})

    def test_keeps_events_separate(self):
        markets = [
            self._market("e1", "m1", 78000),
            self._market("e1", "m2", 82000),
            self._market("e2", "m3", 2400, symbol="ETH"),
            self._market("e2", "m4", 2600, symbol="ETH"),
        ]
        filtered = filter_near_atm_markets(markets, {"BTC": 79000, "ETH": 2500}, max_markets_per_event=1)
        self.assertEqual(len(filtered), 2)
        self.assertEqual({m.market_id for m in filtered}, {"m1", "m3"})

    def test_leaves_market_when_symbol_spot_missing(self):
        markets = [self._market("e1", "m1", 78000)]
        filtered = filter_near_atm_markets(markets, {}, max_markets_per_event=1)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].market_id, "m1")


if __name__ == "__main__":
    unittest.main()
