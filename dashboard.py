"""
dashboard.py
------------
PyQt6 trading dashboard — live view of bot performance.

Data source: Kalshi API (live, on every refresh)
Supplementary: btc_prices.csv for σ calibration tab

Run:
    python dashboard.py

Requires:
    pip install PyQt6 requests cryptography
"""

import sys
import csv
import os
import math
import time
import base64
from datetime import datetime, timezone
from collections import defaultdict

import subprocess
import threading
import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QScrollArea, QSplitter, QPushButton, QTabWidget,
    QPlainTextEdit,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject
from PyQt6.QtGui import QColor, QFont, QPalette, QBrush

# ========================= CONFIG =========================
API_KEY_ID       = "50952777-89c2-4d13-b24b-d7820f8b8931"
PRIVATE_KEY_PATH = "C:/Users/pcdox/Desktop/projects/openclaw/chave2.pem"

PRICES_FILE      = "btc_prices.csv"
REFRESH_MS       = 15_000   # auto-refresh every 15 seconds
LOCAL_TZ_OFFSET  = 1        # UTC+1 (Western European Summer Time / WEST)

# ========================= COLOUR PALETTE =========================
C = {
    "bg":           "#0a0c0f",
    "bg2":          "#10141a",
    "bg3":          "#161b24",
    "border":       "#1e2530",
    "border2":      "#2a3444",
    "text":         "#c8d4e0",
    "text_dim":     "#4a5a6e",
    "text_bright":  "#e8f0f8",
    "green":        "#00e676",
    "green_dim":    "#1a3a2a",
    "red":          "#ff4444",
    "red_dim":      "#3a1a1a",
    "yellow":       "#ffd740",
    "yellow_dim":   "#3a3010",
    "blue":         "#40c4ff",
    "blue_dim":     "#0a2030",
    "purple":       "#ce93d8",
    "accent":       "#00e5ff",
    "accent_dim":   "#003040",
}

MONO = "JetBrains Mono, Consolas, Courier New, monospace"
SANS = "IBM Plex Sans, Segoe UI, system-ui, sans-serif"


# ========================= STYLESHEET =========================
STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {C['bg']};
    color: {C['text']};
    font-family: {SANS};
    font-size: 13px;
}}

QTabWidget::pane {{
    border: 1px solid {C['border']};
    background: {C['bg2']};
}}

QTabBar::tab {{
    background: {C['bg3']};
    color: {C['text_dim']};
    padding: 8px 20px;
    border: 1px solid {C['border']};
    border-bottom: none;
    font-family: {SANS};
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
}}

QTabBar::tab:selected {{
    background: {C['bg2']};
    color: {C['accent']};
    border-top: 2px solid {C['accent']};
}}

QTabBar::tab:hover:!selected {{
    color: {C['text']};
    background: {C['bg2']};
}}

QTableWidget {{
    background: {C['bg2']};
    gridline-color: {C['border']};
    border: none;
    font-family: {MONO};
    font-size: 12px;
    selection-background-color: {C['accent_dim']};
    selection-color: {C['text_bright']};
}}

QTableWidget::item {{
    padding: 6px 12px;
    border-bottom: 1px solid {C['border']};
}}

QHeaderView::section {{
    background: {C['bg3']};
    color: {C['text_dim']};
    padding: 8px 12px;
    border: none;
    border-right: 1px solid {C['border']};
    border-bottom: 1px solid {C['border2']};
    font-family: {SANS};
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    font-weight: bold;
}}

QScrollBar:vertical {{
    background: {C['bg2']};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {C['border2']};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {C['bg2']};
    height: 6px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {C['border2']};
    border-radius: 3px;
}}

QPushButton {{
    background: {C['bg3']};
    color: {C['text']};
    border: 1px solid {C['border2']};
    padding: 6px 16px;
    font-family: {SANS};
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
}}
QPushButton:hover {{
    background: {C['accent_dim']};
    color: {C['accent']};
    border-color: {C['accent']};
}}
QPushButton:pressed {{
    background: {C['accent']};
    color: {C['bg']};
}}

QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {C['border']};
}}

QLabel {{
    background: transparent;
}}

