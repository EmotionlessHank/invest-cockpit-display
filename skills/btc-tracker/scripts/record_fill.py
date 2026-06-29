"""
record_fill.py — the single write entry point for the ledger (a deterministic script). Verbal dip fill / DCA / undo -> updates state.json -> prints one JSON line.

🔴 Design contract: this is the **only legal write entry point** for state.json. The agent (DeepSeek) may only call this script to write the ledger;
   it is never allowed to edit the state file by hand with cat/sed/echo/an editor, never to invent fields, never to rebuild the ledger (see the iron rules in SKILL.md).
   This script: append-only (fills are only appended, never deleted), auto-backs-up before writing (for --undo), atomic write, with input validation and invariant guards.

Usage:
  record_fill.py --price 69000 [--rmb 6000]   # record a dip fill (without --rmb, match the closest leg in the order plan)
  record_fill.py --price 61000 --label C2-a   # exact claim by sub-label (label sets the tier; the price may fall outside the static tier bounds)
  record_fill.py --dca --rmb 3000             # record a manually placed DCA order
  record_fill.py --price 64000 --usdt 2000    # amount denominated in USDT (converted by the ledger's rate_rmb_per_usdt; mutually exclusive with --rmb)
  record_fill.py --undo                        # undo the last entry (restore the pre-write backup)
  add --force: override the "duplicate report / over-budget cap / fill price deviates >50% from the trigger" guards and record anyway

state.json is shared with the display-layer app.py (path overridable via BTC_STATE). This script only writes state; it never places any order.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import shutil
import sys
from datetime import date, datetime

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))  # reuse the shared subcode (distributed into skill scripts with the app module on deploy)

import subcode  # noqa: E402

STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))
BAK = STATE.with_name(STATE.stem + ".bak.json")
LOCK = STATE.with_name(STATE.stem + ".lock")  # write-serialization lock: guards against races across the TG/cron/cockpit write entry points

REQUIRED = ("tiers", "tracks", "invested_rmb", "held_btc", "budget_total_rmb")
PRICE_MIN, PRICE_MAX = 1_000, 1_000_000   # plausible BTC price range, blocks obviously wrong values
RMB_MAX = 200_000                          # per-entry amount cap (blocks a fat-finger extra zero)
EXTRA_MAX = 1_000_000                      # per-entry cap for extra funding (source amounts can be larger, capped separately)


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def emit(d, state, no_board):
    """After a successful recording: first hand back the full updated board (unified progress management: change it and see the new progress, no need to ask again), then print one JSON line (last line, for confirmation/parsing)."""
    if not no_board:
        try:
            import show_progress
            import dash_link
            print(show_progress.build_board(state, dash_link.dash_url()))
        except Exception:
            pass   # a board-render failure must never affect the recording itself
    out(d)


def load():
    """Read state; degrade gracefully on missing/corrupted/incomplete structure (no stack trace)."""
    if not STATE.exists():
        return None, f"ledger not found: {STATE}"
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"ledger JSON corrupted ({e.msg} @line{e.lineno}), please repair manually, do not hand-edit"
    miss = [k for k in REQUIRED if k not in s]
    if miss:
        return None, f"ledger missing fields {miss}, likely corrupted, please verify manually"
    subcode.migrate_btc(s)  # self-healing migration on the load path (old schema gets label+status patched; idempotent)
    return s, None


def save(s):
    """Atomic write: write a temp file then replace, avoiding a corrupted ledger if interrupted mid-write."""
    tmp = STATE.with_name(STATE.stem + ".tmp.json")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


BACKDATE_MIN = date(2020, 1, 1)  # earliest allowed backdate (blocks a fat-finger year)


def parse_backdate(s):
    """Validate the backdate --at: a real calendar day in YYYY-MM-DD, not in the future, not before BACKDATE_MIN.
    On success returns (bare date string, None) — a backdate knows only the date not the time, so honestly store the bare date, which sorts/displays fine alongside full ISO timestamps."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None, f"backdate \"{s}\" is invalid; the format should be YYYY-MM-DD (e.g. 2026-05-20)"
    if d > datetime.now().astimezone().date():
        return None, f"backdate {s} is in the future, please verify"
    if d < BACKDATE_MIN:
        return None, f"backdate {s} is earlier than {BACKDATE_MIN.isoformat()}, please verify"
    return s, None


