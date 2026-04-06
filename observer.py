import time
import requests
import csv
import os
import logging
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

# ========================= CONFIG =========================
SERIES_TICKER    = "KXBTC15M"
POLL_INTERVAL    = 30       # seconds between observations
OUTPUT_FILE      = "market_observations.csv"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)

session = requests.Session()

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


# ========================= HELPERS =========================
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
        logger.error(f"BTC price fetch failed: {e}")
        return None


def get_markets() -> list[dict]:
    """Fetch all open KXBTC15M markets."""
    try:
        resp = session.get(
            f"{BASE_URL}/markets",
            params={
                "series_ticker": SERIES_TICKER,
                "status": "open",
                "limit": 20
            },
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("markets", [])
    except Exception as e:
        logger.error(f"Market fetch failed: {e}")
        return []


def get_orderbook_prices(ticker: str) -> tuple[float, float]:
    """Get best YES/NO ask prices."""
    try:
        resp = session.get(
            f"{BASE_URL}/markets/{ticker}/orderbook",
            timeout=8
        )
        resp.raise_for_status()
        ob = resp.json().get("orderbook_fp", {})
        yes_levels = ob.get("yes_dollars", [])
        no_levels  = ob.get("no_dollars",  [])
        yes_ask = float(yes_levels[-1][0]) if yes_levels else 0.0
        no_ask  = float(no_levels[-1][0])  if no_levels  else 0.0
        return yes_ask, no_ask
    except Exception as e:
        logger.error(f"Orderbook fetch failed for {ticker}: {e}")
        return 0.0, 0.0


def log_observation(row: dict) -> None:
    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ========================= MAIN LOOP =========================
logger.info(
    f"Market observer started — polling every {POLL_INTERVAL}s"
)
logger.info(f"Writing to: {OUTPUT_FILE}")

while True:
    now      = datetime.now(timezone.utc)
    utc_hour = now.hour
    sess     = _get_session(utc_hour)

    markets  = get_markets()
    btc_price = get_btc_price()

    if not markets:
        logger.warning("No open markets found — waiting...")
        time.sleep(POLL_INTERVAL)
        continue

    if btc_price is None:
        logger.warning("Could not fetch BTC price — skipping cycle")
        time.sleep(POLL_INTERVAL)
        continue

    for market in markets:
        ticker         = market.get("ticker", "")
        close_time_str = market.get("close_time", "")
        floor_strike   = (
            market.get("floor_strike") or
            market.get("result_sources", [{}])[0].get("floor_strike")
        )

        if not ticker or not close_time_str or not floor_strike:
            continue

        try:
            baseline = float(floor_strike)
        except (TypeError, ValueError):
            continue

        try:
            close_dt     = datetime.fromisoformat(
                close_time_str.replace("Z", "+00:00")
            )
            seconds_left = (close_dt - now).total_seconds()
        except Exception:
            continue

        # Skip markets that have already closed
        if seconds_left < 0:
            continue

        yes_ask, no_ask = get_orderbook_prices(ticker)
        btc_variation   = (btc_price - baseline) / baseline * 100
        spread          = round(yes_ask + no_ask, 4)
        mid_yes         = round(yes_ask / (yes_ask + no_ask), 4) if (yes_ask + no_ask) > 0 else 0.0
        minutes_left    = round(seconds_left / 60.0, 2)

        row = {
            "timestamp":      now.isoformat(),
            "utc_hour":       utc_hour,
            "session":        sess,
            "ticker":         ticker,
            "seconds_left":   round(seconds_left, 1),
            "minutes_left":   minutes_left,
            "baseline_btc":   round(baseline, 2),
            "btc_price":      btc_price,
            "btc_variation":  round(btc_variation, 4),
            "yes_ask":        yes_ask,
            "no_ask":         no_ask,
            "spread":         spread,          # yes + no (should be ~1.0, >1.0 = market maker profit)
            "mid_yes":        mid_yes,         # yes / (yes + no) — normalised yes probability
            "candle_phase":   (                # which third of the candle are we in
                "early"  if seconds_left > 600 else
                "middle" if seconds_left > 300 else
                "late"
            ),
        }

        log_observation(row)
        logger.info(
            f"{ticker} | {minutes_left:.1f}min left | "
            f"BTC Δ={btc_variation:+.3f}% | "
            f"YES={yes_ask:.3f} NO={no_ask:.3f} | "
            f"spread={spread:.3f} | phase={row['candle_phase']}"
        )

    time.sleep(POLL_INTERVAL)