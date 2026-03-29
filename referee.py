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

MAX_ORDER_DOLLARS = 50.0

# Price range for entry
PRICE_CEILING = 0.80
PRICE_FLOOR = 0.25

# Entry window
ENTRY_OPEN_SECONDS = 780    # start looking 13 minutes out
ENTRY_CLOSE_SECONDS = 300   # stop entering at 5 minutes out

# Position management
TAKE_PROFIT_CENTS = 0.18
STOP_LOSS_CENTS = 0.10
MIN_SECONDS_TO_HOLD = 25

# BTC move required to trigger entry
BTC_ENTRY_THRESHOLD = 0.10

# Kelly settings
KELLY_MIN_SAMPLES = 30      # minimum trades per side before Kelly kicks in
KELLY_FALLBACK = 0.10       # conservative fraction until enough data
KELLY_MIN_FRACTION = 0.05   # floor on Kelly bet size
KELLY_MAX_FRACTION = 0.30   # ceiling on Kelly bet size

DRY_RUN = False

RESULTS_FILE = "trade_results_v2.csv"

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


# ====================== KELLY CRITERION ======================
def calculate_kelly_fraction(results_file: str, side: str) -> float:
    """
    Calculate half-Kelly fraction from historical results.
    Tracks YES and NO separately since edge may differ per side.
    Falls back to conservative fixed fraction if insufficient data.
    """
    if not os.path.exists(results_file):
        logger.info(
            f"  Kelly ({side}): no results file yet — "
            f"using fallback {KELLY_FALLBACK}"
        )
        return KELLY_FALLBACK

    try:
        wins = 0
        losses = 0
        total_win_pnl = 0.0
        total_loss_pnl = 0.0

        with open(results_file, "r") as f:
            reader = csv.DictReader(f)
            rows = [
                r for r in reader
                if r["side"] == side
                and "dryrun" not in r["exit_reason"]
                and r["exit_reason"] != "expiry"  # expiry exits are ambiguous
            ]

        if len(rows) < KELLY_MIN_SAMPLES:
            logger.info(
                f"  Kelly ({side}): only {len(rows)} samples "
                f"(need {KELLY_MIN_SAMPLES}) — "
                f"using fallback {KELLY_FALLBACK}"
            )
            return KELLY_FALLBACK

        for row in rows:
            pnl = float(row["pnl_per_contract"])
            entry = float(row["entry_price"])
            if entry <= 0:
                continue
            if pnl > 0:
                wins += 1
                total_win_pnl += pnl / entry
            else:
                losses += 1
                total_loss_pnl += abs(pnl) / entry

        total = wins + losses
        if total == 0 or losses == 0 or wins == 0:
            logger.info(
                f"  Kelly ({side}): insufficient win/loss spread — "
                f"using fallback {KELLY_FALLBACK}"
            )
            return KELLY_FALLBACK

        win_prob = wins / total
        avg_win = total_win_pnl / wins
        avg_loss = total_loss_pnl / losses

        if avg_loss == 0:
            return KELLY_FALLBACK

        # Kelly formula: f* = (p * b - q) / b
        # b = win/loss ratio, p = win prob, q = loss prob
        b = avg_win / avg_loss
        p = win_prob
        q = 1 - p
        kelly = (b * p - q) / b

        # Use half-Kelly to reduce variance
        half_kelly = kelly / 2

        # Cap between floor and ceiling for safety
        capped = max(KELLY_MIN_FRACTION, min(KELLY_MAX_FRACTION, half_kelly))

        logger.info(
            f"  Kelly ({side}): samples={total} | "
            f"win_rate={win_prob:.2%} | "
            f"avg_win={avg_win:.4f} | "
            f"avg_loss={avg_loss:.4f} | "
            f"b={b:.2f} | "
            f"raw={kelly:.4f} | "
            f"half={half_kelly:.4f} | "
            f"capped={capped:.4f}"
        )

        return capped

    except Exception as e:
        logger.error(f"Kelly calculation failed: {e}")
        return KELLY_FALLBACK


# ====================== HELPERS ======================
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


