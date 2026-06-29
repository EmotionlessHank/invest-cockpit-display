"""
record_fill_mstr.py — the ONLY legitimate write entry point for the MSTR ledger (deterministic script). Spoken fill report → update mstr-state.json → print one line of JSON.

🔴 Contract: the sole write entry point for mstr-state.json. The agent may only call this script to write the ledger; never edit it by hand, invent fields, or rebuild it (see SKILL.md, the hard rules).
   Append-only (fills are only appended), backup-before-write (--undo), atomic write, input validation, budget-cap guard, duplicate-report guard.

MSTR is denominated in shares and US dollars; rounds are T1/T2 (core pool) + A/B (Swan), each split into batches.
Usage:
  record_fill_mstr.py --round T2 --price 91 --shares 30        # record a fill (most common: round + price + shares)
  record_fill_mstr.py --round T1 --price 130                   # without shares, estimate from the planned batch of that round
  record_fill_mstr.py --price 91                               # without a round, match to the nearest round by price (on ambiguity, SKILL.md requires asking for the round first)
  record_fill_mstr.py --undo                                   # undo the last entry
  add --force: bypass the "duplicate report / over budget" guards

The state path can be overridden via MSTR_STATE (deployment points it at the runtime ledger). Writes the ledger only; never places any order.
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
sys.path.insert(0, str(HERE))  # reuse the shared `subcode` module (distributed alongside the app modules into the skill scripts at deploy time)

import subcode  # noqa: E402

STATE = pathlib.Path(os.environ.get("MSTR_STATE", str(HERE / "mstr-state.seed.json")))
BAK = STATE.with_name(STATE.stem + ".bak.json")
LOCK = STATE.with_name(STATE.stem + ".lock")  # write-serialization lock: guards against races across multiple write entry points (Telegram/cron/dashboard), aligned with the BTC tracker

REQUIRED = ("rounds", "invested_usd", "held_shares", "budget_core_usd", "budget_swan_usd")
PRICE_MIN, PRICE_MAX = 10, 2000      # sane MSTR share-price range
SHARES_MAX = 1000                    # per-fill share cap (fat-finger guard)


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def emit(d, state, no_board):
    """On a successful write: first echo the full updated board (unified progress management: see the new progress as soon as it changes), then print one line of JSON (the last line)."""
    if not no_board:
        try:
            import show_progress_mstr
            import dash_link
            print(show_progress_mstr.build_board(state, dash_link.dash_url(mstr=True)))
        except Exception:
            pass
    out(d)


def load():
    if not STATE.exists():
        return None, f"ledger not found: {STATE}"
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"ledger JSON is corrupt ({e.msg} @line{e.lineno}); please repair manually, do not hand-edit"
    miss = [k for k in REQUIRED if k not in s]
    if miss:
        return None, f"ledger is missing fields {miss}; it may be corrupted"
    subcode.migrate_mstr(s)  # self-healing migration on the load path (fills in status for the old schema; idempotent)
    return s, None


def save(s):
    tmp = STATE.with_name(STATE.stem + ".tmp.json")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


BACKDATE_MIN = date(2020, 1, 1)  # earliest allowed backdating date (fat-finger year guard, aligned with the BTC tracker)


def parse_backdate(s):
    """Validate the backdating date --at: a real calendar day in YYYY-MM-DD, not in the future, not earlier than BACKDATE_MIN.
    On success returns (bare date string, None) — a backdated entry only knows the date, not the time, so we honestly store the bare date, which sorts/renders fine when mixed with full ISO timestamps."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None, f"backdating date \"{s}\" is invalid; the format must be YYYY-MM-DD (e.g. 2026-05-20)"
    if d > datetime.now().astimezone().date():
        return None, f"backdating date {s} is in the future, please check"
    if d < BACKDATE_MIN:
        return None, f"backdating date {s} is earlier than {BACKDATE_MIN.isoformat()}, please check"
    return s, None


def acquire_lock():
    """Write-serialization lock (fcntl.flock on a separate .lock file). When multiple write entry points (Telegram/cron/dashboard) run concurrently they block and queue,
    avoiding lost updates from a "read-modify-write" race. Returns the handle; the caller keeps a reference until the write completes; it is released automatically on process exit."""
    lf = open(LOCK, "w")
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    return lf


