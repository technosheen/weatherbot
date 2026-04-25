import unittest
from unittest.mock import Mock, patch

from crypto.data_sources.polymarket import (
    extract_threshold_markets_from_search_response,
    fetch_clob_prices,
)


class TestCryptoCLOBPricing(unittest.TestCase):
    def test_extracts_yes_token_id_from_clob_token_ids(self):
        payload = {
            "events": [{
                "id": "event1",
                "markets": [{
                    "id": "m1",
                    "question": "Will the price of Bitcoin be above $100,000 on June 30?",
                    "outcomePrices": "[\"0.41\", \"0.59\"]",
                    "clobTokenIds": "[\"yes-token\", \"no-token\"]",
                    "slug": "btc-above-100k-june-30",
                    "volume": 12345,
                    "active": True,
                    "closed": False,
                    "endDate": "2026-06-30T23:59:00Z",
                }],
            }]
        }
        markets = extract_threshold_markets_from_search_response(payload)
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].yes_token_id, "yes-token")

    @patch("crypto.data_sources.polymarket.requests.get")
    def test_fetch_clob_prices_reads_bid_ask_and_sizes(self, mock_get):
        book_response = Mock()
        book_response.raise_for_status.return_value = None
        book_response.json.return_value = {
            "bids": [{"price": "0.42", "size": "100"}],
            "asks": [{"price": "0.44", "size": "50"}],
        }
        mock_get.return_value = book_response

        prices = fetch_clob_prices(["yes-token"])
        self.assertEqual(prices["yes-token"]["bid"], 0.42)
        self.assertEqual(prices["yes-token"]["ask"], 0.44)
        self.assertEqual(prices["yes-token"]["bid_size"], 100.0)
        self.assertEqual(prices["yes-token"]["ask_size"], 50.0)

    def test_extract_overrides_gamma_yes_price_with_clob_ask_when_available(self):
        payload = {
            "events": [{
                "id": "event1",
                "markets": [{
                    "id": "m1",
                    "question": "Will the price of Bitcoin be above $100,000 on June 30?",
                    "outcomePrices": "[\"0.41\", \"0.59\"]",
                    "clobTokenIds": "[\"yes-token\", \"no-token\"]",
                    "slug": "btc-above-100k-june-30",
                    "volume": 12345,
                    "active": True,
                    "closed": False,
                    "endDate": "2026-06-30T23:59:00Z",
                }],
            }]
        }
        markets = extract_threshold_markets_from_search_response(payload, clob_prices={"yes-token": {"bid": 0.40, "ask": 0.43, "bid_size": 120.0, "ask_size": 80.0}})
        self.assertEqual(markets[0].yes_price, 0.43)
        self.assertEqual(markets[0].mark_price, 0.40)
        self.assertEqual(markets[0].ask_size, 80.0)
        self.assertEqual(markets[0].bid_size, 120.0)


if __name__ == "__main__":
    unittest.main()
