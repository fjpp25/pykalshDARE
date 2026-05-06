"""
observer.py
-----------
Generic background data collector — run alongside main.py or standalone.

Polls all open markets for a given series every 30 seconds and writes:
  market_observations_<SERIES>.csv  — one row per market per poll cycle
  candle_settlements_<SERIES>.csv   — one row per settled candle

Supports any Kalshi 15-min series: KXBTC15M, KXETH15M, KXBNB15M, KXSOL15M.

Usage:
    python observer.py                     # defaults to KXBTC15M
    python observer.py KXETH15M
    python observer.py KXBNB15M

Run multiple instances simultaneously in separate terminals — each writes
to its own output files and operates completely independently.
"""

import sys
import time
import csv
import os
import logging
from datetime import datetime, timezone
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True)

from kalshi_common import (
    get_spot_price, get_session_name, get_orderbook_prices,
    get_open_markets, kalshi_get,
)

# ========================= CONFIG =========================
SERIES_TICKER    = sys.argv[1].upper() if len(sys.argv) > 1 else "KXBTC15M"
POLL_INTERVAL    = 30    # seconds between observation cycles
SETTLEMENT_DELAY = 45    # seconds after close before first fetch attempt
SETTLEMENT_RETRIES   = 6
SETTLEMENT_RETRY_GAP = 30

# Output files are named after the series
OBSERVATIONS_FILE = f"market_observations_{SERIES_TICKER}.csv"
SETTLEMENTS_FILE  = f"candle_settlements_{SERIES_TICKER}.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ========================= CANDLE STATE =========================
candle_state: dict[str, dict] = {}
pending_settlement: dict[str, dict] = {}


def _new_candle(ticker: str, session_name: str, baseline: float) -> dict:
    return {
        "ticker":       ticker,
        "session":      session_name,
        "baseline":     baseline,
        "observations": [],
    }


def _record_observation(
    state: dict,
    seconds_left: float,
    variation: float,
    mid_yes: float,
    yes_ask: float,
    no_ask: float,
) -> None:
    state["observations"].append({
        "sl":      seconds_left,
        "var":     variation,
        "mid_yes": mid_yes,
        "yes_ask": yes_ask,
        "no_ask":  no_ask,
    })


def _compute_candle_summary(state: dict, settled_result: str) -> dict:
    obs = state["observations"]
    if not obs:
        return {}

    def phase_of(o: dict) -> str:
        sl = o["sl"]
        if sl >= 600:   return "early"
        elif sl >= 300: return "mid"
        else:           return "late"

    by_phase: dict[str, list] = defaultdict(list)
    for o in obs:
        by_phase[phase_of(o)].append(o)

    def phase_stats(rows: list, prefix: str) -> dict:
        if not rows:
            return {
                f"{prefix}_n":           0,
                f"{prefix}_var_max":     None,
                f"{prefix}_var_min":     None,
                f"{prefix}_mid_yes_max": None,
                f"{prefix}_mid_yes_min": None,
                f"{prefix}_mid_yes_first": None,
            }
        first = max(rows, key=lambda r: r["sl"])
        return {
            f"{prefix}_n":             len(rows),
            f"{prefix}_var_max":       round(max(r["var"] for r in rows), 4),
            f"{prefix}_var_min":       round(min(r["var"] for r in rows), 4),
            f"{prefix}_mid_yes_max":   round(max(r["mid_yes"] for r in rows), 4),
            f"{prefix}_mid_yes_min":   round(min(r["mid_yes"] for r in rows), 4),
            f"{prefix}_mid_yes_first": round(first["mid_yes"], 4),
        }

    final_obs = min(obs, key=lambda r: r["sl"])

    early_sorted = sorted(by_phase["early"], key=lambda r: r["sl"], reverse=True)
    first_extreme_sl  = None
    first_extreme_val = None
    for o in early_sorted:
        if o["mid_yes"] >= 0.85 or o["mid_yes"] <= 0.15:
            first_extreme_sl  = round(o["sl"], 1)
            first_extreme_val = round(o["mid_yes"], 4)
            break

    row: dict = {
        "ticker":          state["ticker"],
        "session":         state["session"],
        "baseline":        state["baseline"],
        "settled_result":  settled_result,
        "settled_at":      "",
        "n_obs_total":     len(obs),
        "first_obs_sl":    round(max(r["sl"] for r in obs), 1),
        "final_var":       round(final_obs["var"], 4),
        "final_mid_yes":   round(final_obs["mid_yes"], 4),
        "candle_var_max":  round(max(r["var"] for r in obs), 4),
        "candle_var_min":  round(min(r["var"] for r in obs), 4),
        "candle_mid_yes_max": round(max(r["mid_yes"] for r in obs), 4),
        "candle_mid_yes_min": round(min(r["mid_yes"] for r in obs), 4),
        "early_first_extreme_sl":  first_extreme_sl,
        "early_first_extreme_val": first_extreme_val,
    }
    for prefix in ("early", "mid", "late"):
        row.update(phase_stats(by_phase[prefix], prefix))

    return row


