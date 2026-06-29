"""
subcode.py — dip-buying-leg sub-code + state-machine data model, single source of truth (Plan A).

Design contract (real money · follows single-source and server-authoritative ledger principles):
  - This module is pure-function only; it mutates the passed-in state dict in-place (migration) or
    derives read-only values. It never touches positions and never places orders.
  - `migrate_btc` / `migrate_mstr` = self-healing load-path migration: upgrades old schema (legs
    without label/status, filled legs already pop-deleted from plan) to new schema (legs always
    resident, with stable label + status).
    **Idempotent**: already-migrated state is a no-op; may be called repeatedly by record_fill /
    display layer load.
  - Single source: BTC/MSTR record_fill, show_progress×2, and app.py all go through this module
    for migration/derivation, eliminating dual-source drift. Distributed alongside app/ shared
    modules to skill scripts at deploy time.

New schema (symmetric for both sides):
  tiers[].plan[] (BTC) / rounds[].plan[] (MSTR) — each leg always present:
    {"label": "B3-b", "price": ..., "rmb"/"usd"/"shares": ..., "status": "open"|"filled"}
  Fills no longer pop: leg status → "filled", and a fills[] entry is appended (back-linking via label).
  Tier/round overall status is derived from filled-leg count (all→done / some→partial / none→keep open/wait).

E1 rule: historically pop-deleted plan legs are NOT reconstructed; only existing open legs get labels,
  and historical fills[] get back-filled labels (nearest-price/order within tier, or "legacy" if
  price is unresolvable).
"""

from __future__ import annotations

# BTC sub-code letter sequence (assigned a/b/c… by descending price), mirroring MSTR T1-a style.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _tier_code(name: str) -> str:
    """Extract tier code prefix (B1/B2/B3/C1/C2…) from tier['name'] (e.g. 'B3 Green-line' / 'C1 Deep-dip')."""
    return str(name or "").split(" ")[0].strip()


# ══════════════════════════ BTC Migration ══════════════════════════
def migrate_btc(state: dict) -> dict:
    """BTC ledger old schema → new schema (mutates state in-place, returns same object; idempotent).

    Old schema characteristics: tiers[].plan[] legs only have {price, rmb}, no label/status;
      tiers[].fills[] have no label; filled legs already pop-deleted (not reconstructed, per E1 rule).
    Migration actions (idempotent):
      1. Existing open legs (in plan): assign label `{code}-a/b/c…` + status="open" by descending price.
         Note: letter slots already occupied by fills must be skipped — total leg order =
         "historical fills count + existing plan count" for that tier; plan legs come after fills
         (fills are higher-priced already-filled legs; not reconstructed per E1 but they hold letter slots).
      2. Historical fills[]: back-fill label by descending price (aligned with tier leg letter sequence);
         if price cannot be parsed, assign "legacy".
    Tiers where all plan legs already have label+status and all fills already have label → no-op (idempotent).
    """
    for tier in state.get("tiers", []):
        code = _tier_code(tier.get("name"))
        plan = tier.get("plan", []) or []
        fills = tier.get("fills", []) or []

        plan_done = all(("label" in p and "status" in p) for p in plan)
        fills_done = all("label" in f for f in fills)
        if plan_done and fills_done and (plan or fills):
            continue  # tier already migrated, no-op (idempotent)
        if not plan and not fills:
            continue

        # Tier-wide "price → letter" ordering: fills (already filled, higher price) + plan (open)
        # merged and sorted by descending price.
        # E1: do not reconstruct pop-deleted historical legs; fills that do exist occupy letter slots,
        # plan legs follow immediately after.
        legs = [("fill", i, f.get("price")) for i, f in enumerate(fills)] \
            + [("plan", i, p.get("price")) for i, p in enumerate(plan)]
        # Missing price (None) sorts last for stable ordering
        legs.sort(key=lambda x: (x[2] is None, -(x[2] or 0)))
        n = len(legs)
        multi = n > 1
        for order, (kind, idx, _price) in enumerate(legs):
            label = f"{code}-{_LETTERS[order]}" if (multi and order < len(_LETTERS)) else code
            if kind == "plan":
                leg = plan[idx]
                if "label" not in leg:
                    leg["label"] = label
                leg.setdefault("status", "open")
            else:
                leg = fills[idx]
                if "label" not in leg:
                    leg["label"] = label if _price is not None else "legacy"
    return state


# ══════════════════════════ MSTR Migration ══════════════════════════
def migrate_mstr(state: dict) -> dict:
    """MSTR ledger old schema → new schema (mutates state in-place, returns same object; idempotent).

    MSTR plan[] **already has labels** (T1-a/A-1…), only missing status; filled legs were removed.
    Migration actions (idempotent):
      1. Existing open legs (in plan): add status="open" (preserve existing label).
      2. fills[] already have labels (record_fill_mstr has always written them back);
         individual historical fills missing label → "legacy".
    standalone_fills do not belong to any round and are not migrated (label already set as a
    standalone-purchase marker).
    """
    for rnd in state.get("rounds", []):
        for leg in rnd.get("plan", []) or []:
            leg.setdefault("status", "open")
        for f in rnd.get("fills", []) or []:
            if "label" not in f:
                f["label"] = "legacy"
    return state


# ══════════════════════════ Status Derivation ══════════════════════════
def derive_tier_status(tier: dict) -> str:
    """Derive tier/round overall status from filled-leg count (new schema):
      All legs (plan + fills) filled → done;
      Some filled, some open → partial;
      None filled → preserve original status (open/wait).
    Under the new schema, plan legs are never deleted (fill = status→filled, leg stays in plan)."""
    plan = tier.get("plan", []) or []
    fills = tier.get("fills", []) or []
    n_filled = sum(1 for p in plan if p.get("status") == "filled") + len(fills)
    n_open = sum(1 for p in plan if p.get("status") != "filled")
    if n_filled and not n_open:
        return "done"
    if n_filled and n_open:
        return "partial"
    return tier.get("status", "open")


def open_legs(tier: dict) -> list[dict]:
    """Return plan legs that are still open (status!=filled) in this tier (new schema)."""
    return [p for p in (tier.get("plan", []) or []) if p.get("status") != "filled"]


def find_leg_by_label(tier: dict, label: str) -> dict | None:
    """Find a leg in this tier's plan by exact label match (includes already-filled legs, for dedup checks)."""
    for p in tier.get("plan", []) or []:
        if p.get("label") == label:
            return p
    return None
