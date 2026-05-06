"""
monitor.py
----------
PyQt6 desktop monitoring app for the KXBTC15M threshold momentum bot.

Reads from:
    trade_results.csv   — settled trade log written by main.py
    btc_prices.csv      — BTC tick log written by main.py

Polls live (no credentials needed):
    Binance API         — real-time BTC/USDT price
    Kalshi public API   — current open KXBTC15M markets + orderbook
                          (authenticated calls optional, gracefully skipped)

Run:
    python monitor.py

Optional — for authenticated Kalshi calls (balance, live fills):
    export KALSHI_API_KEY="your-key-uuid"
    export KALSHI_KEY_PATH="/path/to/chave2.pem"
"""

import os
import sys
import csv
import time
import math
import requests
import traceback
from datetime import datetime, timezone
from collections import defaultdict

import pandas as pd
import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QSplitter, QScrollArea, QSizePolicy,
    QAbstractItemView, QProgressBar, QPushButton, QTextEdit,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize,
)
from PyQt6.QtGui import QColor, QFont, QPalette, QBrush

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
RESULTS_FILE  = "trade_results.csv"
PRICES_FILE   = "btc_prices.csv"
POLL_INTERVAL = 2000          # ms between live data refreshes
CHART_REFRESH = 10000         # ms between chart redraws
CSV_REFRESH   = 5000          # ms between CSV reloads
PNL_REFRESH   = 60000         # ms between Kalshi P&L fetches

KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXBTC15M"

# Strategy parameters — single source of truth in config.py
from config import (
    FIXED_RISK_DOLLARS, MAX_CONTRACTS, MAX_ENTRY_PRICE,
    DAILY_LOSS_LIMIT, get_threshold, THRESHOLD_TABLE,
)

def get_signal(variation: float, seconds_left: float) -> str:
    th = get_threshold(seconds_left)
    if variation >= th:  return "YES"
    if variation <= -th: return "NO"
    return "WAIT"

def fmt_seconds(s: float) -> str:
    s = max(0, int(s))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