def total_budget(s):
    return s["budget_core_usd"] + s["budget_swan_usd"]


def claim_round(state, round_id, price):
    if round_id:
        return next((r for r in state["rounds"] if r["id"].upper() == round_id.upper().replace("SWAN-", "")), None)
    # Nearest-round matching: only consider rounds that still have open legs (under the new schema the plan is not cleared; status distinguishes them)
    cands = [r for r in state["rounds"] if subcode.open_legs(r)]
    if price and cands:   # on ambiguity, SKILL.md requires asking for the round first
        return min(cands, key=lambda r: min(abs(b["price"] - price) for b in subcode.open_legs(r)))
    return None


def recompute(state):
    if state["held_shares"] > 0:
        state["avg_cost_usd"] = round(state["invested_usd"] / state["held_shares"], 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", dest="rnd", default=None, help="round T1/T2/A/B")
    ap.add_argument("--price", type=float, default=None, help="fill price USD")
    ap.add_argument("--shares", type=float, default=None, help="fill shares (if omitted, estimated from the planned batch)")
    ap.add_argument("--label", default=None, help="claim a specific leg by its sub-code (e.g. T2-b); if omitted, match an open leg by nearest price")
    ap.add_argument("--standalone", action="store_true",
                    help="a standalone buy not attached to any round (needs --price --shares; lands in the top-level standalone_fills, counted into holdings/invested/avg)")
    ap.add_argument("--at", default=None, help="backdate a historical entry YYYY-MM-DD (if omitted, records the current time)")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-board", dest="no_board", action="store_true", help="do not echo the board after writing (for tests)")
    a = ap.parse_args()

    # Validate the backdating date first (does not touch the ledger; returns on failure)
    at_val = None
    if a.at:
        at_val, err = parse_backdate(a.at)
        if err:
            return out({"ok": False, "msg": err})

    _lock = acquire_lock()  # write serialization: hold the lock until main returns (released automatically on process exit)

    if a.undo:
        if not BAK.exists():
            return out({"ok": False, "msg": "no previous operation to undo"})
        shutil.copy(BAK, STATE)
        BAK.unlink()
        s, err = load()
        if err:
            return out({"ok": False, "msg": f"failed to read the ledger after undo: {err}"})
        return emit({"ok": True, "action": "undo", "msg": "undid the last entry",
                     "invested_usd": s["invested_usd"], "held_shares": round(s["held_shares"], 4)}, s, a.no_board)

    state, err = load()
    if err:
        return out({"ok": False, "msg": err})

    if a.price is None:
        return out({"ok": False, "msg": "a fill price is required: record_fill_mstr.py --round T2 --price 91 --shares 30"})
    if not (PRICE_MIN <= a.price <= PRICE_MAX):
        return out({"ok": False, "msg": f"fill price ${a.price:,.2f} is unreasonable, please check"})
    if a.shares is not None and (a.shares <= 0 or a.shares > SHARES_MAX):
        return out({"ok": False, "msg": f"shares {a.shares} is unreasonable (should be 0 < shares ≤ {SHARES_MAX})"})

    # —— Standalone buy (not attached to any round: append to top-level standalone_fills, do not touch any round's plan/fills; real shares counted into holdings/invested/avg) ——
    if a.standalone:
        if a.shares is None:
            return out({"ok": False, "msg": "a standalone buy needs shares: --standalone --price 130 --shares 5"})
        usd = round(a.shares * a.price, 2)
        if any(abs(f["price"] - a.price) < 1e-6 for f in state.get("standalone_fills", [])) and not a.force:
            return out({"ok": False, "msg": f"a standalone buy at ${a.price:,.2f} is already recorded; duplicate report? add --force to confirm"})
        if state["invested_usd"] + usd > total_budget(state) and not a.force:
            return out({"ok": False, "msg": f"after this fill the total ${state['invested_usd']+usd:,.0f} exceeds the MSTR cap ${total_budget(state):,.0f}; add --force to confirm"})
        shutil.copy(STATE, BAK)  # back up + write only after passing the guards
        state.setdefault("standalone_fills", []).append({"label": "standalone", "price": a.price,
                                                         "shares": a.shares, "usd": usd, "at": at_val or now_iso()})
        state["invested_usd"] = round(state["invested_usd"] + usd, 2)
        state["held_shares"] = round(state["held_shares"] + a.shares, 4)
        recompute(state)
        save(state)
        return emit({"ok": True, "action": "standalone_fill", "price": a.price, "shares": a.shares, "usd": usd,
                     "invested_usd": state["invested_usd"], "held_shares": state["held_shares"],
                     "avg_cost_usd": state["avg_cost_usd"],
                     "msg": f"recorded a standalone buy ${a.price:,.2f} × {a.shares:g} sh = ${usd:,.0f} (no round attached)"},
                    state, a.no_board)

    rnd = claim_round(state, a.rnd, a.price)
    if not rnd:
        return out({"ok": False, "msg": "could not identify the round. Please specify T1/T2/A/B, e.g. \"T2 filled 91, bought 30 shares\""})

    # —— Claim a leg (new schema does not delete legs; a fill sets that leg's status → filled) ——
    # Prefer an exact --label claim (whitelist: a label that really exists in that round); otherwise match the nearest **open leg only** by price.
    batch = None
    if a.label:
        batch = subcode.find_leg_by_label(rnd, a.label)
        if batch is None:
            opens = [b.get("label") for b in subcode.open_legs(rnd)]
            return out({"ok": False, "msg": f"{rnd['name']} has no sub-code \"{a.label}\"; currently resting legs: {opens or '(none)'}"})
        if batch.get("status") == "filled" and not a.force:
            return out({"ok": False, "msg": f"{a.label} is already filled; duplicate report? add --force to confirm"})
    else:
        opens = subcode.open_legs(rnd)
        if opens:
            batch = min(opens, key=lambda b: abs(b["price"] - a.price))

    # Duplicate guard: this round already has a fill at the same price
    if any(abs(f["price"] - a.price) < 1e-6 for f in rnd["fills"]) and not a.force:
        return out({"ok": False, "msg": f"{rnd['name']} already has a fill recorded at ${a.price:,.2f}; duplicate report? add --force to confirm"})

    # Shares/amount: prefer what the user gave, otherwise estimate from the claimed batch
    if a.shares is not None:
        shares = a.shares
        usd = round(shares * a.price, 2)
    elif batch:
        shares = batch["shares"]
        usd = batch["usd"]
    else:
        return out({"ok": False, "msg": f"{rnd['name']} has all legs filled; please include shares: --round {rnd['id']} --price {a.price:.0f} --shares N"})

    if state["invested_usd"] + usd > total_budget(state) and not a.force:
        return out({"ok": False, "msg": f"after this fill the total ${state['invested_usd']+usd:,.0f} exceeds the MSTR cap ${total_budget(state):,.0f}; add --force to confirm"})

    shutil.copy(STATE, BAK)  # back up + write only after passing the guards
    if batch is not None:
        batch["status"] = "filled"  # a fill does not delete the leg: its state machine is set to filled
    rnd["fills"].append({"label": batch["label"] if batch else "add-on", "price": a.price,
                         "shares": shares, "usd": usd, "at": at_val or now_iso()})
    rnd["status"] = subcode.derive_tier_status(rnd)  # round status is derived from the count of filled legs
    state["invested_usd"] = round(state["invested_usd"] + usd, 2)
    state["held_shares"] = round(state["held_shares"] + shares, 4)
    recompute(state)
    save(state)

    nxt = next((r for r in state["rounds"] if r["status"] in ("open", "partial", "wait")), None)
    core_used = sum(f["usd"] for r in state["rounds"] if r["layer"] == "core" for f in r["fills"])
    emit({
        "ok": True, "action": "fill",
        "round": rnd["name"], "layer": rnd["layer"], "batch": batch["label"] if batch else "add-on",
        "price": a.price, "shares": shares, "usd": usd, "round_status": rnd["status"],
        "invested_usd": state["invested_usd"], "held_shares": state["held_shares"], "avg_cost_usd": state["avg_cost_usd"],
        "core_used_usd": round(core_used, 2), "core_budget_usd": state["budget_core_usd"],
        "next_round": (f"{nxt['name']} ({nxt['layer']})" if nxt else "all filled up"),
        "msg": f"recorded {rnd['name']} {batch['label'] if batch else ''} fill ${a.price:,.2f} × {shares:g} sh = ${usd:,.0f}",
    }, state, a.no_board)


if __name__ == "__main__":
    main()
