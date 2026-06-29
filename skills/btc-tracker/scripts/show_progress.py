"""
show_progress.py — quick in-TG text progress board (code-block terminal style) + cockpit link.
Called by the agent skill when checking progress; detailed charts live on the cockpit web page.

Each dip tier expands its "sub-plan" limit-order legs (price + amount + fill status), so tier-level and total-budget progress is visible at a glance.
The state path can be overridden via BTC_STATE; the cockpit link is injected via DASH_URL.
"""

import json
import os
import pathlib
import sys
import urllib.request

import dash_link

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))  # reuse the shared subcode (distributed into skill scripts with the app module on deploy)

import subcode  # noqa: E402

STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))
DASH = os.environ.get("DASH_URL") or dash_link.dash_url()   # live-signed link (static token + 24h time-limited code)

W = 18  # board separator-line width (kept short so it doesn't wrap on mobile)


def get_spot():
    """Live BTC spot price (Binance public endpoint; returns None on timeout/failure so the board never blocks)."""
    try:
        with urllib.request.urlopen(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=6) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None


def spot_zone(spot, tiers):
    """Which dip tier the spot lands in; returns a note if above the highest tier."""
    for t in tiers:
        if t["lo"] <= spot < t["hi"]:
            return f"in {t['name'].split()[0]} zone"
    hi = max(t["hi"] for t in tiers)
    return "above dip zone" if spot >= hi else "below C2"


def bar(frac, n=10):
    f = max(0, min(1, frac))
    k = round(f * n)
    return "▓" * k + "░" * (n - k)


def k(n):
    """Compact amount: 0->0, 6000->6K, 7135->7K, 14000->14K."""
    return "0" if not n else f"{round(n / 1000)}K"


def tier_phase(t):
    """Derive the tier's display status from data (new schema: plan legs are not deleted; status is derived)."""
    return subcode.derive_tier_status(t)


TICON = {"done": "✅", "partial": "◑", "open": "○", "wait": "·"}


def _legs(t):
    """Expand legs under the new schema: plan legs (with label/status) + orphan fills not tied to a plan leg (top-ups/history).
    Returns [(label, price, rmb, done_bool), ...], priced high to low."""
    plan_labels = {p.get("label") for p in t.get("plan", [])}
    out = [(p.get("label", ""), p.get("price"), p.get("rmb", 0), p.get("status") == "filled")
           for p in t.get("plan", [])]
    for f in t.get("fills", []):
        if f.get("label") not in plan_labels:   # orphan fill (top-up/history, no resident plan leg)
            out.append((f.get("label", ""), f.get("price"), f.get("rmb", 0), True))
    out.sort(key=lambda x: -(x[1] or 0))
    return out


def leg_lines(t, phase):
    """Expand the sub-plan by sub-label: one line per leg with label/price/amount + status (✅filled / ○resting / ·deep-dip-waiting). Priced high to low."""
    pend = "·" if phase == "wait" else "○"
    lines = []
    for lab, price, rmb, done in _legs(t):
        ic = "✅" if done else pend
        lines.append(f"   {ic} {lab} ${price:,.0f}  ¥{k(rmb)}")
    return lines


def build_board(s, dash=""):
    """Build the full text-board string (with the ``` code block + cockpit link). Shared by main and record_fill's post-recording hand-back."""
    subcode.migrate_btc(s)  # self-healing migration before display (old schema gets label+status patched in memory; idempotent, not written back to disk)
    pct = s["invested_rmb"] / s["budget_total_rmb"]
    left = s["budget_total_rmb"] - s["invested_rmb"]
    line = "━" * W

    spot = get_spot()
    avg = s["avg_cost_usd"]
    if spot:
        pnl = (spot - avg) / avg * 100
        spot_line = f"Spot ${spot:,.0f} · {spot_zone(spot, s['tiers'])} · {pnl:+.1f}%"
    else:
        spot_line = "Spot: see cockpit (live price fetch failed)"

    out = ["```", "BTC dip progress", line,
           f"Invested {bar(pct)} {pct * 100:.0f}%",
           f"¥{s['invested_rmb']:,.0f} / ¥{s['budget_total_rmb']:,.0f}",
           f"Holdings {s['held_btc']:.4f} BTC · avg ${avg:,.0f}",
           spot_line,
           line]

    for t in s["tiers"]:
        phase = tier_phase(t)
        done = sum(f["rmb"] for f in t["fills"])
        legs = _legs(t)
        nfill, ntot = sum(1 for x in legs if x[3]), len(legs)
        star = "⭐" if t.get("sem") == "green" else ""
        name = t["name"] + star
        out.append(f"{TICON[phase]} {name} ${t['lo'] // 1000}-{t['hi'] // 1000}K  {nfill}/{ntot} ¥{k(done)}/¥{k(t['amount'])}")
        out.extend(leg_lines(t, phase))

    # Next trigger = the highest-priced open (unfilled) leg (first to be crossed as the price falls)
    pend = [(p["price"], p["rmb"], t["name"]) for t in s["tiers"] for p in subcode.open_legs(t)]
    out.append(line)
    if pend:
        p, r, nm = max(pend, key=lambda x: x[0])
        out.append(f"🎯 Next trigger ${p // 1000}K · {nm} ¥{k(r)}")
    out.append(f"Dry powder ¥{left:,.0f} ({(1 - pct) * 100:.0f}%)")
    out.append("```")
    if dash:
        out.append(f"📊 [Open live cockpit]({dash})")
    return "\n".join(out)


def main():
    s = json.loads(STATE.read_text(encoding="utf-8"))
    print(build_board(s, DASH))


if __name__ == "__main__":
    main()
