import requests, json

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
resp = requests.get(f"{BASE_URL}/markets", params={"series_ticker": "KXBTC15M", "status": "open", "limit": 1})
markets = resp.json().get("markets", [])
if markets:
    m = markets[0]
    print(f"ticker: {m.get('ticker')}")
    print(f"floor_strike: {m.get('floor_strike')}")
    print(f"yes_ask: {m.get('yes_ask_dollars')}")
    print(f"status: {m.get('status')}")