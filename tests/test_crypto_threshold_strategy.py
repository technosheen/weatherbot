import unittest

from crypto.models import CryptoThresholdMarket
from crypto.strategies.crypto_threshold import (
    CryptoThresholdStrategy,
    estimate_probability_above,
)


class TestCryptoThresholdStrategy(unittest.TestCase):
    def test_probability_rises_with_spot(self):
        low = estimate_probability_above(spot=90000, strike=100000, hours_to_expiry=24 * 30, annualized_vol=0.6)
        high = estimate_probability_above(spot=110000, strike=100000, hours_to_expiry=24 * 30, annualized_vol=0.6)
        self.assertLess(low, high)

    def test_probability_falls_with_higher_strike(self):
        lower_strike = estimate_probability_above(spot=100000, strike=105000, hours_to_expiry=24 * 14, annualized_vol=0.6)
        higher_strike = estimate_probability_above(spot=100000, strike=115000, hours_to_expiry=24 * 14, annualized_vol=0.6)
        self.assertGreater(lower_strike, higher_strike)

    def test_signal_is_buy_only_when_ev_positive(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $100,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=100000.0,
            expiry_label="June 30",
            yes_price=0.40,
            no_price=0.60,
            volume=100000.0,
            url_slug="btc-above-100k-june-30",
            end_date="2026-06-30T23:59:00Z",
        )
        signal = strategy.score_market(market=market, spot_price=112000.0, hours_to_expiry=24 * 30, annualized_vol=0.5, balance=100.0)
        self.assertTrue(signal.should_buy)
        self.assertGreater(signal.expected_value, 0)
        self.assertGreater(signal.bet_size, 0)

    def test_signal_is_skip_when_ev_non_positive(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $130,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=130000.0,
            expiry_label="June 30",
            yes_price=0.55,
            no_price=0.45,
            volume=100000.0,
            url_slug="btc-above-130k-june-30",
            end_date="2026-06-30T23:59:00Z",
        )
        signal = strategy.score_market(market=market, spot_price=100000.0, hours_to_expiry=24 * 10, annualized_vol=0.4, balance=100.0)
        self.assertFalse(signal.should_buy)
        self.assertLessEqual(signal.expected_value, 0)
        self.assertEqual(signal.bet_size, 0.0)

    def test_format_signal_line_contains_buy_marker(self):
        from crypto_bot import format_signal_line
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $100,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=100000.0,
            expiry_label="June 30",
            yes_price=0.40,
            no_price=0.60,
            volume=100000.0,
            url_slug="btc-above-100k-june-30",
            end_date="2026-06-30T23:59:00Z",
        )
        signal = strategy.score_market(market=market, spot_price=112000.0, hours_to_expiry=24 * 30, annualized_vol=0.5, balance=100.0)
        line = format_signal_line(signal)
        self.assertIn("[BUY]", line)
        self.assertIn("BTC", line)

    def test_skip_for_tiny_price_tail_market(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25, min_price=0.02)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $120,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=120000.0,
            expiry_label="June 30",
            yes_price=0.001,
            no_price=0.999,
            volume=100000.0,
            url_slug="btc-above-120k-june-30",
            end_date="2026-06-30T23:59:00Z",
        )
        signal = strategy.score_market(market=market, spot_price=115000.0, hours_to_expiry=24 * 7, annualized_vol=0.5, balance=100.0)
        self.assertFalse(signal.should_buy)
        self.assertEqual(signal.reason, "price_too_low")

    def test_skip_for_low_liquidity_market(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25, min_volume=5000.0)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $100,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=100000.0,
            expiry_label="June 30",
            yes_price=0.40,
            no_price=0.60,
            volume=200.0,
            url_slug="btc-above-100k-june-30",
            end_date="2026-06-30T23:59:00Z",
        )
        signal = strategy.score_market(market=market, spot_price=112000.0, hours_to_expiry=24 * 30, annualized_vol=0.5, balance=100.0)
        self.assertFalse(signal.should_buy)
        self.assertEqual(signal.reason, "volume_too_low")

    def test_skip_for_excessive_spread(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25, max_spread=0.10)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $100,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=100000.0,
            expiry_label="June 30",
            yes_price=0.40,
            no_price=0.60,
            volume=100000.0,
            url_slug="btc-above-100k-june-30",
            end_date="2026-06-30T23:59:00Z",
            mark_price=0.20,
        )
        signal = strategy.score_market(market=market, spot_price=112000.0, hours_to_expiry=24 * 30, annualized_vol=0.5, balance=100.0)
        self.assertFalse(signal.should_buy)
        self.assertEqual(signal.reason, "spread_too_wide")

    def test_skip_for_thin_best_ask_size(self):
        strategy = CryptoThresholdStrategy(min_edge=0.05, max_price=0.95, kelly_fraction=0.25, min_top_book_size=100.0)
        market = CryptoThresholdMarket(
            event_id="e1",
            market_id="m1",
            question="Will Bitcoin be above $100,000 on June 30?",
            symbol="BTC",
            direction="above",
            strike=100000.0,
            expiry_label="June 30",
            yes_price=0.20,
            no_price=0.80,
            volume=100000.0,
            url_slug="btc-above-100k-june-30",
            end_date="2026-06-30T23:59:00Z",
            mark_price=0.18,
            ask_size=10.0,
            bid_size=500.0,
        )
        signal = strategy.score_market(market=market, spot_price=120000.0, hours_to_expiry=24 * 30, annualized_vol=0.5, balance=100.0)
        self.assertFalse(signal.should_buy)
        self.assertEqual(signal.reason, "top_book_too_thin")


if __name__ == "__main__":
    unittest.main()
