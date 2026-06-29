"""
test_tier_anchors.py — Dip tier dynamic trigger prices + DCA boundary anchoring unit tests (standalone).

Coverage: strategy.tier_anchors (reads on-chain anchors → derives DCA boundaries + dip trigger prices, pure function)
        + strategy.price_to_tier (uses dynamic boundaries when available; falls back to fixed values on
          missing/out-of-range/non-monotonic anchors).

Invariants tested (real-money scenario):
  L2-1 Valid anchors → per-tier prices are self-consistent (DCA boundaries + B3/C trigger prices
       monotonically decreasing, equal-step spacing)
  L2-2 Missing/zero/negative/out-of-range anchors → fail-closed to fixed boundaries, no garbage prices
  L2-3 No state.json write after execution (pure function, no side effects)
  L2-4 Suspect anchors → degradation flag set
  Backward-compat: price_to_tier(p) without anchors == legacy behavior (fixed 58K/50K boundaries)
  Calibration: tier_anchors reproduces the documented tier positions for current anchor values

Run: PYTHONPATH=app python3 app/test_tier_anchors.py (all green → exit 0).
"""

import glob
import os
import pathlib

import strategy as S

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def _cache(wma200=61_000.0, realized=53_600.0, extra=None):
    """Build L1 cache shape: cache['brk']['data'] contains anchors. extra injects suspect flags etc."""
    data = {"wma200": wma200, "realized_price": realized,
            "green_line": None, "green_line_source": "manual"}
    if extra:
        data.update(extra)
    return {"brk": {"ok": True, "fetched_at": "2026-06-09T08:00:00+10:00", "data": data}}


# ════════════════════════════════════════════════════════════════════
# Backward-compat: price_to_tier(p) without anchors == legacy fixed boundaries
# ════════════════════════════════════════════════════════════════════
print("▶ t01_backcompat: price_to_tier without anchors == legacy fixed boundaries")
for p, tier in [(82_000, S.Tier.PAUSE), (80_000, S.Tier.PAUSE), (75_000, S.Tier.CAUTIOUS),
                (70_000, S.Tier.CAUTIOUS), (67_000, S.Tier.HEAVIER), (58_000, S.Tier.HEAVIER),
                (55_000, S.Tier.DOUBLE), (50_000, S.Tier.DOUBLE), (49_000, S.Tier.ENHANCED)]:
    check(f"${p:,} → {S.TIER_LABEL[tier]}", S.price_to_tier(p) == tier, str(S.price_to_tier(p)))
check("58000 → HEAVIER (fixed boundary edge)", S.price_to_tier(58_000) == S.Tier.HEAVIER)
check("50000 → DOUBLE (fixed boundary edge)", S.price_to_tier(50_000) == S.Tier.DOUBLE)
check("anchors=None explicit → legacy behavior", S.price_to_tier(55_000, None) == S.Tier.DOUBLE)


# ════════════════════════════════════════════════════════════════════
# L2-1 Valid anchors → DCA boundaries + dip trigger prices self-consistent
# ════════════════════════════════════════════════════════════════════
print("▶ t02_normal_anchors: valid anchors → DCA boundaries derived from single source")
a = S.tier_anchors(_cache(wma200=61_000.0, realized=53_600.0), manual_green=60_000.0)
check("ok=True (anchors complete and monotonic)", a["ok"] is True, str(a))
check("no degradation reasons", a["degraded"] == [], str(a["degraded"]))
check("heavier|double boundary = wma200", a["dca_edges"]["heavier_double"] == 61_000.0, str(a["dca_edges"]))
check("double|enhanced boundary = realized_price", a["dca_edges"]["double_enhanced"] == 53_600.0, str(a["dca_edges"]))

print("▶ t03_dynamic_edges: price_to_tier uses dynamic boundaries")
# Dynamic boundaries wma200=61K / realized=53.6K: $60K falls into DOUBLE (< 61K dynamic boundary,
# whereas old fixed 58K boundary would place it in HEAVIER)
check("$60K dynamic → DOUBLE (dynamic boundary 61K active, vs old fixed 58K)",
      S.price_to_tier(60_000, a) == S.Tier.DOUBLE, str(S.price_to_tier(60_000, a)))
