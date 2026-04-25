import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crypto.models import CryptoThresholdMarket, CryptoTradeSignal


class TestCryptoCLI(unittest.TestCase):
    def _signal(self, event_id: str, market_id: str, should_buy: bool = True, edge: float = 0.2, ev: float = 0.9, yes_price: float = 0.10, mark_price: float | None = None, strike: float = 80000.0, symbol: str = "BTC"):
        market = CryptoThresholdMarket(
            event_id=event_id,
            market_id=market_id,
            question=f"Will the price of {symbol} be above ${strike:,.0f} on April 25?",
            symbol=symbol,
            direction="above",
            strike=strike,
            expiry_label="April 25",
            yes_price=yes_price,
            no_price=0.90,
            volume=10000.0,
            url_slug=f"slug-{market_id}",
            end_date="2026-04-25T23:59:00Z",
            mark_price=mark_price if mark_price is not None else yes_price,
        )
        return CryptoTradeSignal(
            market=market,
            fair_probability=0.5,
            edge=edge,
            expected_value=ev,
            kelly_fraction=0.1,
            bet_size=1.0 if should_buy else 0.0,
            should_buy=should_buy,
            reason="buy" if should_buy else "skip",
        )

    def test_status_command_prints_position_count(self):
        import crypto_bot
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crypto_state.json"
            with patch.object(crypto_bot, "CRYPTO_STATE_FILE", state_path):
                state = crypto_bot.default_crypto_state(100.0)
                state["positions"] = [{"market_id": "m1", "status": "open", "symbol": "BTC", "expiry_label": "April 25", "strike": 80000.0, "entry_price": 0.10, "bet_size": 1.0}]
                crypto_bot.save_crypto_state(state_path, state)
                with patch.object(crypto_bot, "discover_and_score_signals", return_value=[]):
                    rc = crypto_bot.main(["status"])
                self.assertEqual(rc, 0)

    def test_scan_opens_only_best_signal_per_event(self):
        import crypto_bot
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crypto_state.json"
            signals = [
                self._signal("e1", "m1", should_buy=True, edge=0.10, ev=0.5),
                self._signal("e1", "m2", should_buy=True, edge=0.20, ev=0.9),
                self._signal("e2", "m3", should_buy=True, edge=0.15, ev=0.6),
            ]
            with patch.object(crypto_bot, "CRYPTO_STATE_FILE", state_path), \
                 patch.object(crypto_bot, "discover_and_score_signals", return_value=signals):
                rc = crypto_bot.main(["scan"])
                self.assertEqual(rc, 0)
                state = crypto_bot.load_crypto_state(state_path, starting_balance=100.0)
                self.assertEqual({p["market_id"] for p in state["positions"]}, {"m2", "m3"})

    def test_status_marks_positions_from_latest_signal_prices(self):
        import crypto_bot
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crypto_state.json"
            with patch.object(crypto_bot, "CRYPTO_STATE_FILE", state_path):
                state = crypto_bot.default_crypto_state(100.0)
                state["positions"] = [{
                    "event_id": "e1",
                    "market_id": "m1",
                    "symbol": "BTC",
                    "expiry_label": "April 25",
                    "strike": 80000.0,
                    "entry_price": 0.10,
                    "bet_size": 1.0,
                    "shares": 10.0,
                    "status": "open",
                }]
                crypto_bot.save_crypto_state(state_path, state)
                with patch.object(crypto_bot, "discover_and_score_signals", return_value=[self._signal("e1", "m1", yes_price=0.12, mark_price=0.09)]):
                    rc = crypto_bot.main(["status"])
                    self.assertEqual(rc, 0)
                updated = crypto_bot.load_crypto_state(state_path, starting_balance=100.0)
                self.assertEqual(updated["positions"][0]["mark_price"], 0.09)

    def test_scan_applies_near_atm_filter_before_opening_positions(self):
        import crypto_bot
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crypto_state.json"
            signals = [
                self._signal("e1", "m1", strike=70000, ev=0.7),
                self._signal("e1", "m2", strike=78000, ev=0.8),
                self._signal("e1", "m3", strike=82000, ev=0.9),
            ]
            with patch.object(crypto_bot, "CRYPTO_STATE_FILE", state_path), \
                 patch.object(crypto_bot, "discover_and_score_signals", return_value=signals):
                rc = crypto_bot.main(["scan"])
                self.assertEqual(rc, 0)
                state = crypto_bot.load_crypto_state(state_path, starting_balance=100.0)
                self.assertEqual({p["market_id"] for p in state["positions"]}, {"m3"})


if __name__ == "__main__":
    unittest.main()
