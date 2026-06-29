#!/bin/bash
# server-snapshot — full server snapshot + one-command rollback.
#
# Unified entry point for "backup before changes + disaster recovery" on a real-money investment system.
# This is a scaled-up version of the ledger-protection principle: a single restore command can overwrite
# the entire server state tree, so ledger protection is enforced by engineering controls (not "be careful"):
# ledger lock via flock, atomic writes, SHA verification, ledger excluded from restore by default, secrets redacted.
#
# Positioning: this is a pure server-side SSH tool.
#   - Never register as an agent skill, never added to any deploy allowlist, never touched by the curator.
#   - restore --include-ledger is only accessible from an interactive SSH terminal (not via bot or automation).
#   - Self-updates require an explicit --install (the deploy channel must not self-modify,
#     same principle as deploy-server.sh's cockpit-deploy):
#       ./server-snapshot.sh --install   # copies this script to ~/.local/bin/server-snapshot on the server
#
# Usage:
#   server-snapshot.sh snapshot                      # full snapshot to ~/backups/<ts>/ + manifest.json
#   server-snapshot.sh restore <ts>                  # restore non-ledger files (ledger untouched by default)
#   server-snapshot.sh restore <ts> --include-ledger # also restore ledger (interactive TTY only; forced diff + typed confirmation)
#   server-snapshot.sh --install                     # local only: push this script to the server
#
# Testability: all paths are rooted at $HOME; tests redirect HOME to a mktemp -d fixture dir for full isolation.
set -eo pipefail
set +x                       # never xtrace — prevents config/.env contents from leaking into logs

# ── Paths (all relative to $HOME so tests can redirect) ─────────────────────────────────
COCKPIT="$HOME/.cockpit"
BSTATE="$COCKPIT/btc-tracker/state.json"
MSTATE="$COCKPIT/mstr-tracker/mstr-state.json"
SKILLS_DIR="$COCKPIT/skills"
CRYPTO_REF="$COCKPIT/skills/crypto-context/references"
CONFIG_YAML="$COCKPIT/config.yaml"
CRON_JOBS="$COCKPIT/cron/jobs.json"
DASH_ENV="$HOME/apps/btc-dashboard/.env"
BACKUPS="$HOME/backups"
LOCK="$COCKPIT/.snapshot.lock"   # same serialization semantics as record_fill ledger writes

REDACT='***REDACTED***'

# ── Utilities ─────────────────────────────────────────────────────────────
log(){ printf '%s\n' "$*"; }
die(){ printf '[ERROR] %s\n' "$*" >&2; exit 1; }

# SHA256: cross-platform (macOS lacks sha256sum, falls back to shasum -a 256). Returns hash only, never file contents.
sha256_of(){
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# Ledger lock: contends on the same lock as record_fill, web server, and cron (fcntl.flock semantics).
# Uses flock(1); falls back to mkdir atomic lock on systems without flock (e.g. macOS) —
# ensures snapshot reads a consistent ledger, not a half-written one.
LOCK_FD=
acquire_lock(){
  if command -v flock >/dev/null 2>&1; then
    exec {LOCK_FD}>"$LOCK"
    flock -x "$LOCK_FD"
  else
    local d="${LOCK}.d" i=0
    until mkdir "$d" 2>/dev/null; do
      i=$((i+1)); [ "$i" -gt 200 ] && die "Timed out acquiring ledger lock ($d held too long)"
      sleep 0.05
    done
    LOCK_FD="mkdir:$d"
  fi
}
release_lock(){
  case "$LOCK_FD" in
    mkdir:*) rmdir "${LOCK_FD#mkdir:}" 2>/dev/null || true ;;
    "") : ;;
    *) eval "exec $LOCK_FD>&-" 2>/dev/null || true ;;
  esac
  LOCK_FD=
}

# Read invested field from ledger (grep-based, no full JSON parse; matches the approach in deploy-server.sh for zero extra dependencies).
binv(){ [ -f "$BSTATE" ] && grep -m1 invested_rmb "$BSTATE" | grep -oE '[0-9][0-9.]*' | head -1 || true; }
minv(){ [ -f "$MSTATE" ] && grep -m1 invested_usd "$MSTATE" | grep -oE '[0-9][0-9.]*' | head -1 || true; }