QSplitter::handle {{
    background: {C['border']};
    width: 1px;
    height: 1px;
}}
"""


# ========================= DATA LAYER =========================

# ── Kalshi API client ────────────────────────────────────────
_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_kalshi_session = requests.Session()

with open(PRIVATE_KEY_PATH, "r") as _f:
    _private_key = serialization.load_pem_private_key(
        _f.read().strip().encode(), password=None
    )


def _sign(method: str, path: str) -> dict:
    ts  = str(int(time.time() * 1000))
    msg = ts + method.upper() + path
    sig = _private_key.sign(
        msg.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }


def _kalshi_get(path: str, params: dict | None = None) -> dict:
    """
    GET a Kalshi API endpoint with RSA-PSS auth.
    path should be the short form, e.g. "/portfolio/trades"
    The full signing path and URL are constructed here.
    """
    full_path = f"/trade-api/v2{path}"
    headers   = _sign("GET", full_path)
    resp = _kalshi_session.get(
        f"{_KALSHI_BASE}{path}",
        headers=headers,
        params=params or {},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()


def fetch_kalshi_positions() -> list[dict]:
    """
    Build a full position history from two endpoints:

    /portfolio/fills       — every executed fill (buy or sell)
      key fields: ticker, side, action, count_fp, yes_price_dollars,
                  no_price_dollars, created_time, fee_cost

    /portfolio/settlements — every settled market
      key fields: ticker, market_result, revenue,
                  yes_count_fp, no_count_fp,
                  yes_total_cost_dollars, no_total_cost_dollars,
                  settled_time, fee_cost

    Strategy:
      1. Paginate fills  → group by ticker → compute side/count/avg_price
      2. Paginate settlements → keyed by ticker → get result + revenue
      3. For fills with no settlement entry, fetch market status individually
      4. Merge into position records
    """

    # ── 1. Fetch all fills ──
    all_fills: list[dict] = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data   = _kalshi_get("/portfolio/fills", params)
            fills  = data.get("fills", [])
            cursor = data.get("cursor")
            all_fills.extend(fills)
            if not fills or not cursor:
                break
        except Exception as e:
            print(f"[DATA] Fills fetch error: {e}")
            break

    # ── 2. Fetch all settlements — paginate fully ──
    # Must paginate all pages because KXBTC15M settlements may be
    # behind other series (KXLOWTCHI etc.) in the response ordering.
    all_settlements: dict[str, dict] = {}
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data        = _kalshi_get("/portfolio/settlements", params)
            settlements = data.get("settlements", [])
            cursor      = data.get("cursor")
            for s in settlements:
                t = s.get("ticker", "")
                if t:
                    all_settlements[t] = s
            if not cursor or not settlements:
                break
        except Exception as e:
            print(f"[DATA] Settlements fetch error: {e}")
            break

    if not all_fills and not all_settlements:
        return []

    # ── 3. Group fills by ticker — KXBTC15M only ──
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for f in all_fills:
        t = f.get("ticker", "")
        if t and t.startswith("KXBTC15M"):
            by_ticker[t].append(f)

    # Also include KXBTC15M tickers from settlements with no fills
    for t in all_settlements:
        if t.startswith("KXBTC15M") and t not in by_ticker:
            by_ticker[t] = []

    # ── 4. Fetch market status for open/unknown tickers ──
    # Only needed for tickers not in settlements
    market_cache: dict[str, dict] = {}
    unsettled = [t for t in by_ticker if t not in all_settlements]
    for ticker in unsettled:
        try:
            data = _kalshi_get(f"/markets/{ticker}")
            market_cache[ticker] = data.get("market", {})
        except Exception:
            market_cache[ticker] = {}

    # ── 5. Build position records ──
    positions: list[dict] = []

    for ticker, fills in by_ticker.items():
        settlement = all_settlements.get(ticker)
        mkt        = market_cache.get(ticker, {})

        # Aggregate fills
        def _fp(v):
            try: return float(v or 0)
            except (TypeError, ValueError): return 0.0

        yes_bought = sum(_fp(f.get("count_fp")) for f in fills if f.get("side") == "yes" and f.get("action") == "buy")
        no_bought  = sum(_fp(f.get("count_fp")) for f in fills if f.get("side") == "no"  and f.get("action") == "buy")
        yes_sold   = sum(_fp(f.get("count_fp")) for f in fills if f.get("side") == "yes" and f.get("action") == "sell")
        no_sold    = sum(_fp(f.get("count_fp")) for f in fills if f.get("side") == "no"  and f.get("action") == "sell")

        # Track total bought separately — for settled positions held to expiry,
        # contracts are consumed by settlement (not sold back), so net = 0
        # but we still need the original count.
        total_yes_bought = yes_bought
        total_no_bought  = no_bought

        # Legacy positions with no fills at all — use settlement counts
        if not fills and settlement:
            yes_fp = _fp(settlement.get("yes_count_fp"))
            no_fp  = _fp(settlement.get("no_count_fp"))
            if yes_fp > 0:
                yes_bought = total_yes_bought = yes_fp
            elif no_fp > 0:
                no_bought = total_no_bought = no_fp

        net_yes = yes_bought - yes_sold
        net_no  = no_bought  - no_sold

        if net_yes > 0:
            side, count = "yes", net_yes
        elif net_no > 0:
            side, count = "no", net_no
        elif total_yes_bought > 0:
            # Settled YES position — held to expiry, contracts consumed
            side, count = "yes", total_yes_bought
        elif total_no_bought > 0:
            # Settled NO position — held to expiry, contracts consumed
            side, count = "no", total_no_bought
        elif settlement:
            # No fills at all — infer side from which cost field is non-zero
            yes_cost = _fp(settlement.get("yes_total_cost_dollars"))
            no_cost  = _fp(settlement.get("no_total_cost_dollars"))
            if yes_cost > 0:
                side  = "yes"
                count = max(1, round(_fp(settlement.get("yes_count_fp"))))
            elif no_cost > 0:
                side  = "no"
                count = max(1, round(_fp(settlement.get("no_count_fp"))))
            else:
                side, count = "yes", 0
        else:
            side, count = "yes", 0

        count = max(1, round(count)) if count > 0 else 0

        # Average entry price from fills
        buy_fills = [f for f in fills if f.get("action") == "buy" and f.get("side") == side]
        if buy_fills:
            price_key  = "yes_price_dollars" if side == "yes" else "no_price_dollars"
            total_cost = sum(_fp(f.get(price_key)) * _fp(f.get("count_fp")) for f in buy_fills)
            total_cnt  = sum(_fp(f.get("count_fp")) for f in buy_fills)
            avg_price  = total_cost / total_cnt if total_cnt > 0 else 0.0
        elif settlement:
            if side == "yes":
                cost = _fp(settlement.get("yes_total_cost_dollars"))
                cnt  = _fp(settlement.get("yes_count_fp")) or 1.0
            else:
                cost = _fp(settlement.get("no_total_cost_dollars"))
                cnt  = _fp(settlement.get("no_count_fp")) or 1.0
            avg_price = cost / cnt if cnt > 0 else 0.0
        else:
            avg_price = 0.0

        # Exit reason
        has_sell    = any(f.get("action") == "sell" for f in fills)
        exit_reason = "take_profit" if has_sell else "expiry"

        # Result + outcome
        if settlement:
            result = settlement.get("market_result") or ""
            status = "settled"
        else:
            result = mkt.get("result") or ""
            status = mkt.get("status") or "unknown"

        if result in ("yes", "no"):
            outcome = "win" if result == side else "loss"
        elif status in ("open", "active"):
            outcome = "open"
        else:
            outcome = "unknown"

        # PnL — settlement revenue is the most accurate source.
        # revenue field is an integer in cents; cost fields are in dollars.
        try:
            if settlement and result in ("yes", "no"):
                revenue   = _fp(settlement.get("revenue")) / 100.0
                fee_cost  = _fp(settlement.get("fee_cost"))
                cost_paid = _fp(settlement.get("yes_total_cost_dollars" if side == "yes" else "no_total_cost_dollars"))
                pnl = revenue - cost_paid - fee_cost
            elif result in ("yes", "no") and avg_price > 0 and count > 0:
                pnl = (1.0 - avg_price) * count if result == side else -avg_price * count
            else:
                pnl = 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            pnl = 0.0

        # Timestamp — prefer fills, fall back to settlement
        if fills:
            timestamps = [f.get("created_time", "") for f in fills if f.get("created_time")]
            first_time = min(timestamps) if timestamps else ""
        elif settlement:
            first_time = settlement.get("settled_time", "")
        else:
            first_time = ""

        utc_hour, sess, ts_fmt = 0, "unknown", ""
        if first_time:
            try:
                dt       = datetime.fromisoformat(first_time.replace("Z", "+00:00"))
                utc_hour = dt.hour
                sess     = _get_session(utc_hour)
                # Display in local time
                local_dt = dt.replace(hour=(dt.hour + LOCAL_TZ_OFFSET) % 24)
                ts_fmt   = local_dt.strftime("%m-%d %H:%M")
            except Exception:
                ts_fmt = first_time[:16]

        # Ticker display — works for any series, not just KXBTC15M
        try:
            parts          = ticker.split("-")
            hhmm           = parts[-2]
            ticker_display = f"{hhmm[-4:-2]}:{hhmm[-2:]}" if hhmm.isdigit() else ticker
        except Exception:
            ticker_display = ticker

        positions.append({
            "ticker":           ticker,
            "ticker_display":   ticker_display,
            "first_trade_time": ts_fmt,
            "utc_hour":         utc_hour,
            "session":          sess,
            "side":             side,
            "count":            count,
            "avg_price":        round(avg_price, 4),
            "result":           result or "pending",
            "outcome":          outcome,
            "pnl":              round(pnl, 4),
            "exit_reason":      exit_reason,
            "market_status":    status,
        })

    positions.sort(key=lambda x: x["first_trade_time"])
    return positions


def _get_session(utc_hour: int) -> str:
    """Map UTC hour to trading session using local time (UTC+LOCAL_TZ_OFFSET)."""
    local_hour = (utc_hour + LOCAL_TZ_OFFSET) % 24
    if 0 <= local_hour < 6:
        return "asia"
    elif 6 <= local_hour < 12:
        return "europe"
    elif 12 <= local_hour < 20:
        return "us"
    else:
        return "us_close"


def load_csv(path: str) -> list[dict]:
    """Still used for btc_prices.csv (σ calibration tab)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def calc_stats(positions: list[dict]) -> dict:
    settled = [p for p in positions if p.get("outcome") in ("win", "loss")]
    wins    = [p for p in settled if p["outcome"] == "win"]
    losses  = [p for p in settled if p["outcome"] == "loss"]

    total_trades = len(settled)
    win_rate     = len(wins) / total_trades if total_trades else 0.0
    total_pnl    = sum(p["pnl"] for p in settled)

    by_session: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for p in settled:
        s = p.get("session", "unknown")
        by_session[s]["pnl"] += p["pnl"]
        by_session[s]["wins" if p["outcome"] == "win" else "losses"] += 1

    by_side: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for p in settled:
        by_side[p.get("side", "unknown")]["wins" if p["outcome"] == "win" else "losses"] += 1

    streak, streak_type = 0, ""
    for p in reversed(settled):
        if not streak_type:
            streak_type = p["outcome"]
        if p["outcome"] == streak_type:
            streak += 1
        else:
            break

    tp_exits = sum(1 for p in settled if p.get("exit_reason") == "take_profit")
    unknowns = sum(1 for p in positions if p["outcome"] == "unknown")
    opens    = sum(1 for p in positions if p["outcome"] == "open")

    pnl_series: list[dict] = []
    cumulative = peak = max_dd = 0.0
    for p in settled:
        cumulative += p["pnl"]
        peak        = max(peak, cumulative)
        drawdown    = cumulative - peak
        max_dd      = min(max_dd, drawdown)
        pnl_series.append({
            "ts":         p["first_trade_time"],
            "pnl":        round(p["pnl"], 4),
            "cumulative": round(cumulative, 4),
            "drawdown":   round(drawdown, 4),
            "outcome":    p["outcome"],
            "side":       p.get("side", ""),
            "session":    p.get("session", ""),
        })

    rolling_wr = 0.0
    if len(settled) >= 10:
        rolling_wr = sum(1 for p in settled[-10:] if p["outcome"] == "win") / 10

    return {
        "total_trades": total_trades,
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     win_rate,
        "rolling_wr":   rolling_wr,
        "total_pnl":    total_pnl,
        "max_drawdown": max_dd,
        "by_session":   dict(by_session),
        "by_side":      dict(by_side),
        "streak":       streak,
        "streak_type":  streak_type,
        "tp_exits":     tp_exits,
        "unknowns":     unknowns,
        "opens":        opens,
        "pnl_series":   pnl_series,
    }


