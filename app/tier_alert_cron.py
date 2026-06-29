"""
tier_alert_cron.py — Dip tier price alert cron runner (reuses the notification channel).

Layer positioning (three-layer isolation): This script is the L3 "alert" layer runtime shell.
It only reads the ledger/cache, only produces alerts, and only writes the independent dedup file
alert_state.json:
  · Reads onchain_cache.json (onchain_signals.load_cache, derived cache, not the ledger)
  · Reads state.json (ledger, read-only — never writes back, path via BTC_STATE)
  · Reads/writes alert_state.json (independent dedup state, physically separate from state.json,
    sole writer = this script)
  Never calls record_fill, never places orders, never writes state.json, never imports exchange SDKs.

Notification channel: this script only prints alert messages to stdout, which the agent gateway
  injects into a skill and wraps for Telegram delivery (same mechanism as the weekly DCA advice).
  The script itself holds no Telegram token and does not call the API directly.

Spot price rule: spot is fetched from the Binance public API (no key required). On fetch failure
  the script exits gracefully without fabricating a price or emitting a false alert.
  build_tier_view's spot sanity band provides a secondary guard: an invalid spot disables all alerts.

Usage:
  tier_alert_cron.py --check        # production run: fetch price → compute alerts → print to stdout + write alert_state
  tier_alert_cron.py --check --spot 57000   # override spot (offline / debugging; still writes alert_state)
  tier_alert_cron.py --dry          # dry run: print decisions only, no push, no alert_state write
  tier_alert_cron.py --dry --spot 57000

Paths: BTC_STATE → ledger, ONCHAIN_CACHE → cache, ALERT_STATE → dedup file, MANUAL_ANCHORS → green line config
  (defaults: see tier_alert/onchain_signals _default_* functions; server converges to ~/.cockpit/btc-tracker/).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))  # Allow importing tier_alert / onchain_signals from the same directory

import onchain_signals  # noqa: E402
import tier_alert  # noqa: E402

STATE = pathlib.Path(os.environ.get("BTC_STATE", str(HERE / "state.json")))


def get_spot():
    """Fetch real-time BTC spot price (Binance public API, no key). Returns None on timeout/error
    — never blocks, never fabricates a price."""
    try:
        with urllib.request.urlopen(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None


def out(d):
    print(json.dumps(d, ensure_ascii=False))


def _load_state() -> dict | None:
    """Read ledger (read-only — never writes back). Returns None on missing/corrupt file
    (caller degrades gracefully, never fabricates ledger data)."""
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run(dry: bool, spot_override: float | None) -> dict:
    """Main flow: load cache + state → build_tier_view + alert_decisions → push + write alert_state (--check).

    Returns result dict (last line JSON, for cron/test assertions). --dry: print decisions only, no write.
    On spot fetch failure: exits gracefully without fabricating a price or emitting a false alert;
    build_tier_view's spot sanity band provides a secondary guard.
    """
    st = _load_state()
    if st is None:
        msg = f"⚠️ Dip alert: ledger unavailable ({STATE}), skipping this cycle. Manual review required."
        print(msg)
        return {"ok": False, "msg": "ledger unavailable", "alerted": []}

    # Spot: fetch once per cycle (same source and timestamp as the dashboard display)
    # On failure: do not fabricate a price, do not emit a false alert
    spot = spot_override if spot_override is not None else get_spot()
    if spot is None:
        msg = "⚠️ Dip alert: real-time spot price fetch failed — skipping zone check this cycle (no fabricated price, no false alert). Open the dashboard to check manually."
        print(msg)
        return {"ok": False, "msg": "price fetch failed", "alerted": []}

    cache = onchain_signals.load_cache()
    # Green line single source + staleness check: read from independent config file manual_anchors.json
    # (config != ledger). Stale green line → None + stale flag, B3 fail-closed (no alert on expired anchor).
    green, green_stale = tier_alert.manual_green()
    view = tier_alert.build_tier_view(cache, st, manual_green=green, spot=spot, green_stale=green_stale)
    # alert_state missing/corrupt → {} (fail-toward-alert: treat as no alert today)
    alert_state = tier_alert.load_alert_state()
    to_alert, new_state = tier_alert.alert_decisions(view, st, alert_state)

    if not to_alert:
        # No new price-in-zone: do not print, do not write alert_state (idempotent, no spurious writes)
        return {"ok": True, "msg": "no new price alerts", "alerted": [],
                "spot": view.get("spot"), "dry": dry}

    # New price-in-zone: print each alert message to stdout (= the agent gateway → Telegram channel)
    for a in to_alert:
        print(a["message"])
        print()  # blank line between messages for readability in Telegram

    if not dry:
        # Persist dedup state (sole writer; independent file, never writes state.json)
        tier_alert.save_alert_state(new_state)

    return {"ok": True, "msg": f"{len(to_alert)} tier price alert(s)",
            "alerted": [a["code"] for a in to_alert],
            "spot": view.get("spot"), "dry": dry,
            "alert_state_written": (not dry)}


def main():
    ap = argparse.ArgumentParser(description="Dip tier price alert cron (L3)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="Production run: push price alerts + write alert_state")
    g.add_argument("--dry", action="store_true", help="Dry run: print decisions only, no push, no write")
    ap.add_argument("--spot", type=float, default=None, help="Override real-time spot price (offline/debugging)")
    a = ap.parse_args()

    res = run(dry=a.dry, spot_override=a.spot)
    out(res)
    sys.exit(0)


if __name__ == "__main__":
    main()
