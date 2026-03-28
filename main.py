import time
import logging
import sys
import requests
import uuid
import csv
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64

# Force immediate output in PyCharm/VS Code console
sys.stdout.reconfigure(line_buffering=True)

# ========================= CONFIG =========================
API_KEY_ID = "50952777-89c2-4d13-b24b-d7820f8b8931"
PRIVATE_KEY_PATH = "C:/Users/pcdox/Desktop/projects/openclaw/chave2.pem"

USE_DEMO = False
SERIES_TICKER = "KXBTC15M"
CHECK_INTERVAL_SECONDS = 2

RISK_FRACTION = 0.25
MAX_ORDER_DOLLARS = 50.0

PRICE_CEILING = 0.82
PRICE_FLOOR = 0.25

TRIGGER_AT_SECONDS = 480
MIN_SECONDS_TO_TRADE = 25

DRY_RUN = False

RESULTS_FILE = "trade_results.csv"

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# ====================== BASE SETUP ======================
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2" if USE_DEMO else "https://api.elections.kalshi.com/trade-api/v2"
logger.info(f"Starting bot in {'DEMO' if USE_DEMO else 'LIVE'} mode → {BASE_URL}")

with open(PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY_PEM = f.read().strip()

private_key = serialization.load_pem_private_key(PRIVATE_KEY_PEM.encode(), password=None)
session = requests.Session()


# ====================== HELPERS ======================
def get_threshold(seconds_left: float):
    """
    Returns the BTC move threshold required to trigger a trade,
    based on how much time is left. Ceiling is fixed — it does
    not rise as time runs out.
    Returns None if outside the trading window.
    """
    if seconds_left > TRIGGER_AT_SECONDS or seconds_left < MIN_SECONDS_TO_TRADE:
        return None
    elif seconds_left > 360:   # 8–6 min out
        return 0.25
    elif seconds_left > 240:   # 6–4 min out
        return 0.20
    elif seconds_left > 180:   # 4–3 min out
        return 0.15
    elif seconds_left > 120:   # 3–2 min out
        return 0.10
    else:                      # last 2 min
        return 0.05


def get_btc_price() -> float | None:
    """Fetch current BTC/USDT spot price from Binance"""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        logger.error(f"Failed to fetch BTC price: {e}")
        return None


def log_market_result(
    ticker: str,
    baseline: float,
    final_btc: float,
    variation: float,
    side: str,
    price: float,
    filled: bool,
    outcome: str
):
    """Log the result of a completed market to CSV"""
    try:
        file_exists = os.path.exists(RESULTS_FILE)
        with open(RESULTS_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "ticker", "baseline_btc", "final_btc",
                    "variation_pct", "side", "price", "filled", "outcome"
                ])
            writer.writerow([
                datetime.now().isoformat(),
                ticker,
                round(baseline, 2),
                round(final_btc, 2),
                round(variation, 4),
                side,
                price,
                filled,
                outcome
            ])
        logger.info(f"📊 Logged result for {ticker}: {outcome} | BTC Δ={variation:.3f}% | side={side} | price={price}")
    except Exception as e:
        logger.error(f"Failed to log market result: {e}")


# ====================== AUTH SIGNING ======================
def sign_request(method: str, path: str) -> dict:
    """Generate Kalshi RSA-PSS signed headers"""
    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + method.upper() + path
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    encoded_sig = base64.b64encode(signature).decode("utf-8")
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": encoded_sig,
        "Content-Type": "application/json"
    }


# ====================== API FUNCTIONS ======================
def get_current_market():
    """Get the soonest-closing open KXBTC15M market, with retry logic"""
    for attempt in range(1, 6):
        try:
            url = f"{BASE_URL}/markets"
            params = {"series_ticker": SERIES_TICKER, "status": "open", "limit": 20}
            resp = session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            markets = resp.json().get("markets", [])
            if not markets:
                return None
            return sorted(markets, key=lambda m: m.get("close_time", "9999"))[0]
        except Exception as e:
            logger.error(f"GET error (attempt {attempt}): {e}")
            time.sleep(2 ** attempt)
    logger.error(f"All retries exhausted for GET {BASE_URL}/markets")
    return None


