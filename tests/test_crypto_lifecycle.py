import tempfile
import unittest
from pathlib import Path

from crypto.models import CryptoThresholdMarket, CryptoTradeSignal
from crypto.portfolio import (
    default_crypto_state,
    load_crypto_state,
    mark_open_positions,
    open_paper_positions,
    resolve_positions,
    save_crypto_state,
)


class TestCryptoLifecycle(unittest.TestCase):
    def _signal(self, event_id: str, market_id: str, price: float = 0.10):
        market = CryptoThresholdMarket(
            event_id=event_id,
            market_id=market_id,
            question="Will the price of Bitcoin be above $80,000 on April 25?",
            symbol="BTC",
            direction="above",
            strike=80000.0,
            expiry_label="April 25",
            yes_price=price,
            no_price=round(1.0 - price, 3),
            volume=10000.0,
            url_slug=f"slug-{market_id}",
            end_date="2026-04-25T23:59:00Z",
        )
        return CryptoTradeSignal(
            market=market,
            fair_probability=0.5,
            edge=0.2,
            expected_value=0.9,
            kelly_fraction=0.1,
            bet_size=1.0,
            should_buy=True,
            reason="buy",
        )

    def test_mark_open_positions_updates_unrealized_pnl(self):
        state = default_crypto_state(100.0)
        open_paper_positions(state, [self._signal("e1", "m1", price=0.10)])
        marks = {"m1": 0.25}
        marked = mark_open_positions(state, marks)
        self.assertEqual(marked, 1)
        position = state["positions"][0]
        self.assertEqual(position["mark_price"], 0.25)
        self.assertAlmostEqual(position["unrealized_pnl"], 1.5, places=6)

    def test_resolve_positions_closes_win_and_updates_balance(self):
        state = default_crypto_state(100.0)
        open_paper_positions(state, [self._signal("e1", "m1", price=0.10)])
        closed = resolve_positions(state, {"m1": 1.0})
        self.assertEqual(closed, 1)
        position = state["positions"][0]
        self.assertEqual(position["status"], "resolved")
        self.assertEqual(position["exit_price"], 1.0)
        self.assertAlmostEqual(position["realized_pnl"], 9.0, places=6)
        self.assertAlmostEqual(state["balance"], 109.0, places=6)

    def test_resolve_positions_closes_loss(self):
        state = default_crypto_state(100.0)
        open_paper_positions(state, [self._signal("e1", "m1", price=0.20)])
        resolve_positions(state, {"m1": 0.0})
        position = state["positions"][0]
        self.assertEqual(position["status"], "resolved")
        self.assertAlmostEqual(position["realized_pnl"], -1.0, places=6)
        self.assertAlmostEqual(state["balance"], 99.0, places=6)

    def test_open_paper_positions_does_not_reenter_resolved_market(self):
        state = default_crypto_state(100.0)
        signal = self._signal("e1", "m1", price=0.10)
        open_paper_positions(state, [signal])
        resolve_positions(state, {"m1": 1.0})
        opened = open_paper_positions(state, [signal])
        self.assertEqual(opened, 0)
        self.assertEqual(len(state["positions"]), 1)

    def test_state_roundtrip_preserves_mark_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crypto_state.json"
            state = default_crypto_state(100.0)
            open_paper_positions(state, [self._signal("e1", "m1")])
            mark_open_positions(state, {"m1": 0.15})
            save_crypto_state(path, state)
            loaded = load_crypto_state(path, starting_balance=100.0)
            self.assertEqual(loaded["positions"][0]["mark_price"], 0.15)


if __name__ == "__main__":
    unittest.main()
