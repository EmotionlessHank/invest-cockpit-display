"""
test_strategy_mstr.py — Test suite for strategy_mstr.py: MSTR core-position deadline-paced DCA.

Standalone; no third-party dependencies.
Run with: PYTHONPATH=app python3 test_strategy_mstr.py (already configured in run-tests.sh). All passing → exit 0.
All tests inject now/window_end; no dependency on the real current date.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import strategy
from strategy_mstr import (
    CORE_BUDGET_USD_DEFAULT,
    MAX_SINGLE_ORDER_USD,
    core_invested_usd,
    decide_weekly_mstr,
    mstr_tilt,
)

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


WE = date(2026, 9, 12)
NOW = dt(2026, 6, 15)
WEEKS = strategy.remaining_weeks(NOW, WE)  # 13


# ══ 1. base = pool/weeks correct ══════════════════════════════════════════════
print("▶ t01_base: base = remaining core pool ÷ remaining weeks")
# Default: core_invested=4434.43, core_budget=14000 → pool=9565.57; mnav 0.81 → tilt 1.0
d = decide_weekly_mstr(4434.43, 14000, 0.81, now=NOW, window_end=WE)
base_exp = (14000 - 4434.43) / WEEKS
check(f"base ≈ ${base_exp:,.0f} · mnav 0.81 tilt 1.0", abs(d.amount_usd - base_exp) < 1.0
      and d.action == "BUY" and d.tilt == 1.0, f"{d.amount_usd} vs {base_exp}")
check("weeks=13 recorded correctly", d.weeks == 13, str(d.weeks))


# ══ 2. mstr_tilt per-mNAV-tier ════════════════════════════════════════════════
print("▶ t02_tilt: per-mNAV-tier tilt coefficients")
for mnav, tilt in [(0.90, 0.7), (0.85, 0.7), (0.81, 1.0), (0.78, 1.0),
                   (0.75, 1.3), (0.70, 1.3), (0.65, 1.6), (0.50, 1.6)]:
    check(f"mnav {mnav} → tilt {tilt}", mstr_tilt(mnav) == tilt, str(mstr_tilt(mnav)))
# Verify tilt flows through to the decision amount
pool = 14000 - 4000
base = pool / WEEKS
for mnav, tilt in [(0.90, 0.7), (0.81, 1.0), (0.75, 1.3), (0.65, 1.6)]:
    d = decide_weekly_mstr(4000, 14000, mnav, now=NOW, window_end=WE)
    exp = min(base * tilt, MAX_SINGLE_ORDER_USD)
    check(f"mnav {mnav} → amount ≈ ${exp:,.0f}", abs(d.amount_usd - exp) < 1.0, f"{d.amount_usd} vs {exp}")


# ══ 3. Black Swan reserve excluded from pool ══════════════════════════════════
print("▶ t03_swan_isolation: Black Swan $14k never enters the core pool")
# core_budget default 14000; pool is only computed against core_budget
d = decide_weekly_mstr(0, CORE_BUDGET_USD_DEFAULT, 0.81, now=NOW, window_end=WE)
check("pool = core position $14k (Black Swan not included)", abs(d.pool_usd - 14000) < 0.01, str(d.pool_usd))
# Even at the final week force-fill, amount never exceeds the core pool (Black Swan untouched)
d = decide_weekly_mstr(0, CORE_BUDGET_USD_DEFAULT, 0.81, now=dt(2026, 9, 7), window_end=WE)
check("final week force-fill ≤ core position $14k (Black Swan not touched)", d.amount_usd <= 14000 + 0.01, str(d.amount_usd))


# ══ 4. Final week force-fill (large pool → capped at single-order limit) ══════
print("▶ t04_last_week: final window week force-fill (large pool → capped at single-order limit)")
# Pool $9565.57 > single-order cap $1500 → force-fill intent capped by single-order limit
d = decide_weekly_mstr(4434.43, 14000, 0.81, now=dt(2026, 9, 7), window_end=WE)
check("final week force-fill but capped at single-order limit $1500", d.amount_usd == MAX_SINGLE_ORDER_USD, str(d.amount_usd))
# Post-window (09-20 > 09-12) → REVIEW, not perpetual force-fill
d = decide_weekly_mstr(4434.43, 14000, 0.81, now=dt(2026, 9, 20), window_end=WE)
check("post-window → REVIEW (no automatic force-fill)", d.action == "REVIEW" and d.amount_usd == 0, str(d))
# Small pool below the single-order cap → complete force-fill to zero
d = decide_weekly_mstr(13000, 14000, 0.81, now=dt(2026, 9, 7), window_end=WE)
check("small pool: final week complete force-fill = pool $1000", abs(d.amount_usd - 1000) < 0.01, str(d.amount_usd))


# ══ 5. Invalid mNAV → DEGRADED ════════════════════════════════════════════════
print("▶ t05_mnav_degraded: invalid mNAV → DEGRADED, refuse calculation")
for bad in (None, 0, -0.5, 5.1, 99):
    d = decide_weekly_mstr(4000, 14000, bad, now=NOW, window_end=WE)
    check(f"mnav={bad} → DEGRADED, refuse calculation", d.action == "DEGRADED" and d.amount_usd == 0, str(d.action))


# ══ 6. Single-order cap / pool cap / fully deployed SKIP ══════════════════════
print("▶ t06_guards: single-order cap / pool cap / fully deployed SKIP")
# Final week large amount → capped at single-order limit
d = decide_weekly_mstr(0, 14000, 0.65, now=dt(2026, 9, 7), window_end=WE)
check("final week large amount → capped at single-order limit $1500", d.amount_usd == MAX_SINGLE_ORDER_USD, str(d.amount_usd))
# Core position fully deployed → SKIP
d = decide_weekly_mstr(14000, 14000, 0.81, now=NOW, window_end=WE)
check("core position fully deployed → SKIP (pool=0)", d.action == "SKIP" and d.amount_usd == 0, str(d))


# ══ Regression (CRITICAL①): Black Swan fill does not leak into core DCA pool ══
print("▶ t_reg_critical1_swan_isolation: Black Swan fill does not leak into core DCA pool; standalone counted as core")
# Ledger: core T1 fills total $4,434.43; core_invested should count only core-position fills + standalone.
# Before fix: using invested_usd included Black Swan fills, double-counting them into the core pool.
_st_core = {"rounds": [
    {"id": "T1", "layer": "core", "fills": [
        {"usd": 1914.43, "price": 130, "shares": 14.5},
        {"usd": 2520.0, "price": 118, "shares": 21.4}]},
    {"id": "T2", "layer": "core", "fills": []},
    # Black Swan A: $3,500 fill — before fix, using invested_usd would count this against the core pool
    {"id": "A", "layer": "swan", "fills": [{"usd": 3500.0, "price": 90, "shares": 39}]},
]}
ci_core = core_invested_usd(_st_core)
check("core_invested = core-position fills $4434.43 (Black Swan $3500 not counted)",
      abs(ci_core - 4434.43) < 0.01, str(ci_core))
# Same ledger: core pool is still 14000 − 4434.43; Black Swan fill has no effect
d_swan = decide_weekly_mstr(ci_core, 14000, 0.81, now=NOW, window_end=WE)
check("Black Swan fill does not affect core pool (still = 14000 − core fills)",
      abs(d_swan.pool_usd - (14000 - 4434.43)) < 0.01, str(d_swan.pool_usd))
# Add a standalone fill (core DCA add-on: $500) → core_invested grows, pool shrinks accordingly
_st_sa = {**_st_core, "standalone_fills": [{"usd": 500.0, "price": 110, "shares": 4.5}]}
ci_sa = core_invested_usd(_st_sa)
check("standalone $500 counted as core → core_invested = $4934.43", abs(ci_sa - 4934.43) < 0.01, str(ci_sa))
d_sa = decide_weekly_mstr(ci_sa, 14000, 0.81, now=NOW, window_end=WE)
check("standalone included → core pool shrinks accordingly = 14000 − 4934.43",
      abs(d_sa.pool_usd - (14000 - 4934.43)) < 0.01, str(d_sa.pool_usd))

# ══ Regression: post-window → REVIEW (not perpetual force-fill) ════════════════
print("▶ t_reg_window_past_review: now=2026-09-20 (post-window) → REVIEW, not full-pool BUY")
d = decide_weekly_mstr(4434.43, 14000, 0.81, now=dt(2026, 9, 20), window_end=WE)
check("post-window: action=REVIEW · amount=0", d.action == "REVIEW" and d.amount_usd == 0, str(d))
check("post-window: reason contains 'Window has ended' pending manual review",
      any("Window has ended" in r for r in d.reasons), str(d.reasons))
# Boundary check: last window week (not yet past) still generates BUY
d = decide_weekly_mstr(4434.43, 14000, 0.81, now=dt(2026, 9, 7), window_end=WE)
check("last window week (not past) still BUY (boundary check, not mistakenly REVIEW)", d.action == "BUY", str(d.action))

# ══ Regression: SANE_MNAV_MIN = 0.2 → mnav 0.05 treated as dirty data ════════
print("▶ t_reg_mnav_min_guard: mnav=0.05 < 0.2 → DEGRADED (dirty data, not treated as deep discount)")
d = decide_weekly_mstr(4000, 14000, 0.05, now=NOW, window_end=WE)
check("mnav 0.05 → DEGRADED (dirty data, not treated as deep discount)", d.action == "DEGRADED" and d.amount_usd == 0, str(d))
# Boundary: exactly 0.2 still treated as dirty (open lower bound); 0.21 is normal
check("mnav 0.20 (lower bound) → DEGRADED (open interval)", decide_weekly_mstr(4000, 14000, 0.20, now=NOW, window_end=WE).action == "DEGRADED")
check("mnav 0.21 (> lower bound) → BUY, normal", decide_weekly_mstr(4000, 14000, 0.21, now=NOW, window_end=WE).action == "BUY")


print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