def get_orderbook_prices(market_ticker: str):
    """Get best YES/NO ask prices from the orderbook"""
    try:
        url = f"{BASE_URL}/markets/{market_ticker}/orderbook"
        resp = session.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        ob = data.get("orderbook_fp", {})
        yes_levels = ob.get("yes_dollars", [])
        no_levels = ob.get("no_dollars", [])
        yes_ask = float(yes_levels[-1][0]) if yes_levels else 0.0
        no_ask = float(no_levels[-1][0]) if no_levels else 0.0
        return yes_ask, no_ask
    except Exception as e:
        logger.error(f"Orderbook error {market_ticker}: {e}")
        return 0.0, 0.0


def get_balance_cents() -> int:
    """Fetch live account balance in cents"""
    try:
        path = "/trade-api/v2/portfolio/balance"
        headers = sign_request("GET", path)
        resp = session.get(f"{BASE_URL}/portfolio/balance", headers=headers, timeout=8)
        resp.raise_for_status()
        balance = resp.json().get("balance", 600)
        logger.info(f"Account balance: ${balance / 100:.2f}")
        return balance
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 600


def place_market_order(market_ticker: str, side: str, count: int, price: float) -> bool:
    """
    Place an aggressive limit IOC order.
    Adds a buffer to cross the spread and fill immediately.
    """
    try:
        path = "/trade-api/v2/portfolio/orders"
        headers = sign_request("POST", path)

        price_cents = int(round(price * 100))

        if side == "yes":
            if price_cents >= 95:
                yes_price_cents = 99
            else:
                yes_price_cents = min(price_cents + 5, 99)
        else:
            if price_cents >= 95:
                yes_price_cents = 1
            else:
                yes_price_cents = max(100 - price_cents - 5, 1)

        order_payload = {
            "action": "buy",
            "client_order_id": str(uuid.uuid4()),
            "count": count,
            "side": side,
            "ticker": market_ticker,
            "type": "limit",
            "yes_price": yes_price_cents,
            "time_in_force": "immediate_or_cancel",
        }

        logger.info(f"Placing order: {order_payload}")
        resp = session.post(
            f"{BASE_URL}/portfolio/orders",
            headers=headers,
            json=order_payload,
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json()

        order = result.get("order", {})
        order_id = order.get("order_id", "unknown")
        status = order.get("status", "unknown")
        logger.info(f"Order response → ID: {order_id} | Status: {status}")

        return wait_for_fill(order_id)

    except requests.HTTPError as e:
        logger.error(f"Order HTTP error: {e.response.status_code} — {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False


def wait_for_fill(order_id: str, timeout_seconds: int = 10) -> bool:
    """Poll order status until filled, canceled, or timeout"""
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            headers = sign_request("GET", path)
            resp = session.get(
                f"{BASE_URL}/portfolio/orders/{order_id}",
                headers=headers,
                timeout=8
            )
            resp.raise_for_status()
            order = resp.json().get("order", {})
            status = order.get("status")
            filled = order.get("filled_count", 0)
            logger.info(f"Order {order_id} → status: {status} | filled: {filled}")

            if status == "filled":
                logger.info(f"✅ Order fully filled: {filled} contracts")
                return True
            elif status in ("canceled", "rejected", "executed"):
                if filled > 0:
                    logger.info(f"✅ Partially filled: {filled} contracts")
                    return True
                logger.warning(f"❌ Order {status} with 0 fills")
                return False

        except Exception as e:
            logger.error(f"Fill check error: {e}")

        time.sleep(0.6)

    logger.warning(f"⏱ Order {order_id} not confirmed filled within {timeout_seconds}s")
    return False


# ===================== STATE =====================
current_ticker = None
market_baseline_btc = None
traded_this_market = False
last_side = None
last_price = None


# ===================== MAIN LOOP =====================
logger.info("Bot started — monitoring KXBTC15M markets...")

while True:
    market = get_current_market()
    if not market:
        logger.info("No open KXBTC15M market found. Waiting...")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    ticker = market.get("ticker")
    close_time_str = market.get("close_time")
    floor_strike = market.get("floor_strike") or market.get("result_sources", [{}])[0].get("floor_strike")

    if not ticker or not close_time_str:
        logger.warning("Market missing ticker or close_time")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # Detect new market — log previous result then reset state
    if ticker != current_ticker:

        # Log the previous market's result before resetting
        if current_ticker and market_baseline_btc:
            final_btc = get_btc_price()
            if final_btc:
                final_variation = (final_btc - market_baseline_btc) / market_baseline_btc * 100
                if last_side:
                    outcome = "win" if (
                        (last_side == "yes" and final_variation > 0) or
                        (last_side == "no" and final_variation < 0)
                    ) else "loss"
                else:
                    outcome = "no_trade"
                log_market_result(
                    ticker=current_ticker,
                    baseline=market_baseline_btc,
                    final_btc=final_btc,
                    variation=final_variation,
                    side=last_side or "none",
                    price=last_price or 0.0,
                    filled=traded_this_market,
                    outcome=outcome
                )

        # Reset state for new market
        current_ticker = ticker
        traded_this_market = False
        market_baseline_btc = None
        last_side = None
        last_price = None

        try:
            baseline = float(floor_strike) if floor_strike else None
        except (TypeError, ValueError):
            baseline = None

        if baseline:
            market_baseline_btc = baseline
            logger.info(f"📌 New market: {ticker} | BTC baseline (floor_strike): ${baseline:,.2f}")
        else:
            logger.info(f"📌 New market: {ticker} | No floor_strike available")

    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        seconds_left = (close_dt - datetime.now(timezone.utc)).total_seconds()
    except Exception as e:
        logger.error(f"Time parsing error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    threshold = get_threshold(seconds_left)

    logger.info(
        f"Market: {ticker} | {seconds_left:.1f}s left | "
        f"Threshold: {f'{threshold:.2f}%' if threshold else '—'} | "
        f"Ceiling: {PRICE_CEILING} | Traded: {traded_this_market}"
    )

    # Outside trading window
    if threshold is None:
        if seconds_left > TRIGGER_AT_SECONDS:
            logger.info(f"  ⏳ {seconds_left:.0f}s left — waiting for <8 min window to open")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # Already traded this market
    if traded_this_market:
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # Need BTC baseline to compare against
    if not market_baseline_btc:
        logger.warning("No BTC baseline available — skipping")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # Fetch current BTC price
    btc_price = get_btc_price()
    if btc_price is None:
        logger.warning("Could not fetch BTC price — skipping this cycle")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    btc_variation = (btc_price - market_baseline_btc) / market_baseline_btc * 100
    yes_price, no_price = get_orderbook_prices(ticker)
    logger.info(
        f"  BTC: ${btc_price:,.2f} | Baseline: ${market_baseline_btc:,.2f} | "
        f"Variation: {btc_variation:.3f}% | YES ask: {yes_price:.4f} | NO ask: {no_price:.4f}"
    )

    # Determine trade direction
    side = price = None

    if btc_variation >= threshold:
        if PRICE_FLOOR <= yes_price <= PRICE_CEILING:
            side, price = "yes", yes_price
        else:
            logger.info(f"  📈 BTC up {btc_variation:.3f}% but YES ask {yes_price:.4f} outside [{PRICE_FLOOR}–{PRICE_CEILING}]")

    elif btc_variation <= -threshold:
        if PRICE_FLOOR <= no_price <= PRICE_CEILING:
            side, price = "no", no_price
        else:
            logger.info(f"  📉 BTC down {btc_variation:.3f}% but NO ask {no_price:.4f} outside [{PRICE_FLOOR}–{PRICE_CEILING}]")

    else:
        logger.info(f"  BTC Δ={btc_variation:.3f}% — below threshold {threshold:.2f}%")

    # Place order if signal triggered
    if side and price and price > 0:
        last_side = side
        last_price = price

        balance_cents = get_balance_cents()
        risk_dollars = min((balance_cents / 100.0) * RISK_FRACTION, MAX_ORDER_DOLLARS)
        count = max(1, int(risk_dollars * 100) // int(price * 100))

        logger.info(
            f"🚀 SIGNAL: {side.upper()} | BTC Δ={btc_variation:.3f}% | "
            f"threshold={threshold:.2f}% | ceiling={PRICE_CEILING} | "
            f"price={price:.4f} | contracts={count} | risk=${risk_dollars:.2f} | "
            f"Dry-run: {DRY_RUN}"
        )

        if not DRY_RUN:
            filled = place_market_order(ticker, side, count, price)
            traded_this_market = True
            if filled:
                logger.info("✅ Trade complete — waiting for next market")
            else:
                logger.warning("⚠️ Order not confirmed filled — will not retry this market")
        else:
            logger.info("DRY RUN — order NOT placed")
            traded_this_market = True

    time.sleep(CHECK_INTERVAL_SECONDS)