# Redaction: before writing config.yaml / .env to a snapshot, replace secret/key/token values
# with placeholders. Non-secret structure and path keys (RECORD_BTC/RECORD_MSTR etc.) are
# preserved (dropping them would silently break ledger writes). Rule: if the key name matches
# a sensitive keyword → redact the value; otherwise keep as-is. Never echo the original secret value.
redact_into(){  # redact_into <src> <dst>
  local src="$1" dst="$2"
  # Two-layer defense: key-name pattern list covers known sensitive keys; value-layer entropy check
  # catches future credential keys not yet in the name list.
  # Key names (case-insensitive): key/secret/token/password/passwd/api_key/apikey/auth/credential etc.
  # Covers both YAML (key: value) and .env (KEY=value) formats.
  awk -v R="$REDACT" '
    # Key name matches sensitive pattern → redact value. Extended to cover authorization/webhook/pat/bearer.
    function is_secret(k,   lk){ lk=tolower(k);
      return (lk ~ /(^|[^a-z])(secret|token|password|passwd|apikey|api_key|auth|authorization|credential|access_key|private_key|webhook|bearer)([^a-z]|$)/ \
              || lk ~ /(^|[^a-z])pat([^a-z]|$)/ \
              || lk ~ /key$/ || lk ~ /_key([^a-z]|$)/) }
    # Value-layer entropy check: if the value matches sk-/xoxb-/ghp_/Bearer/Telegram token (\d+:AA...) → redact the entire value.
    function val_has_secret(v){
      return (v ~ /(^|[^A-Za-z0-9])sk-[A-Za-z0-9]/ \
              || v ~ /(^|[^A-Za-z0-9])xoxb-[A-Za-z0-9]/ \
              || v ~ /(^|[^A-Za-z0-9])ghp_[A-Za-z0-9]/ \
              || v ~ /(^|[^A-Za-z])[Bb]earer[[:space:]]+[A-Za-z0-9._-]/ \
              || v ~ /(^|[^0-9])[0-9]+:AA[A-Za-z0-9_-]/) }
    {
      line=$0
      # .env: KEY=VALUE
      if (line ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/) {
        eq=index(line,"="); k=substr(line,1,eq-1); gsub(/[[:space:]]/,"",k)
        if (substr(line,eq+1) !~ /^[[:space:]]*$/ && (is_secret(k) || val_has_secret(substr(line,eq+1)))) { print substr(line,1,eq) R; next }
        print line; next
      }
      # YAML: key: value (indented keys supported)
      if (line ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_-]*[[:space:]]*:[[:space:]]*[^[:space:]]/) {
        co=index(line,":"); k=substr(line,1,co-1); gsub(/[[:space:]]/,"",k)
        if (is_secret(k) || val_has_secret(substr(line,co+1))) { print substr(line,1,co) " " R; next }
        print line; next
      }
      print line
    }' "$src" > "$dst"
}

# ── Snapshot manifest (source path · archive-relative name) ──────────────────────────────────────
# Each line: "<absolute-source-path>|<archive-relative-path>|<redact:plain/redact>|<category:ledger/other>"
manifest_plan(){
  printf '%s|%s|%s|%s\n' "$BSTATE"      "ledger/state.json"            plain  ledger
  printf '%s|%s|%s|%s\n' "$MSTATE"      "ledger/mstr-state.json"       plain  ledger
  printf '%s|%s|%s|%s\n' "$CONFIG_YAML" "config.yaml"                  redact other
  printf '%s|%s|%s|%s\n' "$CRON_JOBS"   "cron/jobs.json"               plain  other
  printf '%s|%s|%s|%s\n' "$DASH_ENV"    "btc-dashboard/.env"           redact other
  # Directories: skills/ and crypto-context references (expanded file by file)
  if [ -d "$SKILLS_DIR" ]; then
    find "$SKILLS_DIR" -type f 2>/dev/null | while read -r f; do
      printf '%s|%s|%s|%s\n' "$f" "skills/${f#$SKILLS_DIR/}" plain other
    done
  fi
  if [ -d "$CRYPTO_REF" ]; then
    find "$CRYPTO_REF" -type f 2>/dev/null | while read -r f; do
      printf '%s|%s|%s|%s\n' "$f" "crypto-context-references/${f#$CRYPTO_REF/}" plain other
    done
  fi
}

