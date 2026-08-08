#!/usr/bin/env bash
#
# Backup-path regression test for squire-backup.sh — runs WITHOUT Docker and
# without restic (a recording stub stands in for the real binary).
#
# Two assertions carry all the weight:
#
#   1. NOT CONFIGURED == EXIT 0. No B2 account exists yet. If this script could
#      fail, crash-loop or block on a missing backend, it would take a supervisord
#      program (and eventually operator attention) with it — for a feature that is
#      not switched on.
#
#   2. NO PLAINTEXT PATH IS EVER PASSED TO RESTIC. The whole privacy design rests
#      on plaintext credentials existing only on tmpfs. A backup is durable AND
#      offsite, so a path slip here is the worst possible version of that failure.
#      We assert it from the outside — on the exact argv the stub received — not by
#      reading the script and hoping.
#
# Usage: bash tenant-image/tests/test_backup.sh
# Exit:  0 all assertions pass · 1 otherwise
set -uo pipefail

IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$IMAGE_ROOT/bin"
ROOT="$(mktemp -d)"
VOL="$ROOT/data"
TMPFS="$ROOT/shm"
STUB_DIR="$ROOT/stub"
ARGV_LOG="$ROOT/restic-argv.log"
ENV_LOG="$ROOT/restic-env.log"
mkdir -p "$VOL/.squire/secrets" "$TMPFS" "$STUB_DIR"

DEK="$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")"
SECRET_VALUE="sk-ant-plaintext-must-never-be-backed-up"

# A plaintext credential on tmpfs, symlinked into the volume exactly as the
# entrypoint arranges it. This is the thing that must not leave the container.
printf 'ANTHROPIC_API_KEY=%s\n' "$SECRET_VALUE" > "$TMPFS/.env"
ln -sf "$TMPFS/.env" "$VOL/.env"
printf 'sealed-blob\n' > "$VOL/.squire/secrets/.env.enc"

fails=0
check() { if [ "$2" = "1" ]; then echo "  PASS  $1"; else echo "  FAIL  $1 ${3:-}"; fails=$((fails+1)); fi; }
yn() { if "$@" >/dev/null 2>&1; then echo 1; else echo 0; fi; }
# `grep -e` so a pattern that starts with a dash (--exclude ...) is treated as a
# pattern rather than as grep's own option.
says() { echo "$1" | grep -q -e "$2" && echo 1 || echo 0; }

# --- restic stub: records every invocation's argv and the password it was given -
cat > "$STUB_DIR/restic" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$ARGV_LOG"
printf 'RESTIC_PASSWORD=%s\n' "\${RESTIC_PASSWORD:-}" >> "$ENV_LOG"
case "\$1" in
  "cat")  exit 1 ;;   # "repository does not exist yet" -> forces the init path
  *)      exit 0 ;;
esac
STUB
chmod +x "$STUB_DIR/restic"

# run_backup <extra env assignments...>
run_backup() {
  env -i PATH="$PATH" \
      HERMES_HOME="$VOL" \
      SQUIRE_VOLUME="$VOL" \
      SQUIRE_STATE_DIR="$VOL/.squire" \
      SQUIRE_SECRETS_TMPFS="$TMPFS" \
      SQUIRE_PYTHON="$(command -v python3)" \
      SQUIRE_BIN="$BIN" \
      SQUIRE_RESTIC="$STUB_DIR/restic" \
      SQUIRE_DEK="$DEK" \
      TENANT_ID=t-backup-test \
      "$@" \
      bash "$BIN/squire-backup.sh" --once 2>&1
}

echo "== 1. no B2 configuration: exit 0, do nothing =="
out="$(run_backup)"; rc=$?
check "unconfigured run exits 0"      "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
check "says backups are not configured" "$(says "$out" 'backups not configured')"
check "restic was never invoked"      "$([ ! -f "$ARGV_LOG" ] && echo 1 || echo 0)" \
    "$(cat "$ARGV_LOG" 2>/dev/null)"

echo "== 2. repository set but no credentials: still exit 0 =="
# Half-configured is the state an operator lands in mid-setup. It must be inert,
# not a crash loop against a backend it cannot authenticate to.
out="$(run_backup RESTIC_REPOSITORY=s3:s3.us-west-004.backblazeb2.com/squire/t-1)"; rc=$?
check "half-configured run exits 0"   "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
check "half-configured says not configured" "$(says "$out" 'backups not configured')"
check "restic still never invoked"    "$([ ! -f "$ARGV_LOG" ] && echo 1 || echo 0)"

