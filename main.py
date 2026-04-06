import time
import logging
import sys
import math
import requests
import uuid
import csv
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True)

# ========================= CONFIG =========================
API_KEY_ID = "50952777-89c2-4d13-b24b-d7820f8b8931"
PRIVATE_KEY_PATH = "C:/Users/pcdox/Desktop/projects/openclaw/chave2.pem"

USE_DEMO = False
SERIES_TICKER = "KXBTC15M"
CHECK_INTERVAL_SECONDS = 2

MAX_ORDER_DOLLARS = 50.0

PRICE_CEILING = 0.88
PRICE_FLOOR = 0.25

TRIGGER_AT_SECONDS = 480
MIN_SECONDS_TO_TRADE = 25

# ---- Dynamic threshold ----
CONFIDENCE = 0.95

SESSION_SIGMA: dict[str, tuple[float, float, float]] = {
    "asia":     (0.000301, 1.23, 0.50),
    "europe":   (0.000256, 1.23, 0.50),
    "us":       (0.000491, 1.40, 0.50),
    "us_close": (0.000491, 1.40, 0.50),
}

# Minimum rows per session before σ calibration runs.
# #4: lowered from 200 to account for full-candle logging.
SIGMA_MIN_SAMPLES = 200

# ---- Kelly criterion ----
KELLY_MIN_SAMPLES  = 30
KELLY_FALLBACK     = 0.10
KELLY_MIN_FRACTION = 0.05
KELLY_MAX_FRACTION = 0.30

# ---- Take profit ----
TAKE_PROFIT_CENTS = 0.18

# ---- Settlement fetching ----
SETTLEMENT_RETRIES    = 6
SETTLEMENT_RETRY_WAIT = 5

DRY_RUN = False

RESULTS_FILE = "trade_results.csv"
PRICES_FILE  = "btc_prices.csv"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)

# ====================== BASE SETUP ======================
BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if USE_DEMO else
    "https://api.elections.kalshi.com/trade-api/v2"
)
logger.info(
    f"Starting bot in {'DEMO' if USE_DEMO else 'LIVE'} mode → {BASE_URL}"
)

