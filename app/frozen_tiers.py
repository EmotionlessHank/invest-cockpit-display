"""
frozen_tiers.py — Trigger price freeze snapshot + threshold drift detection (Plan B).

Positioning (config != ledger, physically separate — three-layer isolation):
  frozen_tiers.json is a config/derived snapshot, NOT the ledger. It stores only open legs'
  "frozen trigger price + anchor provenance + frozen_at + current drift". It never writes
  state.json, never places orders, never calls record_fill.
  Path resolution mirrors manual_anchors: env FROZEN_TIERS override → server ~/.cockpit/btc-tracker/
  → local app fallback. BTC and MSTR are stored in the same file under separate "btc"/"mstr"
  keys (paired, single source of truth).

Why freeze? Trigger prices are derived in real time from strategy.tier_anchors on each request;
  anchors (200WMA / realized price / green line) drift by tens to hundreds of dollars daily,
  which the user cannot track when managing GTC orders on the exchange. Solution: display uses
  the frozen value (stable within the week / within the threshold); when anchor drift exceeds
  the threshold, the frozen price is advanced and a "trigger price updated" alert is sent so
  the user knows to adjust the Binance GTC order manually.

Thresholds (E2 final): BTC max(2%, $1000); MSTR 3%.
Weekly baseline reset (E3 final): on Monday, recompute the frozen baseline even if the threshold
  is not breached (catches slow accumulating drift; aligned with DCA/onchain cron day).
Multi-leg merge (E5 final): when multiple legs exceed the threshold in the same cycle, merge
  into a single Telegram message.

frozen_tiers.json structure:
  {
    "btc": {
      "frozen_at": "2026-06-09T12:00:00+10:00",
      "legs": { "B3-a": {"price": 60822, "anchor": "green_line", "anchor_val": 60822}, ... },
      "drift": { "B3-a": 0.004 }   // current live-vs-frozen drift rate (display only)
    },
    "mstr": { ... same structure ... }
  }
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime

HERE = pathlib.Path(__file__).parent

# Drift thresholds (E2 final)
BTC_DRIFT_PCT = 0.02          # BTC percentage threshold: 2%
BTC_DRIFT_FLOOR_USD = 1000.0  # BTC absolute floor: $1000 (low-price tiers: even a small % can be a large move)
MSTR_DRIFT_PCT = 0.03         # MSTR: 3% (mNAV leverage amplifies volatility)


def _default_path() -> str:
    """Resolve frozen_tiers.json path. Mirrors the manual_anchors pattern: server converges
    to ~/.cockpit/btc-tracker/ (config != ledger, physically separate); falls back to the app
    directory locally. Override with FROZEN_TIERS env var (tests point to /tmp)."""
    server_dir = pathlib.Path.home() / ".cockpit" / "btc-tracker"
    if server_dir.is_dir():
        return str(server_dir / "frozen_tiers.json")
    return str(HERE / "frozen_tiers.json")


FROZEN_PATH = pathlib.Path(os.environ.get("FROZEN_TIERS", _default_path()))


# ── Read/write (sole writer = cron drift-detection process; idempotent overwrite, no flock needed) ──
def load_frozen(path: pathlib.Path | None = None) -> dict:
    """Read frozen snapshot. Missing/corrupt/non-dict → {} (no snapshot → caller falls back to
    live recomputation or seed values)."""
    p = path or FROZEN_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_frozen(state: dict, path: pathlib.Path | None = None) -> None:
    """Persist frozen snapshot (atomic write: tmp + os.replace, reader never sees partial JSON).
    Only writes frozen_tiers.json — never state.json (config != ledger)."""
    p = path or FROZEN_PATH
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_monday(now: datetime | None = None) -> bool:
    """Whether today is Monday locally (ISO weekday == 1). Used for the weekly baseline reset (E3)."""
    now = now or datetime.now().astimezone()
    return now.isoweekday() == 1


# ── Threshold check (E2: BTC max(2%,$1000) / MSTR 3%) ─────────────────────────
def exceeds_threshold(frozen_price: float, live_price: float, asset: str) -> bool:
    """Whether the live recomputed price has drifted beyond the threshold relative to the frozen price.
    BTC: drift rate ≥ 2% OR absolute difference ≥ $1000 (dual criteria — the easier one triggers;
         low-price tiers rely on the floor, high-price tiers on the percentage).
    MSTR: drift rate ≥ 3%.
    frozen_price ≤ 0 is treated as an invalid baseline → always exceeds threshold (force rebuild)."""
    if frozen_price is None or live_price is None:
        return False
    if frozen_price <= 0:
        return True
    pct = abs(live_price - frozen_price) / frozen_price
    if asset == "mstr":
        return pct >= MSTR_DRIFT_PCT
    return pct >= BTC_DRIFT_PCT or abs(live_price - frozen_price) >= BTC_DRIFT_FLOOR_USD


def drift_pct(frozen_price: float, live_price: float) -> float | None:
    """Signed drift rate of live relative to frozen (positive = live above frozen).
    Invalid baseline → None."""
    if frozen_price is None or live_price is None or frozen_price <= 0:
        return None
    return round((live_price - frozen_price) / frozen_price, 4)


# ── Drift detection: live trigger prices vs frozen; advance + alert on threshold breach (E2/E3/E5) ──
def detect_drift(
    asset: str,
    live_legs: dict,
    prev: dict | None = None,
    now: datetime | None = None,
    force_weekly: bool | None = None,
    filled_labels: set | None = None,
) -> tuple[dict, list[dict]]:
    """Drift detection core (semi-pure function: does not persist; new snapshot returned to caller).

    Parameters:
      asset: "btc" | "mstr" (determines which threshold to apply).
      live_legs: {label: {"price": float, "anchor": str, "anchor_val": float}}
                 = live recomputed open-leg trigger prices for this cycle.
                 Legs whose price is unavailable (fail-closed / fetch failure) must be
                 excluded by the caller before passing in — they will be kept frozen unchanged.
      prev: previous asset-segment snapshot {"frozen_at","legs","drift"}; None/empty = first freeze.
      now: injectable timestamp for testing.
      force_weekly: weekly baseline reset (E3). None = auto-detect via _is_monday(now);
                    explicit True/False overrides.
      filled_labels: labels of legs that have been filled (executed) this cycle. Used to distinguish
                     two cases when a leg present in prev is absent from live_legs:
                     · In filled_labels → filled: remove from frozen (correct; no longer tracked).
                     · Not in filled_labels (lost-anchor: fetch failed / anchor suspect / fail-closed)
                       → keep old frozen leg + old frozen_at unchanged, exclude from changes,
                       do not alert (preserves: no drift on unverifiable data, no false alerts).
                     None/omitted → treat as empty set (most conservative: all absent legs kept frozen).

    Decision logic (three-tier guard: caller has already excluded unavailable legs):
      · Leg absent from prev (first freeze) → freeze immediately + include in "updated" changes (new leg).
      · Live price drifted beyond threshold relative to frozen (E2) → advance frozen price +
        record old/new/drift% → "updated" change.
      · Weekly baseline reset (E3, Monday) → recompute baseline even without threshold breach +
        include change (reason=weekly) to catch slow accumulating drift.
      · Otherwise → update drift field only (display), price/frozen_at unchanged, no alert.
    E5 (same-cycle multi-leg merge) is handled by the caller grouping changes by label prefix;
    this function produces per-leg changes with a round prefix attached.

    Returns (new_snapshot, changes):
      new_snapshot: {"frozen_at","legs","drift"} containing live legs + lost-anchor preserved legs;
                    filled legs are removed.
      changes: [{label, round, old, new, drift_pct, anchor, anchor_val, reason}, ...]
               (reason ∈ {"new","threshold","weekly"}; empty = no updates, caller does not push/persist)
    """
    now = now or datetime.now().astimezone()
    weekly = _is_monday(now) if force_weekly is None else bool(force_weekly)
    prev = prev or {}
    prev_legs = (prev.get("legs") or {}) if isinstance(prev, dict) else {}
    prev_drift = (prev.get("drift") or {}) if isinstance(prev, dict) else {}
    filled_labels = filled_labels or set()

    new_legs: dict = {}
    new_drift: dict = {}
    changes: list[dict] = []
    any_advance = False

    for label, live in live_legs.items():
        live_price = live.get("price")
        anchor = live.get("anchor")
        anchor_val = live.get("anchor_val")
        old = prev_legs.get(label)
        old_price = old.get("price") if isinstance(old, dict) else None

        if old_price is None:
            # First freeze for this leg (new leg online) → include in changes
            new_legs[label] = {"price": live_price, "anchor": anchor, "anchor_val": anchor_val}
            new_drift[label] = 0.0
            changes.append({"label": label, "round": _round_of(label), "old": None,
                            "new": live_price, "drift_pct": None, "anchor": anchor,
                            "anchor_val": anchor_val, "reason": "new"})
            any_advance = True
            continue

        d = drift_pct(old_price, live_price)
        if exceeds_threshold(old_price, live_price, asset) or weekly:
            reason = "threshold" if exceeds_threshold(old_price, live_price, asset) else "weekly"
            new_legs[label] = {"price": live_price, "anchor": anchor, "anchor_val": anchor_val}
            new_drift[label] = 0.0
            changes.append({"label": label, "round": _round_of(label), "old": old_price,
                            "new": live_price, "drift_pct": d, "anchor": anchor,
                            "anchor_val": anchor_val, "reason": reason})
            any_advance = True
        else:
            # Under threshold: keep frozen price, only update the drift display value
            new_legs[label] = dict(old)
            new_drift[label] = d if d is not None else 0.0

    # Handle legs present in prev but absent from live_legs this cycle
    for label, old in prev_legs.items():
        if label in live_legs:
            continue
        if label in filled_labels:
            continue  # Filled: remove from frozen (correct — no longer needs to be tracked)
        # Lost-anchor (fetch failed / suspect / fail-closed) → keep frozen unchanged,
        # exclude from changes, do not alert (no drift on unverifiable data, no false alarm)
        new_legs[label] = dict(old) if isinstance(old, dict) else old
        if label in prev_drift:
            new_drift[label] = prev_drift[label]

    frozen_at = now_iso() if any_advance or not prev.get("frozen_at") else prev.get("frozen_at")
    new_snapshot = {"frozen_at": frozen_at, "legs": new_legs, "drift": new_drift}
    return new_snapshot, changes


def _round_of(label: str) -> str:
    """Extract the tier/round prefix from a sub-label (B3-b → B3 / T2-a → T2 / A-1 → A).
    Used by E5 same-cycle merge to group legs."""
    s = str(label or "")
    return s.split("-")[0] if "-" in s else s


def build_update_message(asset: str, round_code: str, legs: list[dict]) -> str:
    """Build a 'trigger price updated' alert message (E5: multiple legs in the same round
    merged into one message). legs = subset of changes for a single round_code.
    Includes old/new values, drift %, and anchor source. Printed to stdout (the notification
    channel); this function only produces text, it does not send anything."""
    cur = "$" if asset == "btc" else "$"
    head = f"📌 Trigger price updated · {round_code} (please update your Binance GTC orders manually)"
    lines = [head]
    for c in legs:
        label = c.get("label")
        new = c.get("new")
        old = c.get("old")
        anchor = c.get("anchor") or "—"
        new_s = f"{cur}{new:,.0f}" if isinstance(new, (int, float)) else "—"
        if old is None:
            lines.append(f"· {label}: new entry {new_s} (anchor: {anchor})")
        else:
            old_s = f"{cur}{old:,.0f}"
            dp = c.get("drift_pct")
            dp_s = f"{dp * 100:+.1f}%" if isinstance(dp, (int, float)) else "—"
            lines.append(f"· {label}: {old_s} → {new_s} (drift: {dp_s} · anchor: {anchor})")
    lines.append("⚠️ Alert only — manual review required before adjusting orders (record_fill). This alert does not execute automatically.")
    return "\n".join(lines)
