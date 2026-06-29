"""
dca_advice.py — computes "this week's suggested DCA amount" (reusing the strategy module's dynamic tiers). **Read-only; never writes the ledger, never places orders.**

Uses:
  1. Monday cron reminder: the cron calls this script -> stdout is injected into the btc-tracker skill -> wrapped into a TG reminder.
  2. "DCA done" recording: the skill calls this first to get this week's amount, then record_fill.py --dca --rmb {amount} (same source, so reminder amount == recorded amount).

Convention: strategy.decide_weekly (price into 5 tiers + downgrade if a dip filled in the last 7 days + cap/pause rules).
  The spot price comes from Binance's public endpoint (no key); if the fetch fails -> no made-up price, emit a degraded notice (honoring iron rule ② in SKILL).

Usage:
  dca_advice.py                 # fetch live price and compute this week's suggestion (text + final JSON line)
  dca_advice.py --spot 67000    # override the spot price (offline / testing)

The state path can be overridden via BTC_STATE; strategy.py lives in the same directory (distributed by the deploy script).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).parent
STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))
sys.path.insert(0, str(HERE))  # reuse strategy.py from the same directory (distributed into skill scripts on deploy)

import strategy  # noqa: E402


def get_spot():
    """Live BTC spot price (Binance public endpoint; returns None on timeout/failure — never blocks, never invents a price)."""
    try:
        with urllib.request.urlopen(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def advise(st, spot, rate):
    """Pure function: ledger + spot + rate -> (human text, result dict). spot=None means the fetch failed (no made-up price)."""
    # —— Fetch failed: no made-up price, emit a degraded notice (honoring SKILL iron rule ②) ——
    if spot is None:
        return ("⚠️ This week's DCA reminder: the live spot price fetch failed, so the tier cannot be determined. Open the cockpit to check the current tier, then DCA manually.",
                {"ok": False, "msg": "price fetch failed", "week_amount_rmb": None})

    dip = strategy.dip_filled_last_7d(st)   # single source in strategy.py (shared with the cockpit, prevents paired-path drift)
    # Inject the golden-window deadline -> use "window pacing" (remaining pool / remaining weeks * tilt, fully deployed by window end).
    ctx = strategy.WeeklyContext(
        btc_price_usd=spot, invested_rmb=st["invested_rmb"],
        available_usdt=10 ** 9,  # the reminder does not check balance
        dip_filled_last_7d=dip, rate_rmb_per_usdt=rate,
        window_end=strategy._golden_window_end(),
    )
    d = strategy.decide_weekly(ctx)

    weeks = d.weeks   # single source: the decision already computed the remaining weeks in the window; do not recompute
    if d.action == "BUY":
        head = f"📅 This week's DCA suggestion: ¥{d.amount_rmb:,.0f} (window pacing · {strategy.TIER_LABEL[d.base_tier]} tier ≈ ${d.amount_usdt:,.0f} USDT)"
    else:
        head = f"📅 This week's DCA: {strategy.TIER_LABEL[d.final_tier]} (no add for now)"
    lines = [head, f"Spot ${spot:,.0f} -> lands in the \"{strategy.TIER_LABEL[d.base_tier]}\" tier ({weeks} weeks left in window)"]
    if d.action == "BUY":
        lines.append("Convention: remaining pool / remaining weeks in window * spot-tier tilt, forced full deployment by window end")

    return ("\n".join(lines), {
        "ok": True,
        "week_amount_rmb": d.amount_rmb,
        "tier": strategy.TIER_LABEL[d.final_tier],
        "base_tier": strategy.TIER_LABEL[d.base_tier],
        "downgraded": d.final_tier != d.base_tier,
        "btc_price": round(spot),
        "action": d.action,
        "reasons": d.reasons,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", type=float, default=None, help="override the live spot price (offline / testing)")
    a = ap.parse_args()

    if not STATE.exists():
        return out({"ok": False, "msg": f"ledger not found: {STATE}"})
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return out({"ok": False, "msg": f"ledger JSON corrupted ({e.msg} @line{e.lineno}), please verify manually"})

    rate = st.get("rate_rmb_per_usdt", 7.0)
    spot = a.spot if a.spot is not None else get_spot()
    text, res = advise(st, spot, rate)
    print(text)
    out(res)


if __name__ == "__main__":
    main()
