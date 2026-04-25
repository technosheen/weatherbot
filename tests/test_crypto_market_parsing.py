import unittest
from datetime import datetime, timezone

from crypto.data_sources.polymarket import (
    build_threshold_search_queries,
    extract_threshold_markets_from_search_response,
    hours_to_expiry,
)


class TestCryptoMarketParsing(unittest.TestCase):
    def test_parse_bitcoin_above_threshold(self):
        from crypto.data_sources.polymarket import parse_threshold_question
        parsed = parse_threshold_question("Will Bitcoin be above $100,000 on June 30?")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.symbol, "BTC")
        self.assertEqual(parsed.direction, "above")
        self.assertEqual(parsed.strike, 100000.0)
        self.assertEqual(parsed.expiry_label, "June 30")

    def test_parse_bitcoin_price_above_threshold(self):
        from crypto.data_sources.polymarket import parse_threshold_question
        parsed = parse_threshold_question("Will the price of Bitcoin be above $84,000 on April 25?")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.symbol, "BTC")
        self.assertEqual(parsed.direction, "above")
        self.assertEqual(parsed.strike, 84000.0)
        self.assertEqual(parsed.expiry_label, "April 25")

    def test_parse_eth_above_threshold(self):
        from crypto.data_sources.polymarket import parse_threshold_question
        parsed = parse_threshold_question("Will ETH be above $3,500 on May 1?")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.symbol, "ETH")
        self.assertEqual(parsed.strike, 3500.0)
        self.assertEqual(parsed.expiry_label, "May 1")

    def test_reject_non_threshold_market(self):
        from crypto.data_sources.polymarket import parse_threshold_question
        parsed = parse_threshold_question("Will Solana outperform Ethereum this quarter?")
        self.assertIsNone(parsed)

    def test_build_threshold_search_queries(self):
        queries = build_threshold_search_queries(["BTC", "ETH"])
        self.assertIn("bitcoin above", queries)
        self.assertIn("btc above", queries)
        self.assertIn("ethereum above", queries)
        self.assertIn("eth above", queries)
        self.assertEqual(len(queries), len(set(queries)))

    def test_extract_threshold_markets_from_gamma_search(self):
        payload = {
            "events": [
                {
                    "id": "event1",
                    "markets": [
                        {
                            "id": "m1",
                            "question": "Will the price of Bitcoin be above $100,000 on June 30?",
                            "outcomePrices": "[\"0.41\", \"0.59\"]",
                            "slug": "btc-above-100k-june-30",
                            "volume": 12345,
                            "active": True,
                            "closed": False,
                            "endDate": "2026-06-30T23:59:00Z",
                        },
                        {
                            "id": "m2",
                            "question": "Will Solana outperform Ethereum this quarter?",
                            "outcomePrices": "[\"0.50\", \"0.50\"]",
                            "slug": "sol-vs-eth",
                            "volume": 5000,
                            "active": True,
                            "closed": False,
                        },
                    ],
                }
            ]
        }
        markets = extract_threshold_markets_from_search_response(payload)
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].symbol, "BTC")
        self.assertEqual(markets[0].yes_price, 0.41)
        self.assertEqual(markets[0].no_price, 0.59)
        self.assertEqual(markets[0].end_date, "2026-06-30T23:59:00Z")

    def test_extract_skips_closed_and_deduplicates(self):
        payload = {
            "events": [
                {
                    "id": "event1",
                    "markets": [
                        {
                            "id": "m1",
                            "question": "Will the price of Bitcoin be above $100,000 on June 30?",
                            "outcomePrices": "[\"0.41\", \"0.59\"]",
                            "slug": "btc-above-100k-june-30",
                            "volume": 12345,
                            "active": True,
                            "closed": False,
                        },
                        {
                            "id": "m1",
                            "question": "Will the price of Bitcoin be above $100,000 on June 30?",
                            "outcomePrices": "[\"0.41\", \"0.59\"]",
                            "slug": "btc-above-100k-june-30",
                            "volume": 12345,
                            "active": True,
                            "closed": False,
                        },
                        {
                            "id": "m3",
                            "question": "Will the price of ETH be above $3,500 on May 1?",
                            "outcomePrices": "[\"0.35\", \"0.65\"]",
                            "slug": "eth-above-3500-may-1",
                            "volume": 22222,
                            "active": False,
                            "closed": True,
                        },
                    ],
                }
            ]
        }
        markets = extract_threshold_markets_from_search_response(payload)
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].market_id, "m1")

    def test_hours_to_expiry_from_iso_timestamp(self):
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        hours = hours_to_expiry("2026-04-25T00:00:00Z", now=now)
        self.assertAlmostEqual(hours, 12.0, places=2)


if __name__ == "__main__":
    unittest.main()