def calc_kelly_preview(positions: list[dict], side: str) -> str:
    relevant = [
        p for p in positions
        if p.get("side") == side
        and p.get("outcome") in ("win", "loss")
        and p.get("exit_reason") != "take_profit"
    ]
    if len(relevant) < 30:
        return f"fallback (n={len(relevant)}/30)"
    wins  = sum(1 for p in relevant if p["outcome"] == "win")
    p_win = wins / len(relevant)
    prices = [p["avg_price"] for p in relevant if p.get("avg_price", 0) > 0]
    if not prices:
        return "n/a"
    avg_p  = sum(prices) / len(prices)
    b      = (1.0 - avg_p) / avg_p if avg_p > 0 else 1.0
    q      = 1.0 - p_win
    kelly  = (b * p_win - q) / b
    capped = max(0.05, min(0.30, kelly / 2.0))
    return f"{capped:.1%} (raw {kelly:.1%})"

# ========================= WIDGETS =========================
class StatCard(QFrame):
    """A single KPI tile."""

    def __init__(
        self,
        title: str,
        value: str = "—",
        subtitle: str = "",
        color: str = C["text"],
        parent=None
    ):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.Box)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C['bg2']};
                border: 1px solid {C['border']};
                border-radius: 2px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        self._title_lbl = QLabel(title.upper())
        self._title_lbl.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 10px; "
            f"letter-spacing: 2px; "
            f"font-family: {SANS};"
        )

        self._value_lbl = QLabel(value)
        self._value_lbl.setStyleSheet(
            f"color: {color}; "
            f"font-size: 26px; "
            f"font-family: {MONO}; "
            f"font-weight: bold;"
        )

        self._sub_lbl = QLabel(subtitle)
        self._sub_lbl.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 11px; "
            f"font-family: {MONO};"
        )

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)
        if subtitle:
            layout.addWidget(self._sub_lbl)

    def update(self, value: str, subtitle: str = "", color: str = C["text"]):
        self._value_lbl.setText(value)
        self._value_lbl.setStyleSheet(
            f"color: {color}; "
            f"font-size: 26px; "
            f"font-family: {MONO}; "
            f"font-weight: bold;"
        )
        if subtitle:
            self._sub_lbl.setText(subtitle)


class SectionHeader(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 10px; "
            f"letter-spacing: 3px; "
            f"font-family: {SANS}; "
            f"font-weight: bold; "
            f"padding: 8px 0 4px 0; "
            f"border-bottom: 1px solid {C['border']};"
        )


def make_table(headers: list[str], row_height: int = 32) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents
    )
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(row_height)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setShowGrid(False)
    t.setAlternatingRowColors(False)
    t.setStyleSheet(
        f"QTableWidget::item:alternate {{ background: {C['bg3']}; }}"
    )
    return t


def cell(
    text: str,
    color: str = C["text"],
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    bold: bool = False,
    mono: bool = True,
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QBrush(QColor(color)))
    item.setTextAlignment(
        align | Qt.AlignmentFlag.AlignVCenter
    )
    font = QFont(
        "JetBrains Mono" if mono else "IBM Plex Sans",
        11 if mono else 12
    )
    font.setBold(bold)
    item.setFont(font)
    return cell


def make_item(
    text: str,
    fg: str = C["text"],
    bg: str = "",
    bold: bool = False,
    align=Qt.AlignmentFlag.AlignLeft,
    mono: bool = True,
) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setForeground(QBrush(QColor(fg)))
    if bg:
        item.setBackground(QBrush(QColor(bg)))
    item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
    f = QFont("JetBrains Mono" if mono else "IBM Plex Sans", 11)
    f.setBold(bold)
    item.setFont(f)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


