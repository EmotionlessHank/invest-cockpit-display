"""
tier_alert.py — Dip tier dynamic display + price alert with dedup (pure/semi-pure functions, testable)

Layer positioning (three-layer isolation — L3: display-derived + alert decision):
  This module is the L3 "display-derived + alert decision" layer. Read-only, compute-only,
  alert-decision-only:
    · Reads cache (onchain_signals derived cache, not the ledger)
    · Reads state (state.json ledger, read-only — never writes back)
    · Reads alert_state (separate dedup state file, see below)
  Never calls record_fill, never places orders, never writes state.json, never imports exchange SDKs.
  Fail-closed tiers (from L2) are marked "Data unavailable" and excluded from alerts.

Design principles (aligned with strategy.py / onchain_signals.py):
  - build_tier_view is a pure function: inputs (cache, state, manual_green, now) → output display dict,
    no side effects.
  - alert_decisions is a semi-pure function: inputs (view, state, alert_state, now) → outputs
    (to_alert, new_alert_state). The new state is persisted by the caller (the cron process);
    this module never touches disk directly.
  - Fully unit-testable offline (dict fixtures, no network, no real ledger).

────────────────────────────────────────────────────────────────────────
Spot price source and anchor consistency
────────────────────────────────────────────────────────────────────────
  "Spot" is passed in by the caller (app.py / cron) via the `spot` parameter: source = Binance
  BTCUSDT ticker (app.get_spot()). Anchors (wma200 / realized_price / green line) come from
  BRK/bitview cache (onchain_signals.load_cache()). The two sources are intentionally different:
  spot is a real-time exchange price; anchors are on-chain daily-level figures. However, they must
  be compared at the same instant: callers should fetch spot once and read cache once per
  request/cron cycle, passing the same spot value to build_tier_view to avoid time-skew.
  A sanity band (SANE_PRICE_*) is applied to spot — out-of-range values disable all alerts.

────────────────────────────────────────────────────────────────────────
alert_state.json structure and path
────────────────────────────────────────────────────────────────────────
  Physically separate from state.json (ledger and dedup state must not share files).
  Path resolution:
    Server: ~/.cockpit/btc-tracker/alert_state.json (when that directory exists)
    Local:  app/alert_state.json
    Override: environment variable ALERT_STATE
  Structure (last-write-wins, idempotent overwrite, no read-modify-write accumulation
             → flock not needed, same pattern as cache):
    {
      "alerts": {
        "<tier_code>": "<YYYY-MM-DD>",   # date of the most recent alert for this tier; same tier same day = skip
        ...
      }
    }
  Writer: the sole writer is the cron process running L3 (alert_decisions produces new_alert_state;
  the caller persists it).
  Fail direction = fail-toward-alert: if alert_state is missing/corrupt/missing fields → treat as
  "no alert today", prefer an extra alert over a missed one (real-money scenario: a missed dip
  entry is worse than an extra notification).

────────────────────────────────────────────────────────────────────────
app.py / cron integration interface
────────────────────────────────────────────────────────────────────────
  Display (inside app.py build_dashboard):
    cache = onchain_signals.load_cache()
    view  = tier_alert.build_tier_view(cache, st, manual_green=<value or None>, now=None)
    # view["tiers"] feeds the frontend: each tier has code/name/triggers/nearest/dist_pct/display/filled/alertable
  Alert (cron running L3, reusing the notification channel):
    spot  = app.get_spot()
    cache = onchain_signals.load_cache()
    view  = tier_alert.build_tier_view(cache, st, manual_green=..., spot=spot, now=now)
    alert_state = tier_alert.load_alert_state()  # returns {} on failure — fail-toward-alert
    to_alert, new_state = tier_alert.alert_decisions(view, st, alert_state, now=now)
    for a in to_alert: <push TG message a["message"]>  # message includes anchor value+source+fetched_at
    if to_alert: tier_alert.save_alert_state(new_state)  # sole writer; only write if there are new alerts
  Note: record_fill remains the sole ledger write entry point. Users place orders and record fills
  manually after receiving an alert. This module does not participate in order execution.
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime

import strategy as S

HERE = pathlib.Path(__file__).parent

# Staleness threshold for the manually-maintained green line anchor.
# If updated_at is older than this many days the value is considered stale and B3 is fail-closed.
MANUAL_GREEN_STALE_DAYS = 14

# Spot price sanity band (shared constants from strategy — single source of truth).
SANE_PRICE_MIN_USD = S.SANE_PRICE_MIN_USD
SANE_PRICE_MAX_USD = S.SANE_PRICE_MAX_USD

# Dip tier display order (aligned with dip_triggers / state.json tier name prefixes).
DIP_CODES = ("B1", "B2", "B3", "C1", "C2")

# B1/B2 = already-filled tiers: semantically "already bought, no longer anchor/alert".
# This is a code invariant — regardless of whether state.json carries fills, or whether
# the current price is inside the zone, these two tiers are always alertable=False.
ALREADY_FILLED_CODES = ("B1", "B2")

DISPLAY_UNAVAILABLE = "Data unavailable"
DISPLAY_GREEN_STALE = "Green line stale — manual review required"


def _default_alert_state_path() -> str:
    """Resolve alert_state.json path. On the server: ~/.cockpit/btc-tracker/ (same directory as the
    ledger but physically separate — this is a dedup state file, not the ledger itself).
    Falls back to the app directory locally. Override with ALERT_STATE env var (tests point to /tmp)."""
    server_dir = pathlib.Path.home() / ".cockpit" / "btc-tracker"
    if server_dir.is_dir():
        return str(server_dir / "alert_state.json")
    return str(HERE / "alert_state.json")


ALERT_STATE_PATH = pathlib.Path(os.environ.get("ALERT_STATE", _default_alert_state_path()))


def _default_manual_anchors_path() -> str:
    """Resolve manual_anchors.json path. On the server: ~/.cockpit/btc-tracker/ (config file, not
    the ledger — config and ledger must remain physically separate).
    Falls back to the app directory locally. Override with MANUAL_ANCHORS env var (tests point to /tmp)."""
    server_dir = pathlib.Path.home() / ".cockpit" / "btc-tracker"
    if server_dir.is_dir():
        return str(server_dir / "manual_anchors.json")
    return str(HERE / "manual_anchors.json")


MANUAL_ANCHORS_PATH = pathlib.Path(os.environ.get("MANUAL_ANCHORS", _default_manual_anchors_path()))


# ── alert_state read/write (sole writer = cron L3 process; idempotent overwrite, no flock needed) ──
def load_alert_state(path: pathlib.Path | None = None) -> dict:
    """Read dedup state. Returns {} on missing/corrupt file (fail-toward-alert: treat as no alert today)."""
    p = path or ALERT_STATE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_alert_state(state: dict, path: pathlib.Path | None = None) -> None:
    """Persist dedup state (atomic write: tmp + os.replace, reader never sees partial JSON).
    This module only writes alert_state.json — never state.json (three-layer isolation)."""
    p = path or ALERT_STATE_PATH
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ── Green line single source + staleness check (prevents paired-path drift) ──────────
def _load_manual_anchors(path: pathlib.Path | None = None) -> dict:
    """Read manual_anchors.json config file (config != ledger, physically separate from state.json).
    Returns {} on missing/corrupt/non-dict file (B3 fail-closed: no anchor → no alert, no error)."""
    p = path or MANUAL_ANCHORS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def manual_green(
    now: datetime | None = None,
    path: pathlib.Path | None = None,
) -> tuple[float | None, bool]:
    """Green line anchor (B3 dip tier) source: the green_line field in the independent
    manual_anchors.json config file (config != ledger, physically separate from state.json).

    Path resolution follows the onchain_signals._default_cache pattern: env MANUAL_ANCHORS
    override → server ~/.cockpit/btc-tracker/manual_anchors.json → local app directory fallback.

    Single implementation: both app.py and tier_alert_cron.py read through this function,
    eliminating any source-of-truth divergence between the two call sites.

    File format: {"green_line": {"value": <num>, "updated_at": "YYYY-MM-DD"}};
    also accepts bare numeric form {"green_line": <num>} (no updated_at → treated as current,
    no staleness check — backward-compatible).

    Staleness check: with {value, updated_at} format, if updated_at exceeds MANUAL_GREEN_STALE_DAYS
    days ago → stale, returns (None, True): B3 is fail-closed and will not alert on a stale anchor.

    Returns (value, is_stale):
      value:    valid, non-stale green line value; None on missing/corrupt/invalid/stale (B3 fail-closed)
      is_stale: True only when "has a valid value but updated_at has expired" (B3 shows stale message)
    Read-only (three-layer isolation). Missing/corrupt file → None (B3 fail-closed), no exception raised.
    """
    raw = _load_manual_anchors(path).get("green_line")
    updated_at = None
    if isinstance(raw, dict):
        updated_at = raw.get("updated_at")
        raw = raw.get("value")
    if not (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
        return None, False
    val = float(raw)

    # Staleness check only applies when updated_at is present;
    # bare numeric form (no updated_at) is accepted as-is for backward compatibility.
    if updated_at:
        now = now or datetime.now().astimezone()
        try:
            d = datetime.fromisoformat(str(updated_at))
        except (TypeError, ValueError):
            # Cannot parse updated_at → cannot verify freshness → fail-closed (prefer not alerting
            # on a suspicious anchor over alerting on a potentially stale value).
            return None, True
        d = d.astimezone(now.tzinfo) if d.tzinfo else d.replace(tzinfo=now.tzinfo)
        if (now - d).days > MANUAL_GREEN_STALE_DAYS:
            return None, True
    return val, False


# ── L3 display: per-tier dynamic trigger prices + distance from spot % ───────────────
def _tier_code(name: str) -> str | None:
    """Extract tier code prefix from a state.json tier name (e.g. 'B3 Green Line' → 'B3')."""
    head = str(name or "").split(" ")[0].strip()
    return head if head in DIP_CODES else None


def _tier_filled(tier: dict) -> bool:
    """Whether this tier has any dip fills (reads state.json tier.fills, read-only). Any fill → True."""
    return bool((tier or {}).get("fills"))


def build_tier_view(
    cache: dict,
    state: dict,
    manual_green: float | None = None,
    spot: float | None = None,
    now: datetime | None = None,
    green_stale: bool = False,
) -> dict:
    """L3 display-derived: calls strategy.tier_anchors to compute per-tier dynamic trigger prices
    and distance from spot %.

    Pure display-derived function — never writes state.json. L2 fail-closed/degraded tiers
    (tier_anchors returns None for that tier) are marked display="Data unavailable", alertable=False,
    and excluded from alerts.

    Spot vs anchor source notes: see module docstring. spot=None (price unavailable) → dist_pct=None
    for all tiers; trigger prices are still shown but alertable=False (cannot judge price-in-zone
    without a valid spot — prefer no alert over a false alarm).
    Spot outside the sanity band → treated as invalid (same as None).

    Returns dict:
      {
        "spot": float|None,                  # spot price used in this comparison (None = invalid/unavailable)
        "anchors_ok": bool,                  # whether tier_anchors DCA boundaries are fully available
        "degraded": [str, ...],              # L2 degradation reasons (which anchors are unavailable)
        "tiers": [
          {
            "code": "B3", "name": "B3 Green Line",
            "triggers": [float, ...] | None, # dynamic trigger prices for this tier (fail-closed → None)
            "filled": bool,                  # whether this tier has any fills (reads state.json, read-only)
            "nearest": float|None,           # nearest trigger leg below spot (for dist %)
            "dist_pct": float|None,          # (spot - nearest)/nearest*100, positive = spot above
            "in_zone": bool,                 # whether spot has entered this tier's zone (≤ top trigger price)
            "display": str,                  # display string: trigger price list or "Data unavailable"
            "alertable": bool,               # whether this tier can participate in alerts
            "anchor": {...},                 # anchor provenance (raw value/source/fetched_at)
          }, ...
        ],
      }
    """
    anchors = S.tier_anchors(cache or {}, manual_green=manual_green)
    triggers_by_code = anchors.get("dip_triggers") or {}
    degraded = anchors.get("degraded") or []

    # Spot sanity: missing / out-of-range → treat as invalid (exclude from price-in-zone checks)
    spot_val: float | None = None
    if isinstance(spot, (int, float)) and not isinstance(spot, bool):
        if SANE_PRICE_MIN_USD <= float(spot) <= SANE_PRICE_MAX_USD:
            spot_val = float(spot)

    # Index state.json tiers by tier code (read-only)
    state_tier_by_code: dict[str, dict] = {}
    for t in (state or {}).get("tiers", []):
        code = _tier_code(t.get("name"))
        if code:
            state_tier_by_code[code] = t

    # Anchor provenance (for message text and debugging; reads cache only)
    brk = ((cache or {}).get("brk") or {})
    brk_data = brk.get("data") or {}
    fetched_at = brk.get("fetched_at", "")
    anchor_src = {
        "B3": ("green_line", manual_green if S._anchor_ok(manual_green, "green_line") else None, "manual"),
        "C1": ("wma200", brk_data.get("wma200"), "brk"),
        "C2": ("realized_price", brk_data.get("realized_price"), "brk"),
        "B1": (None, None, "filled"),
        "B2": (None, None, "filled"),
    }

    tiers_view: list[dict] = []
    for code in DIP_CODES:
        st_tier = state_tier_by_code.get(code, {})
        triggers = triggers_by_code.get(code)
        # B1/B2 = already-filled tiers (code invariant): always treated as filled, never alerted,
        # regardless of what state.json carries in its fills field.
        filled = code in ALREADY_FILLED_CODES or _tier_filled(st_tier)

        a_key, a_val, a_src = anchor_src.get(code, (None, None, ""))
        anchor = {"key": a_key, "value": a_val, "source": a_src, "fetched_at": fetched_at}

        if not triggers:
            # L2 fail-closed/degraded tier → Data unavailable, excluded from alerts.
            # B3 specifically: if the green line is stale, show a distinct message to prompt manual update.
            disp = DISPLAY_GREEN_STALE if (code == "B3" and green_stale) else DISPLAY_UNAVAILABLE
            tiers_view.append({
                "code": code, "name": st_tier.get("name", code),
                "triggers": None, "filled": filled,
                "nearest": None, "dist_pct": None, "in_zone": False,
                "display": disp, "alertable": False, "anchor": anchor,
            })
            continue

        top = max(triggers)        # Highest trigger price for this tier = upper edge of the zone
        in_zone = spot_val is not None and spot_val <= top
        # nearest = closest trigger leg below spot (the next dip-buy opportunity price).
        # If spot has fallen below the deepest leg, nearest = deepest leg (dist_pct will be negative).
        nearest = None
        dist_pct = None
        if spot_val is not None:
            below = [p for p in triggers if spot_val > p]
            nearest = max(below) if below else min(triggers)
            dist_pct = round((spot_val - nearest) / nearest * 100, 2)

        display = " / ".join(f"${p:,.0f}" for p in triggers)
        alertable = bool(triggers) and spot_val is not None and not filled

        tiers_view.append({
            "code": code, "name": st_tier.get("name", code),
            "triggers": list(triggers), "filled": filled,
            "nearest": nearest, "dist_pct": dist_pct, "in_zone": in_zone,
            "display": display, "alertable": alertable, "anchor": anchor,
        })

    return {
        "spot": spot_val,
        "anchors_ok": bool(anchors.get("ok")),
        "degraded": list(degraded),
        "tiers": tiers_view,
    }


# ── L3 alert: price alert decisions (double dedup + fail-toward-alert) ───────────────
def _today(now: datetime | None = None) -> str:
    """Local date string YYYY-MM-DD (the 'day' key for same-tier same-day dedup)."""
    now = now or datetime.now().astimezone()
    return now.date().isoformat()


def alert_decisions(
    view: dict,
    state: dict,
    alert_state: dict,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """Price alert decisions (semi-pure function: does not persist; new state returned to caller).

    Double dedup + fail-toward-alert:
      ① Alert only when spot is inside a tier's zone (in_zone: spot ≤ tier's highest trigger price);
      ② Filled tiers are never alerted (already-bought, do-not-nag — enforced via view.alertable);
      ③ Same-tier same-day dedup (alert_state.alerts[code] == today) → skip;
      ④ alert_state missing/corrupt/no record for this tier → treat as "no alert today"
         (fail-toward-alert: never silently suppress).
    Fail-closed tiers (view marks alertable=False / display="Data unavailable") are naturally excluded.

    Returns (to_alert, new_alert_state):
      to_alert: [{code, name, message, triggers, nearest, dist_pct, anchor}, ...]
      new_alert_state: input alert_state updated with today's date for each alerted tier (others preserved).
    Three-layer isolation: this function never calls record_fill, never places orders,
    never writes state.json — it only produces alert decisions and new dedup state.
    """
    today = _today(now)
    # fail-toward-alert: non-dict alert_state or missing 'alerts' key → treat as empty (no alerts today)
    prev_alerts = {}
    if isinstance(alert_state, dict):
        a = alert_state.get("alerts")
        if isinstance(a, dict):
            prev_alerts = a

    to_alert: list[dict] = []
    new_alerts = dict(prev_alerts)  # Preserve history for other tiers; don't accidentally clear them

    for t in (view or {}).get("tiers", []):
        code = t.get("code")
        # ① Only alert when spot is inside the zone and the tier is alertable (has price, valid spot, not filled)
        if not t.get("alertable") or not t.get("in_zone"):
            continue
        # ② Extra guard: skip filled tiers (redundant with alertable, but explicit for clarity)
        if t.get("filled"):
            continue
        # ③ Same-tier same-day dedup (missing record → fail-toward-alert, proceed with alert)
        if prev_alerts.get(code) == today:
            continue

        to_alert.append({
            "code": code,
            "name": t.get("name", code),
            "triggers": t.get("triggers"),
            "nearest": t.get("nearest"),
            "dist_pct": t.get("dist_pct"),
            "anchor": t.get("anchor"),
            "message": _render_alert(t, view.get("spot")),
        })
        new_alerts[code] = today  # Record today's alert (dedup anchor)

    new_alert_state = dict(alert_state) if isinstance(alert_state, dict) else {}
    new_alert_state["alerts"] = new_alerts
    return to_alert, new_alert_state


def _render_alert(tier_view: dict, spot: float | None) -> str:
    """Render the price alert message with anchor value + source + fetched_at provenance,
    so the user can verify the basis before placing an order.
    This module only produces message text — it does not send messages or place orders
    (sending is handled by the cron caller via the notification channel)."""
    code = tier_view.get("code", "")
    name = tier_view.get("name", code)
    nearest = tier_view.get("nearest")
    triggers = tier_view.get("triggers") or []
    anchor = tier_view.get("anchor") or {}
    a_key = anchor.get("key") or "—"
    a_val = anchor.get("value")
    a_src = anchor.get("source") or "—"
    a_at = anchor.get("fetched_at") or "unknown"

    lines = [f"🟢 Price Alert · {name}"]
    if spot is not None:
        lines.append(f"Spot ${spot:,.0f} has entered the {code} tier zone")
    if nearest is not None:
        lines.append(f"Nearest trigger leg: ${nearest:,.0f}")
    if triggers:
        lines.append("Tier trigger prices: " + " / ".join(f"${p:,.0f}" for p in triggers))
    a_val_str = f"${a_val:,.0f}" if isinstance(a_val, (int, float)) and not isinstance(a_val, bool) else "—"
    lines.append(f"Anchor: {a_key}={a_val_str} (source: {a_src} · {a_at})")
    lines.append("⚠️ Manual review required before placing orders (record_fill). This alert does not execute automatically.")
    return "\n".join(lines)