def log_trade_result(
    ticker: str,
    side: str,
    entry_price: float,
    exit_price: float,
    count: int,
    exit_reason: str,
    pnl_per_contract: float,
    total_pnl: float
):
    """Log the result of a completed trade to CSV"""
    try:
        file_exists = os.path.exists(RESULTS_FILE)
        with open(RESULTS_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "ticker", "side",
                    "entry_price", "exit_price", "count",
                    "exit_reason", "pnl_per_contract", "total_pnl"
                ])
            writer.writerow([
                datetime.now().isoformat(),
                ticker,
                side,
                round(entry_price, 4),
                round(exit_price, 4),
                count,
                exit_reason,
                round(pnl_per_contract, 4),
                round(total_pnl, 4)
            ])
        logger.info(
            f"📊 Logged: {side.upper()} | "
            f"entry={entry_price:.4f} | "
            f"exit={exit_price:.4f} | "
            f"reason={exit_reason} | "
            f"PnL/contract={pnl_per_contract:+.4f} | "
            f"total={total_pnl:+.4f}"
        )
    except Exception as e:
        logger.error(f"Failed to log trade result: {e}")


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
            return sorted(
                markets,
                key=lambda m: m.get("close_time", "9999")
            )[0]
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
        resp = session.get(
            f"{BASE_URL}/portfolio/balance",
            headers=headers,
            timeout=8
        )
        resp.raise_for_status()
        balance = resp.json().get("balance", 600)
        logger.info(f"Account balance: ${balance / 100:.2f}")
        return balance
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 600


def place_order(
    market_ticker: str,
    action: str,
    side: str,
    count: int,
    price: float
) -> tuple[bool, int]:
    """
    Place a limit IOC order — either buy or sell.
    Returns (success, filled_count).
    action: "buy" or "sell"
    side: "yes" or "no"
    price: the current market price for this side
    """
    try:
        path = "/trade-api/v2/portfolio/orders"
        headers = sign_request("POST", path)

        price_cents = int(round(price * 100))

        if action == "buy":
            if side == "yes":
                yes_price_cents = min(price_cents + 5, 99) if price_cents < 95 else 99
            else:
                yes_price_cents = max(100 - price_cents - 5, 1) if price_cents < 95 else 1
        else:
            if side == "yes":
                yes_price_cents = max(price_cents - 3, 1)
            else:
                yes_price_cents = min(100 - price_cents + 3, 99)

        order_payload = {
            "action": action,
            "client_order_id": str(uuid.uuid4()),
            "count": count,
            "side": side,
            "ticker": market_ticker,
            "type": "limit",
            "yes_price": yes_price_cents,
            "time_in_force": "immediate_or_cancel",
        }

        logger.info(f"Placing {action.upper()} order: {order_payload}")
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
        logger.error(
            f"Order HTTP error: {e.response.status_code} — {e.response.text}"
        )
        return False, 0
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False, 0


def wait_for_fill(order_id: str, timeout_seconds: int = 10) -> tuple[bool, int]:
    """
    Poll order status until filled, canceled, or timeout.
    Returns (success, filled_count).
    """
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
            logger.info(
                f"Order {order_id} → status: {status} | filled: {filled}"
            )

            if status == "filled":
                logger.info(f"✅ Order fully filled: {filled} contracts")
                return True, filled
            elif status in ("canceled", "rejected", "executed"):
                if filled > 0:
                    logger.info(f"✅ Partially filled: {filled} contracts")
                    return True, filled
                logger.warning(f"❌ Order {status} with 0 fills")
                return False, 0

        except Exception as e:
            logger.error(f"Fill check error: {e}")

        time.sleep(0.6)

    logger.warning(
        f"⏱ Order {order_id} not confirmed filled within {timeout_seconds}s"
    )
    return False, 0


# ===================== STATE =====================
current_ticker = None
market_baseline_btc = None

# Entry state
entry_attempted = False

# Position state
position_open = False
position_side = None
position_entry_price = None
position_count = None
position_entry_time = None


def reset_market_state():
    """Reset all state for a new market"""
    global entry_attempted, position_open, position_side
    global position_entry_price, position_count, position_entry_time
    entry_attempted = False
    position_open = False
    position_side = None
    position_entry_price = None
    position_count = None
    position_entry_time = None


# ===================== MAIN LOOP =====================
logger.info(
    "Bot started — monitoring KXBTC15M markets "
    "(buy-and-sell strategy with Kelly sizing)..."
)