def acquire_lock():
    """Write-serialization lock (fcntl.flock on a separate .lock file). When the multiple write entry points (TG/cron/cockpit) run concurrently they block and queue,
    avoiding lost updates from a read-modify-write race. Returns the handle; the caller holds the reference until the write completes; released automatically on process exit."""
    lf = open(LOCK, "w")
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    return lf


def track(state, name):
    return next((t for t in state["tracks"] if t["name"] == name), None)


def claim_tier(state, price):
    for t in state["tiers"]:
        if t["lo"] <= price < t["hi"]:
            return t
    return None


def recompute(state):
    """Recompute the average cost from cumulative invested and holdings (¥ -> $/BTC)."""
    rate = state.get("rate_rmb_per_usdt", 7.0)
    if state["held_btc"] > 0:
        state["avg_cost_usd"] = round(state["invested_rmb"] / rate / state["held_btc"])


def over_budget(state, rmb):
    return state["invested_rmb"] + rmb > state["budget_total_rmb"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--price", type=float)
    ap.add_argument("--rmb", type=float, default=None)
    ap.add_argument("--label", default=None, help="exact-claim a specific leg by sub-label (e.g. B3-b); without it, match the closest open leg by price")
    ap.add_argument("--usdt", type=float, default=None,
                    help="amount denominated in USDT (mutually exclusive with --rmb; converted by the ledger's rate_rmb_per_usdt then runs the same validation chain)")
    ap.add_argument("--dca", action="store_true", help="record a placed DCA order")
    ap.add_argument("--extra-fund", dest="extra_fund", action="store_true",
                    help="record an extra-funding injection (separate pool, not counted toward the 200K hard cap)")
    ap.add_argument("--source", default=None, help="source of the extra funding (required with --extra-fund)")
    ap.add_argument("--at", default=None, help="backdate to a historical date YYYY-MM-DD (without it, record the current time; applies to fills/extra-funding)")
    ap.add_argument("--undo", action="store_true", help="undo the last entry")
    ap.add_argument("--force", action="store_true", help="override the duplicate/over-budget guards and record anyway")
    ap.add_argument("--no-board", dest="no_board", action="store_true", help="do not hand back the board after recording (for testing)")
    a = ap.parse_args()

    # Validate the backdate first (does not touch the ledger; return immediately on failure)
    at_val = None
    if a.at:
        at_val, err = parse_backdate(a.at)
        if err:
            return out({"ok": False, "msg": err})

    _lock = acquire_lock()  # write serialization: hold the lock until main returns (released automatically on process exit)

    # —— Undo (no need to load first) ——
    if a.undo:
        if not BAK.exists():
            return out({"ok": False, "msg": "no previous operation to undo"})
        shutil.copy(BAK, STATE)
        BAK.unlink()
        s, err = load()
        if err:
            return out({"ok": False, "msg": f"failed to read the ledger after undo: {err}"})
        return emit({"ok": True, "action": "undo", "msg": "undid the last entry",
                     "invested_rmb": s["invested_rmb"], "held_btc": round(s["held_btc"], 8)}, s, a.no_board)

    state, err = load()
    if err:
        return out({"ok": False, "msg": err})
    rate = state.get("rate_rmb_per_usdt", 7.0)
    if rate <= 0:
        return out({"ok": False, "msg": f"the rate_rmb_per_usdt field = {rate} is invalid, please verify the ledger"})

    # —— Single-point --usdt conversion: convert to rmb by the ledger's rate, then reuse the entire existing amount-validation chain ——
    if a.usdt is not None:
        if a.rmb is not None:
            return out({"ok": False, "msg": "--usdt and --rmb are mutually exclusive; give only one amount"})
        if a.usdt <= 0:
            return out({"ok": False, "msg": f"USDT amount {a.usdt:,.0f} is invalid (should be > 0), please verify"})
        a.rmb = round(a.usdt * rate, 2)

    # —— Extra-funding injection (separate pool: appended to extra_funding, does not touch invested/tracks/budget, not counted toward the 200K hard cap) ——
    if a.extra_fund:
        if a.rmb is None or a.rmb <= 0 or a.rmb > EXTRA_MAX:
            return out({"ok": False, "msg": f"extra-funding amount is invalid (should be 0 < amount ≤ {EXTRA_MAX:,}): --extra-fund --rmb 50000 --source year-end-bonus"})
        src = (a.source or "").strip()
        if not src:
            return out({"ok": False, "msg": "extra funding needs a source: --extra-fund --rmb 50000 --source year-end-bonus"})
        shutil.copy(STATE, BAK)
        state.setdefault("extra_funding", []).append({"amount": a.rmb, "source": src, "at": at_val or now_iso()})
        save(state)
        total_extra = sum(e["amount"] for e in state["extra_funding"])
        return emit({"ok": True, "action": "extra_fund", "rmb": a.rmb, "source": src,
                     "total_extra_rmb": total_extra,
                     "msg": f"recorded extra funding ¥{a.rmb:,.0f} (source: {src}), extra pool now ¥{total_extra:,.0f}"},
                    state, a.no_board)

    # —— Amount validation (shared by dca and an explicit --rmb) ——
    if a.rmb is not None and (a.rmb <= 0 or a.rmb > RMB_MAX):
        return out({"ok": False, "msg": f"amount ¥{a.rmb:,.0f} is invalid (should be 0 < amount ≤ {RMB_MAX:,}), please verify"})

    # —— DCA placed ——
    if a.dca:
        rmb = a.rmb or 0
        if rmb <= 0:
            return out({"ok": False, "msg": "DCA needs an amount: record_fill.py --dca --rmb 3000"})
        if over_budget(state, rmb) and not a.force:
            return out({"ok": False, "msg": f"after this entry the cumulative ¥{state['invested_rmb']+rmb:,.0f} exceeds the budget cap ¥{state['budget_total_rmb']:,.0f}; add --force to confirm"})
        shutil.copy(STATE, BAK)
        btc = rmb / rate / state["avg_cost_usd"]
        state["invested_rmb"] += rmb
        state["held_btc"] += btc
        track(state, "Total budget used")["used"] = state["invested_rmb"]
        track(state, "DCA")["used"] += rmb
        # Per-entry DCA flow: append-only; btc is an approximation converted at the average cost at recording time
        state.setdefault("dca_fills", []).append({"rmb": rmb, "btc": round(btc, 8), "at": at_val or now_iso()})
        if not at_val:
            state["dca_last_at"] = now_iso()  # "DCA'd this week" check: update only on a present-time DCA; a backdate does not light up the current week
        recompute(state)
        save(state)
        return emit({"ok": True, "action": "dca", "rmb": rmb, "btc": round(btc, 8),
                     "invested_rmb": state["invested_rmb"], "held_btc": round(state["held_btc"], 8),
                     "msg": f"recorded DCA ¥{rmb:,.0f}" + (f" (backdated {at_val})" if at_val else "")}, state, a.no_board)

    # —— Dip fill ——
    if a.price is None:
        return out({"ok": False, "msg": "a fill price is needed: record_fill.py --price 69000"})
    if not (PRICE_MIN <= a.price <= PRICE_MAX):
        return out({"ok": False, "msg": f"fill price ${a.price:,.0f} is invalid, please verify"})
    # —— Set the tier + claim the leg: under the new schema legs are not deleted; a fill sets that leg's status -> filled ——
    # If --label is given -> set the tier by the label's owning tier (**replacing** price-based tiering: dynamic trigger prices in B3/C1/C2
    # can drift outside the static tier bounds, and price-based tiering would then misplace the fill / mis-bucket the track / flip the leg status in the wrong tier).
    # All subsequent same-price dedupe / track attribution / status derivation follow the label's owning tier.
    # Without --label (the TG verbal-report legacy path) -> tier by price + closest-open-leg match, behavior unchanged.
    leg = None
    if a.label:
        tier = next((t for t in state["tiers"] if subcode.find_leg_by_label(t, a.label)), None)
        if tier is None:
            opens = [p.get("label") for t in state["tiers"] for p in subcode.open_legs(t)]
            return out({"ok": False, "msg": f"no sub-label \"{a.label}\"; currently resting legs: {opens or '(none)'}"})
        leg = subcode.find_leg_by_label(tier, a.label)
        if leg.get("status") == "filled" and not a.force:
            return out({"ok": False, "msg": f"{a.label} already filled (filled), duplicate report? add --force to confirm"})
        # Price-deviation soft check: once the label path drops the tier-bound check, block a fat-finger absurd price from silently entering and polluting the average cost;
        # a limit fill price ≈ the trigger price is the norm, so a 50% threshold is unobtrusive.
        ref = leg.get("price")
        if ref and abs(a.price - ref) / ref > 0.5 and not a.force:
            return out({"ok": False, "msg": f"fill price ${a.price:,.0f} deviates from {a.label}'s trigger ${ref:,.0f} by over 50%, please verify; add --force to confirm"})
    else:
        tier = claim_tier(state, a.price)
        if not tier:
            return out({"ok": False, "msg": f"${a.price:,.0f} is not in any dip-tier range (B1 66-70K / B2 60-66K / B3 55-60K / C1 52-55K / C2 48-52K)"})
        opens = subcode.open_legs(tier)
        if opens:
            leg = min(opens, key=lambda p: abs(p["price"] - a.price))
        # No open leg: either the tier is full, or it's a top-up fill (with an amount it goes through the fallback)

    # Duplicate-report guard: this price was already recorded once and no --force (keeps the original "same-price dedupe" semantics; tolerance symmetric with the MSTR side, for future decimal prices).
    # On the label path, a same-price-different-leg in the same tier (reachable when deep-dip trigger prices are close) -> not falsely called a "duplicate",
    # gives a label-aware notice but still requires --force to confirm (a conservative guard on real money).
    dup = next((f for f in tier["fills"] if abs(f["price"] - a.price) < 1e-6), None)
    if dup and not a.force:
        if a.label and dup.get("label") and dup["label"] != a.label:
            return out({"ok": False, "msg": f"${a.price:,.0f} ({tier['name']}) was already recorded once by {dup['label']}; if this {a.label} is genuinely another leg filling at the same price, add --force to confirm"})
        return out({"ok": False, "msg": f"${a.price:,.0f} ({tier['name']}) already has a recorded fill; duplicate report? add --force to confirm"})

    # Amount: prefer --rmb, otherwise take the claimed leg's planned amount
    fill_label = leg.get("label") if leg else tier["name"].split(" ")[0]
    if a.rmb is not None:
        rmb = a.rmb
    elif leg is not None:
        rmb = leg["rmb"]
    else:
        # No open leg to claim and no amount given -> tier-full notice
        return out({"ok": False, "msg": f"{tier['name']} has all legs filled; if there really is a new fill, include the amount: --price {a.price:.0f} --rmb amount"})

    # Over-budget guard: not yet saved here, an early return means nothing is persisted and the ledger is unchanged
    if over_budget(state, rmb) and not a.force:
        return out({"ok": False, "msg": f"after this entry the cumulative ¥{state['invested_rmb']+rmb:,.0f} exceeds the budget cap ¥{state['budget_total_rmb']:,.0f}; add --force to confirm"})

    shutil.copy(STATE, BAK)  # back up + write only after passing every guard
    btc = rmb / rate / a.price
    if leg is not None:
        leg["status"] = "filled"  # a fill does not delete the leg: set that leg's state machine to filled
    tier["fills"].append({
        "label": fill_label, "price": a.price, "rmb": rmb,
        "usd": round(rmb / rate, 2), "btc": round(btc, 8), "at": at_val or now_iso(),
    })
    tier["status"] = subcode.derive_tier_status(tier)  # tier status derived from the count of filled legs
    state["invested_rmb"] += rmb
    state["held_btc"] += btc
    track(state, "Total budget used")["used"] = state["invested_rmb"]
    is_c = tier["name"].startswith("C")
    track(state, "C deep-dip reserve" if is_c else "B core dip-buy")["used"] += rmb
    recompute(state)
    save(state)

    nxt = next((t for t in state["tiers"] if t["status"] in ("open", "partial", "wait")), None)
    emit({
        "ok": True, "action": "fill",
        "claimed": tier["name"], "label": fill_label, "price": a.price, "rmb": rmb, "btc": round(btc, 8),
        "tier_status": tier["status"],
        "invested_rmb": state["invested_rmb"], "held_btc": round(state["held_btc"], 8),
        "avg_cost_usd": state["avg_cost_usd"],
        "budget_used_pct": round(state["invested_rmb"] / state["budget_total_rmb"] * 100, 1),
        "linkage": "a dip filled recently -> this week's DCA auto-downgrades one tier (the savings go to deeper dip tiers)",
        "next_tier": (f"{nxt['name']} ${nxt['lo']//1000}–{nxt['hi']//1000}K" if nxt else "all dip tiers fully filled"),
        "msg": f"recorded {tier['name']} fill ${a.price:,.0f} / ¥{rmb:,.0f}",
    }, state, a.no_board)


if __name__ == "__main__":
    main()
