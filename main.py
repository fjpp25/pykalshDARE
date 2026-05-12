"""
main.py
-------
Kalshi KXBTC15M threshold momentum bot.

Strategy (derived from backtesting 2,070 contracts, Apr–May 2026):
  For each active 15-minute BTC candle, compute how far BTC has moved
  from the candle's opening price (btc_variation %). If that move
  exceeds a time-dependent threshold, the direction is overwhelmingly
  likely to hold through settlement.

  btc_variation = (btc_price - floor_strike) / floor_strike * 100

  Threshold table — aggressive set (historical win rates from backtest):
    0 – 30s  remaining : |variation| >= 0.015 %  → 100.0 %
    30 – 60s            : |variation| >= 0.070 %  →  97.7 %
    1 – 2min            : |variation| >= 0.090 %  →  99.1 %
    2 – 3min            : |variation| >= 0.150 %  →  99.6 %
    3 – 5min            : |variation| >= 0.210 %  →  98.9 %
    5 – 15min           : |variation| >= 0.350 %  →  96.8 %

  variation > 0 and above threshold  → ENTER YES
  variation < 0 and below -threshold → ENTER NO
  otherwise                          → WAIT

One entry per contract. Position held to settlement (no take-profit exit).
Fixed risk sizing: FIXED_RISK_DOLLARS per trade (~3-4 contracts at typical entry prices).
Kelly criterion is implemented but commented out — re-enable once confidence is established.

DRY_RUN = True by default. The monitor's Start Trading toggle controls live execution.
To run main.py in live mode directly, set DRY_RUN = False in this file.

Run:
    export KALSHI_API_KEY="your-key-uuid"
    export KALSHI_KEY_PATH="/path/to/chave2.pem"
    # optional: export KALSHI_DEMO=true
    python main.py
"""

import time
import logging
import sys
import math
import requests
import uuid
import csv
import os
from datetime import datetime, timezone
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True)

from kalshi_common import (
    sign_request, get_btc_price, get_session_name,
    get_orderbook_prices, get_open_markets, kalshi_get,
    BASE_URL, SERIES_TICKER, session,
)

# ========================= CONFIG =========================
USE_DEMO = os.environ.get("KALSHI_DEMO", "false").lower() == "true"

# Kill switch. Defaults to True (safe).
# Set KALSHI_DRY_RUN=false in your environment or .env to enable live orders.
# The monitor's Start Trading toggle also controls this for its own BotWorker.
DRY_RUN = os.environ.get("KALSHI_DRY_RUN", "true").lower() != "false"

# Strategy parameters — single source of truth in config.py
from config import (
    FIXED_RISK_DOLLARS, MAX_CONTRACTS, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE,
    DAILY_LOSS_LIMIT, get_threshold,
)

# ---- Kelly criterion (commented out — re-enable after confidence established) ----
# KELLY_MIN_SAMPLES  = 30
# KELLY_FALLBACK     = 0.15
# KELLY_MIN_FRACTION = 0.05
# KELLY_MAX_FRACTION = 0.40

# Settlement fetching
SETTLEMENT_RETRIES    = 6
SETTLEMENT_RETRY_WAIT = 5

# σ calibration
SESSION_SIGMA: dict[str, tuple[float, float, float]] = {
    "asia":     (0.000301, 1.23, 0.50),
    "europe":   (0.000256, 1.23, 0.50),
    "us":       (0.000491, 1.40, 0.50),
    "us_close": (0.000491, 1.40, 0.50),
}
SIGMA_MIN_SAMPLES = 200

RESULTS_FILE = "trade_results.csv"
PRICES_FILE  = "btc_prices.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)
logger.info(f"Starting bot in {'DEMO' if USE_DEMO else 'LIVE'} mode | DRY_RUN={DRY_RUN}")


# ====================== THRESHOLD SIGNAL ======================



