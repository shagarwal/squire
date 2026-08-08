#!/usr/bin/env bash
#
# squire-secrets-sync.sh — keep the volume's sealed copies in step with tmpfs.
#
# Why this exists: the credential files are not write-once. `hermes auth`,
# OAuth token refresh, the concierge's connect-your-LLM flow and the user
# pasting an API key all rewrite $HERMES_HOME/.env or auth.json — which are
# symlinks into tmpfs. Without this loop those writes would live only in RAM
# and vanish on the next redeploy, and the user would be asked to reconnect
# their LLM every time we ship an image.
#
# Design notes:
#   * Poll, don't watch. inotify would be tidier but needs another dependency,
#     and a 15s worst-case window on a credential write is fine — nothing here
#     is on a request path.
#   * Compare content hashes, not mtimes. Re-sealing produces a fresh random
#     nonce every time, so an mtime-triggered loop would rewrite the volume
#     forever; a hash guard makes a no-change pass genuinely free.
#   * Never delete a sealed file because its plaintext went missing. A tmpfs
#     that lost a file is a bug, not an instruction to destroy the only durable
#     copy of the tenant's credentials.
#
# Usage:
#   squire-secrets-sync.sh --once      one pass, then exit (used at boot)
#   squire-secrets-sync.sh             loop forever (supervisord program)

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
STATE_DIR="${SQUIRE_STATE_DIR:-$HERMES_HOME/.squire}"
SECRETS_ENC_DIR="$STATE_DIR/secrets"
DIGEST_DIR="$STATE_DIR/digests"
TMPFS_DIR="${SQUIRE_SECRETS_TMPFS:-/dev/shm/squire}"
SECRETS="${SQUIRE_SECRET_FILES:-.env auth.json}"
INTERVAL="${SQUIRE_SECRETS_SYNC_INTERVAL:-15}"
# Same overridable interpreter/bin pair as squire-entrypoint.sh, so the sync
# loop can be exercised outside a container.
SQUIRE_PYTHON="${SQUIRE_PYTHON:-/opt/squire/venv/bin/python}"
SQUIRE_BIN="${SQUIRE_BIN:-/opt/squire/bin}"
SECRETS_TOOL="$SQUIRE_PYTHON $SQUIRE_BIN/squire_secrets.py"

log() { printf '[squire-secrets-sync] %s\n' "$*"; }

mkdir -p "$SECRETS_ENC_DIR" "$DIGEST_DIR"
chmod 0700 "$SECRETS_ENC_DIR" "$DIGEST_DIR" 2>/dev/null || true

sync_once() {
    local name plain enc digest_file current previous
    for name in $SECRETS; do
        plain="$TMPFS_DIR/$name"
        enc="$SECRETS_ENC_DIR/$name.enc"
        # Flatten any path separators so a nested secret name cannot escape the
        # digest directory.
        digest_file="$DIGEST_DIR/${name//\//_}.sha256"

        [ -f "$plain" ] || continue

        current="$(sha256sum "$plain" 2>/dev/null | cut -d' ' -f1)"
        [ -n "$current" ] || continue

        previous=""
        [ -f "$digest_file" ] && previous="$(cat "$digest_file" 2>/dev/null)"

        # Re-seal when the content changed, or when the sealed copy is missing
        # entirely (first pass after the file was created).
        if [ "$current" = "$previous" ] && [ -f "$enc" ]; then
            continue
        fi

        if $SECRETS_TOOL seal "$plain" "$enc" "$name"; then
            printf '%s' "$current" > "$digest_file"
            chmod 0600 "$digest_file" 2>/dev/null || true
            log "re-sealed $name"
        else
            # Leave the digest untouched so the next pass retries.
            log "ERROR: failed to seal $name — will retry in ${INTERVAL}s"
        fi
    done
}

if [ "${1:-}" = "--once" ]; then
    sync_once
    exit 0
fi

# On SIGTERM (supervisord shutdown / Railway redeploy), do one last pass so a
# credential written seconds before the stop is not lost.
trap 'log "SIGTERM — final sync"; sync_once; exit 0' TERM INT

log "watching ${SECRETS} every ${INTERVAL}s"
while true; do
    sync_once
    # `sleep & wait` rather than a bare `sleep`. bash does not run a trap while
    # a foreground builtin/child is executing — it waits for it to finish — so a
    # bare `sleep 15` would delay the SIGTERM handler by up to a full interval.
    # `wait` IS interruptible: the signal lands immediately, the final re-seal
    # runs at once, and the whole shutdown fits inside supervisord's
    # stopwaitsecs instead of racing it.
    sleep "$INTERVAL" &
    wait $!
done