# ========================= TABS =========================
class OverviewTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── KPI row ──
        kpi_layout = QHBoxLayout()
        kpi_layout.setSpacing(10)

        self.card_trades   = StatCard("Total Trades", "—")
        self.card_winrate  = StatCard("Win Rate",     "—")
        self.card_pnl      = StatCard("Total PnL",    "—")
        self.card_streak   = StatCard("Current Streak", "—")
        self.card_tp       = StatCard("Take Profit Exits", "—")
        self.card_unknowns = StatCard("Unknown Outcomes",  "—")

        for card in (
            self.card_trades, self.card_winrate, self.card_pnl,
            self.card_streak, self.card_tp, self.card_unknowns
        ):
            kpi_layout.addWidget(card)

        layout.addLayout(kpi_layout)

        # ── Session breakdown ──
        layout.addWidget(SectionHeader("Session Breakdown"))
        self.session_table = make_table(
            ["Session", "Trades", "Wins", "Losses", "Win Rate", "PnL ($)"]
        )
        self.session_table.setMaximumHeight(180)
        layout.addWidget(self.session_table)

        # ── Side breakdown ──
        layout.addWidget(SectionHeader("Side Breakdown"))
        self.side_table = make_table(
            ["Side", "Trades", "Wins", "Losses", "Win Rate",
             "Kelly Preview"]
        )
        self.side_table.setMaximumHeight(120)
        layout.addWidget(self.side_table)

        # ── Recent trades ──
        layout.addWidget(SectionHeader("Recent Trades (last 20)"))
        self.recent_table = make_table([
            "Time (UTC)", "Ticker", "Session", "Side",
            "Price", "Contracts", "Outcome", "Exit", "PnL"
        ])
        layout.addWidget(self.recent_table)

        layout.addStretch(0)

    def refresh(self, positions: list[dict], stats: dict):
        # KPI cards
        total  = stats["total_trades"]
        wr     = stats["win_rate"]
        pnl    = stats["total_pnl"]
        streak = stats["streak"]
        st     = stats["streak_type"]

        wr_color = (
            C["green"] if wr >= 0.6 else
            C["yellow"] if wr >= 0.45 else
            C["red"]
        )
        pnl_color    = C["green"] if pnl >= 0 else C["red"]
        streak_color = C["green"] if st == "win" else C["red"] if st == "loss" else C["text"]

        self.card_trades.update(str(total))
        self.card_winrate.update(
            f"{wr:.1%}", f"{stats['wins']}W / {stats['losses']}L", wr_color
        )
        self.card_pnl.update(f"${pnl:+.2f}", "", pnl_color)
        self.card_streak.update(
            f"{streak} {st.upper()}" if st else "—", color=streak_color
        )
        self.card_tp.update(str(stats["tp_exits"]), color=C["blue"])
        self.card_unknowns.update(
            str(stats["unknowns"]),
            color=C["yellow"] if stats["unknowns"] > 0 else C["text_dim"]
        )

        # Session table
        sess_order = ["asia", "europe", "us", "us_close"]
        self.session_table.setRowCount(len(sess_order))
        for i, sess in enumerate(sess_order):
            s    = stats["by_session"].get(sess, {})
            w    = s.get("wins", 0)
            l    = s.get("losses", 0)
            n    = w + l
            wr_s = w / n if n > 0 else 0.0
            p    = s.get("pnl", 0.0)
            wr_c = (
                C["green"] if wr_s >= 0.6 else
                C["yellow"] if wr_s >= 0.45 else
                C["red"] if n > 0 else C["text_dim"]
            )
            self.session_table.setItem(i, 0, make_item(sess, C["text_bright"]))
            self.session_table.setItem(i, 1, make_item(str(n)))
            self.session_table.setItem(i, 2, make_item(str(w), C["green"]))
            self.session_table.setItem(i, 3, make_item(str(l), C["red"]))
            self.session_table.setItem(i, 4, make_item(f"{wr_s:.1%}", wr_c))
            self.session_table.setItem(i, 5, make_item(
                f"{p:+.4f}", C["green"] if p >= 0 else C["red"]
            ))

        # Side table
        sides = ["yes", "no"]
        self.side_table.setRowCount(len(sides))
        for i, side in enumerate(sides):
            s    = stats["by_side"].get(side, {})
            w    = s.get("wins", 0)
            l    = s.get("losses", 0)
            n    = w + l
            wr_s = w / n if n > 0 else 0.0
            wr_c = (
                C["green"] if wr_s >= 0.6 else
                C["yellow"] if wr_s >= 0.45 else
                C["red"] if n > 0 else C["text_dim"]
            )
            kelly = calc_kelly_preview(positions, side)
            self.side_table.setItem(i, 0, make_item(
                side.upper(), C["blue"] if side == "yes" else C["purple"]
            ))
            self.side_table.setItem(i, 1, make_item(str(n)))
            self.side_table.setItem(i, 2, make_item(str(w), C["green"]))
            self.side_table.setItem(i, 3, make_item(str(l), C["red"]))
            self.side_table.setItem(i, 4, make_item(f"{wr_s:.1%}", wr_c))
            self.side_table.setItem(i, 5, make_item(kelly, C["text_dim"]))

        # Recent trades — last 20 settled/open, newest first
        recent = [
            p for p in positions
            if p.get("outcome") != "unknown"
        ][-20:][::-1]

        self.recent_table.setRowCount(len(recent))
        for i, p in enumerate(recent):
            outcome    = p.get("outcome", "")
            exit_r     = p.get("exit_reason", "expiry")
            side       = p.get("side", "")
            avg_price  = p.get("avg_price", 0.0)
            count      = p.get("count", 0)
            pnl_r      = p.get("pnl", 0.0)

            o_color = (
                C["green"]  if outcome == "win"  else
                C["red"]    if outcome == "loss" else
                C["blue"]   if outcome == "open" else
                C["yellow"]
            )
            side_color = (
                C["blue"]   if side == "yes" else
                C["purple"] if side == "no"  else
                C["text_dim"]
            )

            self.recent_table.setItem(i, 0, make_item(p.get("first_trade_time", ""), C["text_dim"]))
            self.recent_table.setItem(i, 1, make_item(p.get("ticker_display", ""), C["text"]))
            self.recent_table.setItem(i, 2, make_item(p.get("session", ""), C["text_dim"]))
            self.recent_table.setItem(i, 3, make_item(side.upper() if side else "—", side_color))
            self.recent_table.setItem(i, 4, make_item(
                f"{avg_price:.3f}" if avg_price > 0 else "—"
            ))
            self.recent_table.setItem(i, 5, make_item(str(count) if count else "—"))
            self.recent_table.setItem(i, 6, make_item(
                outcome.upper() if outcome else "—",
                o_color, bold=outcome in ("win", "loss")
            ))
            self.recent_table.setItem(i, 7, make_item(
                "TP" if exit_r == "take_profit" else "EXP",
                C["blue"] if exit_r == "take_profit" else C["text_dim"]
            ))
            self.recent_table.setItem(i, 8, make_item(
                f"{pnl_r:+.4f}" if outcome in ("win", "loss") else "—",
                C["green"] if pnl_r > 0 else C["red"] if pnl_r < 0 else C["text_dim"]
            ))


