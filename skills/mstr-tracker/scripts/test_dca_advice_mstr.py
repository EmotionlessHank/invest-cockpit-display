"""
test_dca_advice_mstr.py — test suite for dca_advice_mstr.py (standalone, no third-party dependencies).

Two coverage paths:
  1. Pure function advise(st, mnav, now, window_end): window-paced suggested amount + next open batch + mNAV-fetch-failure degradation
     (now/window_end/mnav are injected; no network, no dependence on the real date).
  2. CLI subprocess (= how cron / the skill actually invokes it): --mnav injection, JSON last line, graceful error on missing/corrupt ledger.

Run: python3 test_dca_advice_mstr.py (needs PYTHONPATH set so strategy*/mnav resolve; run-tests.sh already sets it). All green → exit 0.
"""

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dca_advice_mstr  # noqa: E402

SCRIPT = str(pathlib.Path(__file__).parent / "dca_advice_mstr.py")

# Ledger fixture: T1-a/T1-b filled (≈ $4,434.43 total), T1-c/T2 all open; core-pool budget 6300+7700=$14,000.
BASE = {
    "updated": "2026-06-04T10:00:00+10:00", "currency": "USD",
    "held_shares": 35, "invested_usd": 4434.43, "avg_cost_usd": 126,
    "budget_core_usd": 14000, "budget_swan_usd": 14000,
    "rounds": [
        {"id": "T1", "name": "T1 Opening Salvo", "layer": "core", "budget_usd": 6300, "sem": "amber",
         "trigger_kind": "price", "status": "partial",
         "plan": [{"label": "T1-a", "price": 130, "usd": 1890, "shares": 14.5, "status": "filled"},
                  {"label": "T1-b", "price": 118, "usd": 2520, "shares": 21.4, "status": "filled"},
                  {"label": "T1-c", "price": 105, "usd": 1890, "shares": 18, "status": "open"}],
         "fills": [{"label": "T1-a", "price": 130, "usd": 1914.43, "shares": 14.5, "at": "x"},
                   {"label": "T1-b", "price": 118, "usd": 2520, "shares": 21.4, "at": "x"}]},
        {"id": "T2", "name": "T2 Green-Line Core", "layer": "core", "budget_usd": 7700, "sem": "green",
         "trigger_kind": "mnav", "status": "open",
         "plan": [{"label": "T2-a", "mnav": 0.78, "price": 101, "usd": 2695, "shares": 27, "status": "open"},
                  {"label": "T2-b", "mnav": 0.70, "price": 91, "usd": 2695, "shares": 30, "status": "open"},
                  {"label": "T2-c", "mnav": 0.65, "price": 84, "usd": 2310, "shares": 27, "status": "open"}],
         "fills": []},
        # Swan A/B: layer == "swan", do not participate in DCA, not counted into the core pool
        {"id": "A", "name": "Swan-A High Probability", "layer": "swan", "budget_usd": 7000, "sem": "red",
         "trigger_kind": "mnav", "status": "wait",
         "plan": [{"label": "A-1", "mnav": 0.70, "price": 90, "usd": 3500, "shares": 39, "status": "open"}],
         "fills": []},
    ],
}

WE = date(2026, 9, 12)
NOW = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def state(**over):
    s = copy.deepcopy(BASE)
    s.update(over)
    return s


def fixture(tmp, **kw):
    p = pathlib.Path(tmp) / "mstr-state.json"
    p.write_text(json.dumps(state(**kw), ensure_ascii=False), "utf-8")
    return p


def run_cli_env(state_path, base_env, *args):
    env = {**base_env, "MSTR_STATE": str(state_path)}
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"_raw": r.stdout, "_err": r.stderr}
    return res, r.stdout


def run_cli(state_path, *args):
    return run_cli_env(state_path, dict(os.environ), *args)


# ── 1. core_budget / next_open_leg helpers ──
print("▶ t01_core_budget: core-pool budget = sum of T1+T2 budgets (excludes Swan)")
check("core_budget = 14000 (6300+7700)", dca_advice_mstr.core_budget(state()) == 14000,
      str(dca_advice_mstr.core_budget(state())))