echo "== 3. fully configured: initialises, backs up, prunes =="
REPO="s3:s3.us-west-004.backblazeb2.com/squire-backups/t-backup-test"
out="$(run_backup \
    RESTIC_REPOSITORY="$REPO" \
    AWS_ACCESS_KEY_ID=b2-key-id \
    AWS_SECRET_ACCESS_KEY=b2-app-key)"; rc=$?
check "configured run exits 0"        "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
argv="$(cat "$ARGV_LOG" 2>/dev/null)"
check "probed the repository"         "$(says "$argv" '^cat config')"
check "initialised a missing repository" "$(says "$argv" '^init')"
check "ran a backup"                  "$(says "$argv" '^backup ')"
check "applied retention"             "$(says "$argv" '^forget')"
check "retention keeps dailies"       "$(says "$argv" 'keep-daily 7')"
check "retention prunes"              "$(says "$argv" 'prune')"
check "backed up the volume"          "$(says "$argv" "backup $VOL")"

echo "  -- the assertion this file exists for --"
# Every argument restic ever received, checked against the plaintext locations.
check "tmpfs path never passed as a backup source" \
    "$([ "$(echo "$argv" | grep -v -- '--exclude' | grep -c "$TMPFS")" = 0 ] && echo 1 || echo 0)" \
    "$argv"
check "tmpfs is explicitly excluded" "$(says "$argv" "--exclude $TMPFS")"
check "managed secret files are excluded" "$(says "$argv" "--exclude $VOL/.env")"
check "plaintext secret VALUE never reached restic" \
    "$([ "$(says "$argv" "$SECRET_VALUE")" = 0 ] && echo 1 || echo 0)"
check "DEK never passed as an argument" \
    "$([ "$(says "$argv" "$DEK")" = 0 ] && echo 1 || echo 0)"
check "DEK never printed to the log" \
    "$([ "$(says "$out" "$DEK")" = 0 ] && echo 1 || echo 0)"

echo "  -- repository password is derived from the DEK, not the DEK --"
password="$(sed -n 's/^RESTIC_PASSWORD=//p' "$ENV_LOG" | head -n 1)"
expected="$(SQUIRE_DEK="$DEK" python3 "$BIN/squire_secrets.py" derive restic-repo)"
check "password handed to restic via the environment" \
    "$([ -n "$password" ] && echo 1 || echo 0)"
check "password is HMAC-SHA256(DEK, 'restic-repo')" \
    "$([ "$password" = "$expected" ] && echo 1 || echo 0)"
check "password is not the DEK itself" \
    "$([ "$password" != "$DEK" ] && echo 1 || echo 0)"
check "password is 64 hex chars" \
    "$([ "${#password}" = 64 ] && echo 1 || echo 0)" "len=${#password}"

echo "== 4. a volume path that resolves into tmpfs is fatal =="
# Defence in depth: if a future edit ever points the backup source at the
# plaintext directory, the script must refuse rather than upload it.
rm -f "$ARGV_LOG"
out="$(run_backup \
    RESTIC_REPOSITORY="$REPO" \
    AWS_ACCESS_KEY_ID=k \
    AWS_SECRET_ACCESS_KEY=s \
    SQUIRE_VOLUME="$TMPFS")"; rc=$?
check "refuses to back up the tmpfs"  "$([ "$rc" -ne 0 ] && echo 1 || echo 0)" "rc=$rc"
check "says why it refused"           "$(says "$out" 'plaintext')"
check "restic not invoked on the refusal path" \
    "$([ ! -f "$ARGV_LOG" ] && echo 1 || echo 0)" "$(cat "$ARGV_LOG" 2>/dev/null)"

echo "== 5. --check reports configuration without backing up =="
rm -f "$ARGV_LOG"
out="$(env -i PATH="$PATH" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_PYTHON="$(command -v python3)" \
    SQUIRE_BIN="$BIN" SQUIRE_RESTIC="$STUB_DIR/restic" SQUIRE_DEK="$DEK" \
    RESTIC_REPOSITORY="$REPO" AWS_ACCESS_KEY_ID=k AWS_SECRET_ACCESS_KEY=s \
    bash "$BIN/squire-backup.sh" --check 2>&1)"; rc=$?
check "--check exits 0"               "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
check "--check does not run restic"   "$([ ! -f "$ARGV_LOG" ] && echo 1 || echo 0)"

echo
rm -rf "$ROOT"
if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL BACKUP TESTS PASS"