class PositionsTab(QWidget):
    """Full Kalshi position history from positions_full.csv."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Summary row
        self.pos_summary = QLabel("Load positions_full.csv to see reconciled history")
        self.pos_summary.setStyleSheet(
            f"color: {C['text_dim']}; font-size: 12px; padding: 4px 0;"
        )
        layout.addWidget(self.pos_summary)

        layout.addWidget(SectionHeader("All Positions (from Kalshi)"))
        self.table = make_table([
            "Time (UTC)", "Ticker", "Session", "Side",
            "Count", "Avg Price", "Kalshi Result",
            "Outcome", "PnL ($)", "Exit", "Mismatch"
        ])
        layout.addWidget(self.table)

    def refresh(self, positions: list[dict]):
        if not positions:
            self.pos_summary.setText(
                "No positions_full.csv found — run position_tracker.py first"
            )
            self.table.setRowCount(0)
            return

        settled = [
            p for p in positions
            if p.get("outcome") in ("win", "loss")
        ]
        wins  = sum(1 for p in settled if p["outcome"] == "win")
        total_pnl = sum(float(p.get("pnl", 0)) for p in settled)
        mismatches = sum(1 for p in positions if p.get("mismatch"))

        self.pos_summary.setText(
            f"{len(positions)} total positions  |  "
            f"{len(settled)} settled  |  "
            f"{wins}W / {len(settled)-wins}L  |  "
            f"PnL: ${total_pnl:+.2f}  |  "
            f"{mismatches} mismatches"
        )

        sorted_pos = sorted(
            positions,
            key=lambda x: x.get("first_trade_time", ""),
            reverse=True
        )

        self.table.setRowCount(len(sorted_pos))
        for i, p in enumerate(sorted_pos):
            outcome = p.get("outcome", "")
            result  = p.get("kalshi_result", "")
            mismatch = p.get("mismatch", "")

            o_color = (
                C["green"] if outcome == "win"  else
                C["red"]   if outcome == "loss" else
                C["blue"]  if outcome == "open" else
                C["yellow"]
            )
            r_color = (
                C["green"] if result == "yes" else
                C["red"]   if result == "no"  else
                C["text_dim"]
            )

            try:
                pnl = float(p.get("pnl", 0))
            except (ValueError, TypeError):
                pnl = 0.0

            ts = p.get("first_trade_time", "")
            try:
                dt     = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts_fmt = dt.strftime("%m-%d %H:%M")
            except Exception:
                ts_fmt = ts[:16]

            ticker_short = p.get("ticker", "").replace("KXBTC15M-", "")

            side_color = (
                C["blue"] if p.get("side") == "yes" else
                C["purple"] if p.get("side") == "no" else
                C["text_dim"]
            )

            self.table.setItem(i, 0,  make_item(ts_fmt, C["text_dim"]))
            self.table.setItem(i, 1,  make_item(ticker_short))
            self.table.setItem(i, 2,  make_item(p.get("session", ""), C["text_dim"]))
            self.table.setItem(i, 3,  make_item(p.get("side", "").upper(), side_color))
            self.table.setItem(i, 4,  make_item(str(p.get("count", ""))))
            self.table.setItem(i, 5,  make_item(str(p.get("avg_entry_price", ""))))
            self.table.setItem(i, 6,  make_item(result.upper() if result else "PENDING", r_color))
            self.table.setItem(i, 7,  make_item(outcome.upper() if outcome else "—", o_color, bold=True))
            self.table.setItem(i, 8,  make_item(
                f"{pnl:+.4f}" if pnl != 0 else "—",
                C["green"] if pnl > 0 else C["red"] if pnl < 0 else C["text_dim"]
            ))
            self.table.setItem(i, 9,  make_item(p.get("local_exit_reason", "—"), C["text_dim"]))
            self.table.setItem(i, 10, make_item(
                mismatch if mismatch else "—",
                C["yellow"] if mismatch else C["text_dim"]
            ))


class SigmaTab(QWidget):
    """Current σ calibration state and threshold curves."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(SectionHeader("Session σ Parameters"))
        self.sigma_table = make_table([
            "Session", "σ/min", "Fat-tail k", "α (exponent)",
            "Status"
        ])
        self.sigma_table.setMaximumHeight(160)
        layout.addWidget(self.sigma_table)

        layout.addWidget(SectionHeader("Dynamic Threshold Curve"))
        self.curve_table = make_table([
            "Time Left", "Asia (90%)", "Europe (90%)",
            "US (90%)", "US Close (90%)"
        ])
        layout.addWidget(self.curve_table)

        layout.addWidget(SectionHeader("BTC Price Data Summary"))
        self.btc_table = make_table([
            "Session", "Observations", "Avg |Variation|",
            "Max |Variation|", "σ Sufficient?"
        ])
        self.btc_table.setMaximumHeight(160)
        layout.addWidget(self.btc_table)

        layout.addStretch(0)

    def refresh(self, prices: list[dict]):
        CONFIDENCE = 0.95

        def norm_ppf(p):
            a = (2.515517, 0.802853, 0.010328)
            b = (1.432788, 0.189269, 0.001308)
            t = math.sqrt(-2.0 * math.log(p if p < 0.5 else 1.0 - p))
            z = t - (a[0] + t*(a[1] + t*a[2])) / (1.0 + t*(b[0] + t*(b[1] + t*b[2])))
            return -z if p < 0.5 else z

        _Z = norm_ppf(1.0 - (1.0 - CONFIDENCE) / 2.0)

        sessions = ["asia", "europe", "us", "us_close"]

        # Default σ parameters — kept in sync with SESSION_SIGMA in main.py.
        # The dashboard reads these as display-only; calibration happens
        # inside the bot at runtime and is not imported here to avoid
        # coupling to the live process.
        defaults = {
            "asia":     (0.000301, 1.23, 0.50),
            "europe":   (0.000256, 1.23, 0.50),
            "us":       (0.000491, 1.40, 0.50),
            "us_close": (0.000491, 1.40, 0.50),
        }

        # Attempt to read calibrated values from btc_prices.csv by
        # replicating the same std-of-returns σ estimate the bot uses.
        # Falls back to defaults if insufficient data.
        current_sigma = dict(defaults)
        if prices:
            by_sess: dict[str, list[dict]] = defaultdict(list)
            for row in prices:
                s = row.get("session", "")
                if s in sessions:
                    by_sess[s].append(row)

            for sess, rows in by_sess.items():
                if len(rows) < 200:
                    continue
                # Group by ticker, compute per-minute returns
                by_ticker: dict[str, list[dict]] = defaultdict(list)
                for r in rows:
                    by_ticker[r.get("ticker", "")].append(r)

                minute_returns: list[float] = []
                for ticker_rows in by_ticker.values():
                    ticker_rows.sort(
                        key=lambda x: float(x.get("seconds_left", 0)),
                        reverse=True
                    )
                    for i in range(1, len(ticker_rows)):
                        try:
                            dt = (
                                float(ticker_rows[i-1]["seconds_left"]) -
                                float(ticker_rows[i]["seconds_left"])
                            )
                            if 50 <= dt <= 70:
                                p1 = float(ticker_rows[i-1]["btc_price"])
                                p2 = float(ticker_rows[i]["btc_price"])
                                minute_returns.append((p2 - p1) / p1)
                        except (KeyError, ValueError, TypeError):
                            continue

                if len(minute_returns) < 10:
                    continue

                n      = len(minute_returns)
                mean_r = sum(minute_returns) / n
                var    = sum((r - mean_r)**2 for r in minute_returns) / (n - 1)
                sigma_fit = max(1e-5, min(0.005, math.sqrt(var)))

                old_s, old_k, old_a = defaults[sess]
                current_sigma[sess] = (sigma_fit, old_k, old_a)

        # Sigma table
        self.sigma_table.setRowCount(len(sessions))
        for i, sess in enumerate(sessions):
            s, k, a = current_sigma.get(sess, defaults[sess])
            d_s, d_k, d_a = defaults[sess]
            calibrated = abs(s - d_s) > 1e-7 or abs(a - d_a) > 0.01
            status = "CALIBRATED" if calibrated else "DEFAULT"
            status_c = C["green"] if calibrated else C["yellow"]

            self.sigma_table.setItem(i, 0, make_item(sess, C["text_bright"]))
            self.sigma_table.setItem(i, 1, make_item(f"{s:.6f}"))
            self.sigma_table.setItem(i, 2, make_item(f"{k:.2f}"))
            self.sigma_table.setItem(i, 3, make_item(f"{a:.3f}"))
            self.sigma_table.setItem(i, 4, make_item(status, status_c))

        # Threshold curve
        time_points = [480, 420, 360, 300, 240, 180, 120, 90, 60, 30]
        self.curve_table.setRowCount(len(time_points))
        for i, t_s in enumerate(time_points):
            t_min = t_s / 60.0
            row_vals = [f"{t_s}s"]
            for sess in sessions:
                s, k, a = current_sigma.get(sess, defaults[sess])
                thr = _Z * s * k * (t_min ** a) * 100
                row_vals.append(f"{thr:.3f}%")
            for j, val in enumerate(row_vals):
                self.curve_table.setItem(
                    i, j,
                    make_item(val, C["text"] if j > 0 else C["text_dim"])
                )

        # BTC data summary
        if prices:
            by_sess: dict[str, list[float]] = defaultdict(list)
            for row in prices:
                try:
                    s = row.get("session", "")
                    v = abs(float(row.get("variation_pct", 0)))
                    if s in sessions:
                        by_sess[s].append(v)
                except (ValueError, TypeError):
                    pass

            self.btc_table.setRowCount(len(sessions))
            for i, sess in enumerate(sessions):
                vals = by_sess.get(sess, [])
                n    = len(vals)
                avg  = sum(vals) / n if vals else 0.0
                mx   = max(vals)  if vals else 0.0
                suff = n >= 200
                self.btc_table.setItem(i, 0, make_item(sess, C["text_bright"]))
                self.btc_table.setItem(i, 1, make_item(str(n)))
                self.btc_table.setItem(i, 2, make_item(f"{avg:.4f}%"))
                self.btc_table.setItem(i, 3, make_item(f"{mx:.4f}%"))
                self.btc_table.setItem(i, 4, make_item(
                    f"YES (n={n})" if suff else f"NO (n={n}/200)",
                    C["green"] if suff else C["yellow"]
                ))
        else:
            self.btc_table.setRowCount(0)