while True:
    market = get_current_market()
    if not market:
        logger.info("No open KXBTC15M market found. Waiting...")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    ticker = market.get("ticker")
    close_time_str = market.get("close_time")
    floor_strike = (
        market.get("floor_strike") or
        market.get("result_sources", [{}])[0].get("floor_strike")
    )

    if not ticker or not close_time_str:
        logger.warning("Market missing ticker or close_time")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Detect new market ──
    if ticker != current_ticker:

        # If we had an open position in the previous market, log it as expired
        if position_open and current_ticker:
            logger.warning(
                f"⚠️ Position in {current_ticker} expired without closing"
            )
            yes_price, no_price = get_orderbook_prices(current_ticker)
            exit_price = (
                yes_price if position_side == "yes" else no_price
            )
            pnl = exit_price - position_entry_price
            log_trade_result(
                ticker=current_ticker,
                side=position_side,
                entry_price=position_entry_price,
                exit_price=exit_price,
                count=position_count,
                exit_reason="expiry",
                pnl_per_contract=pnl,
                total_pnl=pnl * position_count
            )

        current_ticker = ticker
        reset_market_state()
        market_baseline_btc = None

        try:
            baseline = float(floor_strike) if floor_strike else None
        except (TypeError, ValueError):
            baseline = None

        if baseline:
            market_baseline_btc = baseline
            logger.info(
                f"📌 New market: {ticker} | "
                f"BTC baseline: ${baseline:,.2f}"
            )
        else:
            logger.info(
                f"📌 New market: {ticker} | No floor_strike available"
            )

    # ── Parse time remaining ──
    try:
        close_dt = datetime.fromisoformat(
            close_time_str.replace("Z", "+00:00")
        )
        seconds_left = (
            close_dt - datetime.now(timezone.utc)
        ).total_seconds()
    except Exception as e:
        logger.error(f"Time parsing error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Fetch prices ──
    yes_price, no_price = get_orderbook_prices(ticker)
    btc_price = get_btc_price()

    logger.info(
        f"Market: {ticker} | {seconds_left:.1f}s left | "
        f"YES: {yes_price:.4f} | NO: {no_price:.4f} | "
        f"Position: "
        f"{'OPEN ' + position_side.upper() if position_open else 'NONE'}"
    )

    # ══════════════════════════════════════════
    # POSITION MANAGEMENT
    # ══════════════════════════════════════════
    if position_open:
        current_price = (
            yes_price if position_side == "yes" else no_price
        )
        price_change = current_price - position_entry_price
        seconds_held = (
            datetime.now(timezone.utc) - position_entry_time
        ).total_seconds()

        logger.info(
            f"  📊 Position: {position_side.upper()} | "
            f"Entry: {position_entry_price:.4f} | "
            f"Current: {current_price:.4f} | "
            f"P&L: {price_change:+.4f} | "
            f"Held: {seconds_held:.0f}s"
        )

        exit_reason = None

        if price_change >= TAKE_PROFIT_CENTS:
            exit_reason = "take_profit"
            logger.info(f"  🎯 Take profit triggered: +{price_change:.4f}")

        elif price_change <= -STOP_LOSS_CENTS:
            exit_reason = "stop_loss"
            logger.info(f"  🛑 Stop loss triggered: {price_change:.4f}")

        elif seconds_left <= MIN_SECONDS_TO_HOLD:
            exit_reason = "time_stop"
            logger.info(f"  ⏱ Time stop: {seconds_left:.0f}s left")

        if exit_reason:
            if not DRY_RUN:
                success, filled = place_order(
                    market_ticker=ticker,
                    action="sell",
                    side=position_side,
                    count=position_count,
                    price=current_price
                )
                if success:
                    pnl = current_price - position_entry_price
                    log_trade_result(
                        ticker=ticker,
                        side=position_side,
                        entry_price=position_entry_price,
                        exit_price=current_price,
                        count=position_count,
                        exit_reason=exit_reason,
                        pnl_per_contract=pnl,
                        total_pnl=pnl * position_count
                    )
                    position_open = False
                    logger.info(
                        f"✅ Position closed | "
                        f"reason={exit_reason} | "
                        f"PnL={pnl:+.4f}/contract | "
                        f"Total={pnl * position_count:+.4f}"
                    )
                else:
                    logger.warning(
                        "⚠️ Failed to close position — will retry next cycle"
                    )
            else:
                pnl = current_price - position_entry_price
                log_trade_result(
                    ticker=ticker,
                    side=position_side,
                    entry_price=position_entry_price,
                    exit_price=current_price,
                    count=position_count,
                    exit_reason=f"{exit_reason}_dryrun",
                    pnl_per_contract=pnl,
                    total_pnl=pnl * position_count
                )
                position_open = False
                logger.info(
                    f"DRY RUN — position would have closed: {exit_reason} | "
                    f"PnL={pnl:+.4f}/contract"
                )

        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ══════════════════════════════════════════
    # ENTRY LOGIC
    # ══════════════════════════════════════════

    if entry_attempted:
        logger.info("  ⏭ Entry already attempted this market — skipping")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    if seconds_left > ENTRY_OPEN_SECONDS:
        logger.info(
            f"  ⏳ {seconds_left:.0f}s left — "
            f"waiting for entry window to open"
        )
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    if seconds_left < ENTRY_CLOSE_SECONDS:
        logger.info(
            f"  🚫 {seconds_left:.0f}s left — entry window has closed"
        )
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    if not market_baseline_btc:
        logger.warning("  No BTC baseline — skipping entry")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    if btc_price is None:
        logger.warning("  Could not fetch BTC price — skipping entry")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    btc_variation = (
        (btc_price - market_baseline_btc) / market_baseline_btc * 100
    )

    logger.info(
        f"  BTC: ${btc_price:,.2f} | "
        f"Baseline: ${market_baseline_btc:,.2f} | "
        f"Variation: {btc_variation:.3f}%"
    )

    # ── Determine entry direction ──
    side = None
    price = None

    if btc_variation >= BTC_ENTRY_THRESHOLD:
        if PRICE_FLOOR <= yes_price <= PRICE_CEILING:
            side, price = "yes", yes_price
            logger.info(
                f"  📈 BTC up {btc_variation:.3f}% → "
                f"targeting YES @ {yes_price:.4f}"
            )
        else:
            logger.info(
                f"  📈 BTC up {btc_variation:.3f}% but "
                f"YES {yes_price:.4f} outside "
                f"[{PRICE_FLOOR}–{PRICE_CEILING}]"
            )

    elif btc_variation <= -BTC_ENTRY_THRESHOLD:
        if PRICE_FLOOR <= no_price <= PRICE_CEILING:
            side, price = "no", no_price
            logger.info(
                f"  📉 BTC down {btc_variation:.3f}% → "
                f"targeting NO @ {no_price:.4f}"
            )
        else:
            logger.info(
                f"  📉 BTC down {btc_variation:.3f}% but "
                f"NO {no_price:.4f} outside "
                f"[{PRICE_FLOOR}–{PRICE_CEILING}]"
            )

    else:
        logger.info(
            f"  BTC Δ={btc_variation:.3f}% — "
            f"below threshold {BTC_ENTRY_THRESHOLD:.2f}%"
        )

    # ── Place entry order with Kelly sizing ──
    if side and price and price > 0:

        # Calculate Kelly fraction from historical results
        kelly_fraction = calculate_kelly_fraction(RESULTS_FILE, side)

        balance_cents = get_balance_cents()
        risk_dollars = min(
            (balance_cents / 100.0) * kelly_fraction,
            MAX_ORDER_DOLLARS
        )
        count = max(1, int(risk_dollars * 100) // int(price * 100))

        logger.info(
            f"  🚀 ENTRY: {side.upper()} @ {price:.4f} | "
            f"kelly={kelly_fraction:.4f} | "
            f"risk=${risk_dollars:.2f} | "
            f"contracts={count} | "
            f"TP=+{TAKE_PROFIT_CENTS} | "
            f"SL=-{STOP_LOSS_CENTS} | "
            f"Dry-run: {DRY_RUN}"
        )

        entry_attempted = True

        if not DRY_RUN:
            success, filled_count = place_order(
                market_ticker=ticker,
                action="buy",
                side=side,
                count=count,
                price=price
            )
            if success and filled_count > 0:
                position_open = True
                position_side = side
                position_entry_price = price
                position_count = filled_count
                position_entry_time = datetime.now(timezone.utc)
                logger.info(
                    f"✅ Position opened: "
                    f"{side.upper()} x{filled_count} @ {price:.4f}"
                )
            else:
                logger.warning("⚠️ Entry order failed or not filled")
        else:
            position_open = True
            position_side = side
            position_entry_price = price
            position_count = count
            position_entry_time = datetime.now(timezone.utc)
            logger.info(
                f"DRY RUN — simulated position: "
                f"{side.upper()} x{count} @ {price:.4f}"
            )

    time.sleep(CHECK_INTERVAL_SECONDS)