with open(PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY_PEM = f.read().strip()

private_key = serialization.load_pem_private_key(
    PRIVATE_KEY_PEM.encode(), password=None
)
session = requests.Session()


# ====================== HELPERS ======================
def _norm_ppf(p: float) -> float:
    a = (2.515517, 0.802853, 0.010328)
    b = (1.432788, 0.189269, 0.001308)
    t = math.sqrt(-2.0 * math.log(p if p < 0.5 else 1.0 - p))
    z = t - (a[0] + t * (a[1] + t * a[2])) / \
        (1.0 + t * (b[0] + t * (b[1] + t * b[2])))
    return -z if p < 0.5 else z


_Z = _norm_ppf(1.0 - (1.0 - CONFIDENCE) / 2.0)


def _get_session(utc_hour: int) -> str:
    if 0 <= utc_hour < 6:
        return "asia"
    elif 6 <= utc_hour < 12:
        return "europe"
    elif 12 <= utc_hour < 20:
        return "us"
    else:
        return "us_close"


def get_btc_price() -> float | None:
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


# ====================== SETTLEMENT FETCHING ======================
def get_market_settlement(ticker: str) -> str | None:
    """
    Fetch the actual settled result ("yes" or "no") from Kalshi.
    Retries up to SETTLEMENT_RETRIES times since settlement
    can take several seconds after market close.
    """
    path = f"/trade-api/v2/markets/{ticker}"

    for attempt in range(1, SETTLEMENT_RETRIES + 1):
        try:
            headers = sign_request("GET", path)
            resp = session.get(
                f"{BASE_URL}/markets/{ticker}",
                headers=headers,
                timeout=8
            )
            resp.raise_for_status()
            market_data = resp.json().get("market", {})
            result = market_data.get("result")

            if result in ("yes", "no"):
                logger.info(
                    f"  ✅ Settlement fetched for {ticker}: "
                    f"{result.upper()} (attempt {attempt})"
                )
                return result

            status = market_data.get("status", "unknown")
            logger.info(
                f"  ⏳ Settlement not ready for {ticker} "
                f"(status={status}, attempt {attempt}/{SETTLEMENT_RETRIES})"
            )

        except Exception as e:
            logger.error(
                f"  Settlement fetch error for {ticker} "
                f"(attempt {attempt}): {e}"
            )

        if attempt < SETTLEMENT_RETRIES:
            time.sleep(SETTLEMENT_RETRY_WAIT)

    logger.warning(
        f"  ⚠️ Could not fetch settlement for {ticker} "
        f"after {SETTLEMENT_RETRIES} attempts"
    )
    return None


# ====================== SIGMA CALIBRATION (#4) ======================
def calibrate_session_sigma() -> None:
    """
    Estimate σ per session from the standard deviation of per-minute
    BTC returns, and fit α via OLS in log-log space.

    #4 — Sampling bias fix:
    btc_prices.csv now logs ticks throughout the ENTIRE candle
    (not just inside the trading window), so the σ estimate reflects
    true full-candle volatility rather than late-candle volatility.
    The OLS for α uses minutes_elapsed (time since candle open) rather
    than minutes_remaining, which avoids the early-candle near-zero
    bias that was corrupting the previous fit.

    Updates SESSION_SIGMA in-place.
    Skips sessions with fewer than SIGMA_MIN_SAMPLES rows.
    """
    if not os.path.exists(PRICES_FILE):
        return

    try:
        session_rows: dict[str, list[dict]] = defaultdict(list)

        with open(PRICES_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    sess = row["session"]
                    if sess in SESSION_SIGMA:
                        session_rows[sess].append({
                            "ticker":       row["ticker"],
                            "seconds_left": float(row["seconds_left"]),
                            "variation":    float(row["variation_pct"]) / 100.0,
                            "btc_price":    float(row["btc_price"]),
                        })
                except (KeyError, ValueError):
                    continue

        for sess, rows in session_rows.items():
            if len(rows) < SIGMA_MIN_SAMPLES:
                logger.info(
                    f"  σ calibration ({sess}): "
                    f"{len(rows)} samples < {SIGMA_MIN_SAMPLES} — skipping"
                )
                continue

            current_sigma, current_k, current_alpha = SESSION_SIGMA[sess]

            # ── σ: std of per-minute returns ──
            minute_returns: list[float] = []
            by_ticker: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                by_ticker[r["ticker"]].append(r)

            for ticker_rows in by_ticker.values():
                ticker_rows.sort(
                    key=lambda x: x["seconds_left"], reverse=True
                )
                prices = [r["btc_price"] for r in ticker_rows]
                times  = [r["seconds_left"] for r in ticker_rows]
                for i in range(1, len(prices)):
                    dt = times[i - 1] - times[i]
                    if 50 <= dt <= 70:
                        ret = (prices[i] - prices[i - 1]) / prices[i - 1]
                        minute_returns.append(ret)

            if len(minute_returns) < 10:
                logger.info(
                    f"  σ calibration ({sess}): "
                    f"insufficient minute-return pairs — skipping"
                )
                continue

            n        = len(minute_returns)
            mean_r   = sum(minute_returns) / n
            variance = sum(
                (r - mean_r) ** 2 for r in minute_returns
            ) / (n - 1)
            sigma_fit = math.sqrt(variance)

            # ── α: OLS on log(|variation|) ~ log(minutes_elapsed) ──
            # #4: use minutes_elapsed (time since open) not minutes_left.
            # This avoids the near-zero bias at candle start where
            # variation is always tiny regardless of t.
            # Only use rows with at least 2 minutes elapsed and
            # meaningful variation (> 0.01%) for a clean fit.
            alpha_data = [
                r for r in rows
                if (900 - r["seconds_left"]) >= 120   # at least 2 min elapsed
                and abs(r["variation"]) > 0.0001
            ]

            alpha_fit = current_alpha

            if len(alpha_data) >= 50:
                log_t = [
                    math.log(
                        max((900 - r["seconds_left"]) / 60.0, 0.1)
                    )
                    for r in alpha_data
                ]
                log_v = [
                    math.log(abs(r["variation"]) / current_k)
                    for r in alpha_data
                ]
                m     = len(log_t)
                s_t   = sum(log_t)
                s_v   = sum(log_v)
                s_tt  = sum(x * x for x in log_t)
                s_tv  = sum(x * y for x, y in zip(log_t, log_v))
                denom = m * s_tt - s_t * s_t
                if abs(denom) > 1e-12:
                    alpha_raw = (m * s_tv - s_t * s_v) / denom
                    alpha_fit = max(0.3, min(1.5, alpha_raw))

            sigma_fit = max(1e-5, min(0.005, sigma_fit))
            old_sigma, old_k, old_alpha = SESSION_SIGMA[sess]
            SESSION_SIGMA[sess] = (sigma_fit, current_k, alpha_fit)

            logger.info(
                f"  σ calibrated ({sess}): "
                f"σ {old_sigma:.6f}→{sigma_fit:.6f} | "
                f"α {old_alpha:.3f}→{alpha_fit:.3f} | "
                f"k={current_k:.2f} | "
                f"n_rows={len(rows)} | "
                f"n_returns={len(minute_returns)}"
            )

    except Exception as e:
        logger.error(f"calibrate_session_sigma failed: {e}")


# ====================== DYNAMIC THRESHOLD ======================
def get_threshold(seconds_left: float, sess: str) -> float | None:
    """
    Δ_min(t) = z * σ * k * t^α  where t = minutes remaining.
    Session locked at candle open.
    Returns None if outside the trading window.
    """
    if (
        seconds_left > TRIGGER_AT_SECONDS or
        seconds_left < MIN_SECONDS_TO_TRADE
    ):
        return None
    sigma, fat_tail, alpha = SESSION_SIGMA[sess]
    minutes_left = seconds_left / 60.0
    threshold_fraction = _Z * sigma * fat_tail * (minutes_left ** alpha)
    return threshold_fraction * 100.0


# ====================== KELLY CRITERION ======================
def estimate_win_probability(side: str) -> float | None:
    """
    Estimate win probability from historical filled trades.
    Excludes "unknown" outcomes (settlement fetch failed) and
    "take_profit" exits (always wins, would bias the estimate upward).
    Returns None if insufficient data.
    """
    if not os.path.exists(RESULTS_FILE):
        return None

    try:
        with open(RESULTS_FILE, "r") as f:
            reader = csv.DictReader(f)
            rows = [
                r for r in reader
                if r.get("side") == side
                and r.get("filled", "").lower() == "true"
                and r.get("outcome") in ("win", "loss")
                and r.get("exit_reason", "expiry") != "take_profit"
            ]

        if len(rows) < KELLY_MIN_SAMPLES:
            return None

        wins = sum(1 for r in rows if r["outcome"] == "win")
        return wins / len(rows)

    except Exception as e:
        logger.error(f"Win probability estimation failed: {e}")
        return None


def calculate_kelly_fraction(side: str, entry_price: float) -> float:
    """
    Half-Kelly using:
      - p: win probability from historical expiry-held results
      - b: actual payout odds at entry price = (1 - price) / price
    Falls back to KELLY_FALLBACK if insufficient data.
    """
    win_prob = estimate_win_probability(side)

    if win_prob is None:
        logger.info(
            f"  Kelly ({side}): insufficient data — "
            f"using fallback {KELLY_FALLBACK}"
        )
        return KELLY_FALLBACK

    b = (1.0 - entry_price) / entry_price
    p = win_prob
    q = 1.0 - p

    if b <= 0:
        return KELLY_FALLBACK

    kelly      = (b * p - q) / b
    half_kelly = kelly / 2.0
    capped     = max(KELLY_MIN_FRACTION, min(KELLY_MAX_FRACTION, half_kelly))

    logger.info(
        f"  Kelly ({side}): win_prob={win_prob:.2%} | "
        f"entry={entry_price:.4f} | b={b:.4f} | "
        f"raw={kelly:.4f} | half={half_kelly:.4f} | capped={capped:.4f}"
    )
    return capped


# ====================== AUTH SIGNING ======================
def sign_request(method: str, path: str) -> dict:
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
    for attempt in range(1, 6):
        try:
            url = f"{BASE_URL}/markets"
            params = {
                "series_ticker": SERIES_TICKER,
                "status": "open",
                "limit": 20
            }
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
    try:
        url = f"{BASE_URL}/markets/{market_ticker}/orderbook"
        resp = session.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        ob = data.get("orderbook_fp", {})
        yes_levels = ob.get("yes_dollars", [])
        no_levels  = ob.get("no_dollars",  [])
        yes_ask = float(yes_levels[-1][0]) if yes_levels else 0.0
        no_ask  = float(no_levels[-1][0])  if no_levels  else 0.0
        return yes_ask, no_ask
    except Exception as e:
        logger.error(f"Orderbook error {market_ticker}: {e}")
        return 0.0, 0.0


def get_balance_cents() -> int:
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


def place_market_order(
    market_ticker: str,
    side: str,
    count: int,
    price: float,
    action: str = "buy"
) -> tuple[bool, int]:
    """
    Place an aggressive limit IOC order.
    action="buy"  — entry
    action="sell" — take profit exit

    #5: returns (success, actual_filled_count) instead of just bool,
    so callers can track the true position size.
    """
    try:
        path = "/trade-api/v2/portfolio/orders"
        headers = sign_request("POST", path)

        price_cents = int(round(price * 100))

        if action == "buy":
            if side == "yes":
                yes_price_cents = (
                    99 if price_cents >= 95
                    else min(price_cents + 5, 99)
                )
            else:
                yes_price_cents = (
                    1 if price_cents >= 95
                    else max(100 - price_cents - 5, 1)
                )
        else:
            # Sell: accept slightly below current price
            if side == "yes":
                yes_price_cents = max(price_cents - 3, 1)
            else:
                yes_price_cents = min(100 - price_cents + 3, 99)

        order_payload = {
            "action":          action,
            "client_order_id": str(uuid.uuid4()),
            "count":           count,
            "side":            side,
            "ticker":          market_ticker,
            "type":            "limit",
            "yes_price":       yes_price_cents,
            "time_in_force":   "immediate_or_cancel",
        }

        logger.info(f"Placing {action.upper()} order: {order_payload}")
        resp = session.post(
            f"{BASE_URL}/portfolio/orders",
            headers=headers,
            json=order_payload,
            timeout=10
        )
        resp.raise_for_status()
        result   = resp.json()
        order    = result.get("order", {})
        order_id = order.get("order_id", "unknown")
        status   = order.get("status",   "unknown")
        logger.info(
            f"Order response → ID: {order_id} | Status: {status}"
        )
        return wait_for_fill(order_id)

    except requests.HTTPError as e:
        logger.error(
            f"Order HTTP error: {e.response.status_code} — "
            f"{e.response.text}"
        )
        return False, 0
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False, 0


def wait_for_fill(
    order_id: str,
    timeout_seconds: int = 10
) -> tuple[bool, int]:
    """
    Poll order status until filled, canceled, or timeout.
    #5: returns (success, filled_count) so callers know the
    exact number of contracts filled, which may be less than
    requested on partial fills.
    """
    path     = f"/trade-api/v2/portfolio/orders/{order_id}"
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
            order  = resp.json().get("order", {})
            status = order.get("status")
            filled = int(order.get("filled_count", 0))
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


# ====================== LOGGING ======================
def log_market_result(
    ticker: str,
    baseline: float,
    final_btc: float,
    variation: float,
    side: str,
    price: float,
    filled: bool,
    filled_count: int,       # #5
    outcome: str,
    settled_result: str,
    sess: str,
    exit_reason: str = "expiry",
):
    """Log the result of a completed market to CSV."""
    try:
        now      = datetime.now(timezone.utc)
        utc_hour = now.hour

        file_exists = os.path.exists(RESULTS_FILE)
        with open(RESULTS_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "utc_hour", "session",
                    "ticker", "baseline_btc", "final_btc",
                    "variation_pct", "side", "price",
                    "filled", "filled_count",   # #5
                    "settled_result", "outcome", "exit_reason",
                ])
            writer.writerow([
                now.isoformat(),
                utc_hour,
                sess,
                ticker,
                round(baseline, 2),
                round(final_btc, 2),
                round(variation, 4),
                side,
                price,
                filled,
                filled_count,                   # #5
                settled_result,
                outcome,
                exit_reason,
            ])
        logger.info(
            f"📊 Logged result for {ticker}: "
            f"outcome={outcome} | settled={settled_result} | "
            f"exit={exit_reason} | filled_count={filled_count} | "
            f"BTC Δ={variation:.3f}% | side={side} | price={price}"
        )
    except Exception as e:
        logger.error(f"Failed to log market result: {e}")


def log_btc_tick(
    btc_price: float,
    ticker: str,
    seconds_left: float,
    baseline: float,
    sess: str,
):
    """
    Log BTC price observation to btc_prices.csv.
    #4: now called every cycle regardless of whether we are inside
    the trading window, so σ calibration uses full-candle data.
    """
    try:
        now      = datetime.now(timezone.utc)
        utc_hour = now.hour
        variation_pct = round((btc_price - baseline) / baseline * 100, 4)

        file_exists = os.path.exists(PRICES_FILE)
        with open(PRICES_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "utc_hour", "session",
                    "ticker", "seconds_left", "btc_price",
                    "baseline_btc", "variation_pct"
                ])
            writer.writerow([
                now.isoformat(),
                utc_hour,
                sess,
                ticker,
                round(seconds_left, 1),
                btc_price,
                baseline,
                variation_pct
            ])
    except Exception as e:
        logger.error(f"Failed to log BTC tick: {e}")


