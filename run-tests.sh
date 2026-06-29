#!/bin/bash
# Full test entry point — shared between local dev and CI to prevent divergence.
# Shared modules (strategy.py / mnav.py) have their single source in app/; tests resolve them via PYTHONPATH.
set -eo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT/app"

echo "=== BTC tracker tests ==="
python3 "$ROOT/skills/btc-tracker/scripts/test_record_fill.py"

echo "=== DCA advice tests ==="
python3 "$ROOT/skills/btc-tracker/scripts/test_dca_advice.py"

echo "=== MSTR DCA advice tests (pacing window) ==="
python3 "$ROOT/skills/mstr-tracker/scripts/test_dca_advice_mstr.py"

echo "=== Pacing window strategy tests (BTC) ==="
python3 "$ROOT/app/test_strategy.py"

echo "=== Pacing window strategy tests (MSTR) ==="
python3 "$ROOT/app/test_strategy_mstr.py"

echo "=== Dashboard link auth tests ==="
python3 "$ROOT/app/test_dash_link.py"

echo "=== Dashboard write-route tests ==="
# Route-level tests require Flask (test_client): installed by CI; falls back to .venv when system python3 lacks Flask
if python3 -c "import flask" 2>/dev/null; then
  python3 "$ROOT/app/test_app_record.py"
else
  "$ROOT/.venv/bin/python" "$ROOT/app/test_app_record.py"
fi

echo "=== On-chain signal fetch/cache tests ==="
python3 "$ROOT/app/test_onchain_signals.py"

echo "=== On-chain signal evaluation tests ==="
python3 "$ROOT/app/test_onchain_judge.py"

echo "=== Sub-tier code migration/derivation tests ==="
python3 "$ROOT/app/test_subcode.py"

echo "=== MSTR tracker tests ==="
python3 "$ROOT/skills/mstr-tracker/scripts/test_record_fill_mstr.py"

echo "=== Buy-zone tier dynamic anchoring tests ==="
python3 "$ROOT/app/test_tier_anchors.py"

echo "=== Buy-zone tier display / price-alert tests ==="
python3 "$ROOT/app/test_tier_alert.py"

echo "=== Dashboard main table trigger-price overlay tests ==="
python3 "$ROOT/app/test_app_tier_overlay.py"

echo "=== Dashboard MSTR DCA card + BTC pacing wiring tests ==="
python3 "$ROOT/app/test_app_mstr_weekly.py"

echo "=== mNAV history series tests ==="
python3 "$ROOT/app/test_mnav_series.py"

echo "=== mNAV daily snapshot tests ==="
python3 "$ROOT/app/test_snapshot_mnav.py"

echo "=== Frozen trigger price / drift alert tests ==="
python3 "$ROOT/app/test_frozen_tiers.py"

echo "=== Price-alert cron integration tests ==="
python3 "$ROOT/app/test_tier_alert_cron.py"

echo "=== Frozen-drift cron integration tests ==="
python3 "$ROOT/app/test_frozen_cron.py"

echo "=== Server snapshot / restore tests ==="
bash "$ROOT/deploy/test_server_snapshot.sh"

echo "All tests passed"