# Materialize a snapshot plan into <destdir>/, writing manifest.json (path+sha256+redacted+category).
# Holds the ledger lock for consistency; caller is responsible for acquiring/releasing the lock.
do_snapshot_into(){  # do_snapshot_into <destdir>
  local dest="$1"
  install -d -m 700 "$dest"                       # force 700 on directory; do not rely on umask
  local mani="$dest/manifest.json" first=1
  printf '{\n  "created_at": "%s",\n  "files": [\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" > "$mani"
  chmod 600 "$mani"
  manifest_plan | while IFS='|' read -r src rel red cat; do
    [ -f "$src" ] || continue                      # skip missing files (do not error)
    local target="$dest/$rel"
    install -d -m 700 "$(dirname "$target")"
    if [ "$red" = redact ]; then redact_into "$src" "$target"; else cp "$src" "$target"; fi
    chmod 600 "$target"                            # force 600 on all snapshot files
    local h; h="$(sha256_of "$target")"            # SHA is computed over the on-disk (post-redaction) content
    [ "$first" = 1 ] && first=0 || printf ',\n' >> "$mani"
    printf '    {"path": "%s", "sha256": "%s", "redacted": %s, "category": "%s"}' \
      "$rel" "$h" "$([ "$red" = redact ] && echo true || echo false)" "$cat" >> "$mani"
  done
  printf '\n  ]\n}\n' >> "$mani"
  log "   manifest: $mani"
}

# ── .gitignore: exclude backups dir (prevents snapshots from being synced or committed) ──────
ensure_gitignore_excludes_backups(){
  # Only runs when a .gitignore exists (local dev repo); no-op on the server where there is no git repo.
  local gi
  for gi in "./.gitignore"; do
    [ -f "$gi" ] || continue
    grep -qE '^/?backups/?$' "$gi" 2>/dev/null && return 0
    { printf '\n# Server snapshot directory (server-snapshot) — contains plaintext ledger/config, never commit\n';
      printf '/backups/\n'; } >> "$gi"
    return 0
  done
  return 0
}

# ── Subcommand: snapshot ─────────────────────────────────────────────────
cmd_snapshot(){
  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  local dest="$BACKUPS/$ts"
  install -d -m 700 "$BACKUPS"
  [ -e "$dest" ] && die "Snapshot directory already exists: $dest"
  log "→ Full snapshot → $dest (holding ledger lock for consistency)"
  acquire_lock
  do_snapshot_into "$dest"
  release_lock
  ensure_gitignore_excludes_backups
  log "Snapshot complete: $ts"
  printf '%s\n' "$dest"   # last line echoes the path so callers/tests can parse it
}

# ── Internal restore: atomically restore a category of files from a snapshot (.tmp → mv) + SHA verify ──────
# Returns non-zero if any SHA verification fails; caller should exit 1 and leave original files in place.
restore_files(){  # restore_files <srcdir> <category-filter:other|all>
  local srcdir="$1" filter="$2"
  local mani="$srcdir/manifest.json"
  [ -f "$mani" ] || die "Snapshot missing manifest.json: $mani"
  # Parse path/sha256/category from manifest (lightweight grep/sed, zero extra dependencies).
  local rc=0
  python3 - "$mani" "$srcdir" "$filter" <<'PY' || rc=$?
import json, sys, os, hashlib, shutil
mani, srcdir, filt = sys.argv[1], sys.argv[2], sys.argv[3]
m = json.load(open(mani, encoding="utf-8"))
HOME = os.environ["HOME"]
# Map archive-relative path → actual server destination (mirrors manifest_plan).
def target_for(rel):
    if rel == "ledger/state.json":       return f"{HOME}/.cockpit/btc-tracker/state.json"
    if rel == "ledger/mstr-state.json":  return f"{HOME}/.cockpit/mstr-tracker/mstr-state.json"
    if rel == "config.yaml":             return f"{HOME}/.cockpit/config.yaml"
    if rel == "cron/jobs.json":          return f"{HOME}/.cockpit/cron/jobs.json"
    if rel == "btc-dashboard/.env":      return f"{HOME}/apps/btc-dashboard/.env"
    if rel.startswith("skills/"):        return f"{HOME}/.cockpit/skills/" + rel[len("skills/"):]
    if rel.startswith("crypto-context-references/"):
        return f"{HOME}/.cockpit/skills/crypto-context/references/" + rel[len("crypto-context-references/"):]
    return None
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""): h.update(b)
    return h.hexdigest()