print("▶ t02_overlay_leg: currently open limit leg (overlay reference) ordered T1-c→T2-a→T2-b→T2-c")
check("T1-a/b filled → overlay_leg = T1-c", dca_advice_mstr.next_open_leg(state()) == "T1-c")
# T1-c also filled → lands on T2-a
s2 = state()
s2["rounds"][0]["plan"][2]["status"] = "filled"
check("T1-c filled → overlay_leg = T2-a", dca_advice_mstr.next_open_leg(s2) == "T2-a")
# entire core pool filled → None (will not fall through to Swan)
s3 = state()
for r in s3["rounds"]:
    if r["layer"] == "core":
        for b in r["plan"]:
            b["status"] = "filled"
check("entire core pool filled → None (Swan does not take over)", dca_advice_mstr.next_open_leg(s3) is None)

# ── 2. advise window pacing: ≈$736 + record_mode=standalone (default scenario) ──
print("▶ t03_advise_default: invested $4,434.43 · mnav 0.81 · now 2026-06-15 → ≈$736 · standalone")
text, r = dca_advice_mstr.advise(state(), 0.81, now=NOW, window_end=WE)
check("BUY · week_amount_usd ≈ 736", r["ok"] and r["action"] == "BUY"
      and abs(r["week_amount_usd"] - 736) < 5, str(r["week_amount_usd"]))
check("record_mode = standalone (does not claim a dip leg)", r["record_mode"] == "standalone", str(r.get("record_mode")))
check("overlay_leg = T1-c (reference only, not a recording instruction)", r["overlay_leg"] == "T1-c", str(r.get("overlay_leg")))
check("weeks=13 · mnav 0.81 · tilt 1.0", r["weeks"] == 13 and r["mnav"] == 0.81 and r["tilt"] == 1.0, str(r))
check("text contains the suggested amount and standalone guidance", "Weekly MSTR" in text and "standalone" in text, text)

# ── 3. mNAV tilt passthrough ──
print("▶ t04_tilt: deeper mNAV discount → higher tilt raises the suggested amount")
_, r_hi = dca_advice_mstr.advise(state(), 0.90, now=NOW, window_end=WE)   # shallow discount, below 0.7
_, r_lo = dca_advice_mstr.advise(state(), 0.65, now=NOW, window_end=WE)   # deep discount, 1.6
check("mnav 0.90 tilt 0.7 < mnav 0.65 tilt 1.6", r_hi["tilt"] == 0.7 and r_lo["tilt"] == 1.6
      and r_lo["week_amount_usd"] > r_hi["week_amount_usd"], f"{r_hi['tilt']}/{r_lo['tilt']}")

# ── 4. core pool already full → SKIP (core_invested single source: core-pool fills + standalone reach budget) ──
print("▶ t05_filled_skip: core pool already filled up → SKIP (pool=0)")
# core_invested = core-pool fills(4434.43) + standalone(9565.57) = 14000; does not depend on top-level invested_usd (no longer read).
s_full = state()
s_full["standalone_fills"] = [{"label": "standalone", "price": 110, "usd": 9565.57, "shares": 87, "at": "x"}]
_, r = dca_advice_mstr.advise(s_full, 0.81, now=NOW, window_end=WE)
check("SKIP · week_amount_usd=0 (core_invested reaches budget)", r["action"] == "SKIP" and r["week_amount_usd"] == 0, str(r))

# ── 5. mNAV fetch failure: do not fabricate a value ──
print("▶ t06_mnav_fail: mNAV fetch failure → no fabrication, degraded notice")
text, r = dca_advice_mstr.advise(state(), None, now=NOW, window_end=WE)
check("ok=false, no amount", (not r["ok"]) and r["week_amount_usd"] is None, str(r))
check("text contains 'fetch failed', no $ amount", "fetch failed" in text and "$" not in text, text)

# ── 6. mNAV abnormal (out of range) → refuse to compute ──
print("▶ t07_mnav_abnormal: mNAV out of range → refuse, no fabricated amount")
_, r = dca_advice_mstr.advise(state(), 99, now=NOW, window_end=WE)
check("ok=false · week_amount_usd=None", (not r["ok"]) and r["week_amount_usd"] is None, str(r))

# ── 7. JSON shape complete ──
print("▶ t08_json_shape: result fields complete")
_, r = dca_advice_mstr.advise(state(), 0.81, now=NOW, window_end=WE)
check("has all fields", all(k in r for k in ("ok", "week_amount_usd", "record_mode", "overlay_leg", "weeks",
      "mnav", "tilt", "action", "reasons")) and isinstance(r["reasons"], list), str(r))

