import json
import tempfile
import unittest
from pathlib import Path

from crypto.models import CryptoThresholdMarket, CryptoTradeSignal
from crypto.portfolio import (
    choose_best_signals,
    default_crypto_state,
    load_crypto_state,
    open_paper_positions,
    save_crypto_state,
)


class TestCryptoPortfolio(unittest.TestCase):
    def _signal(self, event_id: str, market_id: str, edge: float, ev: float, should_buy: bool = True):
        market = CryptoThresholdMarket(
            event_id=event_id,
            market_id=market_id,
            question=f"Will the price of Bitcoin be above $80,000 on April 25? {market_id}",
            symbol="BTC",
            direction="above",
            strike=80000.0,
            expiry_label="April 25",
            yes_price=0.10,
            no_price=0.90,
            volume=10000.0,
            url_slug=f"slug-{market_id}",
            end_date="2026-04-25T23:59:00Z",
        )
        return CryptoTradeSignal(
            market=market,
            fair_probability=0.50,
            edge=edge,
            expected_value=ev,
            kelly_fraction=0.10,
            bet_size=1.0 if should_buy else 0.0,
            should_buy=should_buy,
            reason="buy" if should_buy else "skip",
        )

    def test_choose_best_signals_keeps_one_buy_per_event(self):
        signals = [
            self._signal("e1", "m1", edge=0.12, ev=0.7),
            self._signal("e1", "m2", edge=0.22, ev=1.1),
            self._signal("e2", "m3", edge=0.18, ev=0.9),
        ]
        chosen = choose_best_signals(signals)
        self.assertEqual(len(chosen), 2)
        self.assertEqual({s.market.market_id for s in chosen}, {"m2", "m3"})

    def test_choose_best_signals_drops_non_buy_signals(self):
        signals = [
            self._signal("e1", "m1", edge=0.12, ev=0.7, should_buy=False),
            self._signal("e2", "m2", edge=0.18, ev=0.9, should_buy=True),
        ]
        chosen = choose_best_signals(signals)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].market.market_id, "m2")

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crypto_state.json"
            state = default_crypto_state(100.0)
            state["positions"] = [{"market_id": "m1", "status": "open"}]
            save_crypto_state(path, state)
            loaded = load_crypto_state(path, starting_balance=100.0)
            self.assertEqual(loaded["starting_balance"], 100.0)
            self.assertEqual(len(loaded["positions"]), 1)
            self.assertEqual(loaded["positions"][0]["market_id"], "m1")

    def test_open_paper_positions_adds_selected_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crypto_state.json"
            state = default_crypto_state(50.0)
            signals = [
                self._signal("e1", "m1", edge=0.22, ev=1.1),
                self._signal("e1", "m2", edge=0.18, ev=0.9),
                self._signal("e2", "m3", edge=0.19, ev=0.8),
            ]
            opened = open_paper_positions(state, choose_best_signals(signals))
            save_crypto_state(path, state)
            loaded = load_crypto_state(path, starting_balance=50.0)
            self.assertEqual(opened, 2)
            self.assertEqual(len(loaded["positions"]), 2)
            self.assertEqual({p["market_id"] for p in loaded["positions"]}, {"m1", "m3"})


if __name__ == "__main__":
    unittest.main()
