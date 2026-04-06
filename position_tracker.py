"""
position_tracker.py
-------------------
Fetches all historical trades and their settlement outcomes from Kalshi,
reconciles them against the local trade_results.csv, and produces a
clean summary report.

Run manually whenever you want a full picture of your trading history:
    python position_tracker.py

Outputs:
    positions_full.csv    — every trade from Kalshi with settlement result
    positions_report.txt  — human-readable summary
"""

import csv
import os
import time
import math
import base64
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ========================= CONFIG =========================
API_KEY_ID       = "50952777-89c2-4d13-b24b-d7820f8b8931"
PRIVATE_KEY_PATH = "C:/Users/pcdox/Desktop/projects/openclaw/chave2.pem"
USE_DEMO         = False

RESULTS_FILE   = "trade_results.csv"       # bot's local log
POSITIONS_FILE = "positions_full.csv"      # output: full Kalshi history
REPORT_FILE    = "positions_report.txt"    # output: human-readable summary

BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if USE_DEMO else
    "https://api.elections.kalshi.com/trade-api/v2"
)

# ========================= AUTH =========================
with open(PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY_PEM = f.read().strip()

private_key = serialization.load_pem_private_key(
    PRIVATE_KEY_PEM.encode(), password=None
)
session = requests.Session()


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
    return {
        "KALSHI-ACCESS-KEY":      API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type":            "application/json"
    }


# ========================= KALSHI API =========================
def fetch_all_trades() -> list[dict]:
    """
    Paginate through /portfolio/trades to fetch every trade ever placed.
    Returns a flat list of raw trade dicts from Kalshi.
    """
    all_trades = []
    cursor     = None
    page       = 0

    print("Fetching trades from Kalshi...")

    while True:
        path   = "/trade-api/v2/portfolio/trades"
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            headers  = sign_request("GET", path)
            resp     = session.get(
                f"{BASE_URL}/portfolio/trades",
                headers=headers,
                params=params,
                timeout=15
            )
            resp.raise_for_status()
            data     = resp.json()
            trades   = data.get("trades", [])
            cursor   = data.get("cursor")
            page    += 1

            all_trades.extend(trades)
            print(f"  Page {page}: fetched {len(trades)} trades "
                  f"(total so far: {len(all_trades)})")

            if not trades or not cursor:
                break

            time.sleep(0.3)   # be gentle with the API

        except Exception as e:
            print(f"  Error fetching page {page}: {e}")
            break

    print(f"Total trades fetched: {len(all_trades)}")
    return all_trades


def fetch_market_details(ticker: str) -> dict:
    """Fetch market metadata including settlement result."""
    try:
        path    = f"/trade-api/v2/markets/{ticker}"
        headers = sign_request("GET", path)
        resp    = session.get(
            f"{BASE_URL}/markets/{ticker}",
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("market", {})
    except Exception as e:
        print(f"  Warning: could not fetch market {ticker}: {e}")
        return {}


def fetch_portfolio_positions() -> list[dict]:
    """
    Fetch current open positions from /portfolio/positions.
    These are contracts not yet settled.
    """
    try:
        path    = "/trade-api/v2/portfolio/positions"
        headers = sign_request("GET", path)
        resp    = session.get(
            f"{BASE_URL}/portfolio/positions",
            headers=headers,
            params={"limit": 100},
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("market_positions", [])
    except Exception as e:
        print(f"  Warning: could not fetch positions: {e}")
        return []


# ========================= LOCAL CSV =========================
def load_local_results() -> dict[str, dict]:
    """
    Load trade_results.csv keyed by ticker.
    Returns dict[ticker -> row] for quick lookup.
    """
    if not os.path.exists(RESULTS_FILE):
        return {}

    results = {}
    with open(RESULTS_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", "")
            if ticker:
                results[ticker] = row
    return results


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


def calc_pnl(
    side: str,
    avg_price: float,
    count: int,
    result: str
) -> float:
    """
    Calculate realised PnL for a settled position.
    Each contract pays $1 if you win, $0 if you lose.
    Cost = avg_price * count
    """
    if result == "unknown" or not result:
        return 0.0
    won = (result == side)
    if won:
        return (1.0 - avg_price) * count
    else:
        return -avg_price * count


# ========================= MAIN =========================
def main():
    print("=" * 60)
    print("KALSHI POSITION TRACKER")
    print(f"Run at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    # ── Fetch all data ──
    raw_trades     = fetch_all_trades()
    open_positions = fetch_portfolio_positions()
    local_results  = load_local_results()

    if not raw_trades:
        print("No trades found.")
        return

    # ── Group trades by ticker ──
    # Each ticker may have multiple fills (partial fills, multiple entries).
    # Aggregate into one position per ticker.
    by_ticker: dict[str, list[dict]] = {}
    for t in raw_trades:
        ticker = t.get("ticker", "")
        if ticker not in by_ticker:
            by_ticker[ticker] = []
        by_ticker[ticker].append(t)

    print(f"\nUnique markets traded: {len(by_ticker)}")
    print("Fetching settlement results...\n")

    # ── Build enriched position records ──
    positions = []
    market_cache: dict[str, dict] = {}

    for ticker, trades in sorted(by_ticker.items()):
        # Fetch market details (with cache to avoid duplicate calls)
        if ticker not in market_cache:
            market_cache[ticker] = fetch_market_details(ticker)
            time.sleep(0.2)

        mkt = market_cache[ticker]

        # Aggregate trade fills
        total_yes_bought = sum(
            int(t.get("count", 0))
            for t in trades
            if t.get("side") == "yes" and t.get("action") == "buy"
        )
        total_no_bought = sum(
            int(t.get("count", 0))
            for t in trades
            if t.get("side") == "no" and t.get("action") == "buy"
        )
        total_yes_sold = sum(
            int(t.get("count", 0))
            for t in trades
            if t.get("side") == "yes" and t.get("action") == "sell"
        )
        total_no_sold = sum(
            int(t.get("count", 0))
            for t in trades
            if t.get("side") == "no" and t.get("action") == "sell"
        )

        # Net position
        net_yes = total_yes_bought - total_yes_sold
        net_no  = total_no_bought  - total_no_sold

        # Determine dominant side
        if net_yes > 0:
            side  = "yes"
            count = net_yes
        elif net_no > 0:
            side  = "no"
            count = net_no
        else:
            side  = "closed"
            count = 0

        # Average entry price (cost basis)
        buy_trades = [
            t for t in trades
            if t.get("action") == "buy" and t.get("side") == side
        ]
        if buy_trades:
            total_cost  = sum(
                float(t.get("yes_price", 0) if side == "yes"
                      else (100 - float(t.get("yes_price", 0))))
                * int(t.get("count", 0))
                for t in buy_trades
            )
            total_count = sum(int(t.get("count", 0)) for t in buy_trades)
            avg_price   = (total_cost / total_count / 100.0
                           if total_count > 0 else 0.0)
        else:
            avg_price = 0.0

        # Settlement
        result        = mkt.get("result", "")          # "yes"/"no"/""
        status        = mkt.get("status", "unknown")   # "settled"/"closed"/etc
        close_time    = mkt.get("close_time", "")
        floor_strike  = mkt.get("floor_strike", "")

        # Outcome
        if result in ("yes", "no") and side not in ("closed", ""):
            outcome = "win" if result == side else "loss"
        elif status in ("open", "active"):
            outcome = "open"
        else:
            outcome = "unknown"

        # PnL
        pnl = calc_pnl(side, avg_price, count, result) if result else 0.0

        # Timestamp of first trade
        timestamps = [
            t.get("created_time", "") for t in trades
            if t.get("created_time")
        ]
        first_trade_time = min(timestamps) if timestamps else ""

        # UTC hour and session from first trade
        utc_hour = 0
        sess     = "unknown"
        if first_trade_time:
            try:
                dt       = datetime.fromisoformat(
                    first_trade_time.replace("Z", "+00:00")
                )
                utc_hour = dt.hour
                sess     = _get_session(utc_hour)
            except Exception:
                pass

        # Reconcile with local CSV
        local = local_results.get(ticker, {})
        local_outcome        = local.get("outcome", "not_logged")
        local_settled_result = local.get("settled_result", "not_logged")
        local_exit_reason    = local.get("exit_reason", "not_logged")

        # Flag mismatches
        mismatch = ""
        if local_outcome not in ("not_logged", "no_trade", "unknown"):
            if local_outcome != outcome:
                mismatch = f"OUTCOME_MISMATCH(local={local_outcome},kalshi={outcome})"
        if local_settled_result not in ("not_logged", "unknown", "take_profit"):
            if local_settled_result != result and result:
                mismatch += f" RESULT_MISMATCH(local={local_settled_result},kalshi={result})"

        positions.append({
            "ticker":              ticker,
            "first_trade_time":    first_trade_time,
            "utc_hour":            utc_hour,
            "session":             sess,
            "side":                side,
            "count":               count,
            "avg_entry_price":     round(avg_price, 4),
            "total_yes_bought":    total_yes_bought,
            "total_no_bought":     total_no_bought,
            "total_yes_sold":      total_yes_sold,
            "total_no_sold":       total_no_sold,
            "market_status":       status,
            "close_time":          close_time,
            "floor_strike":        floor_strike,
            "kalshi_result":       result or "pending",
            "outcome":             outcome,
            "pnl":                 round(pnl, 4),
            "local_outcome":       local_outcome,
            "local_settled_result": local_settled_result,
            "local_exit_reason":   local_exit_reason,
            "mismatch":            mismatch.strip(),
        })

    # ── Write positions_full.csv ──
    if positions:
        fieldnames = list(positions[0].keys())
        with open(POSITIONS_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(positions)
        print(f"Written: {POSITIONS_FILE} ({len(positions)} positions)")

    # ── Open positions from portfolio API ──
    print(f"\nFetching open positions...")
    open_pos_summary = []
    for pos in open_positions:
        ticker     = pos.get("ticker", "")
        yes_count  = int(pos.get("position", 0))
        no_count   = int(pos.get("resting_orders_count", 0))
        market_exp = pos.get("market_exposure", 0)
        open_pos_summary.append(
            f"  {ticker}: position={yes_count} | exposure={market_exp}"
        )

    # ── Build report ──
    settled   = [p for p in positions if p["outcome"] in ("win", "loss")]
    wins      = [p for p in settled if p["outcome"] == "win"]
    losses    = [p for p in settled if p["outcome"] == "loss"]
    unknowns  = [p for p in positions if p["outcome"] == "unknown"]
    opens     = [p for p in positions if p["outcome"] == "open"]
    take_profits = [
        p for p in positions
        if p["local_exit_reason"] == "take_profit"
    ]
    mismatches = [p for p in positions if p["mismatch"]]

    total_pnl       = sum(p["pnl"] for p in settled)
    win_rate        = len(wins) / len(settled) if settled else 0.0
    avg_win_pnl     = (
        sum(p["pnl"] for p in wins) / len(wins) if wins else 0.0
    )
    avg_loss_pnl    = (
        sum(p["pnl"] for p in losses) / len(losses) if losses else 0.0
    )

    # By session
    session_stats: dict[str, dict] = {}
    for p in settled:
        s = p["session"]
        if s not in session_stats:
            session_stats[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if p["outcome"] == "win":
            session_stats[s]["wins"] += 1
        else:
            session_stats[s]["losses"] += 1
        session_stats[s]["pnl"] += p["pnl"]

    # By side
    yes_trades  = [p for p in settled if p["side"] == "yes"]
    no_trades   = [p for p in settled if p["side"] == "no"]
    yes_wins    = sum(1 for p in yes_trades if p["outcome"] == "win")
    no_wins     = sum(1 for p in no_trades  if p["outcome"] == "win")

    report_lines = [
        "=" * 60,
        "KALSHI POSITION TRACKER — SUMMARY REPORT",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
        "── OVERALL ──────────────────────────────────────",
        f"  Total positions tracked : {len(positions)}",
        f"  Settled (win/loss)      : {len(settled)}",
        f"  Wins                    : {len(wins)}",
        f"  Losses                  : {len(losses)}",
        f"  Win rate                : {win_rate:.1%}",
        f"  Total PnL               : ${total_pnl:+.2f}",
        f"  Avg win PnL             : ${avg_win_pnl:+.4f}",
        f"  Avg loss PnL            : ${avg_loss_pnl:+.4f}",
        f"  Open positions          : {len(opens)}",
        f"  Unknown outcome         : {len(unknowns)}",
        f"  Take profit exits       : {len(take_profits)}",
        "",
        "── BY SIDE ──────────────────────────────────────",
        f"  YES trades: {len(yes_trades)} | "
        f"wins: {yes_wins} | "
        f"win rate: {yes_wins/len(yes_trades):.1%}" if yes_trades
        else "  YES trades: 0",
        f"  NO trades : {len(no_trades)} | "
        f"wins: {no_wins} | "
        f"win rate: {no_wins/len(no_trades):.1%}" if no_trades
        else "  NO trades : 0",
        "",
        "── BY SESSION ───────────────────────────────────",
    ]

    for sess in ("asia", "europe", "us", "us_close"):
        s = session_stats.get(sess, {})
        w = s.get("wins", 0)
        l = s.get("losses", 0)
        p = s.get("pnl", 0.0)
        n = w + l
        wr = w / n if n > 0 else 0.0
        report_lines.append(
            f"  {sess:<10}: {n:>3} trades | "
            f"{w:>2}W {l:>2}L | "
            f"win rate: {wr:.1%} | "
            f"PnL: ${p:+.2f}"
        )

    report_lines += [
        "",
        "── OPEN POSITIONS ───────────────────────────────",
    ]
    if open_pos_summary:
        report_lines += open_pos_summary
    else:
        report_lines.append("  None")

    if mismatches:
        report_lines += [
            "",
            "── MISMATCHES (local CSV vs Kalshi) ─────────────",
        ]
        for p in mismatches:
            report_lines.append(
                f"  {p['ticker']}: {p['mismatch']}"
            )

    if unknowns:
        report_lines += [
            "",
            "── UNKNOWN OUTCOMES (settlement fetch failed) ───",
        ]
        for p in unknowns:
            report_lines.append(
                f"  {p['ticker']} | side={p['side']} | "
                f"status={p['market_status']}"
            )

    report_lines += [
        "",
        "── FULL DETAIL ──────────────────────────────────",
    ]
    for p in sorted(positions, key=lambda x: x["first_trade_time"]):
        outcome_icon = (
            "✅" if p["outcome"] == "win"  else
            "❌" if p["outcome"] == "loss" else
            "🔄" if p["outcome"] == "open" else
            "❓"
        )
        tp_tag = " [TP]" if p["local_exit_reason"] == "take_profit" else ""
        report_lines.append(
            f"  {outcome_icon} {p['ticker']} | "
            f"{p['side'].upper():>3} x{p['count']} @ {p['avg_entry_price']:.3f} | "
            f"result={p['kalshi_result']:<3} | "
            f"PnL={p['pnl']:+.4f}{tp_tag}"
        )

    report_lines += ["", "=" * 60]
    report_text = "\n".join(report_lines)

    # Print to console
    print("\n" + report_text)

    # Write to file
    with open(REPORT_FILE, "w") as f:
        f.write(report_text)
    print(f"\nWritten: {REPORT_FILE}")


if __name__ == "__main__":
    main()
