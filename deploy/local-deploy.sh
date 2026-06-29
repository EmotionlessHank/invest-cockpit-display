#!/bin/bash
# Local deploy — sole deployment channel for the invest cockpit.
# Called by the deploy-cockpit skill, or run directly. All server-side logic lives in
# deploy-server.sh; this script is the packager + test gate.
#
# Usage:
#   ./deploy/local-deploy.sh               # full pipeline: test gate → sync server script → package and deploy (default)
#   ./deploy/local-deploy.sh --skip-tests  # skip test gate (docs-only / emergency hotfix; use with caution)
#   ./deploy/local-deploy.sh --install     # sync server-side cockpit-deploy script only, no code deploy
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H=your-server  # SSH alias defined in ~/.ssh/config — replace with your own server alias

install_server_script() {
  echo "→ Syncing server-side deploy script to ~/.local/bin/cockpit-deploy"
  scp "$ROOT/deploy/deploy-server.sh" "$H:.local/bin/cockpit-deploy"
  ssh "$H" 'chmod 755 ~/.local/bin/cockpit-deploy'
}

if [ "$1" = "--install" ]; then
  install_server_script
  echo "Done"
  exit 0
fi

# [1] Test gate — the only automated quality gate after CI retirement; runs by default (--skip-tests to bypass)
if [ "$1" = "--skip-tests" ]; then
  echo "[WARN] Skipping test gate (--skip-tests) — docs-only / emergency hotfix only"
else
  echo "→ [1/3] Test gate (run-tests.sh — red means deploy aborted)"
  "$ROOT/run-tests.sh"
fi

# [2] Auto-sync server-side script — any change to deploy-server.sh (e.g. allowlist updates) takes effect on every deploy
echo "→ [2/3] Syncing server-side deploy script (allowlist changes take effect automatically)"
install_server_script

# [3] Package the code allowlist (app/ + skills/, no state files) and stream to server via ssh stdin
echo "→ [3/3] Packaging code allowlist and streaming to server"
tar czf - -C "$ROOT" app skills | ssh "$H" '~/.local/bin/cockpit-deploy'