def get_signal(btc_variation: float, seconds_left: float) -> tuple[str | None, float]:
    """
    Evaluate the threshold momentum signal.

    Returns (side, threshold) where side is "yes", "no", or None (no signal).
    btc_variation is in percent (e.g. 0.15 means +0.15%).
    """
    threshold = get_threshold(seconds_left)
    if btc_variation >= threshold:
        return "yes", threshold
    if btc_variation <= -threshold:
        return "no", threshold
    return None, threshold


# ====================== SETTLEMENT FETCHING ======================

def get_market_settlement(ticker: str) -> str | None:
    """
    Fetch the settled result ("yes" or "no") from Kalshi.
    Retries several times since settlement can lag market close by seconds.
    """
    path = f"/trade-api/v2/markets/{ticker}"
    for attempt in range(1, SETTLEMENT_RETRIES + 1):
        try:
            headers     = sign_request("GET", path)
            resp        = session.get(
                f"{BASE_URL}/markets/{ticker}",
                headers=headers,
                timeout=8,
            )
            resp.raise_for_status()
            market_data = resp.json().get("market", {})
            result      = market_data.get("result")

            if result in ("yes", "no"):
                logger.info(f"  ✅ Settlement {ticker}: {result.upper()} (attempt {attempt})")
                return result

            status = market_data.get("status", "unknown")
            logger.info(
                f"  ⏳ Settlement not ready for {ticker} "
                f"(status={status}, attempt {attempt}/{SETTLEMENT_RETRIES})"
            )
        except Exception as e:
            logger.error(f"  Settlement fetch error {ticker} (attempt {attempt}): {e}")

        if attempt < SETTLEMENT_RETRIES:
            time.sleep(SETTLEMENT_RETRY_WAIT)

    logger.warning(f"  ⚠️ Could not fetch settlement for {ticker} after {SETTLEMENT_RETRIES} attempts")
    return None


# ====================== SIGMA CALIBRATION ======================
# Kept from original to maintain btc_prices.csv logging continuity.

def calibrate_session_sigma() -> None:
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
                continue

            current_sigma, current_k, current_alpha = SESSION_SIGMA[sess]
            minute_returns: list[float] = []
            by_ticker: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                by_ticker[r["ticker"]].append(r)

            for ticker_rows in by_ticker.values():
                ticker_rows.sort(key=lambda x: x["seconds_left"], reverse=True)
                prices = [r["btc_price"] for r in ticker_rows]
                times  = [r["seconds_left"] for r in ticker_rows]
                for i in range(1, len(prices)):
                    dt = times[i - 1] - times[i]
                    if 50 <= dt <= 70:
                        ret = (prices[i] - prices[i - 1]) / prices[i - 1]
                        minute_returns.append(ret)

            if len(minute_returns) < 10:
                continue

            n        = len(minute_returns)
            mean_r   = sum(minute_returns) / n
            variance = sum((r - mean_r) ** 2 for r in minute_returns) / (n - 1)
            sigma_fit = math.sqrt(variance)

            alpha_data = [
                r for r in rows
                if (900 - r["seconds_left"]) >= 120 and abs(r["variation"]) > 0.0001
            ]
            alpha_fit = current_alpha
            if len(alpha_data) >= 50:
                log_t = [math.log(max((900 - r["seconds_left"]) / 60.0, 0.1)) for r in alpha_data]
                log_v = [math.log(abs(r["variation"]) / current_k) for r in alpha_data]
                m     = len(log_t)
                s_t, s_v = sum(log_t), sum(log_v)
                s_tt = sum(x * x for x in log_t)
                s_tv = sum(x * y for x, y in zip(log_t, log_v))
                denom = m * s_tt - s_t * s_t
                if abs(denom) > 1e-12:
                    alpha_fit = max(0.3, min(1.5, (m * s_tv - s_t * s_v) / denom))

            sigma_fit = max(1e-5, min(0.005, sigma_fit))
            SESSION_SIGMA[sess] = (sigma_fit, current_k, alpha_fit)
            logger.info(
                f"  σ calibrated ({sess}): "
                f"σ {current_sigma:.6f}→{sigma_fit:.6f} | "
                f"α {current_alpha:.3f}→{alpha_fit:.3f}"
            )
    except Exception as e:
        logger.error(f"calibrate_session_sigma failed: {e}")


