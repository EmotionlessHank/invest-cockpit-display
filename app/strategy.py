"""
strategy.py — Decision engine for the BTC dollar-cost-averaging (DCA) semi-automated system
             (pure functions; no network calls, no API keys, no side effects).

Strategy reference: BTC Dynamic Investment Plan v3.0
  · Section 4   — Track A: five DCA tiers mapped to BTC spot price
  · Section 3.3 — Three linkage rules (dip fill → tier downgrade / budget conservation / near-cap pause)
  · Execution flow steps 1–4 (tier assignment → downgrade → cap pause → balance guard)

Design principles:
  - This file only computes; it never acts: it accepts a snapshot (price/fills/invested/balance)
    and outputs "how much to buy this week and why."
  - All pure functions; no binance / telegram imports — fully offline-testable (see test_strategy.py).
  - Amount units: strategy tiers use RMB ¥ (aligned with source document); orders are placed in USDT.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import IntEnum


# ── Risk control hard constraints ────────────────────────────────────────────
TOTAL_BUDGET_RMB = 200_000          # Hard cap on BTC position; never exceeded
PAUSE_THRESHOLD_RMB = 180_000       # Cumulative ≥ 90% deployed → pause DCA, preserve dip-buying reserves (rule ③)
MAX_SINGLE_ORDER_USDT = 1_500.0     # Single-order size cap
FALLBACK_RATE = 7.0                 # Fallback RMB/USDT rate (used when live rate is unavailable; USDT is canonical)

# Price sanity guard: reject orders if BTC price falls outside this range
# (defends against bad data / API misfires)
SANE_PRICE_MIN_USD = 10_000.0
SANE_PRICE_MAX_USD = 250_000.0


class Tier(IntEnum):
    """Five DCA tiers for Track A (IntEnum ordinal equals tier weight, enabling simple downgrade arithmetic).

    Higher value = heavier tier; downgrade = ordinal − 1.
    """
    PAUSE = 0     # Pause (> $80K high zone / near budget cap)
    CAUTIOUS = 1  # Cautious ¥3,000/week ($70–80K)
    HEAVIER = 2   # Heavier ¥5,500/week ($58–70K, green-line core zone)
    DOUBLE = 3    # Double ¥7,500/week ($50–58K, below 200 WMA)
    ENHANCED = 4  # Enhanced ¥10,000/week (< $50K, extreme opportunity)


# Tier → planned weekly amount (¥).
TIER_AMOUNT_RMB: dict[Tier, int] = {
    Tier.PAUSE: 0,
    Tier.CAUTIOUS: 3_000,
    Tier.HEAVIER: 5_500,
    Tier.DOUBLE: 7_500,
    Tier.ENHANCED: 10_000,
}

# Tier → display label
TIER_LABEL: dict[Tier, str] = {
    Tier.PAUSE: "Pause",
    Tier.CAUTIOUS: "Cautious",
    Tier.HEAVIER: "Heavier",
    Tier.DOUBLE: "Double",
    Tier.ENHANCED: "Enhanced",
}

# Downgrade floor: rule ② downgrades stop at CAUTIOUS (always maintain exposure; PAUSE is only
# triggered by "high zone > $80K" or "near budget cap", never by downgrading).
# Source chain: Double → Heavier → Cautious
DOWNGRADE_FLOOR = Tier.CAUTIOUS


# ── Deadline-paced DCA: golden window buy-up ─────────────────────────────────
# Fixed tiers (CAUTIOUS ¥3k…) do not guarantee the window is fully deployed on schedule.
# Replaced by a self-correcting pace formula: (remaining pool ÷ remaining weeks) × tilt.
# At the final week (remaining = 1) the full remaining pool is forced in.
# Pattern: constant + env override (not ledger state; can be committed to git).
GOLDEN_WINDOW_END_DEFAULT = "2026-09-12"   # Golden window end date (deploy in full within 2–3 months)


def _golden_window_end() -> date:
    """Golden window end date. Overridable via env var GOLDEN_WINDOW_END (YYYY-MM-DD format).
    Invalid / missing value → fail-closed fallback to the default constant (logged to stderr).
    Pattern mirrors the env-override approach for frozen tiers: tests/runtime use the env var;
    the in-repo constant is the safe dead value."""
    raw = os.environ.get("GOLDEN_WINDOW_END", "")
    try:
        return date.fromisoformat(raw) if raw else date.fromisoformat(GOLDEN_WINDOW_END_DEFAULT)
    except (TypeError, ValueError):
        # Invalid env var (e.g. typo like 2026-9-12) → fail-closed fallback to default, but log to
        # stderr so that a window-end override with a bad format is not silently ignored,
        # causing incorrect pacing with no visibility.
        print(
            f"⚠️ GOLDEN_WINDOW_END={raw!r} is not a valid ISO date (expected YYYY-MM-DD); "
            f"falling back to default {GOLDEN_WINDOW_END_DEFAULT}.",
            file=sys.stderr,
        )
        return date.fromisoformat(GOLDEN_WINDOW_END_DEFAULT)


# BTC tilt coefficients (price-driven self-correction; mean ≈ 1 preserves on-schedule full deployment).
# Pause 0.5 (invest at half rate in high zone rather than skipping entirely, to avoid missing a V-reversal)
# / Cautious 0.8 / Heavier 1.0 (baseline) / Double 1.3 / Enhanced 1.6.
BTC_TILT: dict[Tier, float] = {
    Tier.PAUSE: 0.5,
    Tier.CAUTIOUS: 0.8,
    Tier.HEAVIER: 1.0,
    Tier.DOUBLE: 1.3,
    Tier.ENHANCED: 1.6,
}


def remaining_weeks(now: datetime | None = None, window_end: date | None = None) -> int:
    """Weeks remaining in the window: count of Mondays from this week's Monday to the window-end
    week's Monday, plus 1. Floor is 1 (never divides by zero).

    Convention: Monday-anchored ISO weeks. If window_end ≤ today (last week or already past) → returns 1,
    triggering the "force full deployment" branch without ever dividing by zero.
    now / window_end are injectable for deterministic testing.
    """
    now = now or datetime.now().astimezone()
    today = now.date()
    window_end = window_end or _golden_window_end()
    this_monday = today - timedelta(days=today.weekday())
    end_monday = window_end - timedelta(days=window_end.weekday())
    weeks = (end_monday - this_monday).days // 7 + 1
    return max(1, weeks)


# ── DCA tier-edge fixed dead values (fail-closed fallback baseline + backward compatibility) ──────
# High-zone tiers (Pause/Cautious) have no cost-basis anchor and are always fixed.
# Mid/low tier edges (Heavier|Double = 58K = 200 WMA; Double|Enhanced = 50K = realized price) were
# originally hard-coded snapshots; they are now derived dynamically from tier_anchors; these dead
# values serve as the fail-closed fallback when anchors are unavailable.
DCA_EDGE_PAUSE_CAUTIOUS = 80_000.0   # Pause|Cautious edge (fixed high-zone, no anchor)
DCA_EDGE_CAUTIOUS_HEAVIER = 70_000.0  # Cautious|Heavier edge (fixed high-zone)
DCA_EDGE_HEAVIER_DOUBLE = 58_000.0    # Heavier|Double edge (dead-value snapshot = green-line lower bound; dynamic = wma200)
DCA_EDGE_DOUBLE_ENHANCED = 50_000.0   # Double|Enhanced edge (dead-value snapshot = below realized price; dynamic = on-chain realized_price)


def price_to_tier(btc_price_usd: float, anchors: dict | None = None) -> Tier:
    """Map BTC spot price to the base DCA tier (before linkage rules apply).

    Boundary convention: upper bound belongs to the tier above
    (e.g. $80,000 exactly → Pause; $70,000 exactly → Cautious).

    When valid anchors are provided (output of tier_anchors()), dynamic DCA edges are used
    (Heavier|Double = 200 WMA; Double|Enhanced = realized price). If anchors are None or unavailable
    (degraded / out-of-range / non-monotonic) → fail-closed fallback to fixed dead values 58K/50K,
    ensuring tier assignment has a single authoritative source while strictly rejecting bad anchor data.
    """
    edge_hd = DCA_EDGE_HEAVIER_DOUBLE
    edge_de = DCA_EDGE_DOUBLE_ENHANCED
    edges = (anchors or {}).get("dca_edges") if anchors else None
    if edges:
        a_hd = edges.get("heavier_double")
        a_de = edges.get("double_enhanced")
        # Dynamic edges are only adopted when both are present and strictly monotonic with the fixed
        # high-zone edges (fail-closed)
        if (
            isinstance(a_hd, (int, float)) and isinstance(a_de, (int, float))
            and DCA_EDGE_CAUTIOUS_HEAVIER > a_hd > a_de > 0
        ):
            edge_hd, edge_de = float(a_hd), float(a_de)

    if btc_price_usd >= DCA_EDGE_PAUSE_CAUTIOUS:
        return Tier.PAUSE
    if btc_price_usd >= DCA_EDGE_CAUTIOUS_HEAVIER:
        return Tier.CAUTIOUS
    if btc_price_usd >= edge_hd:
        return Tier.HEAVIER
    if btc_price_usd >= edge_de:
        return Tier.DOUBLE
    return Tier.ENHANCED


# ── Dip anchor valid range + cross-tier spacing ───────────────────────────────────────────────────
# Second-layer guard: layer 1 already guards individual anchors (range + jump check in
# onchain_signals._anchor_guard); layer 2 here re-validates anchor ranges used for derivation —
# out-of-range anchors are treated as unavailable → fail-closed degradation.
# Range matches onchain_signals.ANCHOR_RANGE; intentionally kept separate to catch drift if L1 changes.
TIER_ANCHOR_RANGE: dict[str, tuple[float, float]] = {
    "wma200": (20_000.0, 150_000.0),
    "realized_price": (10_000.0, 150_000.0),
    "green_line": (20_000.0, 150_000.0),
}
# Calibrated per-tier leg counts and anchor offsets (replaces old uniform $2,000/tier three-leg default).
# B3 anchors on green-line core (3 legs); C1 anchors on wma200 (below 200 WMA, 2 legs);
# C2 anchors on realized price (2 legs). Offsets in USD relative to each anchor: trigger[i] = anchor + offset[i].
DIP_LEG_OFFSETS: dict[str, list[float]] = {
    "B3": [0.0, -2_000.0, -4_000.0],     # 3 legs, anchor: green-line core
    "C1": [-4_000.0, -6_000.0],          # 2 legs, anchor: wma200 (below 200 WMA)
    "C2": [1_000.0, -2_000.0],           # 2 legs, anchor: realized price
}

# B1/B2 already filled: retain document dead values (dip filled, no longer anchored to drift).
DIP_B1_FILLED = [70_000.0, 68_000.0, 66_000.0]
DIP_B2_FILLED = [65_000.0, 63_000.0, 61_000.0]


def _anchor_ok(val, key: str) -> bool:
    """Anchor validity check: numeric, positive, within the valid range.
    Out-of-range / missing / zero / negative → False (fail-closed)."""
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return False
    if val <= 0:
        return False
    lo, hi = TIER_ANCHOR_RANGE.get(key, (0.0, float("inf")))
    return lo <= float(val) <= hi


def tier_anchors(cache: dict, manual_green: float | None = None) -> dict:
    """Derive DCA tier edges and dip trigger prices from on-chain anchors
    (pure function; never writes state).

    Single source of truth: DCA tier edges and dip trigger prices share the same anchor set
    (wma200 / realized_price / green_line), eliminating dual hard-coded drift.

    Anchor mapping (faithful to the authoritative strategy document):
      · DCA edges: Heavier|Double = wma200; Double|Enhanced = realized_price (on-chain market
        realized price; Pause|Cautious = $80K and Cautious|Heavier = $70K are always fixed)
      · Dip B3 green-line tier = green + DIP_LEG_OFFSETS["B3"] (3 legs [0, -2000, -4000];
        green = manual_green, as the data provider carries no age-band series for this metric)
      · Dip C1 = wma200 + DIP_LEG_OFFSETS["C1"] (2 legs [-4000, -6000], below 200 WMA)
      · Dip C2 = realized_price + DIP_LEG_OFFSETS["C2"] (2 legs [1000, -2000], around realized price)
      · B1/B2 already filled → retain document dead values (not re-anchored)

    Fail-closed rule: if any required anchor is missing / zero / negative / out-of-range / suspect →
    do not emit a malformed price for that tier; degrade and mark degraded.
    DCA edges missing → ok=False (price_to_tier will fall back to fixed dead values automatically).
    Never return 0 or None as a price.

    Returns dict:
      {
        "dca_edges": {"heavier_double": float|None, "double_enhanced": float|None},
        "dip_triggers": {"B1": [...], "B2": [...], "B3": [...]|None, "C1": [...]|None, "C2": [...]|None},
        "ok": bool,            # True when both DCA anchor values are present and monotonic
        "degraded": [str, ...] # Degradation reasons (which anchors were unavailable); non-empty on fail-closed
      }
    """
    brk = ((cache or {}).get("brk") or {}).get("data") or {}
    degraded: list[str] = []

    def _take(key: str) -> float | None:
        """Retrieve anchor: suspect flag or range/type check failure → None, record degradation reason."""
        if brk.get(f"{key}_suspect"):
            degraded.append(f"{key}_suspect")
            return None
        val = brk.get(key)
        if not _anchor_ok(val, key):
            degraded.append(f"{key}_unavailable")
            return None
        return float(val)

    wma200 = _take("wma200")
    realized = _take("realized_price")

    # Green-line: the data provider carries no 6m–10y age-band series (green_line is always null);
    # relies on manual_green passed in from the caller.
    green = None
    if _anchor_ok(manual_green, "green_line"):
        green = float(manual_green)
    else:
        degraded.append("green_line_unavailable")

    # —— DCA tier edges (single source of truth) ——
    # Monotonicity rule: wma200 > realized (200 WMA must be above realized price) for self-consistency;
    # if both are present but violate this, neither is adopted.
    dca_ok = wma200 is not None and realized is not None and wma200 > realized
    if (wma200 is not None and realized is not None) and not dca_ok:
        degraded.append("dca_edges_non_monotonic")
    dca_edges = {
        "heavier_double": wma200 if dca_ok else None,
        "double_enhanced": realized if dca_ok else None,
    }

    # —— Dip trigger prices (calibrated: per-tier leg counts + anchor offsets) ——
    # ② Round to integer USD (removes decimal noise; anchors themselves may carry decimals;
    #    aligns with display convention).
    def _legs(base: float | None, code: str) -> list[float] | None:
        """Derive trigger prices via DIP_LEG_OFFSETS[code]; returns None when anchor is unavailable (fail-closed)."""
        if base is None:
            return None
        return [float(round(base + off)) for off in DIP_LEG_OFFSETS[code]]

    dip_triggers = {
        "B1": list(DIP_B1_FILLED),   # Already filled: dead values
        "B2": list(DIP_B2_FILLED),   # Already filled: dead values
        "B3": _legs(green, "B3"),    # Green-line core dip tier (3 legs)
        "C1": _legs(wma200, "C1"),   # Deep-dip: below 200 WMA (2 legs)
        "C2": _legs(realized, "C2"), # Deep-dip: around realized price (2 legs)
    }

    # ① Cross-tier monotonicity gate: the top price of each dynamic dip tier (B3/C1/C2) must be
    # strictly decreasing — B3.top > C1.top > C2.top. If a tier's top price ≥ the previous tier's
    # top, that tier is inverted → fail-closed (set to None + record in degraded).
    # Makes the claimed tier ordering an invariant enforced in code.
    prev_top: float | None = None
    for code in ("B3", "C1", "C2"):
        legs = dip_triggers.get(code)
        if not legs:
            continue  # Already fail-closed (None); skip without updating the reference
        top = max(legs)
        if prev_top is not None and top >= prev_top:
            dip_triggers[code] = None
            degraded.append(f"{code}_dip_non_monotonic")
            continue  # Inverted tier discarded; do not update reference (next tier still compares against last valid)
        prev_top = top

    return {
        "dca_edges": dca_edges,
        "dip_triggers": dip_triggers,
        "ok": dca_ok,
        "degraded": degraded,
    }


def dip_filled_last_7d(state: dict, now: datetime | None = None) -> bool:
    """Returns True if any dip-buy fill occurred in the past 7 days (rule ② downgrade criterion).
    Scans all tiers[].fills[].at fields (including partial fills); does not check tier.status —
    status reflects whether a tier is fully filled, not whether a recent fill occurred.
    Single source shared by both the Telegram reminder and the dashboard to prevent path drift.
    The 'at' field accepts both full ISO timestamps and bare date strings (bare values treated as
    local time); unparseable entries are silently skipped rather than causing a false positive.
    now is injectable (for tests); defaults to current local time.
    """
    now = now or datetime.now().astimezone()
    for t in state.get("tiers", []):
        for f in t.get("fills", []):
            try:
                d = datetime.fromisoformat(str(f.get("at", "")))
            except (TypeError, ValueError):
                continue
            d = d.astimezone() if d.tzinfo else d.replace(tzinfo=now.tzinfo)
            if (now - d).days < 7:
                return True
    return False


def same_iso_week(ts_iso: str, now: datetime | None = None) -> bool:
    """Returns True if ts_iso and now fall in the same ISO week (Monday-anchored, local timezone).
    Used as the criterion for "this week's DCA has already been executed."
    Missing / unparseable ts → returns False (treats as not yet done; prefer over-alerting to missing alerts).
    now is injectable for testing."""
    now = now or datetime.now().astimezone()
    try:
        d = datetime.fromisoformat(str(ts_iso))
    except (TypeError, ValueError):
        return False
    d = d.astimezone(now.tzinfo) if d.tzinfo else d.replace(tzinfo=now.tzinfo)
    return d.isocalendar()[:2] == now.isocalendar()[:2]


def downgrade(tier: Tier) -> Tier:
    """Rule ②: dip fill within the past 7 days → drop the DCA tier by one step (floor = Cautious).

    The Pause tier does not participate in downgrading (already at 0).
    """
    if tier <= DOWNGRADE_FLOOR:
        return tier
    return Tier(max(int(DOWNGRADE_FLOOR), int(tier) - 1))


# ── Decision inputs / outputs ──────────────────────────────────────────────────
@dataclass(frozen=True)
class WeeklyContext:
    """All external facts required for a single weekly DCA decision
    (populated by dca.py from exchange data / state)."""
    btc_price_usd: float          # BTC/USDT spot price
    invested_rmb: float           # Cumulative deployed amount (includes initial position + all DCA/dip fills)
    available_usdt: float         # Available USDT spot balance
    dip_filled_last_7d: bool      # True if a dip-buy fill occurred within the past 7 days
    rate_rmb_per_usdt: float = FALLBACK_RATE  # RMB/USDT rate; fallback 7.0 when live rate is unavailable
    anchors: dict | None = None   # Dynamic DCA tier edges from on-chain anchors; None → use fixed dead values
    now: datetime | None = None        # Injectable current time (for window remaining-weeks calc; None = real time)
    window_end: date | None = None     # Golden window end date; None → use legacy fixed-tier logic (backward compatible)


@dataclass(frozen=True)
class WeeklyDecision:
    """Weekly DCA decision result. dca.py uses this to send a notification, place an order, or skip."""
    action: str                   # "BUY" | "SKIP" | "INSUFFICIENT_FUNDS"
    base_tier: Tier               # Tier derived directly from spot price
    final_tier: Tier              # Effective tier after all linkage rules (downgrade + cap pause)
    amount_rmb: float             # Final planned amount (¥)
    amount_usdt: float            # Order size in USDT (after conversion and single-order cap)
    btc_price_usd: float
    reasons: list[str]            # Human-readable decision chain (shown to user: "why this amount")
    shortfall_usdt: float = 0.0   # When action=INSUFFICIENT_FUNDS: USDT still needed
    weeks: int = 0                # Remaining window weeks (window mode); 0 in legacy mode.
                                  # Single source — callers must not recompute.


def decide_weekly(ctx: WeeklyContext) -> WeeklyDecision:
    """Core decision logic: implements steps 1–4 and the three linkage rules.

    Flow:
      1. Price → tier assignment
      2. Rule ②: dip fill in past 7 days → downgrade one tier
      3. Rule ③: cumulative ≥ ¥180K → pause
      4. Convert to USDT + apply single-order cap + budget conservation (rule ①) + balance guard (step 4)
    Returns a WeeklyDecision (does not place an order; caller confirms before execution).
    """
    reasons: list[str] = []

    # —— Sanity guard: reject abnormal price ——
    if not (SANE_PRICE_MIN_USD <= ctx.btc_price_usd <= SANE_PRICE_MAX_USD):
        return WeeklyDecision(
            action="SKIP", base_tier=Tier.PAUSE, final_tier=Tier.PAUSE,
            amount_rmb=0, amount_usdt=0, btc_price_usd=ctx.btc_price_usd,
            reasons=[f"⚠️ BTC spot price ${ctx.btc_price_usd:,.0f} is outside the valid range"
                     f" (${SANE_PRICE_MIN_USD:,.0f}–${SANE_PRICE_MAX_USD:,.0f});"
                     f" order rejected pending manual review."],
        )

    # 1. Price → tier (dynamic anchors if available; price_to_tier falls back to fixed dead values internally)
    base = price_to_tier(ctx.btc_price_usd, ctx.anchors)
    final = base
    weeks = 0  # Remaining window weeks: assigned only in window mode (legacy fixed-tier = 0)

    if ctx.window_end is None:
        # ── Legacy logic (fixed tiers; ctx.window_end not set → behavior unchanged) ──
        reasons.append(
            f"BTC spot ${ctx.btc_price_usd:,.0f} → tier \"{TIER_LABEL[base]}\""
            f" (plan ¥{TIER_AMOUNT_RMB[base]:,}/week)."
        )

        # 2. Rule ②: dip fill → downgrade
        if ctx.dip_filled_last_7d and base > DOWNGRADE_FLOOR:
            final = downgrade(final)
            reasons.append(
                f"Rule ②: dip fill within 7 days → downgraded one tier to \"{TIER_LABEL[final]}\""
                f" (¥{TIER_AMOUNT_RMB[final]:,}); freed budget reserved for deeper dip tiers."
            )
        elif ctx.dip_filled_last_7d:
            reasons.append("Rule ②: dip fill within 7 days, but already at the minimum Cautious tier — staying in the market.")

        # 3. Rule ③: near budget cap → pause
        if ctx.invested_rmb >= PAUSE_THRESHOLD_RMB:
            final = Tier.PAUSE
            reasons.append(
                f"Rule ③: cumulative ¥{ctx.invested_rmb:,.0f} ≥ ¥{PAUSE_THRESHOLD_RMB:,} (90%)"
                f" → DCA paused; dip-buying reserves preserved."
            )

        amount_rmb = float(TIER_AMOUNT_RMB[final])
    else:
        # ── Deadline-paced DCA window mode (base = remaining pool ÷ remaining weeks; × tilt; force-fill on last week) ──
        # Legacy rule ② (downgrade) and rule ③ (near-cap pause) are disabled in window mode:
        #   · Rule ② is replaced by "pool shrinkage + soft 0.8× constraint on recent dip fills";
        #   · Rule ③'s "preserve reserves" conflicts with "deploy on schedule", so it is disabled
        #     (the budget-conservation guard serves as the safety net instead).
        pool = max(0.0, TOTAL_BUDGET_RMB - ctx.invested_rmb)
        weeks = remaining_weeks(ctx.now, ctx.window_end)

        # —— Explicit post-window branch (window already past) ——
        # After window_end, remaining_weeks permanently returns 1, causing the old logic to
        # force-fill the remaining pool every week indefinitely — an infinite bad-advice loop.
        # Post-window: switch to SKIP (REVIEW state); do not auto-force-fill.
        # Remaining ¥ left for manual review. Today determined via ctx.now injection.
        eff_now = (ctx.now or datetime.now().astimezone()).date()
        eff_window_end = ctx.window_end  # Non-None guaranteed (else branch above handles None)
        if eff_now > eff_window_end:
            reasons.append(
                f"Window has ended (deadline: {eff_window_end.isoformat()}). "
                f"Remaining pool ¥{pool:,.0f} is pending manual review; no automatic force-fill."
            )
            return WeeklyDecision(
                action="SKIP", base_tier=base, final_tier=base,
                amount_rmb=0, amount_usdt=0, btc_price_usd=ctx.btc_price_usd,
                reasons=reasons, weeks=weeks,
            )

        base_amt = pool / weeks
        tilt = BTC_TILT[base]
        amount_rmb = base_amt * tilt
        reasons.append(
            f"Deadline-paced DCA: remaining pool ¥{pool:,.0f} ÷ {weeks} weeks remaining"
            f" = base ¥{base_amt:,.0f}/week."
        )
        reasons.append(
            f"BTC spot ${ctx.btc_price_usd:,.0f} → tier \"{TIER_LABEL[base]}\" → tilt ×{tilt}"
            f" (base ¥{base_amt:,.0f} × {tilt} = ¥{amount_rmb:,.0f})."
        )
        # Dip fill within 7 days → additional ×0.8 (avoid double-buying at the same price level).
        # Floor 0.4 (not 0.5): for the Pause tier, tilt 0.5 × 0.8 = 0.4×base; a floor of 0.5 would
        # completely cancel the ×0.8 soft constraint; 0.4 ensures the reduction is real while
        # keeping the position alive.
        if ctx.dip_filled_last_7d:
            floor_amt = 0.4 * base_amt
            amount_rmb = max(floor_amt, amount_rmb * 0.8)
            reasons.append(
                f"Soft constraint: dip fill within 7 days → additional ×0.8"
                f" (floor ¥{floor_amt:,.0f} = 0.4×base), = ¥{amount_rmb:,.0f};"
                f" avoids double-buying at the same price level."
            )
        # Final window week (remaining ≤ 1): force the full remaining pool in (force-fill to zero)
        if weeks <= 1:
            amount_rmb = pool
            reasons.append(
                f"Final window week: force-filling remaining pool ¥{pool:,.0f}"
                f" (⚠️ subject to single-order cap ${MAX_SINGLE_ORDER_USDT:,.0f};"
                f" any excess must be filled manually over subsequent days/orders)."
            )

    # Pause tier: skip order. In window mode, the Pause tier still invests at tilt 0.5 rather than
    # skipping entirely, so only the legacy mode checks final == PAUSE here.
    # Both modes block amount ≤ 0 (remaining pool already depleted).
    if (ctx.window_end is None and final == Tier.PAUSE) or amount_rmb <= 0:
        return WeeklyDecision(
            action="SKIP", base_tier=base, final_tier=final,
            amount_rmb=0, amount_usdt=0, btc_price_usd=ctx.btc_price_usd,
            reasons=reasons, weeks=weeks,
        )

    # 4a. Rule ①: budget conservation — this week's buy must not push cumulative past the hard cap
    room_rmb = TOTAL_BUDGET_RMB - ctx.invested_rmb
    if room_rmb <= 0:
        reasons.append(f"Rule ①: cumulative ¥{ctx.invested_rmb:,.0f} has reached the hard cap of ¥{TOTAL_BUDGET_RMB:,}; DCA stopped.")
        return WeeklyDecision(
            action="SKIP", base_tier=base, final_tier=Tier.PAUSE,
            amount_rmb=0, amount_usdt=0, btc_price_usd=ctx.btc_price_usd, reasons=reasons, weeks=weeks,
        )
    if amount_rmb > room_rmb:
        reasons.append(
            f"Rule ①: this week's plan ¥{amount_rmb:,.0f} exceeds the remaining budget ¥{room_rmb:,.0f};"
            f" capped at remaining budget."
        )
        amount_rmb = room_rmb

    # 4b. ¥ → USDT, apply single-order cap.
    # CRITICAL: at the final window week, amount_rmb may far exceed the single-order limit (e.g. ¥73,547).
    # Capping only amount_usdt while leaving amount_rmb unchanged creates a three-way drift between
    # the dashboard display, the one-tap bookkeeping entry, and the actual orderable amount.
    # After capping, back-calculate amount_rmb = amount_usdt × rate to keep all three values consistent.
    amount_usdt = amount_rmb / ctx.rate_rmb_per_usdt
    if amount_usdt > MAX_SINGLE_ORDER_USDT:
        amount_usdt = MAX_SINGLE_ORDER_USDT
        amount_rmb = round(amount_usdt * ctx.rate_rmb_per_usdt, 2)
        reasons.append(
            f"Risk control: single-order cap ${MAX_SINGLE_ORDER_USDT:,.0f} applied;"
            f" RMB amount back-calculated = ${amount_usdt:,.0f} × {ctx.rate_rmb_per_usdt} = ¥{amount_rmb:,.0f}"
            f" (consistent with actual orderable amount; excess pool must be filled over future weeks/orders)."
        )
    amount_usdt = round(amount_usdt, 2)
    reasons.append(
        f"Conversion: ¥{amount_rmb:,.0f} ÷ {ctx.rate_rmb_per_usdt} = ${amount_usdt:,.2f} USDT (order placed in USDT)."
    )

    # 4c. Balance guard
    if ctx.available_usdt < amount_usdt:
        shortfall = round(amount_usdt - ctx.available_usdt, 2)
        reasons.append(
            f"⚠️ Insufficient balance: need ${amount_usdt:,.2f} this week,"
            f" only ${ctx.available_usdt:,.2f} available;"
            f" short by ${shortfall:,.2f}. Please top up your exchange account before executing."
        )
        return WeeklyDecision(
            action="INSUFFICIENT_FUNDS", base_tier=base, final_tier=final,
            amount_rmb=amount_rmb, amount_usdt=amount_usdt,
            btc_price_usd=ctx.btc_price_usd, reasons=reasons, shortfall_usdt=shortfall, weeks=weeks,
        )

    reasons.append(f"✅ Balance sufficient (available: ${ctx.available_usdt:,.2f}); ready to place order.")
    return WeeklyDecision(
        action="BUY", base_tier=base, final_tier=final,
        amount_rmb=amount_rmb, amount_usdt=amount_usdt,
        btc_price_usd=ctx.btc_price_usd, reasons=reasons, weeks=weeks,
    )


# ══ On-chain signal evaluation: pure decision functions (no fetching) ════════════════
# Inputs come from the on-chain signals cache (written by a cron job; this module only reads and evaluates).
# All judgments are centralized here as the single source of truth — shared by both the dashboard
# and any future Telegram notifications; never duplicated in multiple places.

# ④ VDD pulse rule (calibrated via full-history backtest 2026-06-06):
#   Bear state (price < 365d moving avg) AND multiple > 1.0 AND ≥ 2× the 60d low → pulse.
#   Fires within ±10 days of the 2018-12 and 2022-11 bear bottoms; 2015 weak pulse is missed
#   (convergence signal ⑤ still covers it); 2022-03 and 2025-11 false positives are blocked by
#   the "⑤ P/L crossover" convergence gate — so a single-signal hit is a warning lamp only;
#   acceleration buys require the convergence badge (both ④ and ⑤ firing together).
VDD_MA_LONG = 365          # Classic VDD Multiple = 30d MA / 365d MA
VDD_MA_SHORT = 30
VDD_PULSE_LEVEL = 1.0      # Pulse threshold
VDD_PULSE_JUMP = 2.0       # Multiple of the 60d low required for a pulse
VDD_LOW_WINDOW = 60
HODL_ICE_HIT = 18.8        # ② Ice-point confirmation level
HODL_ICE_WARN = 22.0       # ② Bear-bottom historical reference band lower bound (2015/2018/2022 all 22–23%)
PL_CROSS_NEAR = 1.15       # ⑤ Approach band for the 1.0 crossover
SLOPE_WINDOW = 7           # ③ Divergence evaluation window (days)
STALE_HOURS = 36           # Cache older than this → annotate card with staleness label


def vdd_multiple_series(cdd_hist: list, price_hist: list) -> list[float]:
    """Compute the VDD multiple series. Daily VDD = CDD × close price;
    VDD Multiple = 30-day MA / 365-day MA (classic definition).
    Both input series are daily, ascending, tail-aligned; returns the multiple series tail-aligned
    to the inputs. Uses prefix sums for O(n) computation."""
    n = min(len(cdd_hist or []), len(price_hist or []))
    if n <= VDD_MA_LONG:
        return []
    vdd = [float(c) * float(p) for c, p in zip(cdd_hist[-n:], price_hist[-n:])]
    cum = [0.0]
    for v in vdd:
        cum.append(cum[-1] + v)
    ma = lambda i, w: (cum[i + 1] - cum[i + 1 - w]) / w  # noqa: E731
    out = []
    for i in range(VDD_MA_LONG, n):
        long = ma(i, VDD_MA_LONG)
        if long > 0:
            out.append(ma(i, VDD_MA_SHORT) / long)
    return out


def _trend(series: list, days: int = SLOPE_WINDOW) -> float | None:
    """Returns the head-to-tail difference over the last `days` valid points
    (positive = uptrend, negative = downtrend). Returns None if fewer than `days` points exist."""
    pts = [x for x in series if x is not None]
    if len(pts) < days:
        return None
    return pts[-1] - pts[-days]


def _sig(key, name, status, note, frac, sub=""):
    """Construct a uniform signal card dict.
    status: hit (✅ triggered, green) / warn (⚠️ caution, amber) / wait (⏳ not triggered, neutral) / na (no data).
    """
    sem = {"hit": "green", "warn": "amber", "wait": "wait", "na": "wait"}[status]
    icon = {"hit": "✅", "warn": "⚠️", "wait": "⏳", "na": "—"}[status]
    return {"key": key, "name": name, "status": status, "sem": sem, "icon": icon,
            "note": note, "frac": max(0.0, min(1.0, frac)), "sub": sub, "auto": True}


def sig_sth_inversion(brk: dict) -> dict:
    """① STH realized price (red) crosses below the True Market Mean (blue) —
    confirms deep bear-market capitulation structure."""
    sth, tmm = brk.get("sth_realized_price"), brk.get("true_market_mean")
    if not sth or not tmm:
        return _sig("sth_tmm", "STH / True Market Mean", "na", "No data available", 0)
    gap = (sth - tmm) / tmm * 100
    if sth < tmm:
        return _sig("sth_tmm", "STH / True Market Mean", "hit", "Inverted · deep capitulation confirmed", 1.0,
                    f"Red ${sth/1000:.1f}K < Blue ${tmm/1000:.1f}K ({gap:+.1f}%)")
    return _sig("sth_tmm", "STH / True Market Mean", "wait", f"Not inverted (gap {gap:+.1f}%)", 1 - min(gap, 10) / 10,
                f"Red ${sth/1000:.1f}K / Blue ${tmm/1000:.1f}K")


def sig_hodl_ice(brk: dict) -> dict:
    """② Short-term holder share (< 3 months) at ice-point: below the bear-bottom reference band
    signals peak capital hibernation and increases probability of a bottom."""
    pct = brk.get("hodl_lt3m_pct")
    if pct is None:
        return _sig("hodl_ice", "STH Share Ice-Point", "na", "No data available", 0)
    sub = f"Current {pct:.1f}% · bear-bottom reference 22–23%"
    if pct < HODL_ICE_HIT:
        return _sig("hodl_ice", "STH Share Ice-Point", "hit", f"{pct:.1f}% — ice-point confirmed", 1.0, sub)
    if pct < HODL_ICE_WARN:
        return _sig("hodl_ice", "STH Share Ice-Point", "warn", f"{pct:.1f}% — entering reference band", 0.7, sub)
    return _sig("hodl_ice", "STH Share Ice-Point", "wait", f"{pct:.1f}% — still warm", max(0.0, 1 - (pct - HODL_ICE_HIT) / 20), sub)


def sig_rhodl_divergence(brk: dict, price_hist: list) -> dict:
    """③ RHODL price–volume divergence: price rising 7d while RHODL falling 7d →
    thin-volume rally, caution on chasing. Uses slope direction only; independent of RHODL scale
    (provider scale differs from media convention)."""
    rt = _trend(brk.get("rhodl_hist") or [])
    pt = _trend(price_hist or [])
    if rt is None or pt is None:
        return _sig("rhodl_div", "RHODL Divergence", "na", "No data available", 0)
    rdir, pdir = ("↓" if rt < 0 else "↑"), ("↑" if pt > 0 else "↓")
    sub = f"7d: price {pdir} / RHODL {rdir}"
    if pt > 0 and rt < 0:
        return _sig("rhodl_div", "RHODL Divergence", "warn", "Thin-volume rally · watch for fakeout", 1.0, sub)
    return _sig("rhodl_div", "RHODL Divergence", "wait", "No divergence", 0.0, sub)


def sig_vdd_pulse(brk: dict) -> dict:
    """④ VDD pulse (ultimate panic selling): bear state + multiple > 1.0 + ≥ 2× the 60d low
    (backtest-calibrated rule). A single-signal hit lights the warning lamp only; acceleration buys
    require the convergence badge (⑤ must also fire, filtering bull-market / mid-cycle false positives)."""
    cdd, px = brk.get("cdd_hist") or [], brk.get("price_close_hist") or []
    mult = vdd_multiple_series(cdd, px)
    if len(mult) <= VDD_LOW_WINDOW:
        return _sig("vdd", "VDD Pulse", "na", "Insufficient history (need 365d+)", 0)
    m = mult[-1]
    low60 = min(mult[-(VDD_LOW_WINDOW + 1):-1])
    jump = m / low60 if low60 > 1e-12 else 0.0
    bear = px[-1] < sum(px[-VDD_MA_LONG:]) / VDD_MA_LONG
    sub = f"Multiple {m:.2f} · 60d low ×{jump:.1f}"
    if bear and m > VDD_PULSE_LEVEL and jump >= VDD_PULSE_JUMP:
        return _sig("vdd", "VDD Pulse", "hit", "Pulse burst · ancient coin days destroyed", 1.0, sub)
    if bear and (m > VDD_PULSE_LEVEL or jump >= VDD_PULSE_JUMP):
        return _sig("vdd", "VDD Pulse", "warn", "Pulse building", 0.7, sub)
    return _sig("vdd", "VDD Pulse", "wait", "No pulse", max(0.0, min(1.0, m / VDD_PULSE_LEVEL)), sub)


def sig_pl_cross(brk: dict) -> dict:
    """⑤ 365-day realized P/L crossover: ratio ≤ 1.0 ⇔ orange/blue lines crossing ≈ near the bear market bottom."""
    r = brk.get("pl_ratio_1y")
    if r is None:
        return _sig("pl_cross", "P/L 365d Crossover", "na", "No data available", 0)
    tr = _trend(brk.get("pl_ratio_1y_hist") or [])
    arrow = "" if tr is None else (" (converging)" if tr < 0 else " (temporarily rising)")
    sub = f"365d P/L ratio {r:.2f} → crossover at 1.0"
    if r <= 1.0:
        return _sig("pl_cross", "P/L 365d Crossover", "hit", "Crossed · bear-bottom zone", 1.0, sub)
    if r <= PL_CROSS_NEAR:
        return _sig("pl_cross", "P/L 365d Crossover", "warn", f"{r:.2f} approaching crossover{arrow}", 0.9, sub)
    return _sig("pl_cross", "P/L 365d Crossover", "wait", f"{r:.2f} not yet crossed{arrow}", max(0.0, min(1.0, 2 - r)), sub)


def sig_fng(fng: dict) -> dict:
    """⑧ Fear & Greed Index (replaces manual maintenance): value < 8 sustained is a reference
    condition for the discretionary reserve (single-day indicator shown here; multi-day trigger
    logic is applied upstream)."""
    v = (fng or {}).get("value")
    if v is None:
        return _sig("fng", "Fear & Greed Index", "na", "No data available", 0)
    label = (fng or {}).get("label", "")
    if v < 8:
        return _sig("fng", "Fear & Greed Index", "hit", f"{v} — discretionary reserve alert", 1 - v / 100, label)
    status = "warn" if v < 20 else "wait"
    note = f"{v} — extreme fear" if v < 25 else f"{v}"
    return _sig("fng", "Fear & Greed Index", status, note, 1 - v / 100, label)


def _staleness(fetched_at: str, now: datetime | None = None) -> str:
    """Cache staleness annotation: returns a human-readable string if the data is older than
    STALE_HOURS, otherwise returns an empty string. Returns a fallback string if the timestamp
    cannot be parsed."""
    now = now or datetime.now().astimezone()
    try:
        d = datetime.fromisoformat(str(fetched_at))
    except (TypeError, ValueError):
        return "data timestamp unknown"
    d = d.astimezone(now.tzinfo) if d.tzinfo else d.replace(tzinfo=now.tzinfo)
    hrs = (now - d).total_seconds() / 3600
    if hrs <= STALE_HOURS:
        return ""
    return f"data {int(hrs // 24)}d old" if hrs >= 48 else f"data {int(hrs)}h old"


def onchain_signal_cards(cache: dict, price_hist: list, manual: list, now: datetime | None = None) -> list[dict]:
    """Assemble the signal board: five on-chain auto-signals + Fear & Greed (auto) + manual cards
    (three-arrows / four-year-cycle, from the ledger onchain[] array).
    If the cache is empty or a source fails → corresponding cards show na / stale data with an annotation.
    Manual Fear & Greed cards are kept only as a fallback when the auto F&G card has no data."""
    brk_sec, fng_sec = (cache or {}).get("brk") or {}, (cache or {}).get("fng") or {}
    brk, fng = brk_sec.get("data") or {}, fng_sec.get("data") or {}
    cards = [
        sig_sth_inversion(brk), sig_hodl_ice(brk), sig_rhodl_divergence(brk, price_hist),
        sig_vdd_pulse(brk), sig_pl_cross(brk), sig_fng(fng),
    ]
    sec_of = {"sth_tmm": brk_sec, "hodl_ice": brk_sec, "rhodl_div": brk_sec,
              "vdd": brk_sec, "pl_cross": brk_sec, "fng": fng_sec}
    for c in cards:
        if c["status"] == "na":
            continue
        stale = _staleness(sec_of[c["key"]].get("fetched_at", ""), now)
        if stale:
            c["sub"] = (c["sub"] + " · " if c["sub"] else "") + stale
    fng_ok = cards[-1]["status"] != "na"
    for m in manual or []:
        if fng_ok and "Fear" in str(m.get("name", "")):
            continue   # Auto F&G available → skip manual F&G entry
        cards.append({**m, "auto": False})
    return cards


def onchain_resonance(cards: list[dict]) -> dict:
    """Dual bear-bottom confirmation: ④ VDD pulse + ⑤ P/L crossover firing simultaneously
    → gold badge. Either signal alone only lights its own warning lamp; the badge requires both
    (reduces false positives)."""
    by = {c.get("key"): c for c in cards if c.get("key")}
    vdd_hit = by.get("vdd", {}).get("status") == "hit"
    pl_hit = by.get("pl_cross", {}).get("status") == "hit"
    if vdd_hit and pl_hit:
        return {"active": True, "label": "🏆 Dual Bear-Bottom Confirmation (VDD Pulse + P/L Crossover) · accelerate B3/C tier accumulation"}
    return {"active": False, "label": ""}


def render_confirm_message(d: WeeklyDecision) -> str:
    """Render the decision as a Telegram message for the user."""
    head = {
        "BUY": "🟢 Weekly DCA — pending confirmation",
        "SKIP": "⏸ Weekly DCA — skipped",
        "INSUFFICIENT_FUNDS": "🟠 Insufficient balance — order on hold this week",
    }[d.action]
    lines = [f"*{head}*", ""]
    if d.action == "BUY":
        lines.append(
            f"Suggested buy: *${d.amount_usdt:,.2f} USDT* of BTC"
            f" (≈¥{d.amount_rmb:,.0f}, spot ${d.btc_price_usd:,.0f})"
        )
        lines.append(f"Tier: {TIER_LABEL[d.final_tier]}")
    lines.append("")
    lines.append("Rationale:")
    lines.extend(f"· {r}" for r in d.reasons)
    return "\n".join(lines)
