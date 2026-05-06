"""
kalshi_common.py
----------------
Shared authentication, session management, and API helpers used by every
script in the Kalshi BTC trading suite.

Secrets are read from environment variables — never hardcode them:
    KALSHI_API_KEY   your Kalshi API key UUID
    KALSHI_KEY_PATH  path to your RSA private key .pem file

Import instead of copy-pasting into each file:
    from kalshi_common import (
        sign_request, get_btc_price, get_session_name,
        get_orderbook_prices, get_open_markets, kalshi_get,
        BASE_URL, SERIES_TICKER, session,
    )
"""

import os
import time
import base64
import logging
import threading
import asyncio
import json
import requests
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


# ========================= CF BENCHMARKS RTI WEBSOCKET FEED =========================
# CF Benchmarks exposes all Real Time Indices via a single unauthenticated
# WebSocket at wss://www.cfbenchmarks.com/ws/v4 — the same feed the public
# website uses. Kalshi settles KXBTC15M from BRTI, KXETH15M from ETHUSD_RTI,
# KXBNB15M from BNBUSD_RTI, KXSOL15M from SOLUSD_RTI.
# All indices share one connection — we subscribe to each separately.

CF_WS_URL = "wss://www.cfbenchmarks.com/ws/v4"

# Map from Kalshi series prefix → CF Benchmarks index ID
RTI_INDEX_IDS = {
    "KXBTC": "BRTI",
    "KXETH": "ETHUSD_RTI",
    "KXBNB": "BNBUSD_RTI",
    "KXSOL": "SOLUSD_RTI",
}