# ====================== KELLY CRITERION (disabled — re-enable when ready) ======================
# Uncomment these functions and the Kelly sizing block in the main loop
# once KELLY_MIN_SAMPLES real trades are in trade_results.csv.

# def estimate_win_probability(side: str) -> float | None:
#     """
#     Win probability from historical expiry-held results in trade_results.csv.
#     Excludes take-profit exits (would bias p upward).
#     Returns None if fewer than KELLY_MIN_SAMPLES observations.
#     """
#     if not os.path.exists(RESULTS_FILE):
#         return None
#     try:
#         with open(RESULTS_FILE, "r") as f:
#             rows = [
#                 r for r in csv.DictReader(f)
#                 if r.get("side") == side
#                 and r.get("filled", "").lower() == "true"
#                 and r.get("outcome") in ("win", "loss")
#                 and r.get("exit_reason", "expiry") != "take_profit"
#             ]
#         if len(rows) < KELLY_MIN_SAMPLES:
#             return None
#         wins = sum(1 for r in rows if r["outcome"] == "win")
#         return wins / len(rows)
#     except Exception as e:
#         logger.error(f"Win probability estimation failed: {e}")
#         return None
#
#
# def calculate_kelly_fraction(side: str, entry_price: float) -> float:
#     """
#     Half-Kelly using p (historical win rate from CSV) and b (payout odds).
#     Falls back to KELLY_FALLBACK when insufficient historical data.
#     """
#     win_prob = estimate_win_probability(side)
#
#     if win_prob is None:
#         logger.info(f"  Kelly ({side}): insufficient data — using fallback {KELLY_FALLBACK}")
#         return KELLY_FALLBACK
#
#     b = (1.0 - entry_price) / entry_price
#     if b <= 0:
#         return KELLY_FALLBACK
#
#     p = win_prob
#     q = 1.0 - p
#     kelly      = (b * p - q) / b
#     half_kelly = kelly / 2.0
#     capped     = max(KELLY_MIN_FRACTION, min(KELLY_MAX_FRACTION, half_kelly))
#
#     logger.info(
#         f"  Kelly ({side}): win_prob={win_prob:.2%} | entry={entry_price:.4f} | "
#         f"b={b:.4f} | raw={kelly:.4f} | half={half_kelly:.4f} | capped={capped:.4f}"
#     )
#     return capped


# ====================== ORDER PLACEMENT ======================