check("$62K dynamic → HEAVIER", S.price_to_tier(62_000, a) == S.Tier.HEAVIER)
check("$53,600 dynamic boundary → DOUBLE (upper edge belongs to next tier down)", S.price_to_tier(53_600, a) == S.Tier.DOUBLE)
check("$53,599 dynamic → ENHANCED", S.price_to_tier(53_599, a) == S.Tier.ENHANCED)
check("$75K dynamic → CAUTIOUS (high-price tiers use fixed boundaries)", S.price_to_tier(75_000, a) == S.Tier.CAUTIOUS)
check("$82K dynamic → PAUSE (high-price tiers use fixed boundaries)", S.price_to_tier(82_000, a) == S.Tier.PAUSE)

print("▶ t04_dip_triggers: dip trigger prices (per-tier leg count + relative anchor offsets)")
# B3 = green+[0,-2000,-4000] (3 legs); C1 = wma200+[-4000,-6000] (2 legs); C2 = realized+[1000,-2000] (2 legs)
check("B3 = green+[0,-2000,-4000] (3 legs)",
      a["dip_triggers"]["B3"] == [60_000.0, 58_000.0, 56_000.0], str(a["dip_triggers"]["B3"]))
check("C1 = wma200+[-4000,-6000] (2 legs)",
      a["dip_triggers"]["C1"] == [57_000.0, 55_000.0], str(a["dip_triggers"]["C1"]))
check("C2 = realized+[1000,-2000] (2 legs)",
      a["dip_triggers"]["C2"] == [54_600.0, 51_600.0], str(a["dip_triggers"]["C2"]))
check("leg counts correct: B3 three legs / C1·C2 two legs each", len(a["dip_triggers"]["B3"]) == 3
      and len(a["dip_triggers"]["C1"]) == 2 and len(a["dip_triggers"]["C2"]) == 2)
check("B1/B2 retain fixed already-filled values",
      a["dip_triggers"]["B1"] == S.DIP_B1_FILLED and a["dip_triggers"]["B2"] == S.DIP_B2_FILLED)
check("all dip tiers strictly monotonically decreasing", all(
    all(t[i] > t[i + 1] for i in range(len(t) - 1))
    for t in (a["dip_triggers"]["B3"], a["dip_triggers"]["C1"], a["dip_triggers"]["C2"])))

print("▶ t04b_calibration_pinned: pinned calibration values (eliminate false greens)")
# Pinned anchors: green=60822 / wma200=61573 / realized=53575
a_pin = S.tier_anchors(_cache(wma200=61_573.0, realized=53_575.0), manual_green=60_822.0)
check("B3 = [60822,58822,56822] (3 legs)",
      a_pin["dip_triggers"]["B3"] == [60_822.0, 58_822.0, 56_822.0], str(a_pin["dip_triggers"]["B3"]))
check("C1 = [57573,55573] (2 legs)",
      a_pin["dip_triggers"]["C1"] == [57_573.0, 55_573.0], str(a_pin["dip_triggers"]["C1"]))
check("C2 = [54575,51575] (2 legs)",
      a_pin["dip_triggers"]["C2"] == [54_575.0, 51_575.0], str(a_pin["dip_triggers"]["C2"]))
check("pinned leg counts: B3 three legs / C1·C2 two legs", len(a_pin["dip_triggers"]["B3"]) == 3
      and len(a_pin["dip_triggers"]["C1"]) == 2 and len(a_pin["dip_triggers"]["C2"]) == 2)


print("▶ t04c_integer_round: dynamic trigger prices rounded to whole dollars (no decimal tails)")
# Anchors with decimals (realized=53574.34 → C2=[54574,51574]) → trigger prices must be whole integers.
a_dec = S.tier_anchors(_cache(wma200=61_572.66, realized=53_574.34), manual_green=60_821.5)
_all_dec = (a_dec["dip_triggers"]["B3"] + a_dec["dip_triggers"]["C1"] + a_dec["dip_triggers"]["C2"])
check("all dynamic trigger prices are whole integers (no $57,573.34 decimal tails)",
      all(float(v).is_integer() for v in _all_dec), str(_all_dec))
