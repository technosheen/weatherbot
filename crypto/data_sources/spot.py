import requests


class CoinbaseSpotClient:
    BASE_URL = "https://api.coinbase.com/v2/prices"

    def get_spot_price(self, symbol: str) -> float:
        pair = f"{symbol.upper()}-USD"
        response = requests.get(f"{self.BASE_URL}/{pair}/spot", timeout=(3, 8))
        response.raise_for_status()
        payload = response.json()
        return float(payload["data"]["amount"])