fail = 0
for e in m["files"]:
    if filt == "other" and e["category"] == "ledger":
        continue
    rel = e["path"]; src = os.path.join(srcdir, rel); tgt = target_for(rel)
    if tgt is None:
        print(f"[ERROR] Unknown archive path, refusing to restore: {rel}", file=sys.stderr); fail = 1; continue
    # Path traversal guard: manifest paths under skills/ or crypto-context-references/ are built
    # via string concatenation, so a tampered manifest could escape using ../. Resolve tgt to its
    # realpath and assert it stays within the expected root prefix; reject otherwise.
    if rel.startswith("skills/") or rel.startswith("crypto-context-references/"):
        root = f"{HOME}/.cockpit/skills/crypto-context/references" if rel.startswith("crypto-context-references/") else f"{HOME}/.cockpit/skills"
        rp = os.path.realpath(tgt); rroot = os.path.realpath(root)
        if rp != rroot and not rp.startswith(rroot + os.sep):
            print(f"[ERROR] Path traversal detected, refusing to restore: {rel} (resolved {rp} is outside root {rroot})", file=sys.stderr); fail = 1; continue
    if not os.path.isfile(src):
        print(f"[ERROR] File missing from snapshot: {rel}", file=sys.stderr); fail = 1; continue
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    tmp = tgt + ".restore.tmp"
    shutil.copy(src, tmp)                       # write to .tmp first
    got = sha(tmp)
    if got != e["sha256"]:                       # SHA mismatch → discard .tmp, leave original file intact
        os.remove(tmp)
        print(f"[ERROR] SHA256 mismatch, refusing to replace {rel}: manifest={e['sha256'][:12]} got={got[:12]}", file=sys.stderr)
        fail = 1; continue
    os.replace(tmp, tgt)                         # atomic rename (replace in place)
    try: os.chmod(tgt, 0o600 if e.get("redacted") or "ledger" in rel or rel=="btc-dashboard/.env" else 0o644)
    except OSError: pass
print("__RESTORE_FAIL__" if fail else "__RESTORE_OK__")
sys.exit(1 if fail else 0)
PY
  return $rc
}

# ── Subcommand: restore ──────────────────────────────────────────────────
cmd_restore(){
  local ts="$1"; shift || true
  local include_ledger=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --include-ledger) include_ledger=1 ;;
      *) die "Unknown argument: $1" ;;
    esac
    shift
  done
  [ -n "$ts" ] || die "Usage: restore <ts> [--include-ledger]"
  local srcdir="$BACKUPS/$ts"
  [ -d "$srcdir" ] || die "Snapshot not found: $srcdir"

  # Before restoring anything, unconditionally snapshot the current state (ledger + non-ledger) to pre-restore-<ts>/.
  local prets; prets="$(date +%Y%m%d-%H%M%S)"
  local predir="$BACKUPS/pre-restore-$prets"
  log "→ Pre-restore snapshot of current state → $predir"
  install -d -m 700 "$BACKUPS"
  acquire_lock
  do_snapshot_into "$predir"
  local b_before m_before; b_before="$(binv)"; m_before="$(minv)"
  release_lock
  log "   Current ledger baseline: BTC invested_rmb=$b_before · MSTR invested_usd=$m_before"

  if [ "$include_ledger" = 1 ]; then
    restore_with_ledger "$ts" "$srcdir" "$b_before" "$m_before"
    return $?
  fi

  # ── Default: restore non-ledger files only ──
  log "→ Restoring non-ledger files (ledger is untouched by default; use --include-ledger to override)"
  acquire_lock
  if ! restore_files "$srcdir" other; then
    release_lock
    die "SHA verification failed — aborted, no original files were replaced"
  fi
  # Test hook: only used by test_server_snapshot.sh to simulate a concurrent ledger write during restore.
  # Has zero effect in production (variable is never set).
  [ -n "${HS_TEST_RACE:-}" ] && [ -f "$HS_TEST_RACE" ] && \
    printf '{"invested_rmb": 1, "held_btc": 0, "tiers": []}' > "$HS_TEST_RACE"
  # Post-restore check: verify the ledger was not changed by the non-ledger restore.
  local b_after m_after; b_after="$(binv)"; m_after="$(minv)"
  release_lock
  if [ "$b_before" != "$b_after" ] || [ "$m_before" != "$m_after" ]; then
    die "Non-ledger restore unexpectedly changed the ledger! BTC $b_before→$b_after MSTR $m_before→$m_after"
  fi
  log "   Ledger unchanged: BTC=$b_after MSTR=$m_after"
  log "Restore complete (non-ledger): $ts — pre-restore snapshot at $predir"
}