def get_balance_cents() -> int:
    try:
        path    = "/trade-api/v2/portfolio/balance"
        headers = sign_request("GET", path)
        resp    = session.get(f"{BASE_URL}/portfolio/balance", headers=headers, timeout=8)
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
    yes_bid: float,
    no_bid: float,
    action: str = "buy",
) -> tuple[bool, int]:
    """
    Place an aggressive limit IOC order that crosses the spread.

    Pricing:
      YES buy: bid at YES ask + 2¢  (YES ask ≈ 100 - no_bid_cents)
      NO buy:  bid at NO ask + 2¢   (in yes_price: yes_bid_cents - 2)
    """
    try:
        path    = "/trade-api/v2/portfolio/orders"
        headers = sign_request("POST", path)

        if action == "buy":
            if side == "yes":
                yes_ask_cents   = 100 - int(round(no_bid * 100))
                yes_price_cents = min(yes_ask_cents + 2, 99)
            else:
                yes_bid_cents   = int(round(yes_bid * 100))
                yes_price_cents = max(yes_bid_cents - 2, 1)
        else:
            # Sell — kept for completeness, not currently used.
            if side == "yes":
                yes_price_cents = max(int(round(yes_bid * 100)) - 3, 1)
            else:
                yes_price_cents = min(100 - int(round(no_bid * 100)) + 3, 99)

        implied = yes_price_cents / 100 if side == "yes" else (100 - yes_price_cents) / 100
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

        logger.info(
            f"Placing {action.upper()} {side.upper()} | yes_price={yes_price_cents}¢ "
            f"| implied={implied:.3f} | count={count}"
        )
        resp = session.post(
            f"{BASE_URL}/portfolio/orders",
            headers=headers, json=order_payload, timeout=10,
        )

        try:
            resp_body = resp.json()
        except Exception:
            resp_body = {}

        if not resp.ok:
            logger.error(f"Order POST failed: {resp.status_code} — {resp_body}")
            return False, 0

        order    = resp_body.get("order", {})
        order_id = order.get("order_id", "")
        status   = order.get("status", "unknown")
        logger.info(f"Order created → ID: {order_id} | Status: {status}")

        if not order_id:
            logger.error(f"No order_id in response: {resp_body}")
            return False, 0

        return _wait_for_fill(order_id)

    except requests.HTTPError as e:
        logger.error(f"Order HTTP error: {e.response.status_code} — {e.response.text}")
        return False, 0
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False, 0


def _wait_for_fill(order_id: str, timeout_seconds: int = 12) -> tuple[bool, int]:
    """Poll order status until filled, cancelled, or timeout."""
    path      = f"/trade-api/v2/portfolio/orders/{order_id}"
    deadline  = time.time() + timeout_seconds
    last_status = None

    while time.time() < deadline:
        try:
            headers = sign_request("GET", path)
            resp    = session.get(
                f"{BASE_URL}/portfolio/orders/{order_id}",
                headers=headers, timeout=8,
            )
            resp.raise_for_status()
            order  = resp.json().get("order", {})
            status = order.get("status")
            filled = int(order.get("filled_count", 0))

            if status != last_status:
                logger.info(f"Order {order_id[:8]}… → status={status} | filled={filled}")
                last_status = status

            if status in ("filled", "executed"):
                if filled > 0:
                    logger.info(f"✅ Filled: {filled} contracts")
                    return True, filled
            elif status in ("canceled", "cancelled"):
                if filled > 0:
                    logger.info(f"✅ Partial fill before cancel: {filled}")
                    return True, filled
                logger.warning(f"IOC cancelled with 0 fills at yes_price implied")
                return False, 0
            elif status == "rejected":
                reason = order.get("close_reason", "unknown")
                logger.warning(f"Order rejected: {reason}")
                return False, 0
            # "resting", "open", "pending" → keep polling

        except Exception as e:
            logger.error(f"Fill poll error: {e}")

        time.sleep(0.5)

    logger.warning(f"Fill timeout — last status: {last_status}")
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
    filled_count: int,
    outcome: str,
    settled_result: str,
    sess: str,
    exit_reason: str = "expiry",
    entry_seconds_left: float = 0.0,
    threshold_at_entry: float = 0.0,
) -> None:
    """Append one row per settled contract to trade_results.csv."""
    try:
        now      = datetime.now(timezone.utc)
        file_exists = os.path.exists(RESULTS_FILE)
        with open(RESULTS_FILE, "a", newline="") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_ALL)
            if not file_exists:
                writer.writerow([
                    "timestamp", "utc_hour", "session",
                    "ticker", "baseline_btc", "final_btc",
                    "variation_pct", "side", "price",
                    "filled", "filled_count",
                    "settled_result", "outcome", "exit_reason",
                    "entry_seconds_left", "threshold_at_entry",
                ])
            writer.writerow([
                now.isoformat(), now.hour, sess,
                ticker,
                round(baseline, 2), round(final_btc, 2),
                round(variation, 4),
                side, price, filled, filled_count,
                settled_result, outcome, exit_reason,
                round(entry_seconds_left, 1),
                round(threshold_at_entry, 4),
            ])
        logger.info(
            f"📊 Logged {ticker}: outcome={outcome} | settled={settled_result} | "
            f"exit={exit_reason} | filled={filled_count} | "
            f"Δ={variation:.3f}% | side={side} | price={price} | "
            f"entry@{entry_seconds_left:.0f}s | thresh={threshold_at_entry:.2f}%"
        )
    except Exception as e:
        logger.error(f"Failed to log market result: {e}")


