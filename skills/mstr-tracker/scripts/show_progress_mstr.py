"""
show_progress_mstr.py — MSTR buy-the-dip progress board (Telegram code block) + dashboard link.
Two-layer grouping (Core / Swan); every round is expanded into sub-batches (✅ filled / ○ resting order / · Swan pending), with live mNAV.
For mNAV-triggered batches the resting price is derived from the live NAV-per-share (≈ prefix, same basis as the dashboard's live price); if it cannot be derived it falls back to the static reference price.
The state path can be overridden via MSTR_STATE; mNAV is fetched live (mnav.py, with a safe fallback that never fabricates a value); the dashboard link is injected via DASH_URL.
"""

import json
import os
import pathlib
import sys

import mnav
import dash_link

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))  # reuse the shared `subcode` module (distributed alongside the app modules at deploy time)

import subcode  # noqa: E402

STATE = pathlib.Path(os.environ.get("MSTR_STATE", str(HERE / "mstr-state.seed.json")))
DASH = os.environ.get("DASH_URL") or dash_link.dash_url(mstr=True)   # freshly signed link (carries type=mstr + a time-limited code)
W = 20

TICON = {"done": "✅", "partial": "◑", "open": "○", "wait": "·"}

# Display-only English labels for the layer keys. The keys themselves stay as the
# domain enum values used for comparisons elsewhere; this map only affects rendering.
LAYER_LABEL = {"core": "Core", "swan": "Swan"}


def layer_label(layer):
    return LAYER_LABEL.get(layer, layer)


def uk(n):
    """Short USD with thousands suffix: 0→$0, 6300→$6.3K, 14000→$14K."""
    return "$0" if not n else f"${round(n/1000,1):g}K"


def mnav_zone(m):
    if m is None:
        return "mNAV fetch failed"
    if m > 1.0:
        return f"mNAV {m:.2f}x · premium"
    if m >= 0.78:
        return f"mNAV {m:.2f}x · T1 zone"
    if m >= 0.65:
        return f"mNAV {m:.2f}x · T2 zone"
    if m >= 0.50:
        return f"mNAV {m:.2f}x · Swan-A zone"
    return f"mNAV {m:.2f}x · Swan-B zone"


def leg_price(b, nps):
    """Display price for a resting batch: if mNAV-triggered and a live NAV-per-share is available, derive it live (same basis as the dashboard's live price); otherwise fall back to the static reference price."""
    if b.get("mnav") and nps:
        return round(b["mnav"] * nps)
    return b["price"]


def round_phase(r):
    """Derive the round status (new schema: plan legs are never deleted; status is derived)."""
    return subcode.derive_tier_status(r)


def build_board(s, dash=""):
    """Build the full MSTR text board string (including the ``` code block + link). Shared by main() and by record_fill_mstr's post-write echo."""
    subcode.migrate_mstr(s)  # self-healing migration before display (old schema gets status filled in memory; idempotent, not written back to disk)
    total_budget = s["budget_core_usd"] + s["budget_swan_usd"]
    pct = s["invested_usd"] / total_budget if total_budget else 0
    m = mnav.compute()
    nps = m.get("nav_per_share")
    line = "━" * W

    avg = f"${s['avg_cost_usd']:,.2f}" if s["held_shares"] else "—"
    spot = f"Price ${m['mstr']:,.2f} · {mnav_zone(m['mnav'])}" if m["mstr"] else f"Price see dashboard · {mnav_zone(m['mnav'])}"

    out = ["```", "MSTR Buy-the-Dip Progress", line,
           f"Holdings {s['held_shares']:g} sh · Avg {avg}",
           f"Invested ${s['invested_usd']:,.0f} / ${total_budget:,.0f} ({pct*100:.0f}%)",
           spot, line]

    layer = None
    for r in s["rounds"]:
        if r["layer"] != layer:
            layer = r["layer"]
            cap = s["budget_core_usd"] if layer == "core" else s["budget_swan_usd"]
            out.append(f"[{layer_label(layer)} ${round(cap/1000)}K]")
        ph = round_phase(r)
        done = sum(f["usd"] for f in r["fills"])
        plan_labels = {b.get("label") for b in r.get("plan", [])}
        # New schema: per-leg = plan legs (carrying status; filled shows the real fill price/shares, open shows live/static price) + orphan fills
        filled_by_label = {f.get("label"): f for f in r["fills"]}
        legs = []
        for b in r.get("plan", []):
            done_leg = b.get("status") == "filled"
            f = filled_by_label.get(b.get("label"))
            if done_leg and f:   # filled leg shows the real fill price/shares
                legs.append((f.get("price"), f.get("shares"), b["label"], "✅", ""))
            else:                # resting leg shows the order price (mNAV batches derived live + ≈)
                legs.append((leg_price(b, nps), b["shares"], b["label"],
                             "·" if ph == "wait" else "○", "≈" if b.get("mnav") and nps else ""))
        for f in r["fills"]:     # orphan fills (add-ons / history, with no persistent plan leg)
            if f.get("label") not in plan_labels:
                legs.append((f.get("price"), f.get("shares"), f.get("label"), "✅", ""))
        nf, nt = sum(1 for x in legs if x[3] == "✅"), len(legs)
        star = "⭐" if r.get("sem") == "green" else ""
        out.append(f"{TICON[ph]} {r['id']} {r['name'].split(' ',1)[-1]}{star} {nf}/{nt} {uk(done)}/{uk(r['budget_usd'])}")
        legs.sort(key=lambda x: -(x[0] or 0))
        for price, sh, lab, ic, approx in legs:
            out.append(f"   {ic} {lab} {approx}${price:g} ~{sh:g}sh")

    nxt = next((r for r in s["rounds"] if subcode.open_legs(r)), None)
    out.append(line)
    if nxt:
        out.append(f"🎯 Next {nxt['id']} {nxt['name'].split(' ',1)[-1]} ({layer_label(nxt['layer'])})")
    out.append(f"Untouched ${total_budget - s['invested_usd']:,.0f} ({(1-pct)*100:.0f}%)")
    out.append("```")
    if dash:
        out.append(f"📊 [Open live dashboard]({dash})")
    return "\n".join(out)


def main():
    s = json.loads(STATE.read_text(encoding="utf-8"))
    print(build_board(s, DASH))


if __name__ == "__main__":
    main()