class PnLTab(QWidget):
    """
    PnL tracking tab — cumulative PnL chart, per-trade bars,
    drawdown curve, and rolling win rate.

    Drawn with QPainter directly (no external charting library needed).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── KPI row ──
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self.card_pnl    = StatCard("Total PnL",       "—")
        self.card_maxdd  = StatCard("Max Drawdown",     "—")
        self.card_rwr    = StatCard("Rolling WR (10)",  "—")
        self.card_avg_w  = StatCard("Avg Win",          "—")
        self.card_avg_l  = StatCard("Avg Loss",         "—")
        self.card_ratio  = StatCard("Win/Loss Ratio",   "—")
        for c in (self.card_pnl, self.card_maxdd, self.card_rwr,
                  self.card_avg_w, self.card_avg_l, self.card_ratio):
            kpi_row.addWidget(c)
        layout.addLayout(kpi_row)

        # ── Chart canvas ──
        layout.addWidget(SectionHeader("Cumulative PnL  ·  Drawdown  ·  Per-Trade"))

        self.canvas = _PnLCanvas()
        self.canvas.setMinimumHeight(340)
        layout.addWidget(self.canvas)

        # ── Trade-by-trade table ──
        layout.addWidget(SectionHeader("Trade History"))
        self.table = make_table([
            "#", "Time", "Side", "Session",
            "Trade PnL", "Cumulative", "Drawdown", "Outcome"
        ])
        self.table.setMaximumHeight(220)
        layout.addWidget(self.table)

    def refresh(self, stats: dict):
        series    = stats.get("pnl_series", [])
        total_pnl = stats.get("total_pnl", 0.0)
        max_dd    = stats.get("max_drawdown", 0.0)
        rwr       = stats.get("rolling_wr", 0.0)

        wins   = stats.get("wins", 0)
        losses = stats.get("losses", 0)

        # Avg win / loss
        win_pnls  = [p["pnl"] for p in series if p["outcome"] == "win"]
        loss_pnls = [p["pnl"] for p in series if p["outcome"] == "loss"]
        avg_w = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0.0
        avg_l = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
        ratio = abs(avg_w / avg_l) if avg_l != 0 else 0.0

        pnl_c  = C["green"] if total_pnl >= 0 else C["red"]
        dd_c   = C["red"]   if max_dd < -0.05  else C["yellow"] if max_dd < 0 else C["text"]
        rwr_c  = C["green"] if rwr >= 0.6 else C["yellow"] if rwr >= 0.45 else C["red"]

        self.card_pnl.update(f"${total_pnl:+.2f}", color=pnl_c)
        self.card_maxdd.update(f"${max_dd:.2f}",   color=dd_c)
        self.card_rwr.update(f"{rwr:.0%}",          color=rwr_c)
        self.card_avg_w.update(f"${avg_w:+.4f}",   color=C["green"])
        self.card_avg_l.update(f"${avg_l:+.4f}",   color=C["red"])
        self.card_ratio.update(
            f"{ratio:.2f}x",
            color=C["green"] if ratio >= 1.0 else C["yellow"]
        )

        # Pass data to canvas
        self.canvas.set_data(series)

        # Table
        self.table.setRowCount(len(series))
        for i, p in enumerate(reversed(series)):
            idx   = len(series) - i
            o_c   = C["green"] if p["outcome"] == "win" else C["red"]
            s_c   = C["blue"]  if p["side"] == "yes"   else C["purple"]
            dd_c2 = C["red"]   if p["drawdown"] < -0.01 else C["text_dim"]

            self.table.setItem(i, 0, make_item(str(idx), C["text_dim"]))
            self.table.setItem(i, 1, make_item(p["ts"],  C["text_dim"]))
            self.table.setItem(i, 2, make_item(p["side"].upper(), s_c))
            self.table.setItem(i, 3, make_item(p["session"], C["text_dim"]))
            self.table.setItem(i, 4, make_item(
                f"{p['pnl']:+.4f}",
                C["green"] if p["pnl"] > 0 else C["red"],
                bold=True
            ))
            self.table.setItem(i, 5, make_item(
                f"{p['cumulative']:+.4f}",
                C["green"] if p["cumulative"] >= 0 else C["red"]
            ))
            self.table.setItem(i, 6, make_item(
                f"{p['drawdown']:+.4f}", dd_c2
            ))
            self.table.setItem(i, 7, make_item(
                p["outcome"].upper(), o_c, bold=True
            ))


class _PnLCanvas(QWidget):
    """
    Custom painter canvas — three vertically stacked mini-charts:
      1. Cumulative PnL line
      2. Per-trade bars (green/red)
      3. Drawdown area
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: list[dict] = []
        self.setStyleSheet(f"background: {C['bg2']}; border: 1px solid {C['border']};")

    def set_data(self, series: list[dict]):
        self._series = series
        self.update()   # trigger repaint

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QPen, QPolygonF, QLinearGradient
        from PyQt6.QtCore import QPointF, QRectF

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        W = self.width()
        H = self.height()
        PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 12, 8

        # Background
        painter.fillRect(0, 0, W, H, QColor(C["bg2"]))

        series = self._series
        if not series:
            painter.setPen(QColor(C["text_dim"]))
            painter.drawText(
                QRectF(0, 0, W, H),
                Qt.AlignmentFlag.AlignCenter,
                "No trade data yet"
            )
            painter.end()
            return

        n = len(series)

        # ── Layout: three horizontal bands ──
        band_h  = (H - PAD_T - PAD_B) // 3
        y_cum   = PAD_T
        y_bar   = PAD_T + band_h
        y_dd    = PAD_T + band_h * 2

        chart_w = W - PAD_L - PAD_R

        def x_pos(i: int) -> float:
            if n <= 1:
                return PAD_L + chart_w / 2
            return PAD_L + i / (n - 1) * chart_w

        # ── Helper: draw band label ──
        def band_label(text: str, y_top: int):
            painter.setPen(QColor(C["text_dim"]))
            f = QFont("IBM Plex Sans", 9)
            painter.setFont(f)
            painter.drawText(
                QRectF(0, y_top + 2, PAD_L - 4, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                text
            )

        # ── Helper: draw horizontal gridline + value label ──
        def gridline(y: float, val: float, y_top: int):
            pen = QPen(QColor(C["border"]))
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawLine(int(PAD_L), int(y), int(W - PAD_R), int(y))
            painter.setPen(QColor(C["text_dim"]))
            f = QFont("JetBrains Mono", 8)
            painter.setFont(f)
            painter.drawText(
                QRectF(0, y - 8, PAD_L - 4, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{val:+.2f}"
            )

        # ══════════════════════════════════════
        # 1. CUMULATIVE PnL LINE
        # ══════════════════════════════════════
        cum_vals  = [p["cumulative"] for p in series]
        cum_min   = min(cum_vals + [0])
        cum_max   = max(cum_vals + [0])
        cum_range = max(cum_max - cum_min, 0.01)

        def cum_y(v: float) -> float:
            return y_cum + band_h - 4 - (v - cum_min) / cum_range * (band_h - 8)

        # Zero line
        gridline(cum_y(0), 0.0, y_cum)

        # Filled area under/over zero
        zero_y = cum_y(0)
        for i in range(n - 1):
            x1 = x_pos(i)
            x2 = x_pos(i + 1)
            y1 = cum_y(cum_vals[i])
            y2 = cum_y(cum_vals[i + 1])
            above = cum_vals[i] >= 0

            poly = QPolygonF([
                QPointF(x1, zero_y),
                QPointF(x1, y1),
                QPointF(x2, y2),
                QPointF(x2, zero_y),
            ])
            fill = QColor(C["green_dim"] if above else C["red_dim"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPolygon(poly)

        # Line
        pen = QPen(QColor(C["green"] if cum_vals[-1] >= 0 else C["red"]))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(n - 1):
            painter.drawLine(
                QPointF(x_pos(i),     cum_y(cum_vals[i])),
                QPointF(x_pos(i + 1), cum_y(cum_vals[i + 1]))
            )

        # Dots at each trade
        for i, p in enumerate(series):
            c = QColor(C["green"] if p["outcome"] == "win" else C["red"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawEllipse(QPointF(x_pos(i), cum_y(p["cumulative"])), 3, 3)

        band_label("PnL", y_cum)

        # ══════════════════════════════════════
        # 2. PER-TRADE BARS
        # ══════════════════════════════════════
        trade_vals = [p["pnl"] for p in series]
        t_abs_max  = max(abs(v) for v in trade_vals) if trade_vals else 0.01
        t_range    = max(t_abs_max * 2, 0.01)

        def bar_y_zero() -> float:
            return y_bar + band_h // 2

        bar_zero = bar_y_zero()
        # Thin zero line
        pen = QPen(QColor(C["border2"]))
        painter.setPen(pen)
        painter.drawLine(int(PAD_L), int(bar_zero), int(W - PAD_R), int(bar_zero))

        bar_w = max(2.0, chart_w / n - 1)
        for i, p in enumerate(series):
            v    = p["pnl"]
            h    = abs(v) / t_range * (band_h - 8)
            x    = x_pos(i) - bar_w / 2
            col  = QColor(C["green"] if v >= 0 else C["red"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col)
            if v >= 0:
                painter.drawRect(QRectF(x, bar_zero - h, bar_w, h))
            else:
                painter.drawRect(QRectF(x, bar_zero, bar_w, h))

        band_label("Trade", y_bar)

        # ══════════════════════════════════════
        # 3. DRAWDOWN AREA
        # ══════════════════════════════════════
        dd_vals  = [p["drawdown"] for p in series]
        dd_min   = min(dd_vals + [-0.001])
        dd_range = max(abs(dd_min), 0.001)

        def dd_y(v: float) -> float:
            # drawdown is always <= 0; map 0 → top of band, dd_min → bottom
            return y_dd + 4 + (abs(v) / dd_range) * (band_h - 8)

        dd_zero_y = y_dd + 4
        # Zero line
        pen = QPen(QColor(C["border2"]))
        painter.setPen(pen)
        painter.drawLine(int(PAD_L), int(dd_zero_y), int(W - PAD_R), int(dd_zero_y))

        # Filled area
        poly = QPolygonF()
        poly.append(QPointF(x_pos(0), dd_zero_y))
        for i, p in enumerate(series):
            poly.append(QPointF(x_pos(i), dd_y(p["drawdown"])))
        poly.append(QPointF(x_pos(n - 1), dd_zero_y))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C["red_dim"]))
        painter.drawPolygon(poly)

        # Outline
        pen = QPen(QColor(C["red"]))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(n - 1):
            painter.drawLine(
                QPointF(x_pos(i),     dd_y(dd_vals[i])),
                QPointF(x_pos(i + 1), dd_y(dd_vals[i + 1]))
            )

        band_label("DD", y_dd)

        # ── Dividers between bands ──
        pen = QPen(QColor(C["border"]))
        painter.setPen(pen)
        painter.drawLine(PAD_L, y_bar, W - PAD_R, y_bar)
        painter.drawLine(PAD_L, y_dd,  W - PAD_R, y_dd)

        # ── X-axis labels (show ~6 evenly spaced) ──
        painter.setPen(QColor(C["text_dim"]))
        f = QFont("JetBrains Mono", 8)
        painter.setFont(f)
        step = max(1, n // 6)
        for i in range(0, n, step):
            ts = series[i]["ts"]
            painter.drawText(
                QRectF(x_pos(i) - 30, H - PAD_B - 14, 60, 14),
                Qt.AlignmentFlag.AlignCenter,
                ts
            )

        painter.end()


class RawLogTab(QWidget):
    """Full position history from Kalshi API."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(SectionHeader("Full Position History (Kalshi)"))
        self.table = make_table([
            "Time", "Ticker", "Session", "Side",
            "Count", "Avg Price", "Result", "Outcome", "PnL", "Exit"
        ])
        layout.addWidget(self.table)

    def refresh(self, positions: list[dict]):
        rows = list(reversed(positions))
        self.table.setRowCount(len(rows))

        for i, p in enumerate(rows):
            outcome  = p.get("outcome", "")
            exit_r   = p.get("exit_reason", "expiry")
            result   = p.get("result", "")
            side     = p.get("side", "")
            pnl      = p.get("pnl", 0.0)
            avg_price = p.get("avg_price", 0.0)

            o_color = (
                C["green"] if outcome == "win"  else
                C["red"]   if outcome == "loss" else
                C["blue"]  if outcome == "open" else
                C["yellow"]
            )
            side_color = (
                C["blue"]   if side == "yes" else
                C["purple"] if side == "no"  else
                C["text_dim"]
            )
            r_color = (
                C["green"] if result == "yes" else
                C["red"]   if result == "no"  else
                C["text_dim"]
            )

            self.table.setItem(i, 0, make_item(p.get("first_trade_time", ""), C["text_dim"]))
            self.table.setItem(i, 1, make_item(p.get("ticker_display", ""), C["text"]))
            self.table.setItem(i, 2, make_item(p.get("session", ""), C["text_dim"]))
            self.table.setItem(i, 3, make_item(side.upper() if side else "—", side_color))
            self.table.setItem(i, 4, make_item(str(p.get("count", "—"))))
            self.table.setItem(i, 5, make_item(
                f"{avg_price:.4f}" if avg_price > 0 else "—"
            ))
            self.table.setItem(i, 6, make_item(
                result.upper() if result else "PENDING", r_color
            ))
            self.table.setItem(i, 7, make_item(
                outcome.upper() if outcome else "—", o_color, bold=True
            ))
            self.table.setItem(i, 8, make_item(
                f"{pnl:+.4f}" if outcome in ("win", "loss") else "—",
                C["green"] if pnl > 0 else C["red"] if pnl < 0 else C["text_dim"]
            ))
            self.table.setItem(i, 9, make_item(
                "TP" if exit_r == "take_profit" else "EXP",
                C["blue"] if exit_r == "take_profit" else C["text_dim"]
            ))


# ========================= MAIN WINDOW =========================
# ========================= BOT PROCESS MANAGER =========================
class BotProcess(QObject):
    """
    Manages main.py as a child subprocess.
    Emits log lines via a Qt signal so the UI can display them
    safely from the main thread.
    """
    log_line  = pyqtSignal(str)
    started   = pyqtSignal()
    stopped   = pyqtSignal()

    BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self):
        if self.is_running():
            return
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", self.BOT_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=os.path.dirname(self.BOT_SCRIPT),
            )
            self._reader = threading.Thread(
                target=self._read_output,
                daemon=True
            )
            self._reader.start()
            self.started.emit()
        except Exception as e:
            self.log_line.emit(f"[DASHBOARD] Failed to start bot: {e}")

    def stop(self):
        if not self.is_running():
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception as e:
            self.log_line.emit(f"[DASHBOARD] Error stopping bot: {e}")
        finally:
            self._proc = None
            self.stopped.emit()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_output(self):
        try:
            for line in self._proc.stdout:
                self.log_line.emit(line.rstrip())
        except Exception:
            pass
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
            self.stopped.emit()



# ========================= LOG CONSOLE TAB =========================
class LogConsoleTab(QWidget):
    """Displays live stdout from main.py."""


    MAX_LINES = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        layout.addWidget(SectionHeader("Bot Console Output"))

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(self.MAX_LINES)
        self.console.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {C['bg']};
                color: {C['text']};
                font-family: {MONO};
                font-size: 12px;
                border: 1px solid {C['border']};
                selection-background-color: {C['accent_dim']};
            }}
        """)
        layout.addWidget(self.console)

        # Clear button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        clear_btn = QPushButton("CLEAR CONSOLE")
        clear_btn.setFixedWidth(150)
        clear_btn.clicked.connect(self.console.clear)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

    def append(self, line: str):
        """Colour-code and append a log line."""
        # Pick colour by log level keyword
        if "| ERROR |" in line or "❌" in line:
            colour = C["red"]
        elif "| WARNING |" in line or "⚠️" in line:
            colour = C["yellow"]
        elif "✅" in line or "🚀" in line or "🎯" in line:
            colour = C["green"]
        elif "📌" in line or "📊" in line:
            colour = C["blue"]
        else:
            colour = C["text"]

        # Use HTML to colour the line
        escaped = (
            line.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
        )
        self.console.appendHtml(
            f'<span style="color:{colour};">{escaped}</span>'
        )
        # Auto-scroll to bottom
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kalshi BTC Bot — Dashboard")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)

        # ── Bot process manager ──
        self._bot = BotProcess(self)
        self._bot.log_line.connect(self._on_bot_log)
        self._bot.started.connect(self._on_bot_started)
        self._bot.stopped.connect(self._on_bot_stopped)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ──
        header = QWidget()
        header.setStyleSheet(
            f"background: {C['bg3']}; "
            f"border-bottom: 1px solid {C['border2']};"
        )
        header.setFixedHeight(52)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(20, 0, 20, 0)
        h_layout.setSpacing(10)

        title = QLabel("KALSHI BTC BOT")
        title.setStyleSheet(
            f"color: {C['accent']}; "
            f"font-size: 15px; "
            f"font-family: {MONO}; "
            f"font-weight: bold; "
            f"letter-spacing: 4px;"
        )

        self.status_lbl = QLabel("● STOPPED")
        self.status_lbl.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 11px; "
            f"font-family: {MONO}; "
            f"letter-spacing: 2px;"
        )

        self.last_refresh_lbl = QLabel("")
        self.last_refresh_lbl.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 11px; "
            f"font-family: {MONO};"
        )

        # Start / Stop buttons
        self.start_btn = QPushButton("▶  START BOT")
        self.start_btn.setFixedWidth(130)
        self.start_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {C['green_dim']}; "
            f"  color: {C['green']}; "
            f"  border: 1px solid {C['green']}; "
            f"  padding: 6px 16px; "
            f"  font-family: {SANS}; font-size: 11px; "
            f"  letter-spacing: 1px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {C['green']}; color: {C['bg']};"
            f"}}"
            f"QPushButton:disabled {{"
            f"  background: {C['bg3']}; "
            f"  color: {C['text_dim']}; "
            f"  border-color: {C['border']};"
            f"}}"
        )
        self.start_btn.clicked.connect(self._start_bot)

        self.stop_btn = QPushButton("■  STOP BOT")
        self.stop_btn.setFixedWidth(130)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {C['red_dim']}; "
            f"  color: {C['red']}; "
            f"  border: 1px solid {C['red']}; "
            f"  padding: 6px 16px; "
            f"  font-family: {SANS}; font-size: 11px; "
            f"  letter-spacing: 1px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {C['red']}; color: {C['bg']};"
            f"}}"
            f"QPushButton:disabled {{"
            f"  background: {C['bg3']}; "
            f"  color: {C['text_dim']}; "
            f"  border-color: {C['border']};"
            f"}}"
        )
        self.stop_btn.clicked.connect(self._stop_bot)

        self.refresh_btn = QPushButton("⟳  REFRESH")
        self.refresh_btn.setFixedWidth(120)
        self.refresh_btn.clicked.connect(self.load_data)

        h_layout.addWidget(title)
        h_layout.addSpacing(20)
        h_layout.addWidget(self.status_lbl)
        h_layout.addStretch()
        h_layout.addWidget(self.last_refresh_lbl)
        h_layout.addSpacing(12)
        h_layout.addWidget(self.start_btn)
        h_layout.addWidget(self.stop_btn)
        h_layout.addSpacing(8)
        h_layout.addWidget(self.refresh_btn)

        root.addWidget(header)

        # ── Tabs ──
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.overview_tab   = OverviewTab()
        self.positions_tab  = PositionsTab()
        self.pnl_tab        = PnLTab()
        self.sigma_tab      = SigmaTab()
        self.rawlog_tab     = RawLogTab()
        self.log_tab        = LogConsoleTab()

        self.tabs.addTab(self.overview_tab,  "Overview")
        self.tabs.addTab(self.positions_tab, "Positions")
        self.tabs.addTab(self.pnl_tab,       "PnL Tracking")
        self.tabs.addTab(self.sigma_tab,     "σ Calibration")
        self.tabs.addTab(self.rawlog_tab,    "Raw Log")
        self.tabs.addTab(self.log_tab,       "Bot Console")

        root.addWidget(self.tabs)

        # ── Status bar ──
        status_bar = QWidget()
        status_bar.setStyleSheet(
            f"background: {C['bg3']}; "
            f"border-top: 1px solid {C['border']};"
        )
        status_bar.setFixedHeight(28)
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(16, 0, 16, 0)

        self.file_status_lbl = QLabel("")
        self.file_status_lbl.setStyleSheet(
            f"color: {C['text_dim']}; font-size: 10px; font-family: {MONO};"
        )
        sb_layout.addWidget(self.file_status_lbl)
        sb_layout.addStretch()

        auto_lbl = QLabel(f"AUTO-REFRESH {REFRESH_MS//1000}s")
        auto_lbl.setStyleSheet(
            f"color: {C['text_dim']}; font-size: 10px; "
            f"font-family: {MONO}; letter-spacing: 1px;"
        )
        sb_layout.addWidget(auto_lbl)

        root.addWidget(status_bar)

        # ── Timer ──
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load_data)
        self.timer.start(REFRESH_MS)

        # Initial load
        self.load_data()

    # ── Bot control ──────────────────────────────────────
    def _start_bot(self):
        self.log_tab.append(
            f"[DASHBOARD] Starting bot — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC / {((datetime.now(timezone.utc).hour + LOCAL_TZ_OFFSET) % 24):02d}:{datetime.now(timezone.utc).strftime('%M:%S')} local"
        )
        # Switch to console tab so user can see output immediately
        self.tabs.setCurrentWidget(self.log_tab)
        self._bot.start()

    def _stop_bot(self):
        self.log_tab.append(
            f"[DASHBOARD] Stopping bot — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC / {((datetime.now(timezone.utc).hour + LOCAL_TZ_OFFSET) % 24):02d}:{datetime.now(timezone.utc).strftime('%M:%S')} local"
        )
        self._bot.stop()

    def _on_bot_started(self):
        self.status_lbl.setText("● RUNNING")
        self.status_lbl.setStyleSheet(
            f"color: {C['green']}; "
            f"font-size: 11px; "
            f"font-family: {MONO}; "
            f"letter-spacing: 2px;"
        )
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _on_bot_stopped(self):
        self.status_lbl.setText("● STOPPED")
        self.status_lbl.setStyleSheet(
            f"color: {C['text_dim']}; "
            f"font-size: 11px; "
            f"font-family: {MONO}; "
            f"letter-spacing: 2px;"
        )
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.log_tab.append(
            f"[DASHBOARD] Bot process ended — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC / {((datetime.now(timezone.utc).hour + LOCAL_TZ_OFFSET) % 24):02d}:{datetime.now(timezone.utc).strftime('%M:%S')} local"
        )

    def _on_bot_log(self, line: str):
        self.log_tab.append(line)

    def closeEvent(self, event):
        """Stop the bot cleanly when the dashboard window is closed."""
        if self._bot.is_running():
            self._bot.stop()
        event.accept()

    def load_data(self):
        # ── Fetch live data from Kalshi ──
        try:
            positions = fetch_kalshi_positions()
            api_ok    = True
        except Exception as e:
            positions = []
            api_ok    = False
            self.log_tab.append(f"[DASHBOARD] Kalshi API error: {e}")

        prices = load_csv(PRICES_FILE)
        stats  = calc_stats(positions)

        # Update all tabs
        self.overview_tab.refresh(positions, stats)
        self.positions_tab.refresh(positions)
        self.pnl_tab.refresh(stats)
        self.sigma_tab.refresh(prices)
        self.rawlog_tab.refresh(positions)

        # Update header
        now = datetime.now(timezone.utc)
        local_hour = (now.hour + LOCAL_TZ_OFFSET) % 24
        now_str = now.strftime(f"%Y-%m-%d {local_hour:02d}:%M:%S") + " (local)"
        status_suffix = "" if api_ok else " ⚠ API ERROR"
        self.last_refresh_lbl.setText(f"Last refresh: {now_str}{status_suffix}")

        # Status bar
        settled = sum(1 for p in positions if p["outcome"] in ("win", "loss"))
        opens   = sum(1 for p in positions if p["outcome"] == "open")
        btc_rows = len(prices)
        btc_age  = ""
        if os.path.exists(PRICES_FILE):
            age    = datetime.now() - datetime.fromtimestamp(os.path.getmtime(PRICES_FILE))
            btc_age = f" (updated {int(age.total_seconds())}s ago)"

        self.file_status_lbl.setText(
            f"Kalshi: {len(positions)} positions  |  "
            f"{settled} settled  |  {opens} open  |  "
            f"btc_prices: {btc_rows} rows{btc_age}"
        )



# ========================= ENTRY POINT =========================
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    # Force dark palette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(C["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(C["bg2"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(C["bg3"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(C["bg3"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(C["accent_dim"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(C["text_bright"]))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
