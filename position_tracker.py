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
from datetime import datetime, timezone

from kalshi_common import (
    sign_request, get_session_name, kalshi_get,
    BASE_URL, session,
)

# ========================= CONFIG =========================
USE_DEMO = False

RESULTS_FILE   = "trade_results.csv"
POSITIONS_FILE = "positions_full.csv"
REPORT_FILE    = "positions_report.txt"


# ========================= KALSHI API =========================
def fetch_all_fills() -> list[dict]:
    """Paginate /portfolio/fills and return every fill ever placed."""
    all_fills = []
    cursor    = None
    page      = 0

    print("Fetching fills from Kalshi...")

    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data   = kalshi_get("/portfolio/fills", params)
            fills  = data.get("fills", [])
            cursor = data.get("cursor")
            page  += 1
            all_fills.extend(fills)
            print(f"  Page {page}: {len(fills)} fills (total: {len(all_fills)})")
            if not fills or not cursor:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  Error fetching page {page}: {e}")
            break

    print(f"Total fills fetched: {len(all_fills)}")
    return all_fills


def fetch_market_details(ticker: str) -> dict:
    """Fetch market metadata including settlement result."""
    try:
        return kalshi_get(f"/markets/{ticker}").get("market", {})
    except Exception as e:
        print(f"  Warning: could not fetch market {ticker}: {e}")
        return {}


def fetch_portfolio_positions() -> list[dict]:
    """Fetch current open positions from /portfolio/positions."""
    try:
        data = kalshi_get("/portfolio/positions", {"limit": 100})
        return data.get("market_positions", [])
    except Exception as e:
        print(f"  Warning: could not fetch positions: {e}")
        return []


# ========================= LOCAL CSV =========================
def load_local_results() -> dict[str, dict]:
    """Load trade_results.csv keyed by ticker."""
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
def calc_pnl(side: str, avg_price: float, count: int, result: str) -> float:
    """
    Realised PnL for a settled position, after Kalshi taker fees.
    Each contract pays $1 if you win, $0 if you lose.
    Fee formula: 7% × p × (1-p) per contract (taker rate).
    """
    if result not in ("yes", "no"):
        return 0.0
    won  = (result == side)
    fee  = 0.07 * avg_price * (1.0 - avg_price) * count
    gross = (1.0 - avg_price) * count if won else -avg_price * count
    return gross - fee