def log_btc_tick(
    btc_price: float,
    ticker: str,
    seconds_left: float,
    baseline: float,
    sess: str,
) -> None:
    """Append a BTC price tick to btc_prices.csv (every loop cycle)."""
    try:
        now           = datetime.now(timezone.utc)
        variation_pct = round((btc_price - baseline) / baseline * 100, 4)
        file_exists   = os.path.exists(PRICES_FILE)
        with open(PRICES_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "utc_hour", "session",
                    "ticker", "seconds_left", "btc_price",
                    "baseline_btc", "variation_pct",
                ])
            writer.writerow([
                now.isoformat(), now.hour, sess,
                ticker, round(seconds_left, 1),
                btc_price, baseline, variation_pct,
            ])
    except Exception as e:
        logger.error(f"Failed to log BTC tick: {e}")


# ===================== STATE =====================
from dataclasses import dataclass, field

@dataclass
class BotState:
    """Encapsulates all mutable per-candle and per-session state."""
    # Current market
    current_ticker:      str | None  = None
    market_baseline_btc: float | None = None
    market_session:      str | None  = None

    # Per-candle trade state (reset on each new ticker)
    traded_this_market:  bool        = False
    last_side:           str | None  = None
    last_price:          float | None = None
    last_filled_count:   int         = 0
    entry_seconds_left:  float       = 0.0
    entry_threshold:     float       = 0.0

    # Daily loss circuit breaker
    daily_pnl_today:     float       = 0.0
    daily_pnl_date:      object      = field(
        default_factory=lambda: datetime.now(timezone.utc).date()
    )

    def reset_for_new_market(self, ticker: str, session: str):
        self.current_ticker      = ticker
        self.market_session      = session
        self.market_baseline_btc = None
        self.traded_this_market  = False
        self.last_side           = None
        self.last_price          = None
        self.last_filled_count   = 0
        self.entry_seconds_left  = 0.0
        self.entry_threshold     = 0.0

    def check_and_reset_daily(self) -> bool:
        """Reset daily P&L at UTC midnight. Returns True if reset occurred."""
        today = datetime.now(timezone.utc).date()
        if today != self.daily_pnl_date:
            self.daily_pnl_today = 0.0
            self.daily_pnl_date  = today
            return True
        return False

state = BotState()


# ===================== STARTUP ======================
calibrate_session_sigma()
logger.info(
    f"Threshold momentum bot started | "
    f"strategy: btc_variation vs time-based threshold | "
    f"sizing: fixed ${FIXED_RISK_DOLLARS}/trade (~{MAX_CONTRACTS} contracts max) | "
    f"daily loss limit: ${DAILY_LOSS_LIMIT} | "
    f"DRY_RUN={DRY_RUN}"
)


# ===================== MAIN LOOP =====================
logger.info("Monitoring KXBTC15M markets...")

