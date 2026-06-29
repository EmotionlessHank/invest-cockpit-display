"""
app.py — BTC tracker dashboard backend (Flask, read-only monitoring)

Routes:
  GET /d                      -> Device cookie or URL dual-factor auth (token+code), returns dashboard.html
  GET /api/dashboard          -> Same auth, returns dashboard JSON
  POST /api/record            -> Body/header dual-factor auth, or device cookie + X-Dash-Auth header
Data: state.json (manual ledger) + Binance public price feed (spot/klines, no API key required)
      + strategy module for DCA advice.
Security: HMAC dual-factor + 90-day device cookie (set on TG link click); auth failures return a
          plain fake 404. No trading keys are held.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime

from flask import Flask, request, send_file, jsonify, abort

import strategy
import strategy_mstr
import mnav
import dash_link
import onchain_signals
import tier_alert
import subcode
import frozen_tiers

HERE = pathlib.Path(__file__).parent
# state.json is shared with the record_fill.py conversation layer.
# Override via BTC_STATE env var at deploy time to point to the live ledger path.
STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))
MSTR_STATE = pathlib.Path(os.environ.get("MSTR_STATE", str(HERE / "mstr-state.seed.json")))
RATE = float(os.environ.get("RMB_PER_USDT", "7.0"))
# Cycle clock: bear-market low is a user-estimated constant injected directly by the app
# (not fetched, not used for order decisions). Halving estimates depend on onchain cache.
# Even if the cron job fails, the bear-low date is always shown.
BEAR_LOW_DATE = "2026-10-05"
# ¥/USDT sanity guard: P2P price outside this band indicates an API error or abuse attempt;
# fall back to RATE rather than displaying a nonsensical value.
SANE_RATE_MIN, SANE_RATE_MAX = 5.0, 10.0
# Plain fake 404 returned on all auth failures — does not reveal that the dashboard exists.
NOT_FOUND_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>404 Not Found</title></head>"
    "<body style='font-family:-apple-system,sans-serif;text-align:center;padding:64px 20px;color:#555'>"
    "<h1 style='font-size:48px;margin:0'>404</h1><p>Not Found</p></body></html>"
)
# Sole write-entry scripts: the dashboard POST route forwards writes to these scripts via subprocess
# and never mutates the ledger directly — all state changes are funnelled through the record scripts.
# Default paths are relative (local dev); set RECORD_BTC / RECORD_MSTR at deploy time.
RECORD_BTC = pathlib.Path(os.environ.get("RECORD_BTC", str(HERE.parent / "skills/btc-tracker/scripts/record_fill.py")))
RECORD_MSTR = pathlib.Path(os.environ.get("RECORD_MSTR", str(HERE.parent / "skills/mstr-tracker/scripts/record_fill_mstr.py")))


def check(req):
    """Read-route auth: device cookie or URL dual-factor, either passes; both fail -> fake 404 (see errorhandler).
    Returns True when this request passed via URL dual-factor (i.e. user clicked a TG link), so the
    route layer can set a device cookie on the way out."""
    if dash_link.verify_device_cookie(req.cookies.get(dash_link.COOKIE_NAME, "")):
        return False
    if not dash_link.verify(req.args.get("token", ""), req.args.get("code", "")):
        abort(404)
    return True


def check_write(req, body):
    """Write-route auth: (1) body/header dual-factor (never in URL query string); or
    (2) device cookie + custom X-Dash-Auth header.
    Cookie-only requests without the custom header are rejected — a second CSRF defence
    beyond SameSite=Lax (cross-site requests cannot set custom headers;
    see OWASP CSRF Cheat Sheet custom-header defence). Both paths fail -> fake 404."""
    tok = body.get("token") or req.headers.get("X-Dash-Token") or req.args.get("token", "")
    code = body.get("code") or req.headers.get("X-Dash-Code") or req.args.get("code", "")
    if dash_link.verify(tok, code):
        return
    if req.headers.get("X-Dash-Auth") == "cookie" \
            and dash_link.verify_device_cookie(req.cookies.get(dash_link.COOKIE_NAME, "")):
        return
    abort(404)


def _num(v, lo, hi):
    """Parse input as a float and clamp to [lo, hi]; returns None on invalid input.
    This is the first validation layer in app.py; the record script performs a second check."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _date_str(v):
    """Optional backfill date: YYYY-MM-DD, calendar-valid, 2020-01-01 <= date <= today.
    Returns None on invalid input (first layer in app.py; the record script performs a second check)."""
    s = str(v).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return s if date(2020, 1, 1) <= d <= datetime.now().astimezone().date() else None


