#!/bin/bash
# Server-side deployment script — sole entry point via SSH forced command (shared by CI and local emergency runs).
#
# Invocation: tar czf - app skills | ssh <deploy-key> your-server deploy
#   (authorized_keys restrict,command= forces this script; tar archive is streamed via stdin)
#
# CRITICAL: Only extract files on the code allowlist — never touch ledger/state files.
# Three-layer protection:
#   ① snapshot both ledger files before deploy
#   ② extract list = code allowlist (physically excludes any state file)
#   ③ verify ledger unchanged after deploy; exit 1 if changed
#
# ⚠️ Server install path: ~/.local/bin/cockpit-deploy (not on the allowlist itself; update via
#    deploy/local-deploy.sh --install — the deploy channel must not self-modify, ensuring auditability).
set -eo pipefail
export PATH="$HOME/.local/bin:$PATH"

APP="$HOME/apps/btc-dashboard"
BSKILL="$HOME/.cockpit/skills/btc-tracker"
MSKILL="$HOME/.cockpit/skills/mstr-tracker"
BSTATE="$HOME/.cockpit/btc-tracker/state.json"
MSTATE="$HOME/.cockpit/mstr-tracker/mstr-state.json"
DOM="your-domain.example.com"  # Replace with your server's domain if needed
TS="$(date +%Y%m%d-%H%M%S)"

binv(){ grep -m1 invested_rmb "$BSTATE" | grep -oE '[0-9][0-9.]*' | head -1; }
minv(){ grep -m1 invested_usd "$MSTATE" | grep -oE '[0-9][0-9.]*' | head -1; }

echo "→ [1/7] Snapshot ledger files and record baseline"
mkdir -p "$HOME/.cockpit/btc-tracker/backups" "$HOME/.cockpit/mstr-tracker/backups"
cp "$BSTATE" "$HOME/.cockpit/btc-tracker/backups/state-$TS.json"
cp "$MSTATE" "$HOME/.cockpit/mstr-tracker/backups/mstr-state-$TS.json"
BBEFORE="$(binv)"; MBEFORE="$(minv)"
echo "   BTC invested_rmb=$BBEFORE · MSTR invested_usd=$MBEFORE (snapshot: $TS)"

echo "→ [2/7] Receive tar archive from stdin into staging area"
STAGE="$(mktemp -d "$HOME/.cache/cockpit-deploy.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
tar xzf - -C "$STAGE"
for f in app/app.py app/dashboard.html app/strategy.py app/strategy_mstr.py app/mnav.py app/dash_link.py \
         app/onchain_signals.py app/tier_alert.py app/tier_alert_cron.py app/apple-touch-icon.png app/favicon.svg \
         app/subcode.py app/frozen_tiers.py app/frozen_cron.py \
         app/mstr_holdings_timeline.json app/snapshot_mnav.py \
         skills/btc-tracker/SKILL.md skills/mstr-tracker/SKILL.md; do
  [ -f "$STAGE/$f" ] || { echo "[ERROR] Missing required file in archive: $f — aborting deploy"; exit 1; }
done