while True:
    markets = get_open_markets()
    market  = markets[0] if markets else None

    if not market:
        logger.info("No open KXBTC15M market found. Waiting...")
        time.sleep(2)
        continue

    ticker         = market.get("ticker")
    close_time_str = market.get("close_time")
    floor_strike   = (
        market.get("floor_strike")
        or market.get("result_sources", [{}])[0].get("floor_strike")
    )

    if not ticker or not close_time_str:
        logger.warning("Market missing ticker or close_time")
        time.sleep(2)
        continue

    # ── Detect new market ──
    if ticker != state.current_ticker:

        # Log the previous market's result if we traded it.
        if state.current_ticker and state.market_baseline_btc and state.market_session and state.last_side:
            settled_result = get_market_settlement(state.current_ticker)
            outcome = (
                ("win" if settled_result == state.last_side else "loss")
                if settled_result in ("yes", "no")
                else "unknown"
            )
            final_btc = get_btc_price() or state.market_baseline_btc
            log_market_result(
                ticker=state.current_ticker,
                baseline=state.market_baseline_btc,
                final_btc=final_btc,
                variation=(final_btc - state.market_baseline_btc) / state.market_baseline_btc * 100,
                side=state.last_side,
                price=state.last_price or 0.0,
                filled=state.traded_this_market,
                filled_count=state.last_filled_count,
                outcome=outcome,
                settled_result=settled_result or "unknown",
                sess=state.market_session,
                exit_reason="expiry",
                entry_seconds_left=state.entry_seconds_left,
                threshold_at_entry=state.entry_threshold,
            )

            # Update daily P&L counter for circuit breaker.
            if outcome in ("win", "loss") and state.last_price and state.last_filled_count:
                trade_pnl = (
                    (1 - state.last_price) * state.last_filled_count if outcome == "win"
                    else -state.last_price * state.last_filled_count
                )
                state.daily_pnl_today += trade_pnl
                logger.info(
                    f"  Daily P&L updated: ${state.daily_pnl_today:+.2f} "
                    f"(limit: ${DAILY_LOSS_LIMIT})"
                )

        calibrate_session_sigma()

        # Reset state for the new contract.
        state.reset_for_new_market(
            ticker,
            get_session_name(datetime.now(timezone.utc).hour),
        )

        try:
            baseline = float(floor_strike) if floor_strike else None
        except (TypeError, ValueError):
            baseline = None

        if baseline:
            state.market_baseline_btc = baseline
            logger.info(f"📌 New market: {ticker} | baseline=${baseline:,.2f} | session={state.market_session}")
        else:
            logger.info(f"📌 New market: {ticker} | no floor_strike — will skip until baseline available")

    # ── Parse time remaining ──
    try:
        close_dt     = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        seconds_left = (close_dt - datetime.now(timezone.utc)).total_seconds()
    except Exception as e:
        logger.error(f"Time parsing error: {e}")
        time.sleep(2)
        continue

    # ── Skip if no baseline yet ──
    if not state.market_baseline_btc:
        logger.info(f"  No baseline for {ticker} — skipping cycle")
        time.sleep(2)
        continue

    # ── Fetch prices every cycle ──
    btc_price           = get_btc_price()
    yes_price, no_price = get_orderbook_prices(ticker)

    if not btc_price:
        logger.warning("BTC price unavailable — skipping cycle")
        time.sleep(2)
        continue

    # ── Compute btc_variation ──
    btc_variation = (btc_price - state.market_baseline_btc) / state.market_baseline_btc * 100

    # ── Log tick for σ calibration data ──
    log_btc_tick(btc_price, ticker, seconds_left, state.market_baseline_btc, state.market_session)

    # ── Compute market mid_yes (for logging only) ──
    spread  = yes_price + no_price
    mid_yes = round(yes_price / spread, 4) if spread > 0 else 0.5

    # Fallback: if orderbook returns zeros, estimate from mid_yes.
    # Suppress warning when seconds_left <= 0 — expected on expired markets.
    if yes_price <= 0 and no_price <= 0:
        if seconds_left > 5:
            logger.warning(
                f"Orderbook zeros for {ticker} at {seconds_left:.0f}s left — "
                f"using mid_yes fallback. Check orderbook field names in kalshi_common.py."
            )
        yes_price = mid_yes
        no_price  = round(1.0 - mid_yes, 4)

    # ── Evaluate threshold signal ──
    signal_side, threshold = get_signal(btc_variation, seconds_left)

    logger.info(
        f"Market: {ticker} | {seconds_left:.0f}s | "
        f"BTC ${btc_price:,.2f} | Δ={btc_variation:+.4f}% | "
        f"thresh=±{threshold:.2f}% | signal={signal_side or 'WAIT'} | "
        f"mid_yes={mid_yes:.3f} | traded={state.traded_this_market}"
    )

    # ── Already traded this contract ──
    if state.traded_this_market:
        time.sleep(2)
        continue

    # ── No signal ──
    if signal_side is None:
        time.sleep(2)
        continue

    # ── Signal fired — check daily loss limit, then size and place order ──
    entry_price = yes_price if signal_side == "yes" else no_price

    if entry_price <= 0:
        logger.warning(f"  Entry price is zero for {signal_side} — skipping")
        time.sleep(2)
        continue

    if entry_price < MIN_ENTRY_PRICE:
        logger.info(
            f"  Entry price ${entry_price:.4f} below MIN_ENTRY_PRICE "
            f"${MIN_ENTRY_PRICE} — market already priced against us, skipping"
        )
        time.sleep(2)
        continue

    if entry_price > MAX_ENTRY_PRICE:
        logger.info(
            f"  Entry price ${entry_price:.4f} exceeds MAX_ENTRY_PRICE "
            f"${MAX_ENTRY_PRICE} — skipping (margin too thin / no liquidity)"
        )
        time.sleep(2)
        continue

    # ── Daily loss circuit breaker ──
    if state.check_and_reset_daily():
        logger.info("🔄 New UTC day — daily P&L counter reset")

    if state.daily_pnl_today <= DAILY_LOSS_LIMIT:
        logger.warning(
            f"🛑 Daily loss limit reached (${state.daily_pnl_today:.2f} <= ${DAILY_LOSS_LIMIT}) — "
            f"no new entries until UTC midnight"
        )
        time.sleep(2)
        continue

    # ── Fixed position sizing ──
    count = max(1, min(
        int(FIXED_RISK_DOLLARS / entry_price),
        MAX_CONTRACTS,
    ))

    # ---- Kelly sizing (disabled) ----
    # kelly_fraction = calculate_kelly_fraction(signal_side, entry_price)
    # balance_cents  = get_balance_cents()
    # risk_dollars   = min(
    #     (balance_cents / 100.0) * kelly_fraction,
    #     MAX_ORDER_DOLLARS,
    # )
    # count = min(max(1, int(risk_dollars / entry_price)), MAX_CONTRACTS)

    logger.info(
        f"🚀 SIGNAL: {signal_side.upper()} | Δ={btc_variation:+.4f}% >= ±{threshold:.2f}% | "
        f"{seconds_left:.0f}s left | "
        f"price={entry_price:.4f} | contracts={count} | "
        f"risk=${count * entry_price:.2f} | "
        f"daily_pnl=${state.daily_pnl_today:+.2f} | DRY_RUN={DRY_RUN}"
    )

    # Record entry metadata.
    state.last_side          = signal_side
    state.last_price         = entry_price
    state.entry_seconds_left = seconds_left
    state.entry_threshold    = threshold

    if not DRY_RUN:
        filled, filled_count = place_market_order(
            ticker, signal_side, count, yes_price, no_price, action="buy"
        )
        state.traded_this_market = True

        if filled and filled_count > 0:
            state.last_filled_count = filled_count
            logger.info(
                f"✅ Filled {filled_count}/{count} contracts | "
                f"holding to settlement"
            )
            if filled_count < count:
                logger.warning(f"  ⚠️ Partial fill: requested {count}, got {filled_count}")
        else:
            logger.warning("⚠️ Order not filled — will not retry this market")
    else:
        state.traded_this_market = True
        state.last_filled_count  = count
        logger.info(f"DRY RUN — simulated {signal_side.upper()} x{count} @ {entry_price:.4f}")

    time.sleep(2)