# ── --include-ledger: interactive TTY only + mandatory diff + typed confirmation ──
restore_with_ledger(){
  local ts="$1" srcdir="$2" b_before="$3" m_before="$4"
  # Read the target snapshot's invested amounts (for display only; no auth fields echoed).
  local tgt_b tgt_m
  tgt_b="$(grep -m1 invested_rmb "$srcdir/ledger/state.json" 2>/dev/null | grep -oE '[0-9][0-9.]*' | head -1 || true)"
  tgt_m="$(grep -m1 invested_usd "$srcdir/ledger/mstr-state.json" 2>/dev/null | grep -oE '[0-9][0-9.]*' | head -1 || true)"
  # Print the destructive impact unconditionally before any TTY check, so the operator sees what would be lost.
  log "════════ [CRITICAL] Ledger rollback — last resort operation ════════"
  log "  BTC  invested_rmb: current $b_before  →  target($ts) $tgt_b"
  log "  MSTR invested_usd: current $m_before  →  target($ts) $tgt_m"
  log "  Fills that will be lost (present in current ledger but absent in snapshot, listed by timestamp):"
  print_lost_fills "$BSTATE"  "$srcdir/ledger/state.json"      || true
  print_lost_fills "$MSTATE"  "$srcdir/ledger/mstr-state.json" || true
  log "  For routine ledger corrections, use record_fill --undo. This command is for disaster recovery only."
  # Only allow execution from an interactive TTY (block bot/automation/non-interactive pipes).
  # Test hook: HS_TEST_TTY=1 lets test_server_snapshot.sh bypass the TTY check in a pipe
  # to exercise the dual-confirmation logic. Has zero effect in production.
  if [ -z "${HS_TEST_TTY:-}" ] && [ ! -t 0 ]; then
    die "--include-ledger requires an interactive TTY (SSH terminal). Not allowed via bot, automation, or non-interactive pipe. For routine ledger fixes, use record_fill --undo."
  fi
  printf '  Type target snapshot timestamp "%s" to confirm full ledger overwrite (any other input cancels): ' "$ts"
  local ans; IFS= read -r ans || ans=""
  [ "$ans" = "$ts" ] || die "Confirmation failed (entered \"$ans\" ≠ \"$ts\") — cancelled, ledger untouched"
  # Second confirmation: require typing the current invested_rmb to force the operator to
  # consciously acknowledge how much capital would be overwritten (the timestamp alone is
  # too easy to muscle-memory past from the command line).
  printf '  Type current BTC invested_rmb "%s" to confirm (ensures you have seen the amount at risk): ' "$b_before"
  local ans2; IFS= read -r ans2 || ans2=""
  [ "$ans2" = "$b_before" ] || die "Second confirmation failed (entered \"$ans2\" ≠ invested_rmb \"$b_before\") — cancelled, ledger untouched"

  acquire_lock
  if ! restore_files "$srcdir" all; then
    release_lock
    die "SHA verification failed — aborted, no original files were replaced"
  fi
  # Final post-restore integrity check: recompute SHA256 of both on-disk ledger files and
  # compare against the manifest. Since os.replace is not transactional, a mid-flight kill
  # or disk-full could leave a partially-restored state; this catches that.
  if ! verify_ledgers_against_manifest "$srcdir"; then
    release_lock
    die "Post-ledger-restore integrity check failed — ledger may be in a partially-restored state; roll back immediately using the pre-restore snapshot shown above"
  fi
  release_lock
  log "Restore complete (including ledger): $ts — pre-restore snapshot of prior state is at the path shown above"
}

