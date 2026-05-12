"""
config.py
---------
Single source of truth for all KXBTC15M strategy parameters.
Imported by both main.py and monitor.py — change here, affects both.

To run in live mode:
    Set KALSHI_DRY_RUN=false in your environment or .env file.
    main.py reads this directly; the monitor's Start Trading toggle
    overrides it for its own BotWorker instance.
"""

import os as _os

# ─── Position sizing ───────────────────────────────────────────────────────────
# Fixed-risk mode: risk a set dollar amount per trade.
# Switch to Kelly sizing once KELLY_MIN_SAMPLES real trades are logged.
FIXED_RISK_DOLLARS = 6.0    # dollars at risk per trade
MAX_CONTRACTS      = 8      # hard cap — safety net for very cheap entries
MIN_ENTRY_PRICE    = 0.50   # skip entries below this — market has already decided against us
MAX_ENTRY_PRICE    = 0.985  # skip entries above this — IOC liquidity dries up
DAILY_LOSS_LIMIT   = -20.0  # stop trading for the UTC day if P&L hits this

# ─── Kelly criterion (disabled) ───────────────────────────────────────────────
KELLY_MIN_SAMPLES  = 30     # minimum real trades before Kelly sizing activates
KELLY_MIN_FRACTION = 0.05
KELLY_MAX_FRACTION = 0.30

# ─── Threshold function ────────────────────────────────────────────────────────
def get_threshold(seconds_left: float) -> float:
    """
    Minimum |btc_variation %| required to fire a signal at a given time.

    Aggressive thresholds — fires earlier than the original set, producing
    lower entry prices with better orderbook liquidity and higher net P&L
    per trade. Win rates from backtest on 2,070 contracts (Apr–May 2026):

        0–30s    0.015 %  → 100.0 %
        30–60s   0.070 %  →  97.7 %
        1–2min   0.090 %  →  99.1 %
        2–3min   0.150 %  →  99.6 %
        3–5min   0.175 %  →  98.2 %
        5–15min  0.350 %  →  96.8 %
    """
    if seconds_left <= 30:  return 0.018
    if seconds_left <= 65:  return 0.068
    if seconds_left <= 125: return 0.085
    if seconds_left <= 180: return 0.145
    if seconds_left <= 305: return 0.170
    return 0.350


# ─── Threshold display table (for monitor UI) ─────────────────────────────────
# Kept in sync with get_threshold() above.
THRESHOLD_TABLE = [
    ("0 – 30s",   "0.015 %", "100.0 %"),
    ("30 – 60s",  "0.070 %",  "97.7 %"),
    ("1 – 2 min", "0.087 %",  "99.1 %"),
    ("2 – 3 min", "0.147 %",  "99.6 %"),
    ("3 – 5 min", "0.173 %",  "98.0 %"),
    ("5 – 15min", "0.350 %",  "96.8 %"),
]
