"""
frozen_cron.py — Trigger price freeze/drift alert cron runner (reuses the notification channel).

Positioning (config != ledger — three-layer isolation): This script is the "drift detection"
runtime shell. It only reads the ledger/cache, only writes the independent config snapshot
frozen_tiers.json, and never writes state.json, never places orders, never calls record_fill:
  · Reads onchain_cache.json (onchain_signals.load_cache, derived cache, not the ledger)
  · Reads state.json (ledger, read-only — never writes back, path via BTC_STATE/MSTR_STATE)
  · Reads mnav (MSTR real-time NAV per share, for mNAV-based position entry prices)
  · Reads/writes frozen_tiers.json (independent config snapshot, sole writer = this script,
    path via FROZEN_TIERS env var)

Mechanism (E2/E3/E5 final + three-tier guard):
  Each cycle: use strategy.tier_anchors to recompute all open-leg trigger prices live, then
  compare against frozen_tiers.json:
    drift < threshold → update drift field only;
    drift ≥ threshold (E2: BTC max(2%,$1000) / MSTR 3%) → advance frozen price + alert;
    Monday baseline reset (E3) → recompute baseline even if under threshold.
    Same-cycle multi-leg messages are merged into one Telegram message (E5).
  Legs whose price is unavailable (fetch failure / anchor suspect / out-of-range / fail-closed)
  are excluded before passing to detect_drift (preserves old frozen, no drift, no false alert).

Notification channel: prints message text to stdout (= the agent gateway → Telegram).
  The script holds no token and does not call the Telegram API directly.

Usage:
  frozen_cron.py --check          # production run: detect drift → print to stdout (=TG) + write frozen_tiers.json
  frozen_cron.py --dry            # dry run: print decisions only, do not write frozen
  frozen_cron.py --check --weekly # force weekly baseline reset (E3; call on Monday alongside DCA/onchain cron)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import strategy as S  # noqa: E402
import onchain_signals  # noqa: E402
import tier_alert  # noqa: E402
import frozen_tiers as FT  # noqa: E402
import subcode  # noqa: E402

BTC_STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))
MSTR_STATE = pathlib.Path(os.environ.get("MSTR_STATE", str(HERE / "mstr-state.seed.json")))

# Dynamic dip tiers (aligned with tier_alert.DIP_CODES): B1/B2 are already-filled, not anchored;
# B3/C1/C2 are dynamically anchored.
DYN_CODES = ("B3", "C1", "C2")


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def _load_json(path: pathlib.Path) -> dict | None:
    """Read JSON (read-only — never writes back). Returns None on missing/corrupt file."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def btc_live_legs(state: dict, cache: dict, green: float | None) -> dict:
    """BTC: use tier_anchors to recompute live trigger prices for B3/C1/C2 open legs
    ({label: {price, anchor, anchor_val}}). Fail-closed tiers (triggers=None) are skipped
    entirely — excluded from live_legs so detect_drift preserves their frozen value unchanged."""
    anchors = S.tier_anchors(cache or {}, manual_green=green)
    triggers_by_code = anchors.get("dip_triggers") or {}
    anchor_src = {"B3": ("green_line", green),
                  "C1": ("wma200", ((cache or {}).get("brk") or {}).get("data", {}).get("wma200")),
                  "C2": ("realized_price", ((cache or {}).get("brk") or {}).get("data", {}).get("realized_price"))}
    legs: dict = {}
    by_code = {str(t.get("name", "")).split(" ")[0].strip(): t for t in state.get("tiers", [])}
    for code in DYN_CODES:
        trig = triggers_by_code.get(code)
        if not trig:
            continue  # Fail-closed: exclude from live_legs (preserves old frozen, no false alert)
        tier = by_code.get(code, {})
        opens = subcode.open_legs(tier)  # Open legs only (price high → low, matching trig order)
        a_key, a_val = anchor_src.get(code, (None, None))
        for i, leg in enumerate(opens):
            if i >= len(trig):
                break
            lab = leg.get("label")
            if not lab:
                continue
            legs[lab] = {"price": float(trig[i]), "anchor": a_key, "anchor_val": a_val}
    return legs