# ─────────────────────────────────────────────────────────────
# STYLESHEET
# ─────────────────────────────────────────────────────────────
DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #0c0e12;
    color: #e0e2e8;
    font-family: 'SF Mono', 'Consolas', 'Courier New', monospace;
    font-size: 12px;
}
QTabWidget::pane {
    border: 1px solid #1e2330;
    background: #0c0e12;
    border-radius: 6px;
}
QTabBar::tab {
    background: #13161c;
    color: #6b7280;
    padding: 8px 18px;
    border: 1px solid #1e2330;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    font-size: 11px;
    letter-spacing: 0.06em;
}
QTabBar::tab:selected {
    background: #1a1e27;
    color: #e0e2e8;
    border-color: #2a2f3e;
}
QTabBar::tab:hover { color: #c0c4d0; }
QTableWidget {
    background-color: #0f1218;
    gridline-color: #1a1e27;
    border: none;
    selection-background-color: #1e2840;
}
QTableWidget::item {
    padding: 5px 8px;
    border-bottom: 1px solid #151820;
    color: #c8cad4;
}
QHeaderView::section {
    background-color: #13161c;
    color: #6b7280;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid #1e2330;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
QScrollBar:vertical {
    background: #0f1218;
    width: 6px;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: #2a2f3e;
    border-radius: 3px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QSplitter::handle { background: #1a1e27; }
QProgressBar {
    background: #13161c;
    border: none;
    border-radius: 2px;
    height: 4px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    border-radius: 2px;
    background: #8b5cf6;
}
"""

# ─────────────────────────────────────────────────────────────
# MATPLOTLIB DARK CONFIG
# ─────────────────────────────────────────────────────────────
CHART_BG   = "#0c0e12"
CHART_SURF = "#13161c"
CHART_GRID = "#1a1e27"
CHART_TEXT = "#6b7280"
C_YES      = "#10b981"
C_NO       = "#3b82f6"
C_WARN     = "#f59e0b"
C_MUTED    = "#374151"

def apply_dark_axes(ax, fig):
    fig.patch.set_facecolor(CHART_BG)
    ax.set_facecolor(CHART_SURF)
    ax.tick_params(colors=CHART_TEXT, labelsize=9)
    ax.xaxis.label.set_color(CHART_TEXT)
    ax.yaxis.label.set_color(CHART_TEXT)
    for spine in ax.spines.values():
        spine.set_edgecolor(CHART_GRID)
    ax.grid(color=CHART_GRID, linewidth=0.5, alpha=0.6)


# ─────────────────────────────────────────────────────────────
# BACKGROUND DATA WORKER
# ─────────────────────────────────────────────────────────────
class DataWorker(QThread):
    """Polls Binance + Kalshi in a background thread. Emits signals to the UI."""
    btc_updated    = pyqtSignal(float)
    market_updated = pyqtSignal(dict)
    balance_updated= pyqtSignal(float)
    pnl_updated    = pyqtSignal(float, list)  # (total_net_pnl, list of trade dicts)
    error          = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running   = True
        self._session   = requests.Session()
        self._kalshi_ok = False
        self._sign      = None
        self._base      = KALSHI_BASE

        try:
            from kalshi_common import sign_request, BASE_URL as _bu
            self._sign      = sign_request
            self._base      = _bu
            self._kalshi_ok = True
        except Exception:
            pass

    def stop(self):
        self._running = False

    def run(self):
        cycle = 0
        while self._running:
            try:
                self._poll_btc()          # every 2s — Coinbase/Kraken/Bitstamp fallback chain
            except Exception as e:
                self.error.emit(f"BTC poll: {e}")

            if cycle % 10 == 0:           # every 20s — market changes every 15min
                try:
                    self._poll_market()
                except Exception as e:
                    self.error.emit(f"Market poll: {e}")

            if cycle % 30 == 0 and self._kalshi_ok:   # every 60s
                try:
                    self._poll_balance()
                except Exception:
                    pass

            if (cycle == 0 or cycle % 60 == 0) and self._kalshi_ok:   # immediately + every 2min
                try:
                    self._poll_pnl_since_may()
                except Exception as e:
                    self.error.emit(f"P&L poll: {e}")

            cycle += 1
            time.sleep(2)

    def _poll_btc(self):
        try:
            from kalshi_common import get_btc_price
            price = get_btc_price()
        except Exception:
            price = self._fetch_btc_price()
        if price:
            self.btc_updated.emit(price)

    def _fetch_btc_price(self) -> float | None:
        """HTTP fallback — used only if kalshi_common import fails."""
        for url, parse, params in [
            ("https://api.coinbase.com/v2/prices/BTC-USD/spot",
             lambda r: float(r.json()["data"]["amount"]), {}),
            ("https://api.kraken.com/0/public/Ticker",
             lambda r: float(next(iter(r.json()["result"].values()))["c"][0]),
             {"pair": "XBTUSD"}),
            ("https://www.bitstamp.net/api/v2/ticker/btcusd/",
             lambda r: float(r.json()["last"]), {}),
        ]:
            try:
                r = self._session.get(url, params=params, timeout=5)
                r.raise_for_status()
                return parse(r)
            except Exception:
                continue
        return None

    def _poll_market(self):
        try:
            r = self._session.get(
                f"{self._base}/markets",
                params={"series_ticker": SERIES_TICKER, "status": "open", "limit": 5},
                timeout=6,
            )
            r.raise_for_status()
            markets = r.json().get("markets", [])
            if not markets:
                self.market_updated.emit({})
                return
            markets.sort(key=lambda m: m.get("close_time", "9999"))
            mkt = markets[0]
            ticker = mkt.get("ticker", "")
            try:
                ob_r = self._session.get(
                    f"{self._base}/markets/{ticker}/orderbook", timeout=4
                )
                ob_r.raise_for_status()
                ob = ob_r.json().get("orderbook_fp", {})
                yes_levels = (ob.get("yes_dollars") or ob.get("yes") or [])
                no_levels  = (ob.get("no_dollars")  or ob.get("no")  or [])
                yes_bid = float(yes_levels[-1][0]) if yes_levels else 0.0
                no_bid  = float(no_levels[-1][0])  if no_levels  else 0.0
                mkt["yes_bid"] = yes_bid
                mkt["no_bid"]  = no_bid
                spread = yes_bid + no_bid
                mkt["mid_yes"] = round(yes_bid / spread, 4) if spread > 0 else 0.5
            except Exception:
                mkt["yes_bid"] = mkt["no_bid"] = mkt["mid_yes"] = 0.0
            self.market_updated.emit(mkt)
        except Exception:
            self.market_updated.emit({})

    def _poll_balance(self):
        if not self._sign:
            return
        try:
            path    = "/trade-api/v2/portfolio/balance"
            headers = self._sign("GET", path)
            r = self._session.get(f"{self._base}/portfolio/balance",
                                  headers=headers, timeout=6)
            r.raise_for_status()
            self.balance_updated.emit(r.json().get("balance", 0) / 100.0)
        except Exception:
            pass

    def _poll_pnl_since_may(self):
        """
        Fetch KXBTC15M settlements from May 1 2026 onwards using the
        /portfolio/settlements endpoint with server-side ticker and min_ts
        filters. This is far more efficient than paginating all fills:
          - Only KXBTC15M rows returned (ticker filter)
          - Only post-May rows returned (min_ts filter)
          - Settlement result, cost, revenue and fee are all pre-computed
          - No need to fetch each market separately to get the result
          - Scales regardless of how many other series you trade
        """
        if not self._sign:
            return

        MIN_TS    = 1746057600  # May 1 2026 00:00:00 UTC
        all_setts = []
        cursor    = None

        for _ in range(50):
            # Use only min_ts for server-side filtering — the ticker param on
            # /portfolio/settlements filters by exact market ticker, not series
            # prefix, so passing "KXBTC15M" would return nothing.
            # min_ts limits pages to post-May data only, keeping pagination short.
            params = {"limit": 200, "min_ts": MIN_TS}
            if cursor:
                params["cursor"] = cursor
            try:
                path    = "/trade-api/v2/portfolio/settlements"
                headers = self._sign("GET", path)
                r = self._session.get(f"{self._base}/portfolio/settlements",
                                      headers=headers, params=params, timeout=10)
                r.raise_for_status()
                data   = r.json()
                batch  = data.get("settlements", [])
                cursor = data.get("cursor")
                # Filter client-side: KXBTC15M only, settled May 2026 onwards.
                # min_ts isn't reliably applied server-side so we enforce it here.
                kxbtc  = [
                    s for s in batch
                    if s.get("ticker", "").startswith(SERIES_TICKER)
                    and s.get("settled_time", "") >= "2026-05"
                ]
                all_setts.extend(kxbtc)
                print(f"[Kalshi P&L] page: {len(batch)} settlements, {len(kxbtc)} KXBTC15M May+")

                # Stop once we see pre-May settlements — implies we've gone past
                # the relevant date range (assuming newest-first ordering).
                has_pre_may = any(s.get("settled_time", "") < "2026-05" for s in batch)
                if not batch or not cursor or has_pre_may:
                    break
                time.sleep(0.2)
            except Exception as e:
                print(f"[Kalshi P&L] Settlement fetch error: {e}")
                break

        if not all_setts:
            print("[Kalshi P&L] No KXBTC15M settlements found from May 2026 onwards")
            self.pnl_updated.emit(0.0, [])
            return

        total_pnl = 0.0
        trades    = []

        for s in all_setts:
            ticker  = s.get("ticker", "")
            result  = s.get("market_result", "")
            settled = s.get("settled_time", "")

            utc_hour = 0
            try:
                dt = datetime.fromisoformat(settled.replace("Z", "+00:00"))
                utc_hour = dt.hour
            except Exception:
                pass
            sess = ("asia" if utc_hour < 6 else "europe" if utc_hour < 12
                    else "us" if utc_hour < 20 else "us_close")

            total_count = (float(s.get("yes_count_fp", 0) or 0) +
                           float(s.get("no_count_fp",  0) or 0))
            fee_total = float(s.get("fee_cost", 0) or 0)

            for side in ("yes", "no"):
                count = float(s.get(f"{side}_count_fp", 0) or 0)
                cost  = float(s.get(f"{side}_total_cost_dollars", 0) or 0)
                if count == 0:
                    continue
                avg_price = cost / count
                fee       = fee_total * (count / total_count) if total_count > 0 else 0.0

                if result in ("yes", "no"):
                    won     = (result == side)
                    outcome = "win" if won else "loss"
                    pnl     = ((1.0 - avg_price) * count - fee
                               if won else -avg_price * count - fee)
                else:
                    outcome = "unknown"
                    pnl     = 0.0

                total_pnl += pnl
                trades.append({
                    "ticker":    ticker,
                    "timestamp": settled,
                    "session":   sess,
                    "side":      side,
                    "avg_price": round(avg_price, 4),
                    "count":     count,
                    "result":    result or "pending",
                    "outcome":   outcome,
                    "pnl":       round(pnl, 4),
                })

        settled_count = sum(1 for t in trades if t["outcome"] in ("win", "loss"))
        print(f"[Kalshi P&L] {len(trades)} positions | {settled_count} settled "
              f"| net P&L: ${total_pnl:+.4f}")
        self.pnl_updated.emit(total_pnl, trades)



# ─────────────────────────────────────────────────────────────
# BOT WORKER — runs the trading loop in a background thread
# ─────────────────────────────────────────────────────────────
class BotWorker(QThread):
    """
    Runs the threshold momentum trading loop.
    Mirrors main.py's logic exactly — same signal function, same sizing,
    same daily loss limit. Emits log lines and trade events to the UI.
    Set dry_run=True (default) to simulate without placing real orders.
    """
    log_line     = pyqtSignal(str)           # timestamped log message
    trade_placed = pyqtSignal(str, str, int, float)  # ticker, side, count, price
    daily_pnl_changed = pyqtSignal(float)    # updated running daily P&L

    def __init__(self, dry_run: bool = True):
        super().__init__()
        self._running  = True
        self._dry_run  = dry_run
        self._session  = requests.Session()

        # Per-run state (mirrors main.py globals)
        self._current_ticker      = None
        self._cached_close_time   = ""
        self._baseline_btc        = None
        self._market_session      = None
        self._traded_this_market  = False
        self._last_side           = None
        self._last_price          = None
        self._last_filled_count   = 0
        self._entry_seconds_left  = 0.0
        self._entry_threshold     = 0.0
        self._daily_pnl           = 0.0
        self._daily_pnl_date      = datetime.now(timezone.utc).date()

        self._import_error: str | None = None
        try:
            from kalshi_common import (
                sign_request, get_orderbook_prices,
                get_open_markets, BASE_URL,
            )
            self._sign           = sign_request
            self._get_orderbook  = get_orderbook_prices
            self._get_markets    = get_open_markets
            self._base           = BASE_URL
            self._auth_ok        = True
        except Exception as e:
            self._auth_ok        = False
            self._import_error   = f"kalshi_common import failed: {type(e).__name__}: {e}"

    def stop(self):
        self._running = False

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_line.emit(f"[{ts}] {msg}")

    def _get_btc_binance(self) -> float | None:
        """Delegates to kalshi_common.get_btc_price() which uses the
        live BRTI WebSocket feed with constituent exchange fallback."""
        try:
            from kalshi_common import get_btc_price
            return get_btc_price()
        except Exception:
            return None

    def _place_order(self, ticker: str, side: str, count: int,
                     yes_bid: float, no_bid: float) -> tuple[bool, int]:
        """
        Aggressive limit order (IOC) that crosses the spread.

        Pricing logic:
          YES buy: bid at YES ask + 2¢ buffer.
                   YES ask ≈ 1 - no_bid (what NO sellers will accept for YES).
          NO buy:  bid at NO ask + 2¢ buffer.
                   NO ask ≈ 1 - yes_bid (what YES sellers will accept for NO).

        Using the ask rather than the bid + fixed offset ensures we actually
        cross the spread regardless of how wide it is.
        """
        try:
            import uuid as _uuid
            path = "/trade-api/v2/portfolio/orders"
            headers = self._sign("POST", path)

            if side == "yes":
                # YES ask = 100 - no_bid_cents; add 2¢ buffer to cross reliably
                yes_ask_cents   = 100 - int(round(no_bid * 100))
                yes_price_cents = min(yes_ask_cents + 2, 99)
            else:
                # NO buy: in yes_price terms = yes_bid_cents - 2¢
                # (lower yes_price = higher no_price = more aggressive NO bid)
                yes_bid_cents   = int(round(yes_bid * 100))
                yes_price_cents = max(yes_bid_cents - 2, 1)

            payload = {
                "action":          "buy",
                "client_order_id": str(_uuid.uuid4()),
                "count":           count,
                "side":            side,
                "ticker":          ticker,
                "type":            "limit",
                "yes_price":       yes_price_cents,
                "time_in_force":   "immediate_or_cancel",
            }

            implied_price = yes_price_cents / 100 if side == "yes" else (100 - yes_price_cents) / 100
            self._log(
                f"  Placing {side.upper()} order | yes_price={yes_price_cents}¢ "
                f"| implied={implied_price:.3f} | count={count} | ticker={ticker}"
            )

            t_post = time.time()
            r = self._session.post(
                f"{self._base}/portfolio/orders",
                headers=headers, json=payload, timeout=10,
            )
            post_ms = (time.time() - t_post) * 1000

            # Log full response so we can diagnose issues
            try:
                resp_body = r.json()
            except Exception:
                resp_body = {"raw": r.text[:200]}

            if not r.ok:
                self._log(f"  ❌ Order POST failed: {r.status_code} — {resp_body}")
                return False, 0

            order    = resp_body.get("order", {})
            order_id = order.get("order_id", "")
            status   = order.get("status", "unknown")
            self._log(f"  Order created: id={order_id} | status={status} | POST {post_ms:.0f}ms")

            if not order_id:
                self._log(f"  ❌ No order_id in response: {resp_body}")
                return False, 0

            # Poll for fill
            deadline    = time.time() + 12
            path_o      = f"/trade-api/v2/portfolio/orders/{order_id}"
            last_status = None
            executed_at = float("inf")  # set when executed+filled=0 first seen

            while time.time() < deadline:
                try:
                    h2  = self._sign("GET", path_o)
                    ro  = self._session.get(
                        f"{self._base}/portfolio/orders/{order_id}",
                        headers=h2, timeout=8,
                    )
                    ro.raise_for_status()
                    o      = ro.json().get("order", {})
                    status = o.get("status")
                    filled = int(o.get("filled_count", 0))

                    # Log only when status changes to avoid spam
                    if status != last_status:
                        self._log(f"  Poll: status={status} | filled={filled}")
                        last_status = status
                        if status in ("executed", "filled") and filled == 0:
                            executed_at = time.time()   # start grace period clock

                    if status in ("filled", "executed"):
                        if filled > 0:
                            return True, filled
                        # executed + filled=0: Kalshi may not have updated
                        # filled_count yet even though the position was created.
                        # Give it 2s, then verify against /portfolio/positions
                        # before declaring failure.
                        if time.time() - executed_at > 2.0:
                            actual = self._check_position(ticker)
                            if actual > 0:
                                self._log(
                                    f"  ✅ Position confirmed via /positions: "
                                    f"{actual} contracts (filled_count was 0)"
                                )
                                return True, actual
                            self._log(
                                f"  IOC executed with 0 fills after 2s — "
                                f"no liquidity at {yes_price_cents}¢"
                            )
                            return False, 0
                    elif status in ("canceled", "cancelled"):
                        if filled > 0:
                            self._log(f"  Partial fill before cancel: {filled}")
                            return True, filled
                        self._log(f"  IOC cancelled with 0 fills — no liquidity at {yes_price_cents}¢")
                        return False, 0
                    elif status == "rejected":
                        reason = o.get("close_reason", "unknown")
                        self._log(f"  ❌ Order rejected: {reason}")
                        return False, 0
                    # "resting", "open", "pending" → keep waiting

                except requests.HTTPError as e:
                    self._log(f"  Poll error: {e.response.status_code} — {e.response.text[:100]}")
                except Exception as e:
                    self._log(f"  Poll error: {e}")

                time.sleep(0.5)

            self._log(f"  ⏱ Fill timeout — last status: {last_status}")
            return False, 0

        except requests.HTTPError as e:
            self._log(f"  Order HTTP error: {e.response.status_code} — {e.response.text[:200]}")
            return False, 0
        except Exception as e:
            self._log(f"  Order error: {e}")
            return False, 0

    def _get_settlement(self, ticker: str) -> str | None:
        for attempt in range(6):
            try:
                path    = f"/trade-api/v2/markets/{ticker}"
                headers = self._sign("GET", path)
                r = self._session.get(f"{self._base}/markets/{ticker}",
                                      headers=headers, timeout=8)
                r.raise_for_status()
                result = r.json().get("market", {}).get("result")
                if result in ("yes", "no"):
                    return result
            except Exception:
                pass
            if attempt < 5:
                time.sleep(5)
        return None

    def _log_result(self, ticker, baseline, final_btc, variation,
                    side, price, filled, count, outcome, settled, sess):
        try:
            now = datetime.now(timezone.utc)
            file_exists = os.path.exists(RESULTS_FILE)
            with open(RESULTS_FILE, "a", newline="") as f:
                import csv as _csv
                writer = _csv.writer(f, quoting=_csv.QUOTE_ALL)
                if not file_exists:
                    writer.writerow([
                        "timestamp","utc_hour","session","ticker",
                        "baseline_btc","final_btc","variation_pct",
                        "side","price","filled","filled_count",
                        "settled_result","outcome","exit_reason",
                        "entry_seconds_left","threshold_at_entry",
                    ])
                writer.writerow([
                    now.isoformat(), now.hour, sess, ticker,
                    round(baseline,2), round(final_btc,2),
                    round(variation,4), side, price, filled, count,
                    settled, outcome, "expiry",
                    round(self._entry_seconds_left,1),
                    round(self._entry_threshold,4),
                ])
        except Exception as e:
            self._log(f"⚠️ Log write failed: {e}")

    def run(self):
        if not self._auth_ok:
            self._log(f"❌ {self._import_error or 'Auth not available'} — stopping bot worker")
            return

        self._log(f"🤖 Bot started | DRY_RUN={self._dry_run} | "
                  f"risk=${FIXED_RISK_DOLLARS}/trade | "
                  f"daily limit=${DAILY_LOSS_LIMIT}")

        # Fetch current open positions from Kalshi so we don't double-enter
        # a contract that was already traded in a previous session.
        self._already_traded = self._fetch_open_tickers()
        if self._already_traded:
            self._log(f"  Found {len(self._already_traded)} open position(s) — "
                      f"will skip those tickers: {self._already_traded}")

        cycle = 0
        while self._running:
            try:
                self._loop_cycle(fetch_markets=(cycle % 10 == 0))
            except Exception as e:
                self._log(f"❌ Loop error: {e}")
            cycle += 1
            time.sleep(3)

        self._log("🛑 Bot stopped")

    def _check_position(self, ticker: str) -> int:
        """
        Check /portfolio/positions for a specific ticker.
        Returns the net position size, or 0 if none found.
        Used to verify fills when filled_count=0 but status=executed.
        """
        try:
            path    = "/trade-api/v2/portfolio/positions"
            headers = self._sign("GET", path)
            r = self._session.get(
                f"{self._base}/portfolio/positions",
                headers=headers,
                params={"limit": 100, "settlement_status": "unsettled"},
                timeout=6,
            )
            r.raise_for_status()
            positions = r.json().get("market_positions", [])
            for p in positions:
                if p.get("ticker") == ticker:
                    return int(abs(float(p.get("position", 0) or 0)))
            return 0
        except Exception as e:
            self._log(f"  Position check error: {e}")
            return 0

    def _fetch_open_tickers(self) -> set:
        """Return set of tickers with a current open position in Kalshi."""
        try:
            path    = "/trade-api/v2/portfolio/positions"
            headers = self._sign("GET", path)
            r = self._session.get(
                f"{self._base}/portfolio/positions",
                headers=headers,
                params={"limit": 100, "settlement_status": "unsettled"},
                timeout=8,
            )
            r.raise_for_status()
            positions = r.json().get("market_positions", [])
            return {
                p["ticker"] for p in positions
                if p.get("ticker", "").startswith(SERIES_TICKER)
                and float(p.get("position", 0) or 0) != 0
            }
        except Exception as e:
            self._log(f"  ⚠️ Could not fetch open positions: {e}")
            return set()

    def _loop_cycle(self, fetch_markets: bool = True):
        # Force a market list refresh if the current ticker has already expired.
        if self._current_ticker and self._cached_close_time:
            try:
                close_dt = datetime.fromisoformat(
                    self._cached_close_time.replace("Z", "+00:00")
                )
                if (datetime.now(timezone.utc) - close_dt).total_seconds() > 5:
                    fetch_markets = True
            except Exception:
                pass

        # ── Fetch current market (cached in kalshi_common — cheap after first call) ──
        if fetch_markets or self._current_ticker is None:
            markets = self._get_markets()
            market  = markets[0] if markets else None
        else:
            market = {"ticker": self._current_ticker,
                      "close_time": self._cached_close_time,
                      "floor_strike": self._baseline_btc}

        if not market:
            self._log("No open KXBTC15M market — waiting")
            return

        ticker         = market.get("ticker")
        close_time_str = market.get("close_time", "")
        floor_strike   = (
            market.get("floor_strike")
            or market.get("result_sources", [{}])[0].get("floor_strike")
        )
        if not ticker or not close_time_str:
            return

        # ── Detect new market ──
        if ticker != self._current_ticker:
            if (self._current_ticker and self._baseline_btc
                    and self._market_session and self._last_side):
                settled = self._get_settlement(self._current_ticker)
                outcome = (
                    ("win" if settled == self._last_side else "loss")
                    if settled in ("yes", "no") else "unknown"
                )
                final_btc = self._get_btc_binance() or self._baseline_btc
                variation = (final_btc - self._baseline_btc) / self._baseline_btc * 100
                self._log(
                    f"📋 {self._current_ticker} settled: {settled} | "
                    f"outcome={outcome} | side={self._last_side} | "
                    f"Δ={variation:+.3f}%"
                )
                self._log_result(
                    self._current_ticker, self._baseline_btc, final_btc,
                    variation, self._last_side, self._last_price or 0.0,
                    self._traded_this_market, self._last_filled_count,
                    outcome, settled or "unknown", self._market_session,
                )
                if outcome in ("win", "loss") and self._last_price:
                    trade_pnl = (
                        (1 - self._last_price) * self._last_filled_count
                        if outcome == "win"
                        else -self._last_price * self._last_filled_count
                    )
                    # Reset daily counter if new UTC day
                    today = datetime.now(timezone.utc).date()
                    if today != self._daily_pnl_date:
                        self._daily_pnl      = 0.0
                        self._daily_pnl_date = today
                        self._log("🔄 New UTC day — daily P&L reset")
                    self._daily_pnl += trade_pnl
                    self.daily_pnl_changed.emit(self._daily_pnl)
                    self._log(f"  Daily P&L: ${self._daily_pnl:+.2f} (limit ${DAILY_LOSS_LIMIT})")

            # Reset state
            self._current_ticker     = ticker
            self._cached_close_time  = close_time_str
            # If we have an open position from a previous session, don't re-enter.
            already = ticker in getattr(self, "_already_traded", set())
            self._traded_this_market = already
            if already:
                self._log(f"  ↩ {ticker} already has an open position — skipping entry")
            self._last_side          = None
            self._last_price         = None
            self._last_filled_count  = 0
            self._entry_seconds_left = 0.0
            self._entry_threshold    = 0.0
            hour = datetime.now(timezone.utc).hour
            self._market_session = (
                "asia" if hour < 6 else "europe" if hour < 12
                else "us" if hour < 20 else "us_close"
            )
            try:
                self._baseline_btc = float(floor_strike) if floor_strike else None
            except (TypeError, ValueError):
                self._baseline_btc = None
            self._log(f"📌 New market: {ticker} | baseline=${self._baseline_btc:,.2f}"
                      if self._baseline_btc else f"📌 New market: {ticker} | no baseline")

        # ── Time remaining ──
        try:
            close_dt     = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            seconds_left = max(0, (close_dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return

        if not self._baseline_btc:
            return

        # ── BTC price + variation ──
        btc_price = self._get_btc_binance()
        if not btc_price:
            return
        btc_variation = (btc_price - self._baseline_btc) / self._baseline_btc * 100

        # ── Orderbook prices ──
        yes_price, no_price = self._get_orderbook(ticker)
        spread  = yes_price + no_price
        mid_yes = round(yes_price / spread, 4) if spread > 0 else 0.5

        # Fallback: if orderbook returns zeros (field name mismatch or thin
        # market), estimate price from the market's mid_yes so we can still
        # Fallback: if orderbook returns zeros, estimate from mid_yes.
        # Suppress the warning when seconds_left <= 0 — the orderbook is
        # expected to be empty once a market expires, it's not an error.
        if yes_price <= 0 and no_price <= 0:
            if seconds_left > 5:
                self._log(
                    f"  ⚠️ Orderbook zeros for {ticker} at {seconds_left:.0f}s left — "
                    f"falling back to mid_yes. Check orderbook field names in kalshi_common.py."
                )
            yes_price = mid_yes
            no_price  = round(1.0 - mid_yes, 4)

        # ── Signal ──
        threshold = get_threshold(seconds_left)
        signal_side = None
        if btc_variation >= threshold:
            signal_side = "yes"
        elif btc_variation <= -threshold:
            signal_side = "no"

        self._log(
            f"{ticker} | {fmt_seconds(seconds_left)} | "
            f"Δ={btc_variation:+.4f}% | ±{threshold:.2f}% | "
            f"signal={signal_side or 'WAIT'} | mid={mid_yes:.3f}"
        )

        if self._traded_this_market or signal_side is None:
            return

        # Don't enter with less than 10 seconds left — order won't fill in time.
        if seconds_left < 10:
            self._log(f"  ⏱ {seconds_left:.0f}s left — too late to enter, skipping")
            return

        # ── Daily loss check ──
        today = datetime.now(timezone.utc).date()
        if today != self._daily_pnl_date:
            self._daily_pnl      = 0.0
            self._daily_pnl_date = today
        if self._daily_pnl <= DAILY_LOSS_LIMIT:
            self._log(f"🛑 Daily loss limit ${self._daily_pnl:+.2f} — no new entries")
            return

        entry_price = yes_price if signal_side == "yes" else no_price
        if entry_price <= 0:
            self._log(f"  ⚠️ Entry price zero for {signal_side} — skipping")
            return

        if entry_price > MAX_ENTRY_PRICE:
            self._log(
                f"  ⏭ Entry price ${entry_price:.4f} > max ${MAX_ENTRY_PRICE} "
                f"— margin too thin, skipping"
            )
            return

        count = max(1, min(int(FIXED_RISK_DOLLARS / entry_price), MAX_CONTRACTS))
        self._log(
            f"🚀 SIGNAL {signal_side.upper()} | "
            f"Δ={btc_variation:+.4f}% | price={entry_price:.4f} | "
            f"contracts={count} | risk=${count*entry_price:.2f} | "
            f"DRY={self._dry_run}"
        )

        self._last_side          = signal_side
        self._last_price         = entry_price
        self._entry_seconds_left = seconds_left
        self._entry_threshold    = threshold

        if not self._dry_run:
            t0 = time.time()
            filled, filled_count = self._place_order(
                ticker, signal_side, count, yes_price, no_price
            )
            elapsed = time.time() - t0
            self._traded_this_market = True
            if filled and filled_count > 0:
                self._last_filled_count = filled_count
                self._log(
                    f"✅ Filled {filled_count}/{count} contracts "
                    f"| {elapsed:.2f}s signal→fill"
                )
                self.trade_placed.emit(ticker, signal_side, filled_count, entry_price)
            else:
                self._log(f"⚠️ Order not filled | {elapsed:.2f}s elapsed")
        else:
            self._traded_this_market = True
            self._last_filled_count  = count
            self._log(f"[DRY] Simulated {signal_side.upper()} ×{count} @ {entry_price:.4f}")
            self.trade_placed.emit(ticker, signal_side, count, entry_price)



class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "—", accent: str = "#e0e2e8"):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(f"""
            QFrame {{
                background: #13161c;
                border: 1px solid #1e2330;
                border-radius: 6px;
                padding: 2px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        self._label_w = QLabel(label.upper())
        self._label_w.setStyleSheet("color: #4b5563; font-size: 9px; letter-spacing: 0.1em;")

        self._value_w = QLabel(value)
        self._value_w.setStyleSheet(f"color: {accent}; font-size: 20px; font-weight: bold;")

        lay.addWidget(self._label_w)
        lay.addWidget(self._value_w)

    def set_value(self, v: str):
        self._value_w.setText(v)

    def set_accent(self, color: str):
        self._value_w.setStyleSheet(
            f"color: {color}; font-size: 20px; font-weight: bold;"
        )


class SectionLabel(QLabel):
    def __init__(self, text: str):
        super().__init__(f"  {text.upper()}")
        self.setStyleSheet("""
            color: #4b5563;
            font-size: 9px;
            letter-spacing: 0.12em;
            padding: 6px 0 2px 0;
            border-bottom: 1px solid #1a1e27;
        """)


class MplCanvas(FigureCanvas):
    def __init__(self, nrows=1, ncols=1, height=3.2):
        self.fig = Figure(figsize=(6, height), tight_layout=True)
        self.fig.patch.set_facecolor(CHART_BG)
        self.axes = []
        for i in range(nrows * ncols):
            ax = self.fig.add_subplot(nrows, ncols, i + 1)
            apply_dark_axes(ax, self.fig)
            self.axes.append(ax)
        super().__init__(self.fig)
        self.setStyleSheet("background: transparent;")


# ─────────────────────────────────────────────────────────────
# LIVE SIGNAL PANEL
# ─────────────────────────────────────────────────────────────
class LiveSignalPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ── Top row: ticker + countdown + session ──
        top = QHBoxLayout()
        self._ticker_lbl = QLabel("—")
        self._ticker_lbl.setStyleSheet("color:#e0e2e8; font-size:13px; font-weight:bold;")
        self._time_lbl   = QLabel("0:00")
        self._time_lbl.setStyleSheet("color:#8b5cf6; font-size:28px; font-weight:bold;")
        self._sess_lbl   = QLabel("—")
        self._sess_lbl.setStyleSheet(
            "color:#4b5563; background:#13161c; border:1px solid #1e2330;"
            "border-radius:4px; padding:2px 8px; font-size:10px;"
        )
        top.addWidget(self._ticker_lbl)
        top.addStretch()
        top.addWidget(self._sess_lbl)
        top.addSpacing(8)
        top.addWidget(self._time_lbl)
        root.addLayout(top)

        # ── Time progress bar ──
        self._progress = QProgressBar()
        self._progress.setRange(0, 900)
        self._progress.setValue(0)
        self._progress.setFixedHeight(4)
        root.addWidget(self._progress)

        # ── Signal badge ──
        badge_row = QHBoxLayout()
        badge_row.addStretch()
        self._badge = QLabel("WAIT")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setFixedSize(220, 80)
        self._badge.setStyleSheet(self._badge_style("WAIT"))
        badge_row.addWidget(self._badge)
        badge_row.addStretch()
        root.addLayout(badge_row)

        # ── BTC info row ──
        info = QHBoxLayout()
        info.setSpacing(20)
        self._btc_card   = MetricCard("BTC price",     "—",       "#e0e2e8")
        self._var_card   = MetricCard("variation",      "—",       "#6b7280")
        self._thresh_card= MetricCard("threshold",      "—",       "#8b5cf6")
        self._midyes_card= MetricCard("mid yes",        "—",       "#6b7280")
        for c in [self._btc_card, self._var_card, self._thresh_card, self._midyes_card]:
            info.addWidget(c)
        root.addLayout(info)

        # ── Threshold reference table ──
        root.addWidget(SectionLabel("threshold table"))
        self._thresh_table = QTableWidget(6, 3)
        self._thresh_table.setHorizontalHeaderLabels(["time remaining", "|variation| needed", "hist. win rate"])
        self._thresh_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._thresh_table.verticalHeader().setVisible(False)
        self._thresh_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._thresh_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Let the table expand vertically to fill remaining panel space
        self._thresh_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._thresh_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        rows = THRESHOLD_TABLE
        for r, (t, v, w) in enumerate(rows):
            for c, val in enumerate([t, v, w]):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    item.setForeground(QBrush(QColor("#8b5cf6")))
                if c == 2:
                    item.setForeground(QBrush(QColor("#10b981")))
                self._thresh_table.setItem(r, c, item)
        root.addWidget(self._thresh_table, stretch=1)

        self._current_seconds = 0.0

    def _badge_style(self, signal: str) -> str:
        styles = {
            "YES":  ("background:#0a2e1e; color:#10b981; border:2px solid #10b981;"),
            "NO":   ("background:#0a1b33; color:#3b82f6; border:2px solid #3b82f6;"),
            "WAIT": ("background:#131720; color:#4b5563; border:2px solid #1e2330;"),
        }
        base = styles.get(signal, styles["WAIT"])
        return f"{base} font-size:32px; font-weight:bold; border-radius:8px; letter-spacing:0.06em;"

    def update_market(self, mkt: dict, btc_price: float):
        if not mkt:
            return
        ticker    = mkt.get("ticker", "—")
        close_str = mkt.get("close_time", "")
        floor     = mkt.get("floor_strike")

        try:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs_left = max(0, (close_dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            secs_left = 0.0

        self._current_seconds = secs_left
        self._ticker_lbl.setText(ticker)
        self._time_lbl.setText(fmt_seconds(secs_left))
        self._progress.setValue(int(900 - secs_left))

        hour = datetime.now(timezone.utc).hour
        sess = ("asia" if hour < 6 else "europe" if hour < 12
                else "us" if hour < 20 else "us_close")
        self._sess_lbl.setText(sess)

        # Highlight active threshold row
        th = get_threshold(secs_left)
        row_map = [30, 60, 120, 180, 300, 900]
        active_row = next((i for i, lim in enumerate(row_map) if secs_left <= lim), 5)
        for r in range(6):
            bg = QColor("#1a1e27") if r == active_row else QColor("#0f1218")
            for c in range(3):
                item = self._thresh_table.item(r, c)
                if item:
                    item.setBackground(QBrush(bg))

        if btc_price > 0 and floor:
            try:
                baseline = float(floor)
                variation = (btc_price - baseline) / baseline * 100
                signal = get_signal(variation, secs_left)

                self._btc_card.set_value(f"${btc_price:,.2f}")
                var_color = C_YES if variation > 0 else C_NO if variation < 0 else "#6b7280"
                self._var_card.set_value(f"{variation:+.4f}%")
                self._var_card.set_accent(var_color)
                self._thresh_card.set_value(f"±{th:.2f}%")
                mid = mkt.get("mid_yes", 0.5)
                self._midyes_card.set_value(f"{mid:.3f}")

                self._badge.setText(signal)
                self._badge.setStyleSheet(self._badge_style(signal))
            except Exception:
                pass

    def tick_countdown(self):
        self._current_seconds = max(0, self._current_seconds - 1)
        self._time_lbl.setText(fmt_seconds(self._current_seconds))
        self._progress.setValue(int(900 - self._current_seconds))


# ─────────────────────────────────────────────────────────────
# TRADE HISTORY TABLE
# ─────────────────────────────────────────────────────────────
HISTORY_COLS = [
    "timestamp", "session", "ticker", "side", "avg_price",
    "count", "outcome", "pnl",
]
HISTORY_HEADERS = [
    "time (UTC)", "session", "ticker", "side", "entry $",
    "contracts", "outcome", "net P&L",
]

class TradeHistoryPanel(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        self._table = QTableWidget(0, len(HISTORY_COLS))
        self._table.setHorizontalHeaderLabels(HISTORY_HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(False)
        # Increase font size for readability
        font = QFont()
        font.setPointSize(13)   # ~1.75x the default
        self._table.setFont(font)
        self._table.horizontalHeader().setFont(font)
        self._table.verticalHeader().setDefaultSectionSize(34)  # taller rows for larger font
        lay.addWidget(self._table)

    def refresh(self, df: pd.DataFrame):
        if df is None or df.empty:
            self._table.setRowCount(0)
            return

        df_sorted = df.sort_values("timestamp", ascending=False).reset_index(drop=True)
        n_rows = len(df_sorted)

        self._table.setSortingEnabled(False)
        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(n_rows)

        try:
            for row_i in range(n_rows):
                row = df_sorted.iloc[row_i]
                outcome   = str(row.get("outcome", "")).lower()
                row_color = (QColor("#0a2316") if outcome == "win"
                             else QColor("#2a0a0a") if outcome == "loss"
                             else QColor("#0f1218"))

                for col_i, col_key in enumerate(HISTORY_COLS):
                    # Support both Kalshi column names and legacy CSV names
                    raw = row.get(col_key, row.get(
                        {"avg_price": "price", "count": "filled_count"}.get(col_key, col_key), ""
                    ))
                    if col_key == "timestamp":
                        try:
                            dt  = pd.to_datetime(raw, utc=True)
                            val = dt.strftime("%m-%d %H:%M")
                        except Exception:
                            val = str(raw)[:16]
                    elif col_key in ("avg_price", "price"):
                        try:    val = f"${float(raw):.4f}"
                        except: val = str(raw)
                    elif col_key == "pnl":
                        try:    val = f"${float(raw):+.4f}"
                        except: val = "—"
                    else:
                        val = str(raw)

                    item = QTableWidgetItem(val)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setBackground(QBrush(row_color))

                    if col_key == "outcome":
                        if outcome == "win":
                            item.setForeground(QBrush(QColor("#10b981")))
                        elif outcome == "loss":
                            item.setForeground(QBrush(QColor("#ef4444")))
                    elif col_key == "side":
                        if str(raw).lower() == "yes":
                            item.setForeground(QBrush(QColor("#10b981")))
                        elif str(raw).lower() == "no":
                            item.setForeground(QBrush(QColor("#3b82f6")))
                    elif col_key == "pnl":
                        try:
                            item.setForeground(QBrush(
                                QColor("#10b981") if float(raw) >= 0 else QColor("#ef4444")
                            ))
                        except Exception:
                            pass

                    self._table.setItem(row_i, col_i, item)
        finally:
            self._table.setUpdatesEnabled(True)
            self._table.setSortingEnabled(True)


# ─────────────────────────────────────────────────────────────
# P&L CHART PANEL
# ─────────────────────────────────────────────────────────────
class PnLPanel(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Stat row
        stat_row = QHBoxLayout()
        self._total_card  = MetricCard("total P&L",   "—",  "#10b981")
        self._trades_card = MetricCard("trades",       "—",  "#e0e2e8")
        self._wr_card     = MetricCard("win rate",     "—",  "#10b981")
        self._avg_card    = MetricCard("avg P&L / trade", "—", "#8b5cf6")
        for c in [self._total_card, self._trades_card, self._wr_card, self._avg_card]:
            stat_row.addWidget(c)
        lay.addLayout(stat_row)

        # Cumulative P&L chart
        self._cum_canvas = MplCanvas(nrows=1, ncols=1, height=2.8)
        lay.addWidget(self._cum_canvas)

        # Daily bar chart
        self._daily_canvas = MplCanvas(nrows=1, ncols=1, height=2.2)
        lay.addWidget(self._daily_canvas)

    def refresh(self, df: pd.DataFrame):
        if df is None or df.empty:
            for card in [self._total_card, self._trades_card, self._wr_card, self._avg_card]:
                card.set_value("—")
            return

        settled = df[df["outcome"].isin(["win", "loss"])].copy()
        if settled.empty:
            return

        # Compute P&L per trade: win=(1-price)*count, loss=(-price)*count
        def row_pnl(r):
            try:
                price = float(r["price"])
                count = int(r["filled_count"])
                return (1 - price) * count if r["outcome"] == "win" else -price * count
            except Exception:
                return 0.0

        settled["pnl"] = settled.apply(row_pnl, axis=1)
        settled["timestamp"] = pd.to_datetime(settled["timestamp"], utc=True)
        settled = settled.sort_values("timestamp")
        settled["cum_pnl"] = settled["pnl"].cumsum()

        total_pnl = settled["pnl"].sum()
        win_rate  = (settled["outcome"] == "win").mean()
        avg_pnl   = settled["pnl"].mean()
        n         = len(settled)

        pnl_color = "#10b981" if total_pnl >= 0 else "#ef4444"
        self._total_card.set_value(f"${total_pnl:+.2f}")
        self._total_card.set_accent(pnl_color)
        self._trades_card.set_value(str(n))
        self._wr_card.set_value(f"{win_rate:.1%}")
        self._avg_card.set_value(f"${avg_pnl:+.4f}")

        # ── Cumulative P&L line ──
        ax = self._cum_canvas.axes[0]
        ax.clear()
        apply_dark_axes(ax, self._cum_canvas.fig)
        ts   = settled["timestamp"].dt.to_pydatetime()
        cpnl = settled["cum_pnl"].values
        ax.plot(ts, cpnl, color="#10b981", linewidth=1.8, zorder=3)
        ax.fill_between(ts, 0, cpnl,
                        where=[v >= 0 for v in cpnl],
                        color="#10b981", alpha=0.08)
        ax.fill_between(ts, 0, cpnl,
                        where=[v < 0 for v in cpnl],
                        color="#ef4444", alpha=0.08)
        ax.axhline(0, color=CHART_GRID, linewidth=0.8, zorder=2)
        ax.set_ylabel("cumulative P&L ($)", color=CHART_TEXT, fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        self._cum_canvas.fig.autofmt_xdate(rotation=30, ha="right")
        self._cum_canvas.draw()

        # ── Daily bar ──
        ax2 = self._daily_canvas.axes[0]
        ax2.clear()
        apply_dark_axes(ax2, self._daily_canvas.fig)

        settled["date"] = settled["timestamp"].dt.date
        daily = settled.groupby("date")["pnl"].sum()
        dates  = list(daily.index)
        values = list(daily.values)
        colors = ["#10b981" if v >= 0 else "#ef4444" for v in values]
        ax2.bar(range(len(dates)), values, color=colors, alpha=0.75, width=0.6, zorder=3)
        ax2.set_xticks(range(len(dates)))
        ax2.set_xticklabels([str(d)[5:] for d in dates], rotation=45, ha="right", fontsize=8)
        ax2.axhline(0, color=CHART_GRID, linewidth=0.8)
        ax2.set_ylabel("daily P&L ($)", color=CHART_TEXT, fontsize=9)
        self._daily_canvas.draw()


# ─────────────────────────────────────────────────────────────
# ANALYTICS PANEL
# ─────────────────────────────────────────────────────────────
class AnalyticsPanel(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self._canvas = MplCanvas(nrows=2, ncols=2, height=5.5)
        lay.addWidget(self._canvas)

    def refresh(self, df: pd.DataFrame):
        if df is None or df.empty or "outcome" not in df.columns:
            return

        settled = df[df["outcome"].isin(["win", "loss"])].copy()
        if settled.empty:
            return

        axes = self._canvas.axes
        for ax in axes:
            ax.clear()
            apply_dark_axes(ax, self._canvas.fig)

        # ── Win rate by session ──
        ax0 = axes[0]
        sessions = ["asia", "europe", "us", "us_close"]
        wr_vals, n_vals = [], []
        for s in sessions:
            sub = settled[settled["session"] == s]
            wr  = (sub["outcome"] == "win").mean() if len(sub) > 0 else 0.0
            wr_vals.append(wr * 100)
            n_vals.append(len(sub))
        bars = ax0.bar(sessions, wr_vals, color="#8b5cf6", alpha=0.75, width=0.5, zorder=3)
        ax0.set_ylim(0, 110)
        ax0.axhline(100, color=CHART_GRID, linewidth=0.6, linestyle="--")
        ax0.set_ylabel("win rate %", fontsize=9, color=CHART_TEXT)
        ax0.set_title("win rate by session", fontsize=10, color="#9ca3af", pad=6)
        for bar, wr, n in zip(bars, wr_vals, n_vals):
            ax0.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                     f"{wr:.0f}%\n(n={n})", ha="center", va="bottom",
                     fontsize=8, color="#6b7280")

        # ── Win rate by entry time bucket ──
        ax1 = axes[1]
        try:
            settled["entry_seconds_left"] = pd.to_numeric(settled["entry_seconds_left"], errors="coerce")
            buckets = [(0,60,"0-60s"),(60,180,"1-3m"),(180,300,"3-5m"),
                       (300,480,"5-8m"),(480,660,"8-11m"),(660,900,"11-15m")]
            labels, wrs, ns = [], [], []
            for lo, hi, lbl in buckets:
                sub = settled[
                    (settled["entry_seconds_left"] >= lo) &
                    (settled["entry_seconds_left"] < hi)
                ]
                if len(sub) > 0:
                    labels.append(lbl)
                    wrs.append((sub["outcome"] == "win").mean() * 100)
                    ns.append(len(sub))
            bars2 = ax1.bar(range(len(labels)), wrs, color="#3b82f6", alpha=0.75, width=0.5, zorder=3)
            ax1.set_xticks(range(len(labels)))
            ax1.set_xticklabels(labels, fontsize=9)
            ax1.set_ylim(0, 110)
            ax1.axhline(100, color=CHART_GRID, linewidth=0.6, linestyle="--")
            ax1.set_title("win rate by entry time", fontsize=10, color="#9ca3af", pad=6)
            for bar, wr, n in zip(bars2, wrs, ns):
                ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                         f"{wr:.0f}%\n(n={n})", ha="center", va="bottom",
                         fontsize=8, color="#6b7280")
        except Exception:
            pass

        # ── YES vs NO breakdown ──
        ax2 = axes[2]
        yes_s = settled[settled["side"] == "yes"]
        no_s  = settled[settled["side"] == "no"]
        yes_wr = (yes_s["outcome"] == "win").mean() * 100 if len(yes_s) else 0
        no_wr  = (no_s["outcome"]  == "win").mean() * 100 if len(no_s)  else 0
        ax2.bar(["YES", "NO"], [yes_wr, no_wr],
                color=[C_YES, C_NO], alpha=0.75, width=0.4, zorder=3)
        ax2.set_ylim(0, 110)
        ax2.axhline(100, color=CHART_GRID, linewidth=0.6, linestyle="--")
        ax2.set_title("YES vs NO win rate", fontsize=10, color="#9ca3af", pad=6)
        for x, (wr, n) in enumerate([(yes_wr, len(yes_s)), (no_wr, len(no_s))]):
            ax2.text(x, wr + 1, f"{wr:.1f}%\n(n={n})", ha="center", va="bottom",
                     fontsize=9, color="#6b7280")

        # ── Variation distribution at signal ──
        ax3 = axes[3]
        try:
            settled["variation_pct"] = pd.to_numeric(settled["variation_pct"], errors="coerce")
            yes_var = settled[settled["side"] == "yes"]["variation_pct"].dropna()
            no_var  = settled[settled["side"] == "no"]["variation_pct"].dropna()
            if len(yes_var) > 0:
                ax3.hist(yes_var, bins=20, color=C_YES, alpha=0.6, label="YES entries", zorder=3)
            if len(no_var) > 0:
                ax3.hist(no_var, bins=20, color=C_NO,  alpha=0.6, label="NO entries",  zorder=3)
            ax3.axvline(0, color=CHART_GRID, linewidth=0.8)
            ax3.set_xlabel("variation % at entry", fontsize=9, color=CHART_TEXT)
            ax3.set_title("variation distribution", fontsize=10, color="#9ca3af", pad=6)
            ax3.legend(fontsize=8, facecolor=CHART_SURF, edgecolor=CHART_GRID,
                       labelcolor="#9ca3af")
        except Exception:
            pass

        self._canvas.fig.tight_layout(pad=1.5)
        self._canvas.draw()


# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
class Sidebar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedWidth(200)
        self.setStyleSheet("background: #0a0c10;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 16, 10, 16)
        lay.setSpacing(6)

        title = QLabel("⬡ BTC 15M")
        title.setStyleSheet("color:#8b5cf6; font-size:14px; font-weight:bold; padding-bottom:4px;")
        lay.addWidget(title)

        sub = QLabel("threshold monitor")
        sub.setStyleSheet("color:#374151; font-size:9px; letter-spacing:0.1em;")
        lay.addWidget(sub)
        lay.addSpacing(8)

        lay.addWidget(SectionLabel("account"))
        self._balance = MetricCard("balance",  "—", "#e0e2e8")
        lay.addWidget(self._balance)

        lay.addSpacing(4)
        lay.addWidget(SectionLabel("performance"))
        self._pnl      = MetricCard("total P&L",  "—", "#10b981")
        self._wr       = MetricCard("win rate",   "—", "#10b981")
        self._trades   = MetricCard("trades",     "—", "#e0e2e8")
        self._losses   = MetricCard("losses",     "—", "#ef4444")
        for c in [self._pnl, self._wr, self._trades, self._losses]:
            lay.addWidget(c)

        lay.addSpacing(4)
        lay.addWidget(SectionLabel("live"))
        self._btc_side = MetricCard("BTC / USDT", "—", "#e0e2e8")
        self._status   = MetricCard("status",     "connecting…", "#f59e0b")
        lay.addWidget(self._btc_side)
        lay.addWidget(self._status)

        lay.addStretch()

        # ── Trading toggle ──
        self._toggle_btn = QPushButton("▶  START TRADING")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedHeight(38)
        self._toggle_btn.setStyleSheet(self._btn_style(False))
        lay.addWidget(self._toggle_btn)

        self._last_update = QLabel("last update: —")
        self._last_update.setStyleSheet("color:#2a2f3e; font-size:9px;")
        lay.addWidget(self._last_update)

    def _btn_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton {
                    background: #0a2316; color: #10b981;
                    border: 1.5px solid #10b981; border-radius: 5px;
                    font-size: 11px; font-weight: bold; letter-spacing: 0.08em;
                }
                QPushButton:hover { background: #0d2e1c; }
            """
        return """
            QPushButton {
                background: #13161c; color: #6b7280;
                border: 1px solid #1e2330; border-radius: 5px;
                font-size: 11px; font-weight: bold; letter-spacing: 0.08em;
            }
            QPushButton:hover { background: #1a1e27; color: #9ca3af; }
        """

    def set_trading_active(self, active: bool):
        self._toggle_btn.setText("⏹  STOP TRADING" if active else "▶  START TRADING")
        self._toggle_btn.setStyleSheet(self._btn_style(active))

    @property
    def toggle_btn(self):
        return self._toggle_btn

    def set_btc(self, price: float):
        self._btc_side.set_value(f"${price:,.2f}")

    def set_balance(self, bal: float):
        self._balance.set_value(f"${bal:,.2f}")

    def set_status(self, text: str, color: str = "#10b981"):
        self._status.set_value(text)
        self._status.set_accent(color)

    def set_last_update(self):
        now = datetime.now().strftime("%H:%M:%S")
        self._last_update.setText(f"last update: {now}")

    def refresh_from_df(self, df: pd.DataFrame):
        if df is None or "outcome" not in df.columns:
            return

        settled = df[df["outcome"].isin(["win", "loss"])].copy()

        # Show zeroes rather than dashes when there are no settled trades yet
        if settled.empty:
            self._wr.set_value("—")
            self._trades.set_value("0")
            self._losses.set_value("0")
            self._losses.set_accent("#374151")
            return

        # Use pre-computed pnl column if present (Kalshi data),
        # otherwise reconstruct from price and filled_count (CSV data).
        if "pnl" in settled.columns:
            settled["pnl"] = pd.to_numeric(settled["pnl"], errors="coerce").fillna(0.0)
        else:
            settled["price"]        = pd.to_numeric(settled.get("price",        pd.Series()), errors="coerce")
            settled["filled_count"] = pd.to_numeric(settled.get("filled_count", pd.Series()), errors="coerce")
            settled = settled.dropna(subset=["price", "filled_count"])

            def row_pnl(r):
                try:
                    p = float(r["price"]); c = float(r["filled_count"])
                    fee   = 0.07 * p * (1 - p) * c
                    gross = (1 - p) * c if r["outcome"] == "win" else -p * c
                    return gross - fee
                except Exception:
                    return 0.0

            settled["pnl"] = settled.apply(row_pnl, axis=1)

        total  = settled["pnl"].sum()
        wr     = (settled["outcome"] == "win").mean()
        losses = (settled["outcome"] == "loss").sum()
        n      = len(settled)

        color = "#10b981" if total >= 0 else "#ef4444"
        self._pnl.set_value(f"${total:+.2f}")
        self._pnl.set_accent(color)
        self._wr.set_value(f"{wr:.1%}")
        self._trades.set_value(str(n))
        self._losses.set_value(str(losses))
        self._losses.set_accent("#374151" if losses == 0 else "#ef4444")


# ─────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BTC 15M — Threshold Monitor")
        self.resize(1280, 820)
        self.setMinimumSize(900, 600)

        self._df        = None   # CSV data (internal reference only)
        self._kalshi_df = pd.DataFrame()  # authoritative Kalshi trade data
        self._btc_price = 0.0
        self._current_market = {}

        self._build_ui()
        self._start_worker()
        self._start_timers()
        self._load_csvs()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        self._sidebar = Sidebar()
        root.addWidget(self._sidebar)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #1a1e27;")
        root.addWidget(sep)

        # Main tab area
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, stretch=1)

        self._live_panel    = LiveSignalPanel()
        self._history_panel = TradeHistoryPanel()
        self._pnl_panel     = PnLPanel()
        self._analytics     = AnalyticsPanel()
        self._bot_log       = self._make_bot_log()

        self._tabs.addTab(self._live_panel,    "  Live Signal  ")
        self._tabs.addTab(self._history_panel, "  Trade History  ")
        self._tabs.addTab(self._pnl_panel,     "  P&L Charts  ")
        self._tabs.addTab(self._analytics,     "  Analytics  ")
        self._tabs.addTab(self._bot_log,       "  Bot Log  ")

        # Refresh charts when switching to chart tabs
        self._tabs.currentChanged.connect(self._on_tab_changed)

    def _make_bot_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setStyleSheet("""
            QTextEdit {
                background: #0a0c10;
                color: #9ca3af;
                font-family: 'SF Mono', 'Consolas', monospace;
                font-size: 19px;
                border: 1px solid #1a1e27;
                border-radius: 4px;
            }
        """)
        lay.addWidget(self._log_text)
        return w

    def _append_log(self, line: str):
        self._log_text.append(line)
        # Keep last 500 lines
        doc = self._log_text.document()
        if doc.blockCount() > 500:
            cursor = self._log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _on_trade_placed(self, ticker: str, side: str, count: int, price: float):
        color = "#10b981" if side == "yes" else "#3b82f6"
        self._log_text.append(
            f'<span style="color:{color}">🎯 TRADE: {side.upper()} ×{count} '
            f'@ ${price:.4f} — {ticker}</span>'
        )

    def _start_worker(self):
        self._worker = DataWorker()
        self._worker.btc_updated.connect(self._on_btc)
        self._worker.market_updated.connect(self._on_market)
        self._worker.balance_updated.connect(self._on_balance)
        self._worker.pnl_updated.connect(self._on_kalshi_pnl)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()
        self._sidebar.set_status("connecting…", "#f59e0b")

        self._bot_worker: BotWorker | None = None
        self._sidebar.toggle_btn.toggled.connect(self._on_toggle_trading)

    def _start_timers(self):
        # Countdown ticker (every second)
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._live_panel.tick_countdown)
        self._tick_timer.start(1000)

        # CSV reload
        self._csv_timer = QTimer(self)
        self._csv_timer.timeout.connect(self._load_csvs)
        self._csv_timer.start(CSV_REFRESH)

        # Chart refresh
        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_charts)
        self._chart_timer.start(CHART_REFRESH)

    def _load_csvs(self):
        """
        Load trade_results.csv for internal reference only.
        Performance panels (sidebar, P&L charts, analytics, history)
        are now fed exclusively from Kalshi fills via _on_kalshi_pnl.
        The CSV contains paper trades and estimates — not ground truth.
        """
        try:
            if os.path.exists(RESULTS_FILE):
                df = self._read_results_csv()
                if df is not None and not df.empty and "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["timestamp"], utc=True, errors="coerce"
                    )
                    df = df[
                        df["timestamp"] >= pd.Timestamp("2026-05-01", tz="UTC")
                    ].reset_index(drop=True)
                self._df = df if df is not None else pd.DataFrame()
            else:
                self._df = pd.DataFrame()
        except Exception as e:
            print(f"CSV load error: {e}")

    def _read_results_csv(self) -> "pd.DataFrame | None":
        """
        Read trade_results.csv handling mixed row formats:
          - 14-col rows: old format (no entry_seconds_left/threshold_at_entry)
          - 16-col rows: new format written by BotWorker (full columns)
          - QUOTE_ALL wrapping: entire row as one outer-quoted blob
        Pads short rows with empty strings so all rows fit the widest header.
        """
        try:
            rows   = []
            header = None

            def parse_line(line: str) -> list[str]:
                parsed = next(csv.reader([line]))
                if len(parsed) == 1:
                    inner  = parsed[0].strip('"')
                    parsed = next(csv.reader([inner]))
                return [v.strip('"') for v in parsed]

            with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    parsed = parse_line(line)
                    if header is None:
                        # Use the widest possible header (16 cols)
                        if len(parsed) < 16:
                            parsed = parsed + [
                                "entry_seconds_left", "threshold_at_entry"
                            ][:(16 - len(parsed))]
                        header = parsed
                        continue
                    # Pad short rows to match header length
                    if len(parsed) < len(header):
                        parsed = parsed + [""] * (len(header) - len(parsed))
                    if len(parsed) == len(header):
                        rows.append(parsed)
                    # skip rows longer than header (genuinely malformed)

            if not header or not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=header)
            for col in ["price", "filled_count", "variation_pct",
                        "baseline_btc", "final_btc", "utc_hour",
                        "entry_seconds_left", "threshold_at_entry"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            print(f"CSV read error: {e}")
            return None

    def _kalshi_data(self) -> "pd.DataFrame":
        """Return the authoritative Kalshi-sourced trade DataFrame, or empty."""
        return getattr(self, "_kalshi_df", pd.DataFrame())

    def _refresh_charts(self):
        df  = self._kalshi_data()
        tab = self._tabs.currentIndex()
        if tab == 2:
            self._pnl_panel.refresh(df)
        elif tab == 3:
            self._analytics.refresh(df)

    def _on_tab_changed(self, idx: int):
        df = self._kalshi_data()
        if idx == 1:
            self._history_panel.refresh(df)
        elif idx == 2:
            self._pnl_panel.refresh(df)
        elif idx == 3:
            self._analytics.refresh(df)

    def _on_toggle_trading(self, active: bool):
        self._sidebar.set_trading_active(active)
        if active:
            dry_run = False
            self._bot_worker = BotWorker(dry_run=dry_run)
            self._bot_worker.log_line.connect(self._append_log)
            self._bot_worker.trade_placed.connect(self._on_trade_placed)
            self._bot_worker.daily_pnl_changed.connect(
                lambda pnl: self._sidebar._pnl.set_value(f"${pnl:+.2f}")
            )
            self._bot_worker.start()
            self._tabs.setCurrentIndex(4)   # jump to bot log
            self._append_log("━━━ Trading session started ━━━")
        else:
            if self._bot_worker:
                self._bot_worker.stop()
                self._bot_worker.wait(3000)
                self._bot_worker = None
            self._append_log("━━━ Trading session stopped ━━━")

    def _on_kalshi_pnl(self, pnl: float, trades: list):
        """
        Kalshi fills are the authoritative source for all performance data.
        Always updates the display — shows $+0.00 and zero counts rather
        than leaving dashes if there are no settled trades yet.
        """
        # Build DataFrame from trade list
        if trades:
            df = pd.DataFrame(trades)
            df = df.rename(columns={"avg_price": "price", "count": "filled_count"})
            df["timestamp"]    = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df["price"]        = pd.to_numeric(df["price"],        errors="coerce")
            df["filled_count"] = pd.to_numeric(df["filled_count"], errors="coerce")
            df["pnl"]          = pd.to_numeric(df["pnl"],          errors="coerce").fillna(0.0)
            self._kalshi_df    = df
        else:
            df = pd.DataFrame()
            self._kalshi_df = df

        # Always update sidebar — show real numbers or zero, never dashes
        color = "#10b981" if pnl >= 0 else "#ef4444"
        self._sidebar._pnl.set_value(f"${pnl:+.2f}")
        self._sidebar._pnl.set_accent(color)
        self._sidebar.refresh_from_df(df)

        # Update whichever chart/history tab is visible
        self._history_panel.refresh(df)
        tab = self._tabs.currentIndex()
        if tab == 2:
            self._pnl_panel.refresh(df)
        elif tab == 3:
            self._analytics.refresh(df)

    def _on_btc(self, price: float):
        self._btc_price = price
        self._sidebar.set_btc(price)
        self._sidebar.set_status("live", "#10b981")
        self._sidebar.set_last_update()
        self._live_panel.update_market(self._current_market, price)

    def _on_market(self, mkt: dict):
        self._current_market = mkt
        self._live_panel.update_market(mkt, self._btc_price)

    def _on_balance(self, bal: float):
        self._sidebar.set_balance(bal)

    def _on_worker_error(self, msg: str):
        self._sidebar.set_status("error", "#ef4444")
        print(f"Worker error: {msg}")

    def closeEvent(self, event):
        if self._bot_worker:
            self._bot_worker.stop()
            self._bot_worker.wait(3000)
        self._worker.stop()
        self._worker.wait(2000)
        event.accept()


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    # Force dark palette so native widgets inherit dark bg
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window,          QColor("#0c0e12"))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor("#e0e2e8"))
    palette.setColor(QPalette.ColorRole.Base,            QColor("#0f1218"))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor("#13161c"))
    palette.setColor(QPalette.ColorRole.Text,            QColor("#c8cad4"))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor("#e0e2e8"))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor("#1e2840"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#e0e2e8"))
    app.setPalette(palette)

    win = MainWindow()

    # Centre the window on whichever screen the cursor is currently on,
    # so it always launches fully visible regardless of multi-monitor setup.
    primary   = app.primaryScreen()
    available = primary.availableGeometry()   # excludes taskbar etc.
    win.resize(
        min(win.width(),  available.width()),
        min(win.height(), available.height()),
    )
    win.move(
        available.x() + (available.width()  - win.width())  // 2,
        available.y() + (available.height() - win.height()) // 2,
    )

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
