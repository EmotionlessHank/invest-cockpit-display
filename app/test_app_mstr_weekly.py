"""
test_app_mstr_weekly.py — app.build_dashboard_mstr Monday DCA card + build_dashboard BTC window-pacing
wiring unit tests.

Scope:
  · MSTR: build_dashboard_mstr injects weekly (core-position window pacing: core_budget = sum of T1+T2
    round budget_usd, core_invested = strategy_mstr.core_invested_usd (core fills + standalone,
    excludes swan), mNAV from same source, next open leg T1-c -> T2-a -> ...).
  · BTC: build_dashboard weekly uses window pacing (WeeklyContext has window_end injected),
    not the old fixed-tier hardcoded amount.

Fully monkeypatched (no network, no real ledger/cache); state from seeds/*.seed.json fixtures.
Run: PYTHONPATH=app python3 app/test_app_mstr_weekly.py (all green -> exit 0).
"""

import json
import os
import pathlib
import sys
import types

# app.py top-level imports Flask for routes only; build_dashboard* does not touch Flask.
# Inject minimal stub (consistent with test_app_tier_overlay approach).
if "flask" not in sys.modules:
    _dec = lambda *aa, **kk: (lambda f: f)

    class _FlaskStub:
        def __init__(self, *a, **k):
            self.get = _dec
            self.post = _dec
            self.route = _dec
            self.errorhandler = _dec

    _stub = types.ModuleType("flask")
    _stub.Flask = _FlaskStub
    _stub.request = None
    _stub.send_file = lambda *a, **k: None
    _stub.jsonify = lambda *a, **k: None
    _stub.abort = lambda *a, **k: None
    sys.modules["flask"] = _stub

# Pin the window end so weeks is deterministic (consistent with strategy_mstr tests).
# Must be set before importing app (_golden_window_end reads the env var at call time).
os.environ["GOLDEN_WINDOW_END"] = "2026-09-12"

import app as A  # noqa: E402
import mnav  # noqa: E402
import onchain_signals  # noqa: E402
import tier_alert  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


HERE = pathlib.Path(__file__).resolve().parent
MSTR_SEED = HERE.parent / "seeds" / "mstr-state.seed.json"
BTC_SEED = HERE.parent / "seeds" / "state.seed.json"


def _mstr_state():
    return json.loads(MSTR_SEED.read_text(encoding="utf-8"))


def _patch_mstr(mnav_val=0.81, mstr_price=126.0, btc_price=63000.0, klines=None):
    """Monkeypatch mnav compute + klines to prevent network calls. mnav_val=None simulates fetch failure."""
    nps = round(mnav.nav_per_share(btc_price), 2) if btc_price else None
    q = {"mstr": mstr_price, "btc": btc_price, "nav_per_share": nps,
         "mnav": mnav_val}
    if mnav_val is None:
        q = {"mstr": None, "btc": None, "nav_per_share": None, "mnav": None}
    mnav.compute = lambda *a, **k: q
    mnav.get_mstr_klines_30d = lambda *a, **k: (klines if klines is not None else [130.0] * 22)


def _patch_btc(spot=63_000.0):
    A.get_spot = lambda: spot
    A.get_klines_30d = lambda: [70_000.0] * 30
    A.get_usdt_cny_rate = lambda: (6.81, "binance")
    onchain_signals.load_cache = lambda *a, **k: {"brk": {"ok": True,
        "fetched_at": "2026-06-09T08:00:00+10:00",
        "data": {"wma200": 61_573.0, "realized_price": 53_575.0, "green_line": None}}}
    tier_alert.manual_green = lambda *a, **k: (60_822.0, False)


# ════════════════════════════════════════════════════════════════════
# A. MSTR weekly: window-paced suggestion + next open leg
# ════════════════════════════════════════════════════════════════════
print("▶ a01_mstr_weekly_buy: seed ledger (invested 0) -> BUY · core_budget 14000 · record_mode=standalone · overlay_leg=T1-c")
st = _mstr_state()
_patch_mstr(mnav_val=0.81)
payload = A.build_dashboard_mstr(st)
w = payload["weekly"]
check("weekly present and BUY", w and w["action"] == "BUY", str(w))
# Seed invested_usd=0 -> pool 14000, tilt 1.0 -> base = 14000/weeks (capped at $1500/week).
# weeks derived from real now (route does not inject now), so only assert >=1 + tilt + amount>0
# (do not hardcode a specific week count).
check("amount_usd > 0 · weeks>=1 · tilt 1.0", w["amount_usd"] > 0 and w["weeks"] >= 1 and w["tilt"] == 1.0, str(w))
# DCA recorded via standalone core top-up; must NOT claim open dip legs (overlay_leg is reference only).
check("record_mode=standalone (does not claim dip legs)", w["record_mode"] == "standalone", str(w.get("record_mode")))
check("overlay_leg=T1-c (first open leg when T1/T2 all open; reference only)", w["overlay_leg"] == "T1-c", str(w["overlay_leg"]))
check("next_batch claim semantics no longer exposed", "next_batch" not in w, str(list(w.keys())))
check("tilt_label is 'baseline'", w["tilt_label"] == "baseline", str(w["tilt_label"]))