# ===================== STATE =====================
current_ticker      = None
market_baseline_btc = None
market_session      = None
traded_this_market  = False
last_side           = None
last_price          = None
last_filled_count   = 0      # #5: actual contracts filled

# Take profit state
position_open        = False
position_entry_price = None
position_took_profit = False


# ===================== STARTUP ======================
calibrate_session_sigma()

logger.info("Threshold curve at startup:")
logger.info(f"  {'Session':<10} {'Time left':>10} | {'Threshold':>10}")
logger.info(f"  {'-'*36}")
_startup_sess = _get_session(datetime.now(timezone.utc).hour)
for t_s in [480, 360, 300, 240, 180, 120, 60, 30]:
    thr = get_threshold(t_s, _startup_sess)
    logger.info(f"  {_startup_sess:<10} {t_s:>9}s | {thr:>9.3f}%")


# ===================== MAIN LOOP =====================
logger.info("Bot started — monitoring KXBTC15M markets...")

while True:
    market = get_current_market()
    if not market:
        logger.info("No open KXBTC15M market found. Waiting...")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    ticker         = market.get("ticker")
    close_time_str = market.get("close_time")
    floor_strike   = (
        market.get("floor_strike") or
        market.get("result_sources", [{}])[0].get("floor_strike")
    )

    if not ticker or not close_time_str:
        logger.warning("Market missing ticker or close_time")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Detect new market ──
    if ticker != current_ticker:

        # Log previous market (only if not already logged via take profit)
        if current_ticker and market_baseline_btc and market_session:
            if not position_took_profit:
                settled_result = get_market_settlement(current_ticker)

                if settled_result in ("yes", "no") and last_side:
                    outcome = (
                        "win" if settled_result == last_side else "loss"
                    )
                elif last_side:
                    outcome = "unknown"
                else:
                    outcome        = "no_trade"
                    settled_result = settled_result or "unknown"

                final_btc = get_btc_price() or market_baseline_btc
                final_variation = (
                    (final_btc - market_baseline_btc)
                    / market_baseline_btc * 100
                )

                log_market_result(
                    ticker=current_ticker,
                    baseline=market_baseline_btc,
                    final_btc=final_btc,
                    variation=final_variation,
                    side=last_side or "none",
                    price=last_price or 0.0,
                    filled=traded_this_market,
                    filled_count=last_filled_count,   # #5
                    outcome=outcome,
                    settled_result=settled_result or "unknown",
                    sess=market_session,
                    exit_reason="expiry",
                )

        calibrate_session_sigma()

        # Reset state
        current_ticker       = ticker
        traded_this_market   = False
        market_baseline_btc  = None
        last_side            = None
        last_price           = None
        last_filled_count    = 0      # #5
        position_open        = False
        position_entry_price = None
        position_took_profit = False

        # Lock session at candle open
        market_session = _get_session(datetime.now(timezone.utc).hour)

        try:
            baseline = float(floor_strike) if floor_strike else None
        except (TypeError, ValueError):
            baseline = None

        if baseline:
            market_baseline_btc = baseline
            sigma, k, alpha = SESSION_SIGMA[market_session]
            logger.info(
                f"📌 New market: {ticker} | "
                f"BTC baseline: ${baseline:,.2f} | "
                f"session={market_session} | "
                f"σ={sigma:.6f} k={k:.2f} α={alpha:.3f}"
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

    # ── Fetch prices every cycle (#4: needed for full-candle logging) ──
    btc_price = get_btc_price()
    yes_price, no_price = get_orderbook_prices(ticker)

    # #4: log BTC tick for the ENTIRE candle, not just trading window
    if btc_price and market_baseline_btc and market_session:
        log_btc_tick(
            btc_price, ticker, seconds_left,
            market_baseline_btc, market_session
        )

    threshold = get_threshold(seconds_left, market_session or "us")

    logger.info(
        f"Market: {ticker} | {seconds_left:.1f}s left | "
        f"Threshold: {f'{threshold:.3f}%' if threshold else '—'} | "
        f"Ceiling: {PRICE_CEILING} | Traded: {traded_this_market}"
    )

    # ══════════════════════════════════════════
    # TAKE PROFIT CHECK
    # Runs every tick while a position is open.
    # ══════════════════════════════════════════
    if position_open and not position_took_profit:
        current_price = (
            yes_price if last_side == "yes" else no_price
        )
        price_change = current_price - position_entry_price

        logger.info(
            f"  📊 Position: {last_side.upper()} | "
            f"Entry: {position_entry_price:.4f} | "
            f"Current: {current_price:.4f} | "
            f"P&L: {price_change:+.4f} | "
            f"TP target: +{TAKE_PROFIT_CENTS:.2f}"
        )

        if price_change >= TAKE_PROFIT_CENTS:
            logger.info(
                f"  🎯 Take profit triggered: "
                f"+{price_change:.4f} ≥ +{TAKE_PROFIT_CENTS:.2f}"
            )

            if not DRY_RUN:
                # #5: use actual filled count for the sell order
                sold, _ = place_market_order(
                    market_ticker=ticker,
                    side=last_side,
                    count=last_filled_count,
                    price=current_price,
                    action="sell"
                )
            else:
                sold = True
                logger.info("DRY RUN — take profit sell simulated")

            if sold:
                position_open        = False
                position_took_profit = True

                final_btc = btc_price or market_baseline_btc
                final_variation = (
                    (final_btc - market_baseline_btc)
                    / market_baseline_btc * 100
                ) if market_baseline_btc else 0.0

                log_market_result(
                    ticker=ticker,
                    baseline=market_baseline_btc or 0.0,
                    final_btc=final_btc or 0.0,
                    variation=final_variation,
                    side=last_side,
                    price=last_price,
                    filled=True,
                    filled_count=last_filled_count,   # #5
                    outcome="win",
                    settled_result="take_profit",
                    sess=market_session or "us",
                    exit_reason="take_profit",
                )
                logger.info(
                    f"✅ Take profit exit | "
                    f"entry={position_entry_price:.4f} | "
                    f"exit={current_price:.4f} | "
                    f"gain={price_change:+.4f} | "
                    f"contracts={last_filled_count}"
                )
            else:
                logger.warning(
                    "⚠️ Take profit sell failed — will retry next tick"
                )

        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Outside trading window ──
    if threshold is None:
        if seconds_left > TRIGGER_AT_SECONDS:
            logger.info(
                f"  ⏳ {seconds_left:.0f}s left — "
                f"waiting for <8 min window to open"
            )
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Already traded this market ──
    if traded_this_market:
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    # ── Need BTC baseline ──
    if not market_baseline_btc:
        logger.warning("No BTC baseline available — skipping")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    if btc_price is None:
        logger.warning("Could not fetch BTC price — skipping this cycle")
        time.sleep(CHECK_INTERVAL_SECONDS)
        continue

    btc_variation = (
        (btc_price - market_baseline_btc) / market_baseline_btc * 100
    )

    logger.info(
        f"  BTC: ${btc_price:,.2f} | "
        f"Baseline: ${market_baseline_btc:,.2f} | "
        f"Variation: {btc_variation:.3f}% | "
        f"YES ask: {yes_price:.4f} | NO ask: {no_price:.4f}"
    )

    # ── Determine trade direction ──
    side  = None
    price = None

    if btc_variation >= threshold:
        if PRICE_FLOOR <= yes_price <= PRICE_CEILING:
            side, price = "yes", yes_price
        else:
            logger.info(
                f"  📈 BTC up {btc_variation:.3f}% but "
                f"YES ask {yes_price:.4f} outside "
                f"[{PRICE_FLOOR}–{PRICE_CEILING}]"
            )

    elif btc_variation <= -threshold:
        if PRICE_FLOOR <= no_price <= PRICE_CEILING:
            side, price = "no", no_price
        else:
            logger.info(
                f"  📉 BTC down {btc_variation:.3f}% but "
                f"NO ask {no_price:.4f} outside "
                f"[{PRICE_FLOOR}–{PRICE_CEILING}]"
            )

    else:
        logger.info(
            f"  BTC Δ={btc_variation:.3f}% — "
            f"below threshold {threshold:.3f}%"
        )

    # ── Place order ──
    if side and price and price > 0:
        last_side  = side
        last_price = price

        kelly_fraction = calculate_kelly_fraction(side, price)
        balance_cents  = get_balance_cents()
        risk_dollars   = min(
            (balance_cents / 100.0) * kelly_fraction,
            MAX_ORDER_DOLLARS
        )
        count = max(1, int(risk_dollars * 100) // int(price * 100))

        logger.info(
            f"🚀 SIGNAL: {side.upper()} | "
            f"BTC Δ={btc_variation:.3f}% | "
            f"threshold={threshold:.3f}% | "
            f"price={price:.4f} | "
            f"kelly={kelly_fraction:.4f} | "
            f"b={(1-price)/price:.4f} | "
            f"contracts={count} | "
            f"risk=${risk_dollars:.2f} | "
            f"Dry-run: {DRY_RUN}"
        )

        if not DRY_RUN:
            # #5: unpack (success, filled_count)
            filled, filled_count = place_market_order(
                ticker, side, count, price, action="buy"
            )
            traded_this_market = True

            if filled and filled_count > 0:
                last_filled_count    = filled_count   # #5
                position_open        = True
                position_entry_price = price
                logger.info(
                    f"✅ Trade complete — "
                    f"filled {filled_count}/{count} contracts | "
                    f"monitoring for take profit at +{TAKE_PROFIT_CENTS:.2f}"
                )
                if filled_count < count:
                    logger.warning(
                        f"  ⚠️ Partial fill: requested {count}, "
                        f"got {filled_count} — "
                        f"risk and Kelly sized for {filled_count}"
                    )
            else:
                logger.warning(
                    "⚠️ Order not confirmed filled — "
                    "will not retry this market"
                )
        else:
            logger.info("DRY RUN — order NOT placed")
            traded_this_market   = True
            last_filled_count    = count   # #5: simulate full fill in dry run
            position_open        = True
            position_entry_price = price

    time.sleep(CHECK_INTERVAL_SECONDS)