class _RTIFeed:
    """
    Maintains a single persistent WebSocket connection to CF Benchmarks and
    tracks the latest value for multiple RTI indices simultaneously.
    Thread-safe. Reconnects automatically with exponential backoff.
    """
    FALLBACK_AFTER_SECONDS = 10

    def __init__(self):
        self._prices: dict[str, float]       = {}
        self._updated: dict[str, float]      = {}
        self._lock                            = threading.Lock()
        self._thread                          = threading.Thread(
            target=self._run, daemon=True, name="RTIFeed"
        )
        self._thread.start()

    def price(self, index_id: str) -> float | None:
        """Return current price for an index, or None if stale/unavailable."""
        with self._lock:
            p = self._prices.get(index_id)
            t = self._updated.get(index_id, 0.0)
            if p and (time.time() - t) < self.FALLBACK_AFTER_SECONDS:
                return p
            return None

    def _set(self, index_id: str, value: float):
        with self._lock:
            self._prices[index_id]  = value
            self._updated[index_id] = time.time()

    def _run(self):
        backoff = 1.0
        while True:
            try:
                asyncio.run(self._connect())
                backoff = 1.0
            except Exception as e:
                logger.debug(f"RTI WebSocket error: {e} — reconnecting in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect(self):
        async with websockets.connect(
            CF_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            # Subscribe to all known indices in one connection
            for index_id in RTI_INDEX_IDS.values():
                await ws.send(json.dumps({
                    "type":   "subscribe",
                    "id":     index_id,
                    "stream": "value",
                }))
            logger.info(f"CF Benchmarks RTI WebSocket connected | indices: {list(RTI_INDEX_IDS.values())}")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "value" and "id" in msg and "value" in msg:
                        self._set(msg["id"], float(msg["value"]))
                        logger.debug(f"RTI {msg['id']}: {msg['value']}")
                except Exception:
                    pass


# Single shared instance for all assets
_rti_feed = _RTIFeed()


# ========================= RATE LIMITING & CACHING =========================

class _TTLCache:
    """
    Thread-safe in-process cache with a per-entry TTL.
    Prevents multiple workers from hammering the same endpoint
    within the same time window.
    """
    def __init__(self):
        self._store: dict[str, tuple[float, object]] = {}
        self._lock  = threading.Lock()

    def get(self, key: str) -> object:
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() < entry[0]:
                return entry[1]
            return None

    def set(self, key: str, value: object, ttl: float):
        with self._lock:
            self._store[key] = (time.time() + ttl, value)


_cache = _TTLCache()

# TTL values — tuned to KXBTC15M trading cadence.
# Markets change every 15 min so 20s cache is plenty.
# Orderbook prices move quickly so 4s keeps us responsive without hammering.
MARKET_LIST_TTL  = 20.0   # seconds
ORDERBOOK_TTL    =  4.0   # seconds

def _request_with_backoff(fn, max_retries: int = 3, base_wait: float = 5.0):
    """
    Call fn() and retry on 429 / transient errors with exponential backoff.
    Raises the final exception if all retries are exhausted.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = base_wait * (2 ** attempt)
                logger.warning(f"Rate limited (429) — waiting {wait:.0f}s before retry")
                time.sleep(wait)
            else:
                raise
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            logger.warning(f"Request error ({e}) — retrying in {base_wait}s")
            time.sleep(base_wait)
    raise RuntimeError("All retries exhausted")


# ========================= CONFIG =========================
# Credentials are read from environment variables.
# Set them in your shell, PyCharm run config, or a .env file
# alongside this script:
#
#   KALSHI_API_KEY=your-key-uuid
#   KALSHI_KEY_PATH=C:\path\to\chave2.pem
#
# Fallback: if env vars are absent, try loading a .env file manually.
def _load_env_file():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env_file()

API_KEY_ID = os.environ.get("KALSHI_API_KEY", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_KEY_PATH", "")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise EnvironmentError(
        "Kalshi credentials not found. Set KALSHI_API_KEY and KALSHI_KEY_PATH "
        "as environment variables or in a .env file next to kalshi_common.py."
    )

USE_DEMO      = os.environ.get("KALSHI_DEMO", "false").lower() == "true"
SERIES_TICKER = "KXBTC15M"

BASE_URL: str = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if USE_DEMO else
    "https://api.elections.kalshi.com/trade-api/v2"
)

# ========================= AUTH =========================
with open(PRIVATE_KEY_PATH, "r") as _f:
    _private_key = serialization.load_pem_private_key(
        _f.read().strip().encode(), password=None
    )

# One shared requests.Session for connection pooling.
session = requests.Session()


def sign_request(method: str, path: str) -> dict:
    """
    Generate Kalshi RSA-PSS signed request headers.
    path must be the full API path, e.g. "/trade-api/v2/portfolio/orders".
    """
    timestamp_ms = str(int(time.time() * 1000))
    message      = timestamp_ms + method.upper() + path
    signature    = _private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type":            "application/json",
    }


# ========================= MARKET HELPERS =========================

def get_spot_price(series_ticker: str) -> float | None:
    """
    Return the authoritative CF Benchmarks RTI price for any Kalshi series.

    Primary:  CF Benchmarks RTI WebSocket — same feed Kalshi uses for settlement.
              KXBTC → BRTI, KXETH → ETHUSD_RTI, KXBNB → BNBUSD_RTI, KXSOL → SOLUSD_RTI
    Fallback: Exchange REST APIs (Coinbase, Kraken, Binance, CoinGecko).
              Used when the WebSocket is stale (>10s without an update).
    """
    s        = series_ticker.upper()
    prefix   = next((k for k in RTI_INDEX_IDS if s.startswith(k)), None)
    index_id = RTI_INDEX_IDS.get(prefix) if prefix else None

    # Primary — RTI WebSocket
    if index_id:
        price = _rti_feed.price(index_id)
        if price:
            logger.debug(f"{index_id} (WebSocket): {price:,.4f}")
            return price
        logger.debug(f"{index_id} WebSocket stale — using exchange fallback")

    # Fallback — exchange REST APIs per asset
    if s.startswith("KXBTC"):
        return _btc_fallback()
    elif s.startswith("KXETH"):
        return _exchange_price([
            ("Coinbase", lambda: float(requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot", timeout=5).json()["data"]["amount"])),
            ("Kraken",   lambda: float(next(iter(requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": "ETHUSD"}, timeout=5).json()["result"].values()))["c"][0])),
            ("Binance",  lambda: float(requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "ETHUSDT"}, timeout=5).json()["price"])),
        ], "ETH")
    elif s.startswith("KXBNB"):
        return _exchange_price([
            ("Binance",   lambda: float(requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BNBUSDT"}, timeout=5).json()["price"])),
            ("CoinGecko", lambda: float(requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "binancecoin", "vs_currencies": "usd"}, timeout=5).json()["binancecoin"]["usd"])),
        ], "BNB")
    elif s.startswith("KXSOL"):
        return _exchange_price([
            ("Coinbase", lambda: float(requests.get("https://api.coinbase.com/v2/prices/SOL-USD/spot", timeout=5).json()["data"]["amount"])),
            ("Binance",  lambda: float(requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "SOLUSDT"}, timeout=5).json()["price"])),
        ], "SOL")
    else:
        logger.warning(f"No price source configured for series: {series_ticker}")
        return None


def get_btc_price() -> float | None:
    """Convenience wrapper — returns BRTI price for KXBTC15M settlement."""
    return get_spot_price("KXBTC")


def _exchange_price(sources: list, asset: str) -> float | None:
    """Try a list of (name, fn) exchange sources, return first success."""
    for name, fn in sources:
        try:
            p = fn()
            if p and p > 0:
                return p
        except Exception as e:
            logger.debug(f"{asset} price from {name} failed: {e}")
    return None


def _btc_fallback() -> float | None:
    """BTC-specific fallback: constituent average → Binance."""
    prices = []
    for name, fn in [
        ("Coinbase", lambda: float(requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5).json()["data"]["amount"])),
        ("Kraken",   lambda: float(next(iter(requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}, timeout=5).json()["result"].values()))["c"][0])),
        ("Bitstamp", lambda: float(requests.get("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=5).json()["last"])),
    ]:
        try:
            p = fn()
            if p and p > 0:
                prices.append(p)
        except Exception as e:
            logger.debug(f"BTC from {name} failed: {e}")
    if prices:
        return sum(prices) / len(prices)
    try:
        return float(requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5).json()["price"])
    except Exception as e:
        logger.error(f"All BTC price sources failed: {e}")
        return None


def get_session_name(utc_hour: int) -> str:
    """
    Map a UTC hour (0-23) to a trading session label.

    Sessions (UTC):
        asia      00:00 – 05:59
        europe    06:00 – 11:59
        us        12:00 – 19:59
        us_close  20:00 – 23:59
    """
    if utc_hour < 6:
        return "asia"
    elif utc_hour < 12:
        return "europe"
    elif utc_hour < 20:
        return "us"
    else:
        return "us_close"


def get_orderbook_prices(market_ticker: str) -> tuple[float, float]:
    """
    Return (yes_bid, no_bid) for the current market.
    Results are cached for ORDERBOOK_TTL seconds.

    NOTE: Kalshi's orderbook_fp field names have not been verified against
    a live response. If this returns (0.0, 0.0) check the WARNING log line
    below — it will print the actual response keys so you can correct them.
    """
    cache_key = f"ob:{market_ticker}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    def _fetch():
        resp = session.get(
            f"{BASE_URL}/markets/{market_ticker}/orderbook",
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        ob   = body.get("orderbook_fp", {})

        if not ob:
            logger.warning(
                f"orderbook_fp missing or empty for {market_ticker}. "
                f"Top-level keys: {list(body.keys())}"
            )
            return 0.0, 0.0

        yes_levels = (ob.get("yes_dollars")
                      or ob.get("yes")
                      or ob.get("yes_side")
                      or [])
        no_levels  = (ob.get("no_dollars")
                      or ob.get("no")
                      or ob.get("no_side")
                      or [])

        if not yes_levels and not no_levels:
            ob_keys   = set(ob.keys())
            known     = {"yes_dollars", "no_dollars", "yes", "no", "yes_side", "no_side"}
            if not known.intersection(ob_keys):
                # Genuinely unexpected structure — field names may have changed.
                logger.warning(
                    f"Unexpected orderbook structure for {market_ticker}. "
                    f"orderbook_fp keys: {list(ob_keys)}. "
                    f"Update field names in get_orderbook_prices()."
                )
            else:
                # Keys are correct but lists are empty — thin market,
                # no resting orders. Normal in low-volume sessions (e.g. 04–06 UTC).
                logger.debug(
                    f"Empty orderbook for {market_ticker} — no resting orders"
                )
            return 0.0, 0.0

        # Levels are [[price_dollars, size], ...] sorted ascending by price.
        # The highest bid (last element) is what a buyer would pay — use that
        # as our entry price proxy. Values are already in dollars (0.0–1.0).
        yes_bid = float(yes_levels[-1][0]) if yes_levels else 0.0
        no_bid  = float(no_levels[-1][0])  if no_levels  else 0.0
        return yes_bid, no_bid

    try:
        result = _request_with_backoff(_fetch)
        _cache.set(cache_key, result, ORDERBOOK_TTL)
        return result
    except Exception as e:
        logger.error(f"Orderbook fetch failed for {market_ticker}: {e}")
        return 0.0, 0.0


def get_open_markets(
    series_ticker: str = SERIES_TICKER,
    limit: int = 20,
) -> list[dict]:
    """
    Fetch open markets for a series, sorted by close_time ascending.
    Results are cached for MARKET_LIST_TTL seconds — markets only
    change every 15 minutes so frequent re-fetching is wasteful.
    """
    cache_key = f"markets:{series_ticker}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    def _fetch():
        resp = session.get(
            f"{BASE_URL}/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
        return sorted(markets, key=lambda m: m.get("close_time", "9999"))

    try:
        result = _request_with_backoff(_fetch)
        _cache.set(cache_key, result, MARKET_LIST_TTL)
        return result
    except Exception as e:
        logger.error(f"Market list fetch failed: {e}")
        return []


def kalshi_get(path: str, params: dict | None = None) -> dict:
    """
    Authenticated GET against the Kalshi API.
    path is the short form, e.g. "/portfolio/trades".
    """
    full_path = f"/trade-api/v2{path}"
    headers   = sign_request("GET", full_path)
    resp = session.get(
        f"{BASE_URL}{path}",
        headers=headers,
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