def mstr_live_legs(state: dict, nps: float | None) -> dict:
    """MSTR: mNAV-based open-leg entry prices = round(mnav × real-time NAV per share)
    (same calculation as the live_price shown in the dashboard). Fixed-price legs use the
    static price. nps unavailable → skip mNAV-based legs (preserves old frozen, no false alert)."""
    legs: dict = {}
    for rnd in state.get("rounds", []):
        for leg in subcode.open_legs(rnd):
            lab = leg.get("label")
            if not lab:
                continue
            if leg.get("mnav"):
                if not nps:
                    continue  # Real-time NAV unavailable → do not drift this mNAV leg
                price = round(leg["mnav"] * nps)
                legs[lab] = {"price": float(price), "anchor": "mnav", "anchor_val": leg["mnav"]}
            else:
                legs[lab] = {"price": float(leg.get("price")), "anchor": "price", "anchor_val": leg.get("price")}
    return legs


def _filled_labels(state: dict, group_key: str) -> set:
    """Collect labels of all filled (status=filled) legs in the current state snapshot.
    Passed to detect_drift so it can distinguish 'lost-anchor' from 'filled and remove'.
    group_key: "tiers" for BTC / "rounds" for MSTR."""
    labels: set = set()
    for grp in state.get(group_key, []) or []:
        for leg in grp.get("plan", []) or []:
            if leg.get("status") == "filled" and leg.get("label"):
                labels.add(leg["label"])
    return labels


def _emit_changes(asset: str, changes: list) -> None:
    """E5: merge same-round legs into one message per round, print to stdout (= Telegram channel)."""
    by_round: dict = {}
    for c in changes:
        by_round.setdefault(c.get("round"), []).append(c)
    for rc, legs in by_round.items():
        print(FT.build_update_message(asset, rc, legs))
        print()


def run(dry: bool, weekly: bool | None) -> dict:
    """Main flow: compute BTC + MSTR live legs → detect_drift → merge alerts + write frozen (--check)."""
    frozen_all = FT.load_frozen()
    new_all = dict(frozen_all)
    alerted: list[str] = []

    # —— BTC ——
    st = _load_json(BTC_STATE)
    if st is not None:
        subcode.migrate_btc(st)
        cache = onchain_signals.load_cache()
        green, _stale = tier_alert.manual_green()
        live = btc_live_legs(st, cache, green)
        if live:
            snap, changes = FT.detect_drift("btc", live, prev=frozen_all.get("btc"),
                                            force_weekly=weekly, filled_labels=_filled_labels(st, "tiers"))
            new_all["btc"] = snap
            if changes:
                _emit_changes("btc", changes)
                alerted += [f"btc:{c['label']}" for c in changes]

    # —— MSTR ——
    mst = _load_json(MSTR_STATE)
    if mst is not None:
        subcode.migrate_mstr(mst)
        try:
            import mnav
            nps = mnav.compute().get("nav_per_share")
        except Exception:
            nps = None
        live_m = mstr_live_legs(mst, nps)
        if live_m:
            snap_m, changes_m = FT.detect_drift("mstr", live_m, prev=frozen_all.get("mstr"),
                                                force_weekly=weekly, filled_labels=_filled_labels(mst, "rounds"))
            new_all["mstr"] = snap_m
            if changes_m:
                _emit_changes("mstr", changes_m)
                alerted += [f"mstr:{c['label']}" for c in changes_m]

    if not dry and new_all != frozen_all:
        FT.save_frozen(new_all)  # Sole writer — independent config file, never writes state.json

    return {"ok": True, "alerted": alerted, "dry": dry,
            "frozen_written": (not dry and new_all != frozen_all)}


def main():
    ap = argparse.ArgumentParser(description="Trigger price freeze/drift alert cron")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="Production run: detect drift + push alerts + write frozen")
    g.add_argument("--dry", action="store_true", help="Dry run: print decisions only, do not write frozen")
    ap.add_argument("--weekly", action="store_true", help="Force weekly baseline reset (E3; use on Mondays alongside DCA/onchain cron)")
    a = ap.parse_args()
    res = run(dry=a.dry, weekly=True if a.weekly else None)
    out(res)
    sys.exit(0)


if __name__ == "__main__":
    main()