# ========================= MAIN =========================
def main():
    print("=" * 60)
    print("KALSHI POSITION TRACKER")
    print(f"Run at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    raw_fills      = fetch_all_fills()
    open_positions = fetch_portfolio_positions()
    local_results  = load_local_results()

    if not raw_fills:
        print("No fills found.")
        return

    # Group fills by ticker
    by_ticker: dict[str, list[dict]] = {}
    for f in raw_fills:
        ticker = f.get("ticker", "")
        if ticker not in by_ticker:
            by_ticker[ticker] = []
        by_ticker[ticker].append(f)

    print(f"\nUnique markets traded: {len(by_ticker)}")
    print("Fetching settlement results...\n")

    positions      = []
    market_cache: dict[str, dict] = {}

    for ticker, fills in sorted(by_ticker.items()):
        if ticker not in market_cache:
            market_cache[ticker] = fetch_market_details(ticker)
            time.sleep(0.2)

        mkt = market_cache[ticker]

        total_yes_bought = sum(
            int(f.get("count", 0)) for f in fills
            if f.get("side") == "yes" and f.get("action") == "buy"
        )
        total_no_bought = sum(
            int(f.get("count", 0)) for f in fills
            if f.get("side") == "no" and f.get("action") == "buy"
        )
        total_yes_sold = sum(
            int(f.get("count", 0)) for f in fills
            if f.get("side") == "yes" and f.get("action") == "sell"
        )
        total_no_sold = sum(
            int(f.get("count", 0)) for f in fills
            if f.get("side") == "no" and f.get("action") == "sell"
        )

        net_yes = total_yes_bought - total_yes_sold
        net_no  = total_no_bought  - total_no_sold

        if net_yes > 0:
            side, count = "yes", net_yes
        elif net_no > 0:
            side, count = "no", net_no
        else:
            side, count = "closed", 0

        buy_fills = [
            f for f in fills
            if f.get("action") == "buy" and f.get("side") == side
        ]
        if buy_fills:
            total_cost  = sum(
                float(f.get("yes_price", 0) if side == "yes"
                      else (100 - float(f.get("yes_price", 0))))
                * int(f.get("count", 0))
                for f in buy_fills
            )
            total_count = sum(int(f.get("count", 0)) for f in buy_fills)
            avg_price   = total_cost / total_count / 100.0 if total_count > 0 else 0.0
        else:
            avg_price = 0.0

        result      = mkt.get("result", "")
        status      = mkt.get("status", "unknown")
        close_time  = mkt.get("close_time", "")
        floor_strike = mkt.get("floor_strike", "")

        if result in ("yes", "no") and side not in ("closed", ""):
            outcome = "win" if result == side else "loss"
        elif status in ("open", "active"):
            outcome = "open"
        else:
            outcome = "unknown"

        pnl = calc_pnl(side, avg_price, count, result) if result else 0.0

        timestamps       = [f.get("created_time", "") for f in fills if f.get("created_time")]
        first_trade_time = min(timestamps) if timestamps else ""

        utc_hour = 0
        sess     = "unknown"
        if first_trade_time:
            try:
                dt       = datetime.fromisoformat(first_trade_time.replace("Z", "+00:00"))
                utc_hour = dt.hour
                sess     = get_session_name(utc_hour)
            except Exception:
                pass

        # Reconcile with local CSV
        local                = local_results.get(ticker, {})
        local_outcome        = local.get("outcome", "not_logged")
        local_settled_result = local.get("settled_result", "not_logged")
        local_exit_reason    = local.get("exit_reason", "not_logged")

        mismatch = ""
        if local_outcome not in ("not_logged", "no_trade", "unknown"):
            if local_outcome != outcome:
                mismatch = f"OUTCOME_MISMATCH(local={local_outcome},kalshi={outcome})"
        if local_settled_result not in ("not_logged", "unknown", "take_profit"):
            if local_settled_result != result and result:
                mismatch += f" RESULT_MISMATCH(local={local_settled_result},kalshi={result})"

        positions.append({
            "ticker":               ticker,
            "first_trade_time":     first_trade_time,
            "utc_hour":             utc_hour,
            "session":              sess,
            "side":                 side,
            "count":                count,
            "avg_entry_price":      round(avg_price, 4),
            "total_yes_bought":     total_yes_bought,
            "total_no_bought":      total_no_bought,
            "total_yes_sold":       total_yes_sold,
            "total_no_sold":        total_no_sold,
            "market_status":        status,
            "close_time":           close_time,
            "floor_strike":         floor_strike,
            "kalshi_result":        result or "pending",
            "outcome":              outcome,
            "pnl":                  round(pnl, 4),
            "local_outcome":        local_outcome,
            "local_settled_result": local_settled_result,
            "local_exit_reason":    local_exit_reason,
            "mismatch":             mismatch.strip(),
        })

    # Write positions_full.csv
    if positions:
        fieldnames = list(positions[0].keys())
        with open(POSITIONS_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(positions)
        print(f"Written: {POSITIONS_FILE} ({len(positions)} positions)")

    # Open positions summary
    print("\nOpen positions from portfolio API:")
    for pos in open_positions:
        ticker     = pos.get("ticker", "")
        yes_count  = int(pos.get("position", 0))
        market_exp = pos.get("market_exposure", 0)
        print(f"  {ticker}: position={yes_count} | exposure={market_exp}")
    if not open_positions:
        print("  None")

    # Stats
    settled  = [p for p in positions if p["outcome"] in ("win", "loss")]
    wins     = [p for p in settled if p["outcome"] == "win"]
    losses   = [p for p in settled if p["outcome"] == "loss"]
    unknowns = [p for p in positions if p["outcome"] == "unknown"]
    opens    = [p for p in positions if p["outcome"] == "open"]
    take_profits = [p for p in positions if p["local_exit_reason"] == "take_profit"]
    mismatches   = [p for p in positions if p["mismatch"]]

    total_pnl    = sum(p["pnl"] for p in settled)
    win_rate     = len(wins) / len(settled) if settled else 0.0
    avg_win_pnl  = sum(p["pnl"] for p in wins)  / len(wins)   if wins   else 0.0
    avg_loss_pnl = sum(p["pnl"] for p in losses) / len(losses) if losses else 0.0

    session_stats: dict[str, dict] = {}
    for p in settled:
        s = p["session"]
        if s not in session_stats:
            session_stats[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
        session_stats[s]["wins" if p["outcome"] == "win" else "losses"] += 1
        session_stats[s]["pnl"] += p["pnl"]

    yes_trades = [p for p in settled if p["side"] == "yes"]
    no_trades  = [p for p in settled if p["side"] == "no"]
    yes_wins   = sum(1 for p in yes_trades if p["outcome"] == "win")
    no_wins    = sum(1 for p in no_trades  if p["outcome"] == "win")

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
        (f"  YES trades: {len(yes_trades)} | wins: {yes_wins} | "
         f"win rate: {yes_wins/len(yes_trades):.1%}" if yes_trades else "  YES trades: 0"),
        (f"  NO trades : {len(no_trades)} | wins: {no_wins} | "
         f"win rate: {no_wins/len(no_trades):.1%}" if no_trades else "  NO trades : 0"),
        "",
        "── BY SESSION ───────────────────────────────────",
    ]

    for sess in ("asia", "europe", "us", "us_close"):
        s  = session_stats.get(sess, {})
        w  = s.get("wins", 0)
        l  = s.get("losses", 0)
        n  = w + l
        wr = w / n if n > 0 else 0.0
        p  = s.get("pnl", 0.0)
        report_lines.append(
            f"  {sess:<10}: {n:>3} trades | {w:>2}W {l:>2}L | "
            f"win rate: {wr:.1%} | PnL: ${p:+.2f}"
        )

    report_lines += ["", "── FULL DETAIL ──────────────────────────────────"]
    for p in sorted(positions, key=lambda x: x["first_trade_time"]):
        icon   = "✅" if p["outcome"] == "win" else "❌" if p["outcome"] == "loss" else "🔄" if p["outcome"] == "open" else "❓"
        tp_tag = " [TP]" if p["local_exit_reason"] == "take_profit" else ""
        report_lines.append(
            f"  {icon} {p['ticker']} | "
            f"{p['side'].upper():>3} x{p['count']} @ {p['avg_entry_price']:.3f} | "
            f"result={p['kalshi_result']:<3} | PnL={p['pnl']:+.4f}{tp_tag}"
        )

    if mismatches:
        report_lines += ["", "── MISMATCHES ───────────────────────────────────"]
        for p in mismatches:
            report_lines.append(f"  {p['ticker']}: {p['mismatch']}")

    if unknowns:
        report_lines += ["", "── UNKNOWN OUTCOMES ─────────────────────────────"]
        for p in unknowns:
            report_lines.append(
                f"  {p['ticker']} | side={p['side']} | status={p['market_status']}"
            )

    report_lines += ["", "=" * 60]
    report_text = "\n".join(report_lines)

    print("\n" + report_text)
    with open(REPORT_FILE, "w") as f:
        f.write(report_text)
    print(f"\nWritten: {REPORT_FILE}")


if __name__ == "__main__":
    main()