# ── 8. CLI integration (subprocess = real invocation) ──
print("▶ t09_cli_mnav: CLI --mnav injection, last line is valid JSON")
with tempfile.TemporaryDirectory() as tmp:
    res, raw = run_cli(fixture(tmp), "--mnav", "0.81")
    check("CLI ok and BUY · amount>0 · record_mode=standalone", res.get("ok") and res.get("action") == "BUY"
          and res.get("week_amount_usd", 0) > 0 and res.get("record_mode") == "standalone", str(res))
    check("stdout has human-readable text", "Weekly MSTR" in raw, raw[:80])

print("▶ t10_cli_missing: ledger does not exist → graceful error")
res, _ = run_cli("/nonexistent/mstr-state.json", "--mnav", "0.81")
check("ok=false, no bare throw", res.get("ok") is False and "msg" in res, str(res))

print("▶ t11_cli_corrupt: ledger corrupt → graceful error")
with tempfile.TemporaryDirectory() as tmp:
    p = pathlib.Path(tmp) / "mstr-state.json"
    p.write_text("{ broken json", "utf-8")
    res, _ = run_cli(p, "--mnav", "0.81")
    check("corrupt ok=false", res.get("ok") is False and "msg" in res, str(res))

# ── Regression: the DCA output does not instruct claiming a dip leg (record_mode=standalone) ──
print("▶ t_reg_major_no_claim_dip: output record_mode=standalone, does not instruct recording into the T1-c limit leg")
text, r = dca_advice_mstr.advise(state(), 0.81, now=NOW, window_end=WE)
check("record_mode=standalone (core add-on, not claiming a dip leg)", r["record_mode"] == "standalone", str(r.get("record_mode")))
check("text no longer instructs 'record into the next open batch' (avoids filling the limit leg)",
      "record into the next open batch" not in text and "claim that batch" not in text, text)
check("text contains standalone core-pool DCA add-on guidance", "standalone" in text and "core-pool DCA" in text, text)

# ── Regression: a Swan fill in the ledger → the core pool is unaffected ──
print("▶ t_reg_critical1_swan_via_advise: a Swan fill does not shrink the core pool; a standalone does shrink it")
# baseline (no extra Swan fill): core_invested = core-pool fills 4434.43
_, r_base = dca_advice_mstr.advise(state(), 0.81, now=NOW, window_end=WE)
# inject a Swan A fill $3,500 → before the fix, using invested_usd would leak into core_invested and double-deduct the pool
s_swan = state()
for rr in s_swan["rounds"]:
    if rr["id"] == "A":
        rr["fills"] = [{"label": "A-1", "price": 90, "usd": 3500, "shares": 39, "at": "x"}]
s_swan["invested_usd"] = 4434.43 + 3500   # top-level ledger total includes Swan (simulating real bookkeeping)
_, r_swan = dca_advice_mstr.advise(s_swan, 0.81, now=NOW, window_end=WE)
check("core pool unchanged after a $3500 Swan fill (no leak)",
      abs(r_swan["pool_usd"] - r_base["pool_usd"]) < 0.01,
      f"swan={r_swan['pool_usd']} base={r_base['pool_usd']}")
check("core_invested still = 4434.43 after a Swan fill (only counts core-pool fills)",
      abs(r_swan["core_invested_usd"] - 4434.43) < 0.01, str(r_swan["core_invested_usd"]))
# inject a standalone (core DCA add-on $600) → the pool shrinks accordingly
s_sa = state()
s_sa["standalone_fills"] = [{"label": "standalone", "price": 110, "usd": 600, "shares": 5.45, "at": "x"}]
_, r_sa = dca_advice_mstr.advise(s_sa, 0.81, now=NOW, window_end=WE)
check("standalone $600 counts into core → pool shrinks by $600",
      abs((r_base["pool_usd"] - r_sa["pool_usd"]) - 600) < 0.01,
      f"base={r_base['pool_usd']} sa={r_sa['pool_usd']}")

# ── Regression: window already passed → REVIEW (no auto fill-up) ──
print("▶ t_reg_window_past_review: now=2026-09-20 (after the window) → REVIEW, no fill-up")
_, r = dca_advice_mstr.advise(state(), 0.81, now=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc), window_end=WE)
check("after the window action=REVIEW · week_amount_usd=0 · record_mode=standalone",
      r["ok"] and r["action"] == "REVIEW" and r["week_amount_usd"] == 0
      and r["record_mode"] == "standalone", str(r))


print(f"\n{'=' * 40}\nPASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