print("▶ a02_core_budget_no_swan: core_budget = T1+T2 budget (swan positions excluded from pool)")
# Seed core rounds 6300+7700=14000; pool should = 14000 (invested 0), must not include swan $14k.
check("pool_usd = 14000 (swan excluded)", w["pool_usd"] == 14000, str(w["pool_usd"]))

print("▶ a03_overlay_leg_skips_filled: T1-c filled -> overlay_leg=T2-a (overlay reference skips filled legs)")
st2 = _mstr_state()
for r in st2["rounds"]:
    if r["id"] == "T1":
        for b in r["plan"]:
            if b["label"] == "T1-c":
                b["status"] = "filled"
_patch_mstr(mnav_val=0.81)
w2 = A.build_dashboard_mstr(st2)["weekly"]
check("overlay_leg=T2-a", w2["overlay_leg"] == "T2-a", str(w2["overlay_leg"]))

print("▶ a04_mnav_fail_degraded: mNAV fetch failure -> DEGRADED, no amount computed")
st3 = _mstr_state()
_patch_mstr(mnav_val=None)
w3 = A.build_dashboard_mstr(st3)["weekly"]
check("DEGRADED · amount 0 · mnav None", w3["action"] == "DEGRADED" and w3["amount_usd"] == 0
      and w3["mnav"] is None, str(w3))

print("▶ a05_core_filled_skip: core position fully invested -> SKIP")
# core_invested single source = core fills + standalone (does not read top-level invested_usd).
# Use one $14,000 standalone fill to fully exhaust the core pool -> pool=0 -> SKIP.
st4 = _mstr_state()
st4["standalone_fills"] = [{"label": "standalone buy", "price": 110, "usd": 14000.0, "shares": 127, "at": "x"}]
_patch_mstr(mnav_val=0.81)
w4 = A.build_dashboard_mstr(st4)["weekly"]
check("SKIP · amount 0 (core_invested reaches budget)", w4["action"] == "SKIP" and w4["amount_usd"] == 0, str(w4))

print("▶ a06_swan_fill_no_leak: inject swan fill -> core pool unchanged (regression: swan fill must not drain core pool)")
# Top-level invested_usd includes swan fills, but build_dashboard_mstr uses core_invested_usd ->
# core pool is unaffected by swan activity.
st5 = _mstr_state()
for r in st5["rounds"]:
    if r["id"] == "A":
        r["fills"] = [{"label": "A-1", "price": 90, "usd": 3500.0, "shares": 39, "at": "x"}]
st5["invested_usd"] = 3500.0   # top-level total includes swan (would leak into core pool before the fix)
_patch_mstr(mnav_val=0.81)
w5 = A.build_dashboard_mstr(st5)["weekly"]
check("after swan fill core pool still = 14000 (no leak)", w5["pool_usd"] == 14000, str(w5["pool_usd"]))

# ════════════════════════════════════════════════════════════════════
# B. BTC weekly: window-pacing wired (WeeklyContext has window_end injected)
# ════════════════════════════════════════════════════════════════════
print("▶ b01_btc_weekly_paced: BTC weekly uses window pacing (not old fixed-tier hardcoded amount)")
bst = json.loads(BTC_SEED.read_text(encoding="utf-8"))
_patch_btc(spot=63_000.0)
bp = A.build_dashboard(bst)
bw = bp["weekly"]
# Old fixed-tier amount was ¥5,500 hardcoded; window pacing = remaining pool / 13 * 1.0,
# which varies with pool size (not equal to 5500) and is always BUY.
check("BTC weekly BUY · amount>0", bw["action"] == "BUY" and bw["amount_rmb"] > 0, str(bw))
check("not the old fixed ¥5500 hardcoded amount (uses pacing)", bw["amount_rmb"] != 5500, str(bw["amount_rmb"]))

print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