def _run_record(script, state_key, state_path, args, force=False):
    """Invoke the sole write-entry script via subprocess and return the last JSON line of stdout.
    app.py never touches state directly."""
    full = [sys.executable, str(script), *args]
    if force:
        full.append("--force")
    full.append("--no-board")
    env = {**os.environ, state_key: str(state_path)}
    try:
        r = subprocess.run(full, capture_output=True, text=True, env=env, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Record timed out, please retry"}
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    try:
        return json.loads(lines[-1])
    except Exception:
        return {"ok": False, "msg": "Record script produced no valid output", "_err": (r.stderr or "")[:200]}


def _get(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.loads(r.read())


def _post(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read())


def get_spot() -> float | None:
    try:
        return float(_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        return None


def get_usdt_cny_rate() -> tuple[float, str]:
    """Real-time ¥/USDT reference rate (Binance P2P best sell quote, typically ~6.81).
    Used only for display conversion (showing amounts as "≈$X"); never used for order
    placement or bookkeeping. On any error (network, rate-limit, schema change) falls back
    to RATE (env RMB_PER_USDT or 7.0); never fabricates a value.
    Returns (rate, source) where source in {"binance", "fallback"}."""
    try:
        data = _post(
            "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
            {"asset": "USDT", "fiat": "CNY", "tradeType": "SELL", "page": 1, "rows": 1},
        )
        price = float(data["data"][0]["adv"]["price"])
        if SANE_RATE_MIN <= price <= SANE_RATE_MAX:
            return price, "binance"
    except Exception:
        pass
    return RATE, "fallback"


def get_klines_30d() -> list[float]:
    try:
        rows = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30")
        return [float(r[4]) for r in rows]  # closing price
    except Exception:
        return []


def current_tier(tiers, spot):
    for t in tiers:
        if t["lo"] <= spot < t["hi"]:
            return t["name"].split(" ")[0]
    return None


def build_dashboard(st: dict) -> dict:
    subcode.migrate_btc(st)  # In-memory schema migration before display (idempotent, never writes to disk)
    spot = get_spot() or st["avg_cost_usd"]
    # Real-time ¥/USDT reference rate (display only; fetched once per request alongside get_spot).
    # On failure falls back to RATE — never fabricates a value. Order/bookkeeping flow uses RATE, unaffected.
    rate, rate_source = get_usdt_cny_rate()
    prices = get_klines_30d()
    if prices:
        prices[-1] = spot
    avg = st["avg_cost_usd"]
    pnl = (spot - avg) / avg * 100

    # DCA advice (via strategy module; read-only monitoring, balance guard skipped).
    # Downgrade check uses strategy.dip_filled_last_7d as the single source of truth
    # (scans fills over the last 7 days including partial legs; consistent with TG alerts).
    ctx = strategy.WeeklyContext(
        btc_price_usd=spot, invested_rmb=st["invested_rmb"],
        available_usdt=10 ** 9, dip_filled_last_7d=strategy.dip_filled_last_7d(st),
        rate_rmb_per_usdt=RATE,
        window_end=strategy._golden_window_end(),  # inject window end for paced weekly sizing
    )
    d = strategy.decide_weekly(ctx)
    # Weekly DCA check-in: dca_last_at is written by the record script (--dca flag); read-only here.
    dca_done = strategy.same_iso_week(st.get("dca_last_at", ""))
    weekly = {
        "action": d.action,
        "tier": strategy.TIER_LABEL[d.final_tier],
        "amount_rmb": round(d.amount_rmb),
        "amount_usdt": round(d.amount_usdt),
        "downgraded": d.final_tier != d.base_tier,
        "done_this_week": dca_done,
        "done_at": st.get("dca_last_at", "") if dca_done else "",
        "skip_reason": "" if d.action == "BUY" else (d.reasons[-1] if d.reasons else ""),
    }
    if d.action == "BUY":
        action = {"l1": f"Weekly DCA suggestion ({strategy.TIER_LABEL[d.final_tier]} tier"
                        + (" · downgraded, dip fill in last 7d" if ctx.dip_filled_last_7d else "") + ")",
                  "l2": f"Buy manually ¥{d.amount_rmb:,.0f} ≈ ${d.amount_usdt:,.0f} USDT"}
    else:
        action = {"l1": "Weekly DCA", "l2": f"{strategy.TIER_LABEL[d.final_tier]} (hold, no add)"}

    cur = current_tier(st["tiers"], spot)
    nxt = next((t for t in st["tiers"] if t["status"] in ("open", "wait")), None)

    # Ledger: filled legs per tier (with audit fields, falls back to computed values if missing), newest first
    ledger = []
    for t in st["tiers"]:
        for f in t.get("fills", []):
            ledger.append({
                "at": f.get("at", ""),
                "tier": t["name"].split()[0],
                "price": f["price"], "rmb": f["rmb"],
                "btc": f.get("btc", round(f["rmb"] / RATE / f["price"], 8)),
            })
    ledger.sort(key=lambda x: x["at"], reverse=True)
    dca_used = next((t["used"] for t in st["tracks"] if t["name"] == "DCA"), 0)
    # Per-trade DCA history: dca_fills appended by the record script; pre-itemisation cumulative
    # amounts cannot be split into individual trades and are shown as a single legacy line.
    dca_fills = sorted(st.get("dca_fills", []), key=lambda f: str(f.get("at", "")), reverse=True)
    dca_legacy = max(0, round(dca_used - sum(f.get("rmb", 0) for f in dca_fills)))

    # "A base position (built)" track: dynamically injected — base = total invested minus
    # the three bucket totals (fully deployed, no remaining quota -> neutral bar at 100%).
    # Injected in-memory only; the ledger is never modified. After injection the top
    # "Total budget used" bar equals base + DCA + B + C, keeping the panel balanced.
    base_used = max(0, round(st["invested_rmb"] - sum(t["used"] for t in st["tracks"] if t["name"] != "Total budget used")))
    tracks = list(st["tracks"])
    base_idx = next((i for i, t in enumerate(tracks) if t["name"] == "Total budget used"), -1) + 1
    tracks.insert(base_idx, {"name": "A base position (built)", "used": base_used, "quota": base_used, "color": "teal"})

    # Extra funding pool (independent of the main 200K position cap; records cashflow + total only)
    extra_items = st.get("extra_funding", [])
    extra = {"total": round(sum(e.get("amount", 0) for e in extra_items)),
             "items": sorted(extra_items, key=lambda e: e.get("at", ""), reverse=True)}

    # On-chain signal board: cache is written daily by the cron job (onchain_signals.py --fetch);
    # this endpoint is read-only. Signal logic lives in the strategy module (single source of truth).
    # Missing/stale cache -> cards carry na/staleness markers; the page never goes blank.
    oc_cache = onchain_signals.load_cache()
    oc_cards = strategy.onchain_signal_cards(oc_cache, prices, st.get("onchain", []))
    resonance = strategy.onchain_resonance(oc_cards)
    # Dynamic tier-view (dip-buy levels): calls tier_alert.build_tier_view to compute per-tier
    # dynamic trigger prices and distance-from-spot percentages.
    # Read-only display injection; state.json is never written back.
    # Spot and cache are read once per request (same anchor point for all tiers).
    # manual_green is loaded from a separate config file (manual_anchors.json);
    # missing/stale -> that tier is fail-closed ("data unavailable" / "green line expired").
    green, green_stale = tier_alert.manual_green()
    tier_view = tier_alert.build_tier_view(oc_cache, st, manual_green=green, spot=spot, green_stale=green_stale)
    # Main table trigger-price overlay (frozen snapshots for stability): for open legs of B3/C1/C2,
    # plan[i].price in the payload is replaced with the frozen trigger price (updated at most once
    # per week / threshold breach to avoid constant price churn). Falls back to live recomputed
    # prices (tier_view.triggers) when no frozen snapshot exists.
    # Filled legs always show the real fill price (no overlay). B1/B2 are untouched (already filled).
    # Pure in-memory payload copy — state.json is never written back.
    DYN_CODES = {"B3", "C1", "C2"}
    tv_trig = {t.get("code"): t.get("triggers") for t in (tier_view.get("tiers") or [])}
    tv_filled = {t.get("code"): t.get("filled") for t in (tier_view.get("tiers") or [])}
    frozen_snap = frozen_tiers.load_frozen()           # load frozen snapshot once per request
    frozen_btc_snap = frozen_snap.get("btc") or {}
    frozen_btc = frozen_btc_snap.get("legs") or {}
    frozen_at_btc = frozen_btc_snap.get("frozen_at", "")
    tiers_display = []
    for t in st["tiers"]:
        code = str(t.get("name", "")).split(" ")[0].strip()
        trig = tv_trig.get(code)
        if code in DYN_CODES and trig and not tv_filled.get(code):
            # Open legs: align price-high-to-low with trig (same order); prefer frozen price if available.
            new_plan = []
            open_idx = 0
            for p in t.get("plan", []):
                if p.get("status") == "filled":
                    new_plan.append(dict(p))   # filled leg: preserve original plan price, no overlay
                    continue
                lab = p.get("label")
                fz = frozen_btc.get(lab) if lab else None
                price = fz["price"] if isinstance(fz, dict) and "price" in fz \
                    else (trig[open_idx] if open_idx < len(trig) else p.get("price"))
                new_plan.append({**p, "price": price})
                open_idx += 1
            t = {**t, "plan": new_plan}
        tiers_display.append(t)
    # Chart reference lines: realized price (structural support) / true market mean / STH cost basis
    brk = (oc_cache.get("brk") or {}).get("data") or {}
    chart_lines = [{"key": k, "y": round(brk[k]), "label": f"{lab} ${brk[k]/1000:.1f}K"}
                   for k, lab in (("realized_price", "Realized Price"), ("true_market_mean", "True Market Mean"),
                                  ("sth_realized_price", "STH Cost Basis"))
                   if brk.get(k)]

    # Cycle clock: bear-market low is a static user-estimated constant (always injected);
    # halving estimate is read from the onchain cache cycle section (written by daily cron --fetch),
    # read-only, never written back. No cache / fetch failure -> halving set to None; page stays populated.
    cyc = oc_cache.get("cycle") or {}
    cd = cyc.get("data") or {}
    cycle = {
        "bear_low_date": BEAR_LOW_DATE,
        "halving": ({"est_date": cd["est_date"], "blocks_remaining": cd.get("blocks_remaining"),
                     "height": cd.get("height"), "fetched_at": cyc.get("fetched_at", "")}
                    if cd.get("est_date") else None),
    }

    return {
        "updated": st["updated"],
        "badge": f"Price in {cur} zone" if cur else "Not in dip-buy zone",
        "kpis": {
            "held_btc": st["held_btc"], "avg_cost": avg, "spot": round(spot),
            "pnl_pct": round(pnl, 1), "invested": st["invested_rmb"],
            "pnl_rmb": round((spot - avg) * st["held_btc"] * RATE),
        },
        "tracks": tracks,
        "tiers": tiers_display,
        "ledger": ledger,
        "dca_used": dca_used,
        "dca_fills": dca_fills,
        "dca_legacy": dca_legacy,
        "extra": extra,
        "onchain": oc_cards,
        "resonance": resonance,
        "tier_view": tier_view,
        # Frozen snapshot metadata: frontend shows "last updated frozen_at + current drift%". Empty if no snapshot.
        "frozen": {"at": frozen_at_btc,
                   "drift": frozen_btc_snap.get("drift") or {}},
        "chart": {"prices": prices, "avg": avg, "spot": round(spot), "lines": chart_lines,
                  "fill": next(({"price": f["price"], "label": t["name"].split(" ")[0] + " filled"}
                               for t in st["tiers"] for f in t.get("fills", [])), None),
                  "tiers": [{"lo": t["lo"], "hi": t["hi"], "name": t["name"], "sem": t["sem"],
                             "key": t["sem"] == "green"} for t in st["tiers"]]},
        "action": {**action, "l3": f"Next watch: {nxt['name']} ${nxt['lo']//1000}–{nxt['hi']//1000}K" if nxt else ""},
        "weekly": weekly,
        "rate": rate,
        "rate_source": rate_source,
        "cycle": cycle,
    }


def mstr_zone(m):
    if m is None:
        return "mNAV fetch failed"
    if m > 1.0:
        return f"mNAV {m:.2f}x · premium, not entering"
    if m >= 0.78:
        return f"mNAV {m:.2f}x · in T1 zone"
    if m >= 0.65:
        return f"mNAV {m:.2f}x · in T2 zone"
    if m >= 0.50:
        return f"mNAV {m:.2f}x · Swan-A zone"
    return f"mNAV {m:.2f}x · Swan-B zone"


def _load_mnav_snapshots():
    """Load daily mNAV snapshot JSONL (precise per-trade-time locked values).
    Missing or corrupt file returns []; never raises."""
    path = os.environ.get("MSTR_MNAV_HISTORY", os.path.expanduser("~/.cockpit/mstr-mnav-history.jsonl"))
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def build_dashboard_mstr(st: dict) -> dict:
    subcode.migrate_mstr(st)  # In-memory schema migration before display (idempotent, never writes to disk)
    q = mnav.compute()
    price, m, nps = q["mstr"], q["mnav"], q["nav_per_share"]
    total_budget = st["budget_core_usd"] + st["budget_swan_usd"]
    avg = st["avg_cost_usd"]
    held = st["held_shares"]
    pnl_pct = round((price - avg) / avg * 100, 1) if (price and held and avg) else None
    pnl_usd = round((price - avg) * held) if (price and held and avg) else None

    # Rounds: inject done_usd + live target prices for mNAV batches
    rounds = []
    for r in st["rounds"]:
        plan = []
        for b in r["plan"]:
            nb = dict(b)
            if b.get("mnav") and nps:
                nb["live_price"] = round(b["mnav"] * nps)
            plan.append(nb)
        rounds.append({**r, "plan": plan,
                       "done_usd": round(sum(f["usd"] for f in r["fills"]), 2)})

    # Layer capital allocation
    def layer_used(name):
        return round(sum(f["usd"] for r in st["rounds"] if r["layer"] == name for f in r["fills"]), 2)
    layers = [{"name": "Core", "budget": st["budget_core_usd"], "used": layer_used("core"), "sem": "amber"},
              {"name": "Swan reserve", "budget": st["budget_swan_usd"], "used": layer_used("swan"), "sem": "red"}]

    # Ledger: round fills + standalone buys (not attached to a round)
    ledger = []
    for r in st["rounds"]:
        for f in r["fills"]:
            ledger.append({"at": f.get("at", ""), "round": r["id"], "label": f.get("label", ""),
                           "price": f["price"], "shares": f["shares"], "usd": f["usd"]})
    for f in st.get("standalone_fills", []):
        ledger.append({"at": f.get("at", ""), "round": "standalone", "label": f.get("label", "standalone buy"),
                       "price": f["price"], "shares": f["shares"], "usd": f["usd"]})
    ledger.sort(key=lambda x: x["at"], reverse=True)

    # mNAV position bar (range 0.3–1.1)
    mnav_bar = {"mnav": m, "lo": 0.30, "hi": 1.10,
                "zones": [{"name": "T1", "lo": 0.78, "hi": 1.00, "sem": "amber"},
                          {"name": "T2", "lo": 0.65, "hi": 0.78, "sem": "green"},
                          {"name": "Swan-A", "lo": 0.50, "hi": 0.65, "sem": "red"},
                          {"name": "Swan-B", "lo": 0.30, "hi": 0.50, "sem": "rust"}]}

    # Price chart vs dip-buy levels: 30-day daily candles + per-round trigger price bands.
    # Band range = min–max of plan+fill prices for that round; mNAV batches use live_price (floats with market).
    prices = mnav.get_mstr_klines_30d()
    chart = None
    if prices and price:
        prices[-1] = round(price, 2)   # last point uses live price, consistent with KPIs
        bands = []
        for r in rounds:
            pts = [b.get("live_price") or b.get("price") for b in r["plan"]] \
                + [f["price"] for f in r["fills"]]
            pts = [p for p in pts if p]
            if pts:
                bands.append({"lo": min(pts), "hi": max(pts), "name": r["name"].split(" ")[0],
                              "sem": r["sem"], "key": r["sem"] == "green"})
        fill = next(({"price": f["price"], "label": f"{r['id']} filled"}
                     for r in st["rounds"] for f in r.get("fills", [])), None)
        chart = {"prices": prices, "avg": (avg if held else None), "spot": round(price, 2),
                 "fill": fill, "tiers": bands}

    # mNAV historical series (90 days): daily sequence + threshold bands + current point.
    # Caliber matches saylortracker navPremiumDiluted (no net debt / diluted shares); prices
    # sourced directly, tier entry points from 8-K step timeline, not scraped from saylortracker.
    # Backfill covers the full window (verified to 2dp against saylortracker); daily snapshots
    # override the backfill for trade-time locked precision where available.
    mnav_series = None
    series = mnav.get_mnav_series(90)
    if series:
        by_date = {p["date"]: {"date": p["date"], "mnav": p["mnav"], "mstr": p["mstr"], "src": "calc"}
                   for p in series}
        for sp in _load_mnav_snapshots():           # snapshot overrides backfill for locked precision
            if sp.get("date") in by_date and sp.get("mnav") is not None:
                by_date[sp["date"]] = {"date": sp["date"], "mnav": round(sp["mnav"], 4),
                                       "mstr": sp.get("mstr"), "src": "snap"}
        pts = [by_date[d] for d in sorted(by_date)]
        mnav_series = {
            "points": pts,
            "current": m,                            # live mNAV (same source as mnav.compute)
            "thresholds": [{"v": 1.00, "name": "T1", "sem": "amber"},
                           {"v": 0.78, "name": "T2", "sem": "green"},
                           {"v": 0.65, "name": "Swan-A", "sem": "red"},
                           {"v": 0.55, "name": "Swan-B", "sem": "rust"},
                           {"v": 0.40, "name": "Swan-C", "sem": "rust"}],
            "caliber": "same caliber as saylortracker · no net debt / diluted shares",
        }

    # MSTR Monday DCA card (core-position window pacing, standalone top-up):
    # core_budget = sum of T1+T2 round budget_usd (does NOT include swan positions).
    # core_invested = strategy_mstr.core_invested_usd (core fills + standalone; excludes swan).
    # Using the top-level invested_usd (which includes swan) as core_invested would incorrectly
    # drain the core DCA pool whenever a swan fill is recorded — this bug is intentionally avoided here.
    # mNAV from the same mnav.compute call; fetch failure -> DEGRADED, no amount computed.
    # DCA is recorded via record_fill_mstr --standalone (core top-up); it does NOT claim
    # open limit-price legs. overlay_leg = currently open mNAV limit leg shown as "deep discount
    # overlay" reference only (not an instruction to record it as DCA).
    core_budget = sum(r.get("budget_usd", 0) for r in st["rounds"] if r.get("layer") == "core")
    overlay_leg = strategy_mstr.next_open_core_leg(st)   # single source of truth for next open leg
    md = strategy_mstr.decide_weekly_mstr(strategy_mstr.core_invested_usd(st), core_budget, m,
                                          window_end=strategy._golden_window_end())
    mstr_weekly = {
        "action": md.action,
        "amount_usd": round(md.amount_usd),
        "record_mode": "standalone",   # DCA recorded as standalone core top-up; does not claim dip legs
        "overlay_leg": overlay_leg,     # currently open mNAV limit leg (overlay reference only)
        "weeks": md.weeks,
        "tilt": md.tilt,
        "mnav": md.mnav,
        "tilt_label": strategy_mstr.mstr_tilt_label(m) if m is not None else "",
        "pool_usd": round(md.pool_usd),
        # MSTR has no separate DCA track or timestamp, so done_this_week is always False
        # (the frontend suppresses the check-in indicator accordingly).
        "done_this_week": False,
        "skip_reason": (md.reasons[-1] if md.action != "BUY" and md.reasons else ""),
    }

    nxt = next((r for r in st["rounds"] if r["plan"]), None)
    return {
        "type": "mstr",
        "updated": st["updated"],
        "badge": mstr_zone(m),
        "kpis": {"held_shares": held, "avg_cost": avg, "mstr": price, "mnav": m,
                 "nav_per_share": nps, "invested": st["invested_usd"], "budget_total": total_budget,
                 "pnl_pct": pnl_pct, "pnl_usd": pnl_usd},
        "layers": layers,
        "rounds": rounds,
        "ledger": ledger,
        "exits": st.get("exits", []),
        "mnav_bar": mnav_bar,
        "chart": chart,
        "mnav_series": mnav_series,
        "weekly": mstr_weekly,
        "action": {"l1": "Next watchpoint",
                   "l2": (f"{nxt['name']} ({nxt['layer']})" if nxt else "All positions filled"),
                   "l3": (f"🎯 {nxt['signal']}" if nxt else "")},
    }


app = Flask(__name__)


@app.get("/d")
def page():
    fresh = check(request)
    resp = send_file(HERE / "dashboard.html")
    if fresh:
        # URL dual-factor = TG link activation: set a 90-day device cookie so future
        # browser visits to /d work without a token.
        # Uses __Host- prefix + Secure+HttpOnly+SameSite=Lax+Path=/ per OWASP spec;
        # Lax ensures the TG redirect carries the cookie on the initial hop.
        resp.set_cookie(dash_link.COOKIE_NAME, dash_link.make_device_cookie(),
                        max_age=dash_link.DEVICE_COOKIE_MAX_AGE, secure=True,
                        httponly=True, samesite="Lax", path="/")
    return resp


@app.get("/api/dashboard")
def api():
    check(request)
    if request.args.get("type") == "mstr":
        st = json.loads(MSTR_STATE.read_text(encoding="utf-8"))
        return jsonify(build_dashboard_mstr(st))
    st = json.loads(STATE.read_text(encoding="utf-8"))
    return jsonify({**build_dashboard(st), "type": "btc"})


@app.post("/api/record")
def record():
    """Dashboard write endpoint: auth -> parameter whitelist validation -> forward to the sole record script.
    kind: dca / btc_fill / extra_fund / mstr_fill."""
    body = request.get_json(silent=True) or {}
    check_write(request, body)
    kind = body.get("kind")
    force = bool(body.get("force"))

    # Optional backfill date (btc_fill / extra_fund / mstr_fill; dca has no per-trade record)
    at_args = []
    if body.get("date") not in (None, ""):
        dt = _date_str(body.get("date"))
        if dt is None:
            return jsonify({"ok": False, "msg": "Invalid backfill date (YYYY-MM-DD, from 2020 to today, must not be in the future)"})
        at_args = ["--at", dt]

    if kind == "dca":
        rmb = _num(body.get("rmb"), 1, 200000)
        if rmb is None:
            return jsonify({"ok": False, "msg": "DCA amount invalid (¥0–200000)"})
        return jsonify(_run_record(RECORD_BTC, "BTC_STATE", STATE, ["--dca", "--rmb", str(rmb), *at_args], force))

    if kind == "btc_fill":
        price = _num(body.get("price"), 1000, 1000000)
        if price is None:
            return jsonify({"ok": False, "msg": "Fill price invalid ($1000–1000000)"})
        args = ["--price", str(price)]
        # Leg label (app-layer format whitelist, first pass): must match pattern like B2-a;
        # guards against flag-injection. Existence and filled-status are validated by the script (second pass).
        label = str(body.get("label") or "").strip()
        if label:
            if not re.fullmatch(r"[A-Z]\d?-[a-z]", label):
                return jsonify({"ok": False, "msg": "Leg label format invalid (e.g. B2-a)"})
            args += ["--label", label]
        # Amount unit: mutually exclusive ¥ vs USDT (conversion to ¥ happens inside the script).
        has_rmb = body.get("rmb") not in (None, "")
        has_usdt = body.get("usdt") not in (None, "")
        if has_rmb and has_usdt:
            return jsonify({"ok": False, "msg": "Amount ¥ and USDT are mutually exclusive; provide only one"})
        if has_rmb:
            rmb = _num(body.get("rmb"), 1, 200000)
            if rmb is None:
                return jsonify({"ok": False, "msg": "Amount invalid"})
            args += ["--rmb", str(rmb)]
        if has_usdt:
            usdt = _num(body.get("usdt"), 1, 30000)
            if usdt is None:
                return jsonify({"ok": False, "msg": "USDT amount invalid (1–30000)"})
            args += ["--usdt", str(usdt)]
        return jsonify(_run_record(RECORD_BTC, "BTC_STATE", STATE, args + at_args, force))

    if kind == "extra_fund":
        rmb = _num(body.get("rmb"), 1, 1000000)
        src = str(body.get("source") or "").strip()[:40]
        if rmb is None:
            return jsonify({"ok": False, "msg": "Extra funding amount invalid (¥0–1000000)"})
        if not src:
            return jsonify({"ok": False, "msg": "Please provide a funding source"})
        if src.startswith("-"):  # prevent source from being parsed as a flag by argparse (fail-closed)
            return jsonify({"ok": False, "msg": "Funding source must not start with -"})
        return jsonify(_run_record(RECORD_BTC, "BTC_STATE", STATE,
                                   ["--extra-fund", "--rmb", str(rmb), "--source", src, *at_args]))

    if kind == "mstr_fill":
        price = _num(body.get("price"), 10, 2000)
        if price is None:
            return jsonify({"ok": False, "msg": "MSTR fill price invalid ($10–2000)"})
        rnd = str(body.get("round") or "").strip().upper()
        if rnd and rnd not in ("T1", "T2", "A", "B"):
            return jsonify({"ok": False, "msg": "Round must be T1/T2/A/B (or omit for standalone entry)"})
        sh = None
        if body.get("shares") not in (None, ""):
            sh = _num(body.get("shares"), 0.0001, 1000)
            if sh is None:
                return jsonify({"ok": False, "msg": "Share count invalid (0–1000)"})
        if not rnd and sh is None:
            return jsonify({"ok": False, "msg": "Standalone entry (no round) requires share count"})
        # No round = standalone buy (not attached to a round plan); round provided = claim a batch leg
        args = (["--round", rnd] if rnd else ["--standalone"]) + ["--price", str(price)]
        if sh is not None:
            args += ["--shares", str(sh)]
        return jsonify(_run_record(RECORD_MSTR, "MSTR_STATE", MSTR_STATE, args + at_args, force))

    return jsonify({"ok": False, "msg": f"Unknown operation kind={kind}"})


@app.get("/apple-touch-icon.png")
def touch_icon():
    """iOS home-screen bookmark icon. Uses the same read-route auth as /d — unauthorised
    probes still get a fake 404; an authorised Safari 'Add to Home Screen' request carries
    the active device cookie and succeeds. Icon source: Lucide gauge (ISC)."""
    check(request)
    return send_file(HERE / "apple-touch-icon.png")


@app.get("/favicon.svg")
def favicon():
    """Browser tab favicon (SVG gauge icon, sharp at 16px). Uses the same read-route auth
    as /d — unauthorised probes still get a fake 404; authorised browsers with an active
    device cookie succeed. Icon source: Lucide gauge (ISC)."""
    check(request)
    return send_file(HERE / "favicon.svg")


@app.get("/healthz")
def health():
    return "ok"


@app.errorhandler(404)
def not_found(e):
    """All 404s (including auth-failure abort(404)) return a plain fake page to avoid
    revealing that the dashboard exists."""
    return NOT_FOUND_PAGE, 404


if __name__ == "__main__":
    if "--link" in sys.argv:   # Print current dashboard URL (includes token+code). WARNING: credentials visible in output — call manually only, never in CI/cron/set -x.
        print(dash_link.dash_url(mstr="--mstr" in sys.argv))
        sys.exit(0)
    app.run(host="127.0.0.1", port=8848, debug=False)