check("C2 rounded = realized+[1000,-2000] rounded to nearest dollar",
      a_dec["dip_triggers"]["C2"] == [54_574.0, 51_574.0], str(a_dec["dip_triggers"]["C2"]))
# Pinned values still hold after rounding
check("pinned B3=[60822,58822,56822] unchanged by rounding",
      a_pin["dip_triggers"]["B3"] == [60_822.0, 58_822.0, 56_822.0])
check("pinned C1=[57573,55573] / C2=[54575,51575] unchanged by rounding",
      a_pin["dip_triggers"]["C1"] == [57_573.0, 55_573.0]
      and a_pin["dip_triggers"]["C2"] == [54_575.0, 51_575.0])


print("▶ t04d_cross_monotonic: cross-tier top-price monotonicity gate (B3>C1>C2 inversion → fail-closed)")
# Construct an inversion: wma200=61K → C1 top=57K; realized=58K → C2 top=59K ≥ C1 top → inverted.
a_inv = S.tier_anchors(_cache(wma200=61_000.0, realized=58_000.0), manual_green=60_000.0)
check("C2 inversion (C2 top ≥ C1 top) → fail-closed, C2 set to None",
      a_inv["dip_triggers"]["C2"] is None, str(a_inv["dip_triggers"]["C2"]))
check("C2 inversion → degraded contains C2_dip_non_monotonic",
      "C2_dip_non_monotonic" in a_inv["degraded"], str(a_inv["degraded"]))
# Inversion of C2 does not contaminate B3 or C1
check("inversion is not contagious: B3/C1 still output trigger prices",
      a_inv["dip_triggers"]["B3"] is not None and a_inv["dip_triggers"]["C1"] is not None)
check("B3 top > C1 top (upstream tiers still monotonic)",
      max(a_inv["dip_triggers"]["B3"]) > max(a_inv["dip_triggers"]["C1"]))
# Normal anchors (wma200 > realized sufficiently) → three tiers strictly decreasing top prices, no flag
a_mono = S.tier_anchors(_cache(wma200=61_000.0, realized=53_600.0), manual_green=60_000.0)
check("normal anchors → no dip_non_monotonic degradation",
      not any("dip_non_monotonic" in r for r in a_mono["degraded"]), str(a_mono["degraded"]))
check("normal anchors → B3.top > C1.top > C2.top strictly decreasing",
      max(a_mono["dip_triggers"]["B3"]) > max(a_mono["dip_triggers"]["C1"])
      > max(a_mono["dip_triggers"]["C2"]))


# ════════════════════════════════════════════════════════════════════
# L2-2 Missing/zero/negative/out-of-range anchors → fail-closed, no garbage prices
# ════════════════════════════════════════════════════════════════════
print("▶ t05_failclosed_missing: missing anchors → fail-closed")
a_miss = S.tier_anchors({}, manual_green=None)
check("empty cache → ok=False", a_miss["ok"] is False, str(a_miss))
check("DCA boundaries are None (no garbage prices output)",
      a_miss["dca_edges"]["heavier_double"] is None and a_miss["dca_edges"]["double_enhanced"] is None)
check("price_to_tier with invalid anchors → falls back to fixed boundaries 58K/50K",
      S.price_to_tier(55_000, a_miss) == S.Tier.DOUBLE and S.price_to_tier(60_000, a_miss) == S.Tier.HEAVIER)
check("B3 missing green line → None (never returns 0 or garbage)", a_miss["dip_triggers"]["B3"] is None)
check("C1/C2 missing anchors → None", a_miss["dip_triggers"]["C1"] is None and a_miss["dip_triggers"]["C2"] is None)

print("▶ t06_failclosed_zero_neg: zero/negative anchors → fail-closed")
a_zero = S.tier_anchors(_cache(wma200=0.0, realized=-5.0), manual_green=0.0)
check("zero/negative anchors → ok=False", a_zero["ok"] is False, str(a_zero))
check("zero/negative → DCA boundaries None (no garbage prices)", a_zero["dca_edges"]["heavier_double"] is None)
check("zero green line → B3 None", a_zero["dip_triggers"]["B3"] is None)
check("price_to_tier falls back to fixed values", S.price_to_tier(55_000, a_zero) == S.Tier.DOUBLE)