# ========================= LOGGING HELPERS =========================
def log_observation(row: dict) -> None:
    file_exists = os.path.exists(OBSERVATIONS_FILE)
    with open(OBSERVATIONS_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_candle_settlement(row: dict) -> None:
    if not row:
        return
    file_exists = os.path.exists(SETTLEMENTS_FILE)
    with open(SETTLEMENTS_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ========================= SETTLEMENT FETCH =========================
def fetch_settlement(ticker: str) -> str | None:
    try:
        data   = kalshi_get(f"/markets/{ticker}")
        result = data.get("market", {}).get("result", "")
        return result if result in ("yes", "no") else None
    except Exception as e:
        logger.debug(f"Settlement fetch error for {ticker}: {e}")
        return None


def process_pending_settlements() -> None:
    now      = time.time()
    resolved = []

    for ticker, entry in pending_settlement.items():
        if now < entry["first_attempt_at"]:
            continue
        if entry["attempts"] > 0:
            if now < entry["last_attempt_at"] + SETTLEMENT_RETRY_GAP:
                continue

        entry["attempts"]       += 1
        entry["last_attempt_at"] = now

        result = fetch_settlement(ticker)

        if result:
            summary = _compute_candle_summary(entry["state"], result)
            summary["settled_at"] = datetime.now(timezone.utc).isoformat()
            log_candle_settlement(summary)
            logger.info(
                f"  ✅ Settled: {ticker} → {result.upper()} | "
                f"final_var={summary.get('final_var'):+.4f}%"
            )
            resolved.append(ticker)
        elif entry["attempts"] >= SETTLEMENT_RETRIES:
            summary = _compute_candle_summary(entry["state"], "unknown")
            summary["settled_at"] = datetime.now(timezone.utc).isoformat()
            log_candle_settlement(summary)
            logger.warning(f"  ⚠️ Settlement unknown after {SETTLEMENT_RETRIES} attempts: {ticker}")
            resolved.append(ticker)
        else:
            logger.debug(
                f"  ⏳ Settlement pending: {ticker} "
                f"(attempt {entry['attempts']}/{SETTLEMENT_RETRIES})"
            )

    for ticker in resolved:
        del pending_settlement[ticker]


# ========================= MAIN LOOP =========================
logger.info(
    f"Observer started | series={SERIES_TICKER} | poll every {POLL_INTERVAL}s\n"
    f"  Observations → {OBSERVATIONS_FILE}\n"
    f"  Settlements  → {SETTLEMENTS_FILE}"
)

prev_open_tickers: set[str] = set()

while True:
    now     = datetime.now(timezone.utc)
    sess    = get_session_name(now.hour)
    markets = get_open_markets(SERIES_TICKER)
    spot    = get_spot_price(SERIES_TICKER)

    if not markets:
        logger.warning("No open markets found — waiting...")
        process_pending_settlements()
        time.sleep(POLL_INTERVAL)
        continue

    if spot is None:
        logger.warning("Could not fetch spot price — skipping cycle")
        process_pending_settlements()
        time.sleep(POLL_INTERVAL)
        continue

    current_open_tickers: set[str] = set()

    for market in markets:
        ticker         = market.get("ticker", "")
        close_time_str = market.get("close_time", "")
        floor_strike   = (
            market.get("floor_strike")
            or market.get("result_sources", [{}])[0].get("floor_strike")
        )

        if not ticker or not close_time_str or not floor_strike:
            continue

        try:
            baseline = float(floor_strike)
        except (TypeError, ValueError):
            continue

        try:
            close_dt     = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            seconds_left = (close_dt - now).total_seconds()
        except Exception:
            continue

        if seconds_left < 0:
            continue

        current_open_tickers.add(ticker)

        if ticker not in candle_state:
            candle_state[ticker] = _new_candle(ticker, sess, baseline)
            logger.info(f"📌 New candle: {ticker} | baseline={baseline:,.2f}")

        yes_ask, no_ask = get_orderbook_prices(ticker)
        variation       = (spot - baseline) / baseline * 100
        spread          = round(yes_ask + no_ask, 4)
        mid_yes         = (
            round(yes_ask / (yes_ask + no_ask), 4)
            if (yes_ask + no_ask) > 0 else 0.0
        )
        minutes_left = round(seconds_left / 60.0, 2)

        _record_observation(
            candle_state[ticker],
            seconds_left, variation, mid_yes, yes_ask, no_ask,
        )

        obs_row = {
            "timestamp":   now.isoformat(),
            "utc_hour":    now.hour,
            "session":     sess,
            "series":      SERIES_TICKER,
            "ticker":      ticker,
            "seconds_left": round(seconds_left, 1),
            "minutes_left": minutes_left,
            "baseline":    round(baseline, 4),
            "spot_price":  round(spot, 4),
            "variation":   round(variation, 4),
            "yes_ask":     yes_ask,
            "no_ask":      no_ask,
            "spread":      spread,
            "mid_yes":     mid_yes,
            "candle_phase": (
                "early"  if seconds_left > 600 else
                "middle" if seconds_left > 300 else
                "late"
            ),
        }
        log_observation(obs_row)
        logger.info(
            f"{ticker} | {minutes_left:.1f}min | "
            f"Δ={variation:+.3f}% | "
            f"mid_yes={mid_yes:.3f} | "
            f"YES={yes_ask:.3f} NO={no_ask:.3f}"
        )

    # Detect newly closed markets
    newly_closed = prev_open_tickers - current_open_tickers
    for ticker in newly_closed:
        if ticker in candle_state and ticker not in pending_settlement:
            logger.info(
                f"🔒 Closed: {ticker} — "
                f"settlement fetch in {SETTLEMENT_DELAY}s | "
                f"{len(candle_state[ticker]['observations'])} obs recorded"
            )
            pending_settlement[ticker] = {
                "state":            candle_state.pop(ticker),
                "first_attempt_at": time.time() + SETTLEMENT_DELAY,
                "last_attempt_at":  0.0,
                "attempts":         0,
            }

    prev_open_tickers = current_open_tickers
    process_pending_settlements()
    time.sleep(POLL_INTERVAL)
