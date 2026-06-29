"""
mnav.py — Real-time MSTR mNAV computation (ported from the server-side mstr-monitor,
          same formula and parameters).

mNAV = MSTR share price / NAV per share;
NAV per share = (BTC price × BTC holdings) / diluted share count.

Calibrated to the navPremiumDiluted convention (source-audited 2026-06-29):
**no net-debt deduction**, using **diluted shares** (includes convertibles/options
on an if-converted basis). Consistent with monitor.js / report.js on the server.

Price sources: MSTR = Yahoo Finance chart API; BTC = Binance (consistent with app.py).
BTC holdings refreshed weekly by cron (CoinGecko); diluted shares updated manually from
quarterly 10-Q filings (synchronized with the saylortracker convention).
Returns None on failure; callers must handle gracefully.

Shared by app.py (dashboard) and show_progress_mstr.py (progress panel).
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

# Aligned with mstr-monitor and the holdings environment configuration (injected via EnvironmentFile)
BTC_HOLDINGS = float(os.environ.get("MSTR_BTC_HOLDINGS", "847363"))
# Diluted shares (saylortracker latestEffectiveDilutedShares convention); updated manually from 10-Q filings
DILUTED_SHARES = float(os.environ.get("MSTR_DILUTED_SHARES_MILLION", "388.617")) * 1e6
UA = {"User-Agent": "Mozilla/5.0 (compatible; mstr-tracker/1.0)"}


def _get(url, timeout=6):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_mstr_price():
    """MSTR real-time share price from Yahoo Finance. Returns None on failure."""
    try:
        d = _get("https://query1.finance.yahoo.com/v8/finance/chart/MSTR?interval=1m&range=1d")
        p = d["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(p) if p else None
    except Exception:
        return None


def get_mstr_klines_30d():
    """MSTR daily closing prices for the past ~1 month (Yahoo chart API, ~21–22 trading days).
    Returns [] on failure; callers must handle gracefully."""
    try:
        d = _get("https://query1.finance.yahoo.com/v8/finance/chart/MSTR?interval=1d&range=1mo")
        closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return [round(float(c), 2) for c in closes if c]
    except Exception:
        return []


def get_btc_price():
    """BTC real-time price from Binance public API. Returns None on failure."""
    try:
        return float(_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        return None


def nav_per_share(btc_price):
    # navPremiumDiluted convention: BTC market cap ÷ diluted shares; no net-debt deduction
    return (btc_price * BTC_HOLDINGS) / DILUTED_SHARES


def compute(mstr_price=None, btc_price=None):
    """
    Returns a dict: {mstr, btc, nav_per_share, mnav}.
    Any missing price → corresponding fields are None, mnav is None.
    Prices can be injected (for testing or reuse); otherwise fetched in real time.
    """
    mstr = mstr_price if mstr_price is not None else get_mstr_price()
    btc = btc_price if btc_price is not None else get_btc_price()
    out = {"mstr": mstr, "btc": btc, "nav_per_share": None, "mnav": None}
    if mstr and btc:
        nps = nav_per_share(btc)
        if nps > 0:
            out["nav_per_share"] = round(nps, 2)
            out["mnav"] = round(mstr / nps, 4)
    return out


def mnav_to_price(mnav, nps):
    """Convert an mNAV threshold to the corresponding MSTR target price (using the current NAV per share)."""
    return round(mnav * nps, 2) if nps else None


# ─── mNAV historical series (Part 2) ──────────────────────────────────────────
# Historical daily mNAV = MSTR daily close × diluted shares(t) / (holdings(t) × BTC daily close);
# holdings and diluted shares come from a stepped timeline (mstr_holdings_timeline.json, sourced from
# MSTR 8-K filings; convention = saylortracker navPremiumDiluted).
# Prices fetched from Yahoo (MSTR) + Binance (BTC); no scraping of third-party trackers.
# On any source failure, returns whatever partial data is available — never fabricates values.

_TIMELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mstr_holdings_timeline.json")


def _epoch_to_date(epoch_s):
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%d")


def get_mstr_daily(range_str="3mo"):
    """MSTR daily closing prices as [(date, close)] (Yahoo chart API).
    Returns [] on failure; callers handle gracefully."""
    try:
        d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/MSTR?interval=1d&range={range_str}")
        res = d["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        return [(_epoch_to_date(t), round(float(c), 2)) for t, c in zip(ts, closes) if c]
    except Exception:
        return []


def get_btc_daily(window_days=90):
    """BTC daily closing prices as {date: close} from Binance klines API. Returns {} on failure."""
    try:
        rows = _get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit={int(window_days) + 5}")
        return {_epoch_to_date(r[0] / 1000): float(r[4]) for r in rows}
    except Exception:
        return {}


def load_timeline(path=_TIMELINE_PATH):
    """Load the stepped holdings timeline [{date, btc_holdings, diluted_shares_m}] (ascending by date).
    Returns [] on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["timeline"]
    except Exception:
        return []


def timeline_lookup(timeline, date):
    """Find the most recent timeline entry with date ≤ the given date.
    Returns None if no such entry exists. Timeline must be sorted ascending by date."""
    found = None
    for t in timeline:
        if t["date"] <= date:
            found = t
        else:
            break
    return found


_SERIES_CACHE = {"ts": 0.0, "window": None, "data": None}
_SERIES_TTL = 600  # In-process cache TTL: 10 minutes (historical daily data changes at most once per day;
                   # avoids rate-limiting on repeated dashboard refreshes)


def get_mnav_series(window_days=90, mstr_daily=None, btc_daily=None, timeline=None):
    """Compute the mNAV daily series for the past window_days trading days
    [{date, mnav, mstr, btc}] (convention: diluted shares / no net-debt deduction).
    mstr_daily / btc_daily / timeline can be injected for testing (injection bypasses the
    in-process cache). On any source failure, returns whatever partial data is available
    (possibly empty) — never fabricates values.
    Live fetch results are cached in-process for _SERIES_TTL seconds; the current-day point is
    provided separately by the caller via compute() and is not affected by this cache."""
    injected = mstr_daily is not None or btc_daily is not None or timeline is not None
    if not injected:
        c = _SERIES_CACHE
        if c["data"] is not None and c["window"] == window_days and (time.time() - c["ts"]) < _SERIES_TTL:
            return c["data"]
    if mstr_daily is None:
        mstr_daily = get_mstr_daily("3mo" if window_days <= 92 else "6mo")
    if btc_daily is None:
        btc_daily = get_btc_daily(window_days)
    if timeline is None:
        timeline = load_timeline()
    if not mstr_daily or not btc_daily or not timeline:
        return []
    out = []
    for date, mstr in mstr_daily[-int(window_days):]:
        btc = btc_daily.get(date)
        t = timeline_lookup(timeline, date)
        if btc is None or t is None:
            continue
        nps = (t["btc_holdings"] * btc) / (t["diluted_shares_m"] * 1e6)
        if nps > 0:
            out.append({"date": date, "mnav": round(mstr / nps, 4),
                        "mstr": mstr, "btc": round(btc, 2)})
    if not injected and out:
        _SERIES_CACHE.update(ts=time.time(), window=window_days, data=out)
    return out


if __name__ == "__main__":
    import sys
    r = compute()
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False))
    else:
        if r["mnav"] is not None:
            print(f"MSTR ${r['mstr']:.2f} · BTC ${r['btc']:,.0f} · NAV/share ${r['nav_per_share']:.2f} · mNAV {r['mnav']:.3f}x")
        else:
            print("mNAV fetch failed (price source unreachable)")
