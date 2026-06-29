"""
strategy_mstr.py — Decision engine for the MSTR core-position deadline-paced DCA
                   (pure functions; no network calls, no API keys, no side effects).

Strategy reference: MSTR core position DCA plan (golden window buy-up)
  · Core position $14k: self-correcting pace formula —
    (remaining pool ÷ remaining weeks) × mNAV tilt;
    force-fills the full pool at the final window week.
  · Black Swan A/B $14k: completely excluded from the DCA pool; remains event-triggered
    (mNAV ≤ 0.55x / ≤ 0.50x / ≤ 0.40x); DCA never touches this allocation.

Design principles (aligned with strategy.py):
  - This file only computes; it never acts: it accepts a snapshot
    (deployed/budget/mNAV/time) and outputs "how much to buy for the core position this week and why."
  - All pure functions; no yahoo / telegram imports — fully offline-testable (see test_strategy_mstr.py).
  - Reuses strategy.remaining_weeks as the single source of truth for window week counting
    (same convention as BTC, preventing paired-path drift).
  - Amount units: MSTR uses USD $ (core-position budget: $14,000).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import strategy  # Single source: remaining_weeks (same directory; distributed with app modules)


# ── Risk control hard constraints ─────────────────────────────────────────────
CORE_BUDGET_USD_DEFAULT = 14_000.0   # Core-position budget (DCA only touches this; Black Swan $14k is separate)
SWAN_BUDGET_USD = 14_000.0           # Black Swan A/B reserve (excluded from DCA pool; kept for reference)
MAX_SINGLE_ORDER_USD = 1_500.0       # Single-order size cap (same scale as BTC cap)

# mNAV sanity guard: ≤ 0.2 or > 5 treated as bad data / dirty value → refuse calculation.
# Lower bound 0.2 (not 0): mNAV < 0.2 has never occurred in MSTR history; if seen, almost
# certainly a data source failure or bad feed. Passing it through would be misread as the
# deepest discount and trigger maximum buys — so it is treated as dirty data → DEGRADED.
SANE_MNAV_MIN = 0.2
SANE_MNAV_MAX = 5.0

# Core-position limit-order leg display order (deep-discount overlay reference only).
# Single source: both the dashboard card and the weekly DCA suggestion share this constant;
# never hard-code the order in multiple places.
OVERLAY_LEG_ORDER = ("T1-c", "T2-a", "T2-b", "T2-c")


def next_open_core_leg(state: dict) -> str | None:
    """Find the first core-position limit-order leg (by OVERLAY_LEG_ORDER) whose status is not
    'filled'. Returns None if all legs are filled.
    Serves as a 'deep-discount overlay' reference only; does not instruct DCA to book against it
    (DCA runs as a standalone core add-on)."""
    by_label = {
        b.get("label"): b
        for r in state.get("rounds", []) if r.get("layer") == "core"
        for b in r.get("plan", [])
    }
    for lab in OVERLAY_LEG_ORDER:
        b = by_label.get(lab)
        if b is not None and b.get("status") != "filled":
            return lab
    return None


def core_invested_usd(state: dict) -> float:
    """Single source of truth for core-position deployed capital ($) =
    core-position round fills + standalone_fills (core DCA add-ons).
    Black Swan fills are never included.

    Root cause of the critical double-count bug: the ledger top-level `invested_usd` =
    core + black swan + standalone total. Passing it as core_invested to decide_weekly_mstr
    double-counts black swan fills, artificially shrinking the core DCA pool.
    This helper counts only "capital allocated to the core position":
      · rounds where layer == "core" (T1/T2) fills.usd
      · top-level standalone_fills.usd (standalone = core DCA add-ons; allocated to core under plan A)
    Black Swan A/B round fills are excluded.
    Shared by both the weekly advisor and the dashboard to eliminate paired-path drift.
    """
    core = sum(
        f.get("usd", 0)
        for r in (state.get("rounds") or [])
        if r.get("layer") == "core"
        for f in (r.get("fills") or [])
    )
    standalone = sum(f.get("usd", 0) for f in (state.get("standalone_fills") or []))
    return round(core + standalone, 2)


# mNAV tilt coefficients (mNAV-driven self-correction; deeper discount → larger buy).
# ≥ 0.85x → 0.7 (premium zone, invest less) / 0.78–0.85x → 1.0 (current sticky zone, baseline)
# / 0.70–0.78x → 1.3 / < 0.70x → 1.6.
def mstr_tilt(mnav: float) -> float:
    """mNAV → tilt coefficient. Boundary: upper bound belongs to the tier above
    (≥ 0.85 → 0.7; ≥ 0.78 → 1.0; ≥ 0.70 → 1.3; otherwise 1.6)."""
    if mnav >= 0.85:
        return 0.7
    if mnav >= 0.78:
        return 1.0
    if mnav >= 0.70:
        return 1.3
    return 1.6


def mstr_tilt_label(mnav: float) -> str:
    """Human-readable label for the mNAV tier (used in notification text)."""
    if mnav >= 0.85:
        return "premium zone"
    if mnav >= 0.78:
        return "baseline"
    if mnav >= 0.70:
        return "improving discount"
    return "deep discount"


@dataclass(frozen=True)
class MstrWeeklyDecision:
    """Weekly MSTR core-position DCA decision result (pure computation; no orders placed)."""
    action: str                    # "BUY" | "SKIP" | "DEGRADED" | "REVIEW" (post-window)
    amount_usd: float              # Final planned amount ($, capped at core pool remaining + single-order limit)
    base_usd: float                # Pace base = core pool remaining ÷ remaining weeks (pre-tilt)
    weeks: int                     # Remaining window weeks
    tilt: float                    # mNAV tilt coefficient
    mnav: float | None             # Input mNAV (None when invalid)
    pool_usd: float                # Core-position remaining pool
    reasons: list[str] = field(default_factory=list)


def decide_weekly_mstr(
    core_invested_usd: float,
    core_budget_usd: float = CORE_BUDGET_USD_DEFAULT,
    mnav: float | None = None,
    now: datetime | None = None,
    window_end: date | None = None,
    max_single_order_usd: float = MAX_SINGLE_ORDER_USD,
) -> MstrWeeklyDecision:
    """Core MSTR core-position deadline-paced DCA decision.

    Formula:
      pool  = max(0, core_budget - core_invested)        # core position only; Black Swan $14k never counted
      weeks = remaining_weeks(now, window_end)           # reuses strategy single source; floor 1 (no div-by-zero)
      base  = pool / weeks
      amount = base × mstr_tilt(mnav)                    # mNAV tilt (deeper discount → larger buy)
      final week (weeks ≤ 1) → amount = pool (force-fill to zero)

    Guards: invalid mNAV (≤ 0.2 or > 5) → DEGRADED, refuse calculation; core-position pool cap
    applied; single-order cap applied.
    Black Swan isolation: pool is computed from the core budget only; the Black Swan allocation is
    never touched.
    """
    reasons: list[str] = []

    # —— Guard: invalid mNAV → refuse calculation (avoid emitting bad advice) ——
    if mnav is None or not (SANE_MNAV_MIN < mnav <= SANE_MNAV_MAX):
        shown = "missing" if mnav is None else f"{mnav}"
        reasons.append(
            f"⚠️ mNAV {shown} is out of valid range (expected ({SANE_MNAV_MIN}, {SANE_MNAV_MAX}]);"
            f" calculation rejected pending manual verification of the mNAV data source."
        )
        return MstrWeeklyDecision(
            action="DEGRADED", amount_usd=0.0, base_usd=0.0, weeks=0,
            tilt=0.0, mnav=None, pool_usd=0.0, reasons=reasons,
        )

    # —— Core-position remaining pool (Black Swan isolation: computed only from core_budget) ——
    pool = max(0.0, core_budget_usd - core_invested_usd)

    # —— Explicit post-window branch (window already past) ——
    # After window_end, remaining_weeks permanently returns 1, causing the old logic to
    # force-fill the remaining pool every week indefinitely — an infinite bad-advice loop.
    # Post-window: switch to REVIEW; do not auto-force-fill.
    # Remaining $ left for manual review. Today determined via now injection (no real-clock dependency).
    eff_now = now or datetime.now().astimezone()
    eff_window_end = window_end or strategy._golden_window_end()
    if eff_now.date() > eff_window_end:
        reasons.append(
            f"Window has ended (deadline: {eff_window_end.isoformat()}). "
            f"Remaining core-position pool ${pool:,.0f} is pending manual review; no automatic force-fill."
        )
        return MstrWeeklyDecision(
            action="REVIEW", amount_usd=0.0, base_usd=0.0, weeks=1,
            tilt=mstr_tilt(mnav), mnav=mnav, pool_usd=round(pool, 2), reasons=reasons,
        )

    weeks = strategy.remaining_weeks(now, window_end)
    base = pool / weeks
    tilt = mstr_tilt(mnav)
    amount = base * tilt
    reasons.append(
        f"Deadline-paced DCA: core-position pool ${pool:,.0f} ÷ {weeks} weeks remaining"
        f" = base ${base:,.0f}/week (Black Swan ${SWAN_BUDGET_USD:,.0f} excluded from pool)."
    )
    reasons.append(
        f"mNAV {mnav:.2f}x → \"{mstr_tilt_label(mnav)}\" → tilt ×{tilt}"
        f" (base ${base:,.0f} × {tilt} = ${amount:,.0f})."
    )

    # Final window week (remaining ≤ 1): force-fill the full remaining pool
    if weeks <= 1:
        amount = pool
        reasons.append(f"Final window week: force-filling remaining core-position pool ${pool:,.0f}.")

    # Core-position pool cap (never exceed core budget)
    if amount > pool:
        reasons.append(f"Guard: this week's plan ${amount:,.0f} exceeds the remaining core-position pool ${pool:,.0f}; capped at remaining pool.")
        amount = pool

    # Single-order cap
    if amount > max_single_order_usd:
        reasons.append(f"Risk control: single-order cap ${max_single_order_usd:,.0f}; capping ${amount:,.0f} at the limit.")
        amount = max_single_order_usd

    amount = round(amount, 2)

    if amount <= 0:
        reasons.append(f"Core-position pool ${pool:,.0f} fully deployed; no order this week.")
        return MstrWeeklyDecision(
            action="SKIP", amount_usd=0.0, base_usd=round(base, 2), weeks=weeks,
            tilt=tilt, mnav=mnav, pool_usd=round(pool, 2), reasons=reasons,
        )

    reasons.append(f"✅ Weekly MSTR core-position DCA suggestion: ${amount:,.2f}.")
    return MstrWeeklyDecision(
        action="BUY", amount_usd=amount, base_usd=round(base, 2), weeks=weeks,
        tilt=tilt, mnav=mnav, pool_usd=round(pool, 2), reasons=reasons,
    )