print("▶ t07_failclosed_outofrange: anchors outside sanity band → fail-closed (prevents series mis-binding)")
# wma200 sanity band (20K, 150K): value of 5K violates the lower bound
a_oor = S.tier_anchors(_cache(wma200=5_000.0, realized=53_600.0), manual_green=200_000.0)
check("wma200 out of range → degraded contains wma200_unavailable", "wma200_unavailable" in a_oor["degraded"], str(a_oor["degraded"]))
check("green line above upper bound → degraded contains green_line_unavailable + B3 None",
      "green_line_unavailable" in a_oor["degraded"] and a_oor["dip_triggers"]["B3"] is None)
check("out-of-range anchors → ok=False", a_oor["ok"] is False)

print("▶ t08_failclosed_nonmonotonic: wma200 <= realized → non-monotonic, fail-closed")
a_nm = S.tier_anchors(_cache(wma200=50_000.0, realized=60_000.0), manual_green=58_000.0)
check("non-monotonic → degraded contains dca_edges_non_monotonic", "dca_edges_non_monotonic" in a_nm["degraded"], str(a_nm["degraded"]))
check("non-monotonic → ok=False + DCA boundaries None", a_nm["ok"] is False and a_nm["dca_edges"]["heavier_double"] is None)
check("non-monotonic DCA → price_to_tier falls back to fixed boundaries", S.price_to_tier(55_000, a_nm) == S.Tier.DOUBLE)
# Green line itself is valid → B3 can still be computed (DCA degradation does not contaminate dip tiers)
check("valid green line → B3 still outputs trigger prices (degradation is not contagious)", a_nm["dip_triggers"]["B3"] == [58_000.0, 56_000.0, 54_000.0])


# ════════════════════════════════════════════════════════════════════
# L2-4 Suspect anchors → degradation flag (R6 single-day jump guard marks suspect at L1;
#       L2 reads and immediately degrades)
# ════════════════════════════════════════════════════════════════════
print("▶ t09_suspect: suspect anchor → degradation flag set")
a_susp = S.tier_anchors(_cache(wma200=61_000.0, realized=53_600.0, extra={"wma200_suspect": True}),
                        manual_green=60_000.0)
check("wma200_suspect → degraded contains wma200_suspect", "wma200_suspect" in a_susp["degraded"], str(a_susp["degraded"]))
check("suspect anchor excluded from DCA derivation → ok=False", a_susp["ok"] is False)
check("suspect anchor → corresponding boundary is None (no suspicious price used)", a_susp["dca_edges"]["heavier_double"] is None)
check("suspect wma200 → C1 None (wma200 unavailable)", a_susp["dip_triggers"]["C1"] is None)
check("non-suspect green line → B3 still computes (3 legs)", a_susp["dip_triggers"]["B3"] == [60_000.0, 58_000.0, 56_000.0])

print("▶ t09b_realized_suspect: realized_price suspect → double|enhanced fail-closed + C2 degraded")
# R6 single-day jump guard marks realized_price suspect at L1; L2 must fail-closed immediately.
a_rsusp = S.tier_anchors(_cache(wma200=61_000.0, realized=53_600.0,
                                extra={"realized_price_suspect": True}),
                         manual_green=60_000.0)
check("realized_price_suspect → degraded contains realized_price_suspect",
      "realized_price_suspect" in a_rsusp["degraded"], str(a_rsusp["degraded"]))
check("realized suspect → DCA incomplete → ok=False", a_rsusp["ok"] is False)
check("double|enhanced boundary None (suspicious realized_price not used as a price boundary)",
      a_rsusp["dca_edges"]["double_enhanced"] is None and a_rsusp["dca_edges"]["heavier_double"] is None)