# Recompute SHA256 of both on-disk ledger files and compare against the manifest's ledger/* entries.
# Returns non-zero if any mismatch is found.
verify_ledgers_against_manifest(){  # verify_ledgers_against_manifest <srcdir>
  local srcdir="$1"
  python3 - "$srcdir/manifest.json" <<'PY'
import json, sys, os, hashlib
m = json.load(open(sys.argv[1], encoding="utf-8"))
HOME = os.environ["HOME"]
TARGET = {
    "ledger/state.json":      f"{HOME}/.cockpit/btc-tracker/state.json",
    "ledger/mstr-state.json": f"{HOME}/.cockpit/mstr-tracker/mstr-state.json",
}
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""): h.update(b)
    return h.hexdigest()
fail = 0
for e in m["files"]:
    if e["category"] != "ledger":
        continue
    tgt = TARGET.get(e["path"])
    if tgt is None or not os.path.isfile(tgt):
        print(f"[ERROR] Post-restore check: ledger file missing: {e['path']}", file=sys.stderr); fail = 1; continue
    got = sha(tgt)
    if got != e["sha256"]:
        print(f"[ERROR] Post-restore check: ledger SHA mismatch for {e['path']}: manifest={e['sha256'][:12]} got={got[:12]}", file=sys.stderr); fail = 1
sys.exit(1 if fail else 0)
PY
}

# List fills present in the current ledger but absent in the target snapshot (compared by fills[].at).
# Only prints amounts, prices, and timestamps — no auth fields.
print_lost_fills(){  # print_lost_fills <current_state> <target_state>
  local cur="$1" tgt="$2"
  [ -f "$cur" ] || return 0
  python3 - "$cur" "$tgt" <<'PY' || true
import json, sys
def ats(p):
    try: s = json.load(open(p, encoding="utf-8"))
    except Exception: return set(), {}
    seen, info = set(), {}
    for t in s.get("tiers", []):
        for f in t.get("fills", []):
            a = f.get("at");
            if a: seen.add(a); info[a] = (f.get("price"), f.get("rmb") or f.get("usd"))
    for f in s.get("dca_fills", []):
        a = f.get("at")
        if a: seen.add(a); info[a] = ("DCA", f.get("rmb"))
    return seen, info
cur, info = ats(sys.argv[1])
tgt, _ = ats(sys.argv[2])
lost = sorted(cur - tgt)
if not lost:
    print(f"    (none: target snapshot already contains all current fills)"); sys.exit(0)
for a in lost:
    pr, amt = info.get(a, ("?", "?"))
    print(f"    - {a}  price={pr} amount={amt}")
PY
}

# ── --install (local only): copy this script to ~/.local/bin/server-snapshot on the server ──
# The deploy channel must not self-modify: the server-side script never auto-updates;
# run this command explicitly from local to push changes.
cmd_install(){
  local self; self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  local H="${SERVER_SSH:-your-server}"  # override via SERVER_SSH env var; default assumes 'your-server' is defined in ~/.ssh/config
  log "→ Installing/updating server-side ~/.local/bin/server-snapshot (host=$H)"
  scp "$self" "$H:.local/bin/server-snapshot"
  ssh "$H" 'chmod 755 ~/.local/bin/server-snapshot'
  log "Done"
}

# ── Entry point ─────────────────────────────────────────────────────────────
main(){
  case "${1:-}" in
    snapshot)  shift; cmd_snapshot "$@" ;;
    restore)   shift; cmd_restore "$@" ;;
    --install) cmd_install ;;
    ""|-h|--help)
      grep -E '^#( |!)' "$0" | sed 's/^# \{0,1\}//' | head -28 ;;
    *) die "Unknown subcommand: $1 (use snapshot | restore | --install)" ;;
  esac
}
main "$@"
