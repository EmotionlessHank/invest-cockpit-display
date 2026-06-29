"""
dca_advice_mstr.py — compute "this week's MSTR core-pool window-paced DCA suggestion" (standalone core add-on model). **Read-only; never writes the ledger, never places orders.**

Purpose (Monday cron / when the user says they want to DCA):
  1. First run this script to get this week's suggested amount (week_amount_usd);
  2. After the user buys, use record_fill_mstr.py --standalone to record it as a **core-pool DCA add-on**
     (it does NOT claim or fill any limit dip leg).

Accounting model:
  A market DCA must NOT be recorded into the next limit dip leg (e.g. the T1-c limit leg): doing so would
  claim and fill that leg, erase the limit intent, and mark the batch done while only a small amount was actually
  invested. Instead it is recorded as a standalone core add-on (mirroring the BTC DCA flow). The mNAV limit legs
  (T2-a/b/c) are kept purely as a "deep-discount overlay" reference; they are shown below for context but are NOT
  a recording instruction (record_mode stays "standalone").

Pool definitions:
  · Core pool = the two core rounds (layer == "core"; budgets sum to $14,000, summed from the rounds, never hardcoded);
  · core_invested = the single-source core-pool figure (core-pool fills + standalone add-ons, **never including Swan**);
  · core_budget = the sum of the T1 + T2 budgets; the Swan rounds A/B do not participate in DCA and are excluded
    from the core pool (layer == "swan" is excluded).

Basis: strategy_mstr.decide_weekly_mstr (remaining pool ÷ weeks left in window × mNAV tilt; forced fill-up at window end).
  mNAV reuses the existing computation (mnav.compute(), the same source as the web app and show_progress_mstr.py);
  on a fetch failure it does NOT fabricate a value and instead emits a degraded notice (honoring the "never invent
  a price/mNAV" rule).

Usage:
  dca_advice_mstr.py                 # use live mNAV to compute this week's suggestion (text + JSON last line)
  dca_advice_mstr.py --mnav 0.81     # override mNAV (offline / for tests)

The state path can be overridden via MSTR_STATE (deployment points it at the runtime ledger).
strategy.py / strategy_mstr.py / mnav.py are resolved either from the same directory or via PYTHONPATH (the deploy
scripts distribute them alongside this script).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
STATE = pathlib.Path(os.environ.get("MSTR_STATE", str(HERE / "mstr-state.seed.json")))
sys.path.insert(0, str(HERE))  # reuse the co-located strategy*/mnav modules (distributed into the skill scripts at deploy time)

import mnav  # noqa: E402
import strategy  # noqa: E402
import strategy_mstr  # noqa: E402

# The overlay ordering + leg-selection logic has a single source of truth in strategy_mstr
# (no local hardcoded copy is kept).
OVERLAY_LEG_ORDER = strategy_mstr.OVERLAY_LEG_ORDER


def get_mnav():
    """Live mNAV (reuses mnav.compute, same source as the web app / board); returns None on a fetch failure (no fabrication)."""
    return mnav.compute().get("mnav")


def core_budget(st):
    """Core-pool budget = the sum of budget_usd over rounds with layer == 'core' (= $14,000, never hardcoded)."""
    return sum(r.get("budget_usd", 0) for r in st.get("rounds", []) if r.get("layer") == "core")


def next_open_leg(st):
    """Delegates to the single source strategy_mstr.next_open_core_leg (no local copy of the selection logic/order).
    In this model it is shown only as a "deep-discount overlay" reference; the DCA is NOT recorded into it
    (the DCA goes through a standalone core add-on)."""
    return strategy_mstr.next_open_core_leg(st)


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def advise(st, mnav_val, now=None, window_end=None):
    """Pure function: ledger + mNAV → (human-readable text, result dict). mnav_val=None means a fetch failure (no fabrication).
    now/window_end can be injected (so tests do not depend on the real date); window_end defaults to strategy._golden_window_end()."""
    # —— Fetch failure: do not fabricate a value, emit a degraded notice (honoring the "never invent" rule) ——
    if mnav_val is None:
        return ("⚠️ Weekly MSTR DCA reminder: live mNAV fetch failed, the discount tier cannot be determined. "
                "Open the dashboard to check the current mNAV, then DCA manually.",
                {"ok": False, "msg": "mNAV fetch failed", "week_amount_usd": None})

    if window_end is None:
        window_end = strategy._golden_window_end()
    # core_invested single source: core-pool fills + standalone add-ons, never including Swan.
    invested = strategy_mstr.core_invested_usd(st)
    budget = core_budget(st)
    d = strategy_mstr.decide_weekly_mstr(invested, budget, mnav_val, now=now, window_end=window_end)

    # mNAV abnormal (the value reaching this script is out of range / dirty) → decide_weekly_mstr already DEGRADED; relay honestly, do not invent an amount
    if d.action == "DEGRADED":
        return ("⚠️ Weekly MSTR DCA reminder: mNAV looks abnormal (outside the sane range); refusing to compute, "
                "please have a human verify the mNAV data source.",
                {"ok": False, "msg": "mNAV abnormal, refused to compute", "week_amount_usd": None, "mnav": mnav_val})

    # Window has passed → REVIEW: do not auto fill-up; the remaining pool awaits a human review
    if d.action == "REVIEW":
        return (f"📅 MSTR core-pool DCA: the golden window has ended; ${d.pool_usd:,.0f} of the core pool remains "
                f"for you to review (no auto fill-up).",
                {"ok": True, "action": "REVIEW", "week_amount_usd": 0, "record_mode": "standalone",
                 "pool_usd": d.pool_usd, "weeks": d.weeks, "mnav": mnav_val,
                 "core_invested_usd": round(invested, 2), "core_budget_usd": round(budget, 2),
                 "reasons": d.reasons})

    overlay_leg = next_open_leg(st)   # the currently still-open limit leg (overlay reference, not a recording instruction)
    if d.action == "BUY":
        head = (f"📅 Weekly MSTR core-pool DCA suggestion: ${d.amount_usd:,.0f}"
                f" (window pacing · mNAV {mnav_val:.2f}x \"{strategy_mstr.mstr_tilt_label(mnav_val)}\" tier ×{d.tilt})")
    else:
        head = f"📅 Weekly MSTR core-pool DCA: ${d.pool_usd:,.0f} of the core pool remains (already filled up, no add this week)"
    lines = [head,
             f"Basis: core pool remaining ${d.pool_usd:,.0f} ÷ {d.weeks} weeks left in window, mNAV tilt, forced fill-up at window end"]
    if d.action == "BUY":
        lines.append(
            f"👉 This week's ${d.amount_usd:,.0f} is recorded as a core-pool DCA standalone add-on"
            f" (record_fill_mstr --standalone; does not claim or fill any limit dip leg)"
        )
        if overlay_leg:
            lines.append(f"(reference: the currently still-open mNAV limit leg \"{overlay_leg}\" serves as a deep-discount overlay; trigger handled separately, not counted in this week's DCA)")

    return ("\n".join(lines), {
        "ok": True,
        "week_amount_usd": d.amount_usd,
        "record_mode": "standalone",       # standalone core-pool DCA add-on; does not claim a dip leg (replaces the old next_batch semantics)
        "overlay_leg": overlay_leg,        # the currently still-open limit leg (overlay reference, not a recording instruction)
        "weeks": d.weeks,
        "mnav": mnav_val,
        "tilt": d.tilt,
        "base_usd": d.base_usd,
        "pool_usd": d.pool_usd,
        "core_invested_usd": round(invested, 2),
        "core_budget_usd": round(budget, 2),
        "action": d.action,
        "reasons": d.reasons,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mnav", type=float, default=None, help="override live mNAV (offline / for tests)")
    a = ap.parse_args()

    if not STATE.exists():
        return out({"ok": False, "msg": f"ledger not found: {STATE}"})
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return out({"ok": False, "msg": f"ledger JSON is corrupt ({e.msg} @line{e.lineno}), please check manually"})

    mnav_val = a.mnav if a.mnav is not None else get_mnav()
    text, res = advise(st, mnav_val)
    print(text)
    out(res)


if __name__ == "__main__":
    main()