check("C2 None (realized_price unavailable — no garbage trigger prices)", a_rsusp["dip_triggers"]["C2"] is None)
check("price_to_tier falls back to fixed boundaries 58K/50K (no garbage tiers)",
      S.price_to_tier(55_000, a_rsusp) == S.Tier.DOUBLE and S.price_to_tier(60_000, a_rsusp) == S.Tier.HEAVIER)
# Anchors not affected by the suspect flag still compute independently (degradation is not contagious)
check("non-suspect wma200 → C1 still computes (2 legs)", a_rsusp["dip_triggers"]["C1"] == [57_000.0, 55_000.0])
check("non-suspect green line → B3 still computes (3 legs)", a_rsusp["dip_triggers"]["B3"] == [60_000.0, 58_000.0, 56_000.0])


# ════════════════════════════════════════════════════════════════════
# Calibration: reproduce documented tier positions with current anchor values
# ════════════════════════════════════════════════════════════════════
print("▶ t10_calibration: reproduce documented tier positions with current anchor values")
# Current anchors approximately: wma200=58K, realized=53.6K → restores legacy 58K/50K boundaries.
a_cal = S.tier_anchors(_cache(wma200=58_000.0, realized=53_600.0), manual_green=59_000.0)
check("wma200=58K reproduces legacy heavier|double boundary → $58K falls in HEAVIER",
      S.price_to_tier(58_000, a_cal) == S.Tier.HEAVIER, str(S.price_to_tier(58_000, a_cal)))
check("$57,999 → DOUBLE (dynamic boundary 58K, upper edge belongs to next tier down)", S.price_to_tier(57_999, a_cal) == S.Tier.DOUBLE)
# Calibration: green=59K → B3 reproduces documented positions 59/57/55 with equal spacing
check("green=59K → B3 reproduces documented 59/57/55",
      a_cal["dip_triggers"]["B3"] == [59_000.0, 57_000.0, 55_000.0], str(a_cal["dip_triggers"]["B3"]))


# ════════════════════════════════════════════════════════════════════
# Integration: decide_weekly with anchors (backward-compat + dynamic boundaries active)
# ════════════════════════════════════════════════════════════════════
print("▶ t11_decide_weekly_anchors: decide_weekly accepts optional anchors parameter")
ctx_no = S.WeeklyContext(btc_price_usd=55_000, invested_rmb=50_000, available_usdt=5_000,
                         dip_filled_last_7d=False)
d_no = S.decide_weekly(ctx_no)
check("no anchors → legacy behavior (55K falls in DOUBLE)", d_no.base_tier == S.Tier.DOUBLE, str(d_no.base_tier))
ctx_dyn = S.WeeklyContext(btc_price_usd=60_000, invested_rmb=50_000, available_usdt=5_000,
                          dip_filled_last_7d=False, anchors=a)
d_dyn = S.decide_weekly(ctx_dyn)
check("with dynamic anchors → 60K falls in DOUBLE (dynamic boundary 61K active)", d_dyn.base_tier == S.Tier.DOUBLE, str(d_dyn.base_tier))


# ════════════════════════════════════════════════════════════════════
# L2-3 No state.json write after execution (pure function, no side effects)
# ════════════════════════════════════════════════════════════════════
print("▶ t12_no_state_write: pure functions produce no state.json writes")
_here = pathlib.Path(__file__).resolve().parent
_state_files = glob.glob(str(_here / "**" / "*state*.json"), recursive=True) + \
    glob.glob(str(_here / "**" / "state.json"), recursive=True)
# Re-run all anchor functions, confirm no disk writes
for _ in range(3):
    S.tier_anchors(_cache(), manual_green=60_000.0)
    S.price_to_tier(55_000, a)
_state_after = glob.glob(str(_here / "**" / "*state*.json"), recursive=True) + \
    glob.glob(str(_here / "**" / "state.json"), recursive=True)
check("tier_anchors/price_to_tier produce no new state.json files",
      set(_state_after) == set(_state_files), f"new={set(_state_after) - set(_state_files)}")
check("no state.json created in cwd",
      not os.path.exists(os.path.join(os.getcwd(), "state.json")) or
      os.path.join(os.getcwd(), "state.json") in _state_files)


print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
import sys  # noqa: E402
sys.exit(1 if FAIL else 0)
