"""
fetch_history.py
----------------
Run this locally to pull your full KXBTC15M trade history from Kalshi
and save it to kalshi_history.csv — then share that file for analysis.

    python fetch_history.py
"""

import csv
import time
import base64
from datetime import datetime, timezone
from collections import defaultdict

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ──────────────────────────────────────────────────────────
# Credentials are read from environment variables or a .env file
# in the same directory — same approach as kalshi_common.py.
import os as _os

def _load_env():
    path = _os.path.join(_os.path.dirname(__file__), ".env")
    if not _os.path.exists(path):
        return
    with open(path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                k, _, v = _line.partition("=")
                _os.environ.setdefault(k.strip(), v.strip())

_load_env()

API_KEY_ID       = _os.environ.get("KALSHI_API_KEY", "")
PRIVATE_KEY_PATH = _os.environ.get("KALSHI_KEY_PATH", "")

if not API_KEY_ID or not PRIVATE_KEY_PATH:
    raise EnvironmentError(
        "Set KALSHI_API_KEY and KALSHI_KEY_PATH as environment variables "
        "or in a .env file next to this script."
    )

BASE_URL    = "https://api.elections.kalshi.com/trade-api/v2"
OUTPUT_FILE = "kalshi_history.csv"
# ────────────────────────────────────────────────────────────────────

with open(PRIVATE_KEY_PATH) as f:
    private_key = serialization.load_pem_private_key(f.read().strip().encode(), password=None)

session = requests.Session()


def sign(method: str, path: str) -> dict:
    ts  = str(int(time.time() * 1000))
    sig = private_key.sign(
        (ts + method.upper() + path).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }


def paginate(path: str, key: str) -> list[dict]:
    results, cursor = [], None
    page = 0
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(
            f"{BASE_URL}{path}",
            headers=sign("GET", f"/trade-api/v2{path}"),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data    = resp.json()
        batch   = data.get(key, [])
        cursor  = data.get("cursor")
        page   += 1
        results.extend(batch)
        print(f"  {path}: page {page}, {len(batch)} rows (total {len(results)})")
        if not batch or not cursor:
            break
        time.sleep(0.25)
    return results


print("Fetching fills …")
fills = paginate("/portfolio/fills", "fills")

print("Fetching settlements …")
settlements = paginate("/portfolio/settlements", "settlements")

# ── Filter to KXBTC15M only ──────────────────────────────────────────
fills_btc = [f for f in fills if f.get("ticker", "").startswith("KXBTC15M")]
sett_btc  = {s["ticker"]: s for s in settlements if s.get("ticker", "").startswith("KXBTC15M")}

print(f"\nKXBTC15M fills:       {len(fills_btc)}")
print(f"KXBTC15M settlements: {len(sett_btc)}")

# ── Group fills by ticker ────────────────────────────────────────────
by_ticker: dict[str, list] = defaultdict(list)
for f in fills_btc:
    by_ticker[f["ticker"]].append(f)

# Add tickers only in settlements
for t in sett_btc:
    if t not in by_ticker:
        by_ticker[t] = []

# ── Build one row per position ───────────────────────────────────────
def fp(v):
    try: return float(v or 0)
    except: return 0.0

rows = []
for ticker, flist in sorted(by_ticker.items()):
    s = sett_btc.get(ticker, {})

    yes_bought = sum(fp(f["count_fp"]) for f in flist if f.get("side")=="yes" and f.get("action")=="buy")
    no_bought  = sum(fp(f["count_fp"]) for f in flist if f.get("side")=="no"  and f.get("action")=="buy")
    yes_sold   = sum(fp(f["count_fp"]) for f in flist if f.get("side")=="yes" and f.get("action")=="sell")
    no_sold    = sum(fp(f["count_fp"]) for f in flist if f.get("side")=="no"  and f.get("action")=="sell")

    net_yes = yes_bought - yes_sold
    net_no  = no_bought  - no_sold

    if net_yes > 0:
        side, count = "yes", net_yes
    elif net_no > 0:
        side, count = "no", net_no
    elif yes_bought > 0:
        side, count = "yes", yes_bought   # held to expiry
    elif no_bought > 0:
        side, count = "no", no_bought
    else:
        side, count = "yes", 0

    count = round(count)

    # Average entry price
    buy_fills = [f for f in flist if f.get("action")=="buy" and f.get("side")==side]
    if buy_fills:
        pk = "yes_price_dollars" if side=="yes" else "no_price_dollars"
        tc = sum(fp(f.get(pk)) * fp(f.get("count_fp")) for f in buy_fills)
        tn = sum(fp(f.get("count_fp")) for f in buy_fills)
        avg_price = tc / tn if tn > 0 else 0.0
    elif s:
        cost = fp(s.get("yes_total_cost_dollars" if side=="yes" else "no_total_cost_dollars"))
        cnt  = fp(s.get("yes_count_fp" if side=="yes" else "no_count_fp")) or 1
        avg_price = cost / cnt
    else:
        avg_price = 0.0

    # Settlement result
    result = s.get("market_result", "") or ""
    if result in ("yes", "no"):
        outcome = "win" if result == side else "loss"
    else:
        outcome = "open" if not s else "unknown"

    # PnL from settlement revenue (most accurate)
    pnl = 0.0
    if s and result in ("yes", "no"):
        revenue  = fp(s.get("revenue")) / 100.0
        fee      = fp(s.get("fee_cost"))
        cost     = fp(s.get("yes_total_cost_dollars" if side=="yes" else "no_total_cost_dollars"))
        pnl      = revenue - cost - fee

    # Exit reason
    has_sell   = any(f.get("action")=="sell" for f in flist)
    exit_reason = "take_profit" if has_sell else "expiry"

    # Timestamp
    times = [f.get("created_time","") for f in flist if f.get("created_time")]
    first_time = min(times) if times else s.get("settled_time","")

    utc_hour, sess = 0, "unknown"
    ts_fmt = ""
    if first_time:
        try:
            dt       = datetime.fromisoformat(first_time.replace("Z","+00:00"))
            utc_hour = dt.hour
            h        = utc_hour
            sess     = ("asia" if h < 6 else "europe" if h < 12 else "us" if h < 20 else "us_close")
            ts_fmt   = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts_fmt = first_time[:16]

    rows.append({
        "timestamp":      ts_fmt,
        "utc_hour":       utc_hour,
        "session":        sess,
        "ticker":         ticker,
        "side":           side,
        "count":          count,
        "avg_price":      round(avg_price, 4),
        "result":         result or "pending",
        "outcome":        outcome,
        "pnl":            round(pnl, 4),
        "exit_reason":    exit_reason,
        "yes_bought":     round(yes_bought),
        "no_bought":      round(no_bought),
        "yes_sold":       round(yes_sold),
        "no_sold":        round(no_sold),
    })

# ── Write CSV ────────────────────────────────────────────────────────
with open(OUTPUT_FILE, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"\nWritten {len(rows)} positions → {OUTPUT_FILE}")

# ── Quick summary ─────────────────────────────────────────────────────
settled = [r for r in rows if r["outcome"] in ("win","loss")]
wins    = [r for r in settled if r["outcome"] == "win"]
total_pnl = sum(r["pnl"] for r in settled)
print(f"\nQuick summary:")
print(f"  Total positions: {len(rows)}")
print(f"  Settled:         {len(settled)}")
print(f"  Win rate:        {len(wins)/len(settled):.1%}" if settled else "")
print(f"  Total PnL:       ${total_pnl:+.2f}")
