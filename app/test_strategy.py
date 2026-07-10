"""
test_strategy.py — Test suite for strategy.py: deadline-paced DCA window mode,
backward compatibility, and remaining_weeks.

Standalone; no third-party dependencies.
Run with: PYTHONPATH=app python3 test_strategy.py (already configured in run-tests.sh). All passing → exit 0.
All tests inject now/window_end; no dependency on the real current date (deterministic / reproducible).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import strategy
from strategy import Tier, WeeklyContext, decide_weekly, remaining_weeks

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def dt(y, m, d, h=9):
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


def ctx(btc=63000.0, invested=126453.0, dip=False, now=None, window_end=None, avail=10 ** 9):
    return WeeklyContext(
        btc_price_usd=btc, invested_rmb=invested, available_usdt=avail,
        dip_filled_last_7d=dip, rate_rmb_per_usdt=7.0, now=now, window_end=window_end,
    )


# ══ 1. remaining_weeks: Monday count + floor 1 (no div-by-zero) ══════════════
print("▶ t01_remaining_weeks: Monday count + floor 1 (no div-by-zero)")
# 2026-06-15 is a Monday; window_end 2026-09-12 (Saturday, its Monday = 2026-09-07).
# (2026-09-07 - 2026-06-15) = 84 days = 12 weeks → 12 + 1 = 13.
check("2026-06-15 (Mon) → 2026-09-12 = 13 weeks",
      remaining_weeks(dt(2026, 6, 15), date(2026, 9, 12)) == 13,
      str(remaining_weeks(dt(2026, 6, 15), date(2026, 9, 12))))
# Within the same week (Sunday 06-21) still belongs to Monday 06-15 → same 13 weeks (Monday-anchored)
check("same-week Sunday 06-21 → still 13 weeks (Monday-anchored)",
      remaining_weeks(dt(2026, 6, 21), date(2026, 9, 12)) == 13,
      str(remaining_weeks(dt(2026, 6, 21), date(2026, 9, 12))))
# Crosses to the next Monday 06-22 → 12 weeks
check("next Monday 06-22 → 12 weeks",
      remaining_weeks(dt(2026, 6, 22), date(2026, 9, 12)) == 12,
      str(remaining_weeks(dt(2026, 6, 22), date(2026, 9, 12))))
# window_end within the current week → 1
check("window_end within this week → 1 week",
      remaining_weeks(dt(2026, 6, 15), date(2026, 6, 17)) == 1,
      str(remaining_weeks(dt(2026, 6, 15), date(2026, 6, 17))))
# window_end already past → floor 1 (never 0 or negative)
check("window_end already past → floor 1",
      remaining_weeks(dt(2026, 9, 20), date(2026, 9, 12)) == 1,
      str(remaining_weeks(dt(2026, 9, 20), date(2026, 9, 12))))


# ══ 2. Backward compatibility: window_end=None → legacy fixed tiers unchanged ══
print("▶ t02_legacy_fixed_tiers: window_end=None uses legacy logic")
for btc, amt, tier in [(75000, 3000, Tier.CAUTIOUS), (67000, 5500, Tier.HEAVIER),
                       (55000, 7500, Tier.DOUBLE), (49000, 10000, Tier.ENHANCED)]:
    d = decide_weekly(ctx(btc=btc, invested=50000, window_end=None))
    check(f"${btc} → fixed ¥{amt}", d.amount_rmb == amt and d.final_tier == tier
          and d.action == "BUY", f"{d.amount_rmb} {d.final_tier}")
# Legacy rule ② downgrade still applies
d = decide_weekly(ctx(btc=67000, invested=50000, dip=True, window_end=None))
check("legacy rule ②: dip fill within 7d downgrades Heavier→Cautious ¥3000", d.amount_rmb == 3000 and d.final_tier == Tier.CAUTIOUS, str(d.amount_rmb))
# Legacy rule ③ near-cap pause still applies
d = decide_weekly(ctx(btc=55000, invested=185000, window_end=None))
check("legacy rule ③: ≥¥180K → SKIP (pause)", d.action == "SKIP" and d.amount_rmb == 0, str(d))


# ══ 3. Window pace: base = pool/weeks correct ════════════════════════════════
print("▶ t03_window_base: base = remaining pool ÷ remaining weeks")
# Default: invested=126453 → pool=73547; weeks=13 → base≈5657.46; Heavier tier ×1.0
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
base_expected = (200000 - 126453) / 13
check(f"Heavier tier amount ≈ base ¥{base_expected:,.0f}",
      abs(d.amount_rmb - base_expected) < 1.0 and d.final_tier == Tier.HEAVIER,
      f"{d.amount_rmb} vs {base_expected}")


# ══ 4. Tilt coefficients per tier ════════════════════════════════════════════
print("▶ t04_tilt: per-tier tilt coefficients")
pool = 200000 - 100000  # invested=100000 → pool=100000
weeks = remaining_weeks(dt(2026, 6, 15), date(2026, 9, 12))  # 13
base = pool / weeks
for btc, tilt, tier in [(85000, 0.5, Tier.PAUSE), (75000, 0.8, Tier.CAUTIOUS),
                        (63000, 1.0, Tier.HEAVIER), (55000, 1.3, Tier.DOUBLE),
                        (48000, 1.6, Tier.ENHANCED)]:
    d = decide_weekly(ctx(btc=btc, invested=100000, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
    # If base×tilt converts to USDT above the single-order cap $1500, amount_rmb is back-calculated
    # = 1500×7 (consistent with actual orderable amount, CRITICAL②).
    exp = min(base * tilt, strategy.MAX_SINGLE_ORDER_USDT * 7.0)
    check(f"${btc} → tilt ×{tilt} ≈ ¥{exp:,.0f} (capped at single-order limit if exceeded)",
          abs(d.amount_rmb - exp) < 1.0 and d.base_tier == tier, f"{d.amount_rmb} vs {exp}")
# Pause tier in window mode still generates a BUY (tilt 0.5), not SKIP
d = decide_weekly(ctx(btc=85000, invested=100000, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
check("Pause tier in window mode still BUY (tilt 0.5, ≠ SKIP)", d.action == "BUY" and d.amount_rmb > 0, str(d))


# ══ 5. Final week force-fill; amount_rmb back-calculated; post-window → SKIP ═
print("▶ t05_last_week_dump: final window week force-fill (amount_rmb capped, no phantom ¥73547)")
# Final week: pool=¥73547 > single-order cap → force-fill intent, but the per-order cap rule limits amount_usdt to $1500
# and back-calculates amount_rmb = 1500×7 = ¥10500.
# Prevents ¥73,547 phantom entries in the dashboard and bookkeeping.
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 9, 7), window_end=date(2026, 9, 12)))
check("final week: amount_usdt capped at single-order limit $1500", d.amount_usdt == strategy.MAX_SINGLE_ORDER_USDT, str(d.amount_usdt))
check("final week: amount_rmb back-calculated = 1500×7 = ¥10500 (not phantom ¥73547)",
      abs(d.amount_rmb - strategy.MAX_SINGLE_ORDER_USDT * 7.0) < 0.01, str(d.amount_rmb))


# ══ 6. Dip fill within 7 days → ×0.8 (floor 0.4×base) ══════════════════════
print("▶ t06_dip_0_8: dip fill within 7 days → additional ×0.8")
d_nodip = decide_weekly(ctx(btc=63000, invested=100000, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
d_dip = decide_weekly(ctx(btc=63000, invested=100000, dip=True, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
check("dip ×0.8 (Heavier tilt 1.0: amount = nodip × 0.8)",
      abs(d_dip.amount_rmb - d_nodip.amount_rmb * 0.8) < 1.0, f"{d_dip.amount_rmb} vs {d_nodip.amount_rmb*0.8}")
# Enhanced tier tilt 1.6 × 0.8 = 1.28×base, still > 0.4×base → uses ×0.8.
# Floor validation: Pause tier tilt 0.5 × 0.8 = 0.4×base = floor exactly.
# Floor is 0.4 (not 0.5): a floor of 0.5 would fully cancel the ×0.8 soft constraint for the Pause tier.
d_pause_dip = decide_weekly(ctx(btc=85000, invested=100000, dip=True, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
base6 = (200000 - 100000) / 13
check("Pause tier ×0.8 = 0.4×base (floor 0.4 does not cancel ×0.8)",
      abs(d_pause_dip.amount_rmb - 0.4 * base6) < 1.0, f"{d_pause_dip.amount_rmb} vs {0.4*base6}")


# ══ 7. Budget conservation: never exceeds ¥200k hard cap ════════════════════
print("▶ t07_budget_cap: budget conservation never exceeds ¥200k")
# Near cap: invested=199000, pool=1000; final week force-fills 1000, still within cap
d = decide_weekly(ctx(btc=63000, invested=199000, now=dt(2026, 9, 7), window_end=date(2026, 9, 12)))
check("remaining ¥1000, amount = 1000, does not exceed cap", d.amount_rmb <= 1000 + 0.01, str(d.amount_rmb))
# At cap → SKIP
d = decide_weekly(ctx(btc=63000, invested=200000, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
check("at ¥200k → SKIP (pool=0, amount≤0)", d.action == "SKIP" and d.amount_rmb == 0, str(d))


# ══ 8. Abnormal price rejected in both modes ══════════════════════════════════
print("▶ t08_sane_price: abnormal price rejected in both modes")
for we in (None, date(2026, 9, 12)):
    d = decide_weekly(ctx(btc=5000, invested=100000, now=dt(2026, 6, 15), window_end=we))
    check(f"$5000 abnormal price rejected (window_end={we})", d.action == "SKIP" and d.amount_rmb == 0, str(d.action))
    d = decide_weekly(ctx(btc=300000, invested=100000, now=dt(2026, 6, 15), window_end=we))
    check(f"$300000 abnormal price rejected (window_end={we})", d.action == "SKIP" and d.amount_rmb == 0, str(d.action))


# ══ 9. Single-order cap + balance guard ══════════════════════════════════════
print("▶ t09_guards: single-order cap + balance guard")
# Final week large amount → USDT capped at single-order limit
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 9, 7), window_end=date(2026, 9, 12)))
check("final week large amount → USDT capped at single-order limit $1500", d.amount_usdt == strategy.MAX_SINGLE_ORDER_USDT, str(d.amount_usdt))
# Insufficient balance
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 6, 15), window_end=date(2026, 9, 12), avail=10))
check("insufficient balance → INSUFFICIENT_FUNDS", d.action == "INSUFFICIENT_FUNDS", str(d.action))


# ══ Regression: CRITICAL② amount_rmb three-way drift ════════════════════════
print("▶ t_reg_critical2_amount_rmb_drift: final week pool ¥73547 > single-order cap → amount_rmb back-calculated ≈¥10500")
# Before fix: amount_rmb was still ¥73,547 (phantom entry in dashboard and one-tap bookkeeping).
# After fix: back-calculated = amount_usdt × rate.
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 9, 7), window_end=date(2026, 9, 12)))
check("amount_rmb == amount_usdt × rate (≈¥10500, not ¥73547)",
      abs(d.amount_rmb - d.amount_usdt * 7.0) < 0.01 and abs(d.amount_rmb - 10500) < 0.01
      and d.amount_rmb < 73547, f"amount_rmb={d.amount_rmb} amount_usdt={d.amount_usdt}")

# ══ Regression: post-window explicit branch → SKIP (not perpetual force-fill) ═
print("▶ t_reg_window_past_skip: now=2026-09-20 (post-window) → SKIP, not full-pool BUY")
# Before fix: after window_end, remaining_weeks permanently = 1, force-filling the pool every week.
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 9, 20), window_end=date(2026, 9, 12)))
check("post-window: action=SKIP · amount=0 (not full-pool BUY)",
      d.action == "SKIP" and d.amount_rmb == 0 and d.amount_usdt == 0, str(d))
check("post-window: reason contains 'Window has ended' pending manual review",
      any("Window has ended" in r for r in d.reasons), str(d.reasons))
# Boundary check: last window week (not yet past) still generates BUY
# (09-07 ≤ 09-12 → BUY/capped, must not be mistakenly SKIP)
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 9, 7), window_end=date(2026, 9, 12)))
check("last window week (not past) still BUY (boundary check, not mistakenly SKIP)", d.action == "BUY", str(d.action))

# ══ Regression: WeeklyDecision.weeks surfaced as single source ════════════════
print("▶ t_reg_weeks_field: window mode d.weeks surfaces remaining_weeks; legacy mode = 0")
d = decide_weekly(ctx(btc=63000, invested=126453, now=dt(2026, 6, 15), window_end=date(2026, 9, 12)))
check("window mode d.weeks == 13 (surfaced; callers need not recompute)", d.weeks == 13, str(d.weeks))
d = decide_weekly(ctx(btc=63000, invested=50000, window_end=None))
check("legacy fixed-tier d.weeks == 0 (backward compatible)", d.weeks == 0, str(d.weeks))


# ══ Diminishing-returns indicator (decay_indicator) ══════════════════════════
print("▶ t_decay_center_multiple: derived from CYCLE_HISTORY via ln-diff-ratio decay ≈4.8x")
cm = strategy._decay_center_multiple()
check("center multiple ≈4.8x (matches the article's ~0.54 decay-ratio extrapolation)",
      abs(cm - 4.8) < 0.05, str(cm))

print("▶ t_decay_indicator_targets: low=$57,800 → 3x/center/7x target band")
di = strategy.decay_indicator(57_800, 57_800)
check("conservative target = 3x low", di["conservative"] == round(57_800 * 3))
check("aggressive target = 7x low", di["aggressive"] == round(57_800 * 7))
check("center target ≈ 4.8x low", abs(di["center"] - 57_800 * cm) <= 1)
check("no cycle-low reading → None (never fabricates a low)", strategy.decay_indicator(None, 60_000) is None)
check("low<=0 → None", strategy.decay_indicator(0, 60_000) is None)

print("▶ t_decay_indicator_zones: spot position maps to the right zone/sem/frac across the band")
low = 57_800
d_at_low = strategy.decay_indicator(low, low)
check("spot==low → below_low zone, frac=0, green", d_at_low["zone"] == "below_low" and d_at_low["frac"] == 0 and d_at_low["sem"] == "green")
d_below = strategy.decay_indicator(low, low * 0.9)
check("spot<low (new low intraday) → clamped to frac=0, still below_low/green", d_below["zone"] == "below_low" and d_below["frac"] == 0)
d_mid_low = strategy.decay_indicator(low, low * 2)   # between low and 3x
check("between low and conservative(3x) → low_conservative/green, frac in (0, 1/3)",
      d_mid_low["zone"] == "low_conservative" and d_mid_low["sem"] == "green" and 0 < d_mid_low["frac"] < 1 / 3)
d_at_cons = strategy.decay_indicator(low, low * 3)
check("spot==3x → frac==1/3 (boundary; output is rounded to 4dp)", abs(d_at_cons["frac"] - 1 / 3) < 1e-4)
d_mid_center = strategy.decay_indicator(low, low * 4)   # between 3x and ~4.8x center
check("between conservative(3x) and center(~4.8x) → conservative_center/amber",
      d_mid_center["zone"] == "conservative_center" and d_mid_center["sem"] == "amber" and 1 / 3 < d_mid_center["frac"] < 2 / 3)
d_mid_agg = strategy.decay_indicator(low, low * 6)   # between center(~4.8x) and 7x
check("between center and aggressive(7x) → center_aggressive/red",
      d_mid_agg["zone"] == "center_aggressive" and d_mid_agg["sem"] == "red" and 2 / 3 < d_mid_agg["frac"] < 1.0)
d_over = strategy.decay_indicator(low, low * 9)   # beyond 7x
check("beyond aggressive(7x) → above_aggressive/rust, frac clamped to 1.0",
      d_over["zone"] == "above_aggressive" and d_over["sem"] == "rust" and d_over["frac"] == 1.0)


print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