echo "→ [3/7] Extract allowlisted files (no ledger/state files included)"
# Dashboard layer: onchain_signals.py is the on-chain signal cache module (app reads, cron writes);
# subcode.py is the sub-tier code / state-machine single source of truth;
# frozen_tiers.py manages frozen trigger price config and drift detection.
cp "$STAGE"/app/app.py "$STAGE"/app/dashboard.html "$STAGE"/app/strategy.py "$STAGE"/app/strategy_mstr.py "$STAGE"/app/mnav.py "$STAGE"/app/dash_link.py "$STAGE"/app/onchain_signals.py "$STAGE"/app/tier_alert.py "$STAGE"/app/subcode.py "$STAGE"/app/frozen_tiers.py "$STAGE"/app/apple-touch-icon.png "$STAGE"/app/favicon.svg "$APP/"
# mNAV historical chart: stepped holdings timeline + daily snapshot utility
cp "$STAGE"/app/mstr_holdings_timeline.json "$STAGE"/app/snapshot_mnav.py "$APP/"
# BTC skill — shared modules (strategy.py, dash_link.py, onchain_signals.py, subcode.py,
# frozen_tiers.py/frozen_cron.py, mnav.py) are distributed here alongside skill-specific scripts.
# onchain_signals.py also goes into scripts/ for the daily cron --fetch run (caches to onchain_cache.json).
# frozen_cron.py handles drift detection; mnav.py provides MSTR mNAV batch order prices.
cp "$STAGE"/skills/btc-tracker/SKILL.md "$BSKILL/"
cp "$STAGE"/skills/btc-tracker/scripts/*.py "$STAGE"/app/strategy.py "$STAGE"/app/dash_link.py "$STAGE"/app/onchain_signals.py "$STAGE"/app/tier_alert.py "$STAGE"/app/tier_alert_cron.py "$STAGE"/app/subcode.py "$STAGE"/app/frozen_tiers.py "$STAGE"/app/frozen_cron.py "$STAGE"/app/mnav.py "$BSKILL/scripts/"
cp "$STAGE"/skills/btc-tracker/references/*.md "$BSKILL/references/"
# MSTR skill — shared modules (strategy.py, strategy_mstr.py, mnav.py, dash_link.py, subcode.py)
# are distributed here. dca_advice_mstr.py depends on both strategy (remaining_weeks) and
# strategy_mstr (pacing window); missing either causes ModuleNotFoundError.
cp "$STAGE"/skills/mstr-tracker/SKILL.md "$MSKILL/"
cp "$STAGE"/skills/mstr-tracker/scripts/*.py "$STAGE"/app/strategy.py "$STAGE"/app/strategy_mstr.py "$STAGE"/app/mnav.py "$STAGE"/app/dash_link.py "$STAGE"/app/subcode.py "$MSKILL/scripts/"

echo "→ [4/7] Restart service (dashboard link is signed at runtime by dash_link; no hardcoded DASH_URL injected on deploy)"
systemctl --user restart btc-dashboard
# AGENT_CLI: replace with your agent platform's CLI binary name (the one that owns the gateway/curator).
"$HOME/.local/bin/${AGENT_CLI:-agent}" gateway restart >/dev/null 2>&1 || echo "[WARN] gateway restart failed — manual follow-up required"

echo "→ [5/7] Verify ledger files were not modified by deploy"
BAFTER="$(binv)"; MAFTER="$(minv)"
fail=0
[ "$BBEFORE" = "$BAFTER" ] && echo "   [OK] BTC ledger intact, invested_rmb=$BAFTER" || { echo "[ERROR] BTC ledger changed: $BBEFORE → $BAFTER! Backup at backups/state-$TS.json"; fail=1; }
[ "$MBEFORE" = "$MAFTER" ] && echo "   [OK] MSTR ledger intact, invested_usd=$MAFTER" || { echo "[ERROR] MSTR ledger changed: $MBEFORE → $MAFTER! Backup at backups/mstr-state-$TS.json"; fail=1; }

echo "→ [6/7] Smoke test: healthz"
sleep 2
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8848/healthz || true)"
[ "$code" = "200" ] && echo "   [OK] Service online, healthz=200" || { echo "[ERROR] healthz=$code — service unhealthy"; fail=1; }

echo "→ [7/7] Pin skills (externally managed — curator must never auto-reorganize these)"
for sk in btc-tracker mstr-tracker; do
  "$HOME/.local/bin/${AGENT_CLI:-agent}" curator pin "$sk" >/dev/null 2>&1 && echo "   [OK] pinned $sk" || echo "   [WARN] pin $sk skipped (skill may not exist)"
done

[ "$fail" = 0 ] && echo "[OK] Deploy complete" || exit 1
