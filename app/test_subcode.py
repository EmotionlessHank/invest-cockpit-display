"""
test_subcode.py — Plan A migration/derivation unit tests (standalone; subcode pure functions).

Coverage (real money · single-source and E1 rule — no reconstructed historical legs):
  T-A1 Schema migration idempotency: old schema (no label/status) → existing open legs get
    a/b/c + status=open; second run is no-op.
  T-A2 fills label back-fill: old fills without label → assigned label (by nearest price/order)
    or "legacy"; summary values unchanged at byte level.
  Derivation: derive_tier_status / open_legs / find_leg_by_label behave correctly.
  MSTR symmetry: migrate_mstr adds status, preserves existing labels.

Run: PYTHONPATH=app python3 app/test_subcode.py  (all green → exit 0)
"""

import copy
import json

import subcode as SC

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# Old BTC schema (no label/status; B1 has 1 historical fill + 1 remaining plan leg — mirrors server state)
OLD_BTC = {
    "invested_rmb": 60000, "held_btc": 0.15,
    "tiers": [
        {"name": "B1 Probe", "lo": 66000, "hi": 70000, "status": "partial",
         "plan": [{"price": 66000, "rmb": 6000}],
         "fills": [{"price": 69000, "rmb": 6000, "at": "2026-06-02T10:15:00+10:00"}]},
        {"name": "B3 Green-line", "lo": 55000, "hi": 60000, "status": "open",
         "plan": [{"price": 59000, "rmb": 16000}, {"price": 57000, "rmb": 16000}, {"price": 55000, "rmb": 16000}],
         "fills": []},
    ],
}


print("▶ T-A1 BTC migration: assign label(a/b/c) + status=open to existing open legs")
st = copy.deepcopy(OLD_BTC)
SC.migrate_btc(st)
b3 = st["tiers"][1]
check("B3 three legs labeled B3-a/b/c (descending price)",
      [p["label"] for p in b3["plan"]] == ["B3-a", "B3-b", "B3-c"], [p.get("label") for p in b3["plan"]])
check("B3 three legs status=open", all(p["status"] == "open" for p in b3["plan"]))

print("▶ T-A1 BTC migration idempotency: second run is no-op")
st2 = copy.deepcopy(st)
SC.migrate_btc(st2)
check("Second migration is no-op (deep equal)", st2 == st, "changed on 2nd run")

print("▶ T-A2 BTC fills label back-fill + E1 rule (no reconstruction) + summary values unchanged")
b1 = st["tiers"][0]
# B1: fill price 69000 (higher) + plan price 66000 → letter order B1-a(fill@69000) / B1-b(plan@66000)
check("B1 historical fill back-filled label=B1-a (highest price takes a)", b1["fills"][0]["label"] == "B1-a", b1["fills"])
check("B1 existing plan leg label=B1-b + status=open",
      b1["plan"][0]["label"] == "B1-b" and b1["plan"][0]["status"] == "open", b1["plan"])
check("E1: no reconstruction of pop-deleted historical legs (B1 still has 1 plan leg)", len(b1["plan"]) == 1, b1["plan"])
check("invested_rmb unchanged at byte level", st["invested_rmb"] == 60000 and st["held_btc"] == 0.15)

print("▶ T-A2b fill with no price → label=legacy")
st_h = {"tiers": [{"name": "C1 Deep-dip", "plan": [], "fills": [{"rmb": 8000, "at": "2026-05-01"}]}]}
SC.migrate_btc(st_h)
check("No-price fill → label=legacy", st_h["tiers"][0]["fills"][0]["label"] == "legacy", st_h["tiers"][0]["fills"])

print("▶ Derivation: derive_tier_status / open_legs / find_leg_by_label")
tier = {"plan": [{"label": "X-a", "status": "filled"}, {"label": "X-b", "status": "open"}], "fills": [{}]}
check("Some filled → partial", SC.derive_tier_status(tier) == "partial")
tier_done = {"plan": [{"status": "filled"}], "fills": [{}]}
check("All filled → done", SC.derive_tier_status(tier_done) == "done")
tier_none = {"plan": [{"status": "open"}], "fills": [], "status": "wait"}
check("No filled → preserve original status (wait)", SC.derive_tier_status(tier_none) == "wait")
check("open_legs returns only status!=filled legs",
      [p["label"] for p in SC.open_legs(tier)] == ["X-b"])
check("find_leg_by_label hit (includes filled)", SC.find_leg_by_label(tier, "X-a")["status"] == "filled")
check("find_leg_by_label miss → None", SC.find_leg_by_label(tier, "X-z") is None)


print("▶ T-A10 MSTR migration: add status to plan legs, preserve existing labels; idempotent")
OLD_MSTR = {
    "invested_usd": 0, "held_shares": 0,
    "rounds": [
        {"id": "T2", "name": "T2 Green-line Core", "status": "open",
         "plan": [{"label": "T2-a", "price": 101}, {"label": "T2-b", "price": 91}], "fills": []},
        {"id": "T1", "name": "T1", "status": "partial",
         "plan": [{"label": "T1-b", "price": 118}],
         "fills": [{"label": "T1-a", "price": 130}, {"price": 125}]},
    ],
}
m = copy.deepcopy(OLD_MSTR)
SC.migrate_mstr(m)
check("T2 plan legs get status=open (labels preserved)",
      all(b["status"] == "open" for b in m["rounds"][0]["plan"])
      and m["rounds"][0]["plan"][0]["label"] == "T2-a")
check("MSTR fill with existing label preserved", m["rounds"][1]["fills"][0]["label"] == "T1-a")
check("MSTR fill missing label → legacy", m["rounds"][1]["fills"][1]["label"] == "legacy", m["rounds"][1]["fills"])
m2 = copy.deepcopy(m)
SC.migrate_mstr(m2)
check("MSTR migration idempotent no-op", m2 == m)
check("MSTR summary values unchanged", m["invested_usd"] == 0 and m["held_shares"] == 0)


print("▶ Idempotency: already-new-schema BTC state is not modified")
NEW_BTC = json.loads(json.dumps({
    "tiers": [{"name": "B2 Core", "plan": [{"label": "B2-a", "price": 64000, "rmb": 14000, "status": "open"}],
               "fills": []}]}))
new2 = copy.deepcopy(NEW_BTC)
SC.migrate_btc(new2)
check("New-schema BTC migration is no-op", new2 == NEW_BTC)


print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
import sys  # noqa: E402
sys.exit(1 if FAIL else 0)
