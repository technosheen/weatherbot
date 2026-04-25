import unittest
from unittest.mock import Mock, patch

from crypto.data_sources.polymarket import GammaPolymarketClient


class TestCryptoDiscovery(unittest.TestCase):
    @patch("crypto.data_sources.polymarket.requests.get")
    def test_search_threshold_markets_uses_generated_queries_and_deduplicates(self, mock_get):
        response_1 = Mock()
        response_1.raise_for_status.return_value = None
        response_1.json.return_value = {
            "events": [{
                "id": "e1",
                "markets": [{
                    "id": "m1",
                    "question": "Will Bitcoin be above $100,000 on June 30?",
                    "outcomePrices": "[\"0.41\", \"0.59\"]",
                    "slug": "btc-above-100k-june-30",
                    "volume": 12345,
                    "active": True,
                    "closed": False,
                }],
            }]
        }
        response_2 = Mock()
        response_2.raise_for_status.return_value = None
        response_2.json.return_value = {
            "events": [{
                "id": "e2",
                "markets": [{
                    "id": "m1",
                    "question": "Will Bitcoin be above $100,000 on June 30?",
                    "outcomePrices": "[\"0.41\", \"0.59\"]",
                    "slug": "btc-above-100k-june-30",
                    "volume": 12345,
                    "active": True,
                    "closed": False,
                }, {
                    "id": "m2",
                    "question": "Will ETH be above $3,500 on May 1?",
                    "outcomePrices": "[\"0.35\", \"0.65\"]",
                    "slug": "eth-above-3500-may-1",
                    "volume": 9999,
                    "active": True,
                    "closed": False,
                }],
            }]
        }
        mock_get.side_effect = [response_1, response_2]

        client = GammaPolymarketClient(base_queries=["bitcoin above", "ethereum above"])
        markets = client.search_threshold_markets()

        self.assertEqual(len(markets), 2)
        self.assertEqual({m.market_id for m in markets}, {"m1", "m2"})
        self.assertEqual(mock_get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
