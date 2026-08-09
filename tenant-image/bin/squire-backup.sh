#!/usr/bin/env bash
#
# squire-backup.sh — nightly restic backup of the tenant volume to B2 (Task 0.6).
#
# WHAT IS BACKED UP, AND WHY THAT IS SAFE
# ---------------------------------------
# The volume, and only the volume. Everything credential-bearing on the volume is
# already DEK-sealed (squire_secrets.py): $STATE_DIR/secrets/*.enc are AES-256-GCM
# blobs, and the plaintext copies live on tmpfs, OUTSIDE the volume, reachable only
# through symlinks that restic archives as symlinks rather than following.
#
# So the backup inherits the volume's property rather than needing its own: a B2
# bucket full of these snapshots is unreadable without the tenant's DEK, and
# crypto-shredding the DEK shreds every snapshot at the same instant.
#
# The guard below makes that structural instead of assumed — it refuses to run if
# any backup path resolves inside the tmpfs, and the excludes name the managed
# secret files explicitly. tests/test_backup.sh asserts both.
#
# THE REPOSITORY PASSWORD IS DERIVED FROM THE DEK
# -----------------------------------------------
# RESTIC_PASSWORD = HMAC-SHA256(DEK, "restic-repo"), computed in-container by
# `squire_secrets.py derive`. Whoever holds the B2 credentials holds ciphertext and
# nothing else: they cannot open the repository, and even if they somehow did, the
# files inside are the same sealed blobs. It is passed through the environment,
# never as an argument, so it cannot be read out of `ps`.
#
# POSTGRES CONSISTENCY — READ BEFORE TRUSTING A RESTORE
# -----------------------------------------------------
# pg0's PostgreSQL cluster ($HOME/.pg0/instances/hindsight/data) is copied while it
# is RUNNING. That is a crash-consistent copy, not a hot backup: PostgreSQL will
# normally recover from it exactly as it recovers from a power cut, but that is a
# "normally", not a guarantee, and a torn checkpoint can require manual recovery.
# We accept that for alpha because the alternative — stopping Hindsight nightly —
# means visible downtime for every tenant every night to protect memory that is
# reconstructible from the conversation itself.
#
# Phase 1 fix, in preference order: `pg_dump` into the snapshot before the run
# (needs pg0's bundled client binaries located and pinned), or restic's
# `--stdin-from-command`. Do not claim a verified restore until one of those lands
# AND a restore has actually been performed.
#
# SCHEDULING — DEVIATION FROM THE PLAN, STATED PLAINLY
# -----------------------------------------------------
# docs/implementation-plan.md Task 0.6 says "runs in-container via Railway cron".
# It cannot: Railway's cron schedules a SERVICE (it starts a container to run a
# command), and there is no mechanism to exec a command inside an already-running
# service. A cron-scheduled second service would be a second container that does
# not have this tenant's volume mounted or its DEK, which is precisely the data it
# needs. So the schedule lives here instead: a supervisord program running a loop
# that sleeps until the next nightly window, plus jitter so a thousand tenants do
# not all hit B2 at 03:00:00. Same nightly cadence, different mechanism.
#
# NOT CONFIGURED == EXIT 0
# ------------------------
# No B2 account exists yet. With RESTIC_REPOSITORY (or the credentials) unset this
# logs one line and exits 0. It must never be able to break boot or crash-loop a
# supervisord program over a backend that has not been bought yet.
#
# Usage:
#   squire-backup.sh --once     run one backup now, then exit
#   squire-backup.sh            loop: sleep until the nightly window, back up, repeat
#   squire-backup.sh --check    print whether backups are configured, then exit

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
VOLUME="${SQUIRE_VOLUME:-$HERMES_HOME}"
STATE_DIR="${SQUIRE_STATE_DIR:-$HERMES_HOME/.squire}"
TMPFS_DIR="${SQUIRE_SECRETS_TMPFS:-/dev/shm/squire}"
SECRETS="${SQUIRE_SECRET_FILES:-.env auth.json}"

# Same overridable interpreter/bin pair as the other scripts, so this can be
# exercised outside a container (tests/test_backup.sh does exactly that).
SQUIRE_PYTHON="${SQUIRE_PYTHON:-/opt/squire/venv/bin/python}"
SQUIRE_BIN="${SQUIRE_BIN:-/opt/squire/bin}"
SECRETS_TOOL=("$SQUIRE_PYTHON" "$SQUIRE_BIN/squire_secrets.py")
RESTIC="${SQUIRE_RESTIC:-restic}"

# Label bound into the derived repository password. Changing it makes every
# existing repository unreadable — treat it as part of the on-disk format.
DERIVE_LABEL="${SQUIRE_BACKUP_DERIVE_LABEL:-restic-repo}"

# Nightly window (UTC) and the jitter added to it. 03:00 is quiet everywhere that
# matters; the jitter is what stops the fleet stampeding B2 on the same second.
BACKUP_HOUR="${SQUIRE_BACKUP_HOUR_UTC:-3}"
JITTER_SECONDS="${SQUIRE_BACKUP_JITTER_SECONDS:-3600}"

# Retention. Small numbers on purpose: this is disaster recovery for a $5/month
# tenant, not an archive, and every kept snapshot is B2 storage we pay for.
KEEP_DAILY="${SQUIRE_BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${SQUIRE_BACKUP_KEEP_WEEKLY:-4}"

# Timestamp of the last SUCCESSFUL run, read by squire-heartbeat.py and reported
# to control-api as an age. On the VOLUME, not tmpfs: a tmpfs copy would reset on
# every redeploy and the fleet view would claim "never backed up" for tenants that
# are backing up fine. It is a unix timestamp — nothing secret-derived — so it is
# one of the few things that may sit on the volume in the clear.
SUCCESS_FILE="${SQUIRE_BACKUP_SUCCESS_FILE:-$STATE_DIR/last-backup-success}"

log() { printf '[squire-backup] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Configuration gate
# ---------------------------------------------------------------------------
# Two independent things must be present: a repository, and credentials for the
# backend it names. restic's B2 backend reads B2_ACCOUNT_ID/B2_ACCOUNT_KEY; its
# S3-compatible backend (B2's S3 API, which is what most tooling uses) reads
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. Accept either pair.
backups_configured() {
    [ -n "${RESTIC_REPOSITORY:-}" ] || return 1
    if [ -n "${B2_ACCOUNT_ID:-}" ] && [ -n "${B2_ACCOUNT_KEY:-}" ]; then
        return 0
    fi
    if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
        return 0
    fi
    return 1
}

if ! backups_configured; then
    log "backups not configured (RESTIC_REPOSITORY and B2_*/AWS_* credentials unset) — nothing to do"
    exit 0
fi

if [ "${1:-}" = "--check" ]; then
    log "backups configured: repository=${RESTIC_REPOSITORY%%:*}:... paths=$VOLUME"
    exit 0
fi

# ---------------------------------------------------------------------------
# Safety guard: never hand restic a path that lives on tmpfs
# ---------------------------------------------------------------------------
# The single property this whole design rests on is that plaintext credentials
# exist only on tmpfs and are never copied anywhere durable. A backup is durable
# and offsite, so a path slip here would be the worst version of that failure:
# plaintext credentials in someone else's storage. Assert it rather than trust it.
assert_not_tmpfs() {
    local path="$1" real tmpfs_real
    real="$(realpath -m "$path")"
    tmpfs_real="$(realpath -m "$TMPFS_DIR")"
    case "$real" in
        "$tmpfs_real" | "$tmpfs_real"/*)
            log "FATAL: refusing to back up $path — it resolves into the plaintext"
            log "       secrets tmpfs ($tmpfs_real). Only sealed files may leave this container."
            exit 1
            ;;
        /dev/shm | /dev/shm/*)
            log "FATAL: refusing to back up $path — /dev/shm holds plaintext secrets"
            exit 1
            ;;
    esac
}

assert_not_tmpfs "$VOLUME"

# ---------------------------------------------------------------------------
# Repository password, derived from the DEK
# ---------------------------------------------------------------------------
# Exported, never passed as an argument (arguments are world-readable via /proc).
if ! RESTIC_PASSWORD="$("${SECRETS_TOOL[@]}" derive "$DERIVE_LABEL")"; then
    log "FATAL: could not derive the repository password — is SQUIRE_DEK set?"
    exit 1
fi
export RESTIC_PASSWORD

# --- Excludes ---------------------------------------------------------------
# restic does not follow symlinks (it archives the link itself), so the managed
# secret files would be captured as dangling links pointing into tmpfs — harmless,
# but excluding them by name says the intent out loud and survives someone later
# replacing a symlink with a real file.
#
# The rest is churn with no restore value: logs are shipped to Railway anyway,
# sockets and pid files are meaningless after a restore, and the tmpfs digests
# regenerate on boot.
EXCLUDES=(
    --exclude "$TMPFS_DIR"
    --exclude "$HERMES_HOME/logs"
    --exclude "*.sock"
    --exclude "*.pid"
    --exclude "postmaster.pid"
)
# `read -ra` into an array rather than `for name in $SECRETS`: the split is
# deliberate (SQUIRE_SECRET_FILES is a space-separated list), but an unquoted
# expansion also glob-expands, so a file named `.env` next to a stray `*` in the
# variable would quietly turn into a different exclude list.
read -ra SECRET_NAMES <<< "$SECRETS"
for name in "${SECRET_NAMES[@]}"; do
    EXCLUDES+=(--exclude "$HERMES_HOME/$name")
done

run_backup() {
    local started rc probe_err init_err
    started="$(date -u +%s)"

    # `cat config` is restic's cheapest "does this repository exist" probe. Its
    # STDERR is kept, because two very different situations both fail it and the
    # right response is opposite in each:
    #
    #   * repository does not exist yet          -> init it (first run, normal)
    #   * repository exists, password is wrong   -> DO NOT init. This means the
    #     DEK does not match the one that created the repository, i.e. the tenant
    #     was re-provisioned with a new DEK, or SQUIRE_DEK is wrong. Initialising
    #     would fail anyway, but the *log* would say "init failed" and send an
    #     operator hunting a B2 permissions problem that does not exist.
    if ! probe_err="$("$RESTIC" cat config 2>&1 >/dev/null)"; then
        if printf '%s' "$probe_err" | grep -qiE 'wrong password|invalid password|decrypt|no key found'; then
            log "FATAL: the repository exists but our derived password does not open it."
            log "       SQUIRE_DEK does not match the DEK that created this repository."
            log "       Refusing to re-initialise — that would orphan every existing snapshot."
            return 1
        fi
        log "initialising repository"
        if ! init_err="$("$RESTIC" init 2>&1)"; then
            log "ERROR: repository init failed — skipping this run: ${init_err:0:300}"
            return 1
        fi
    fi

    # A container SIGKILLed mid-prune (Railway redeploys are not gentle) leaves an
    # exclusive lock behind, and every subsequent night would then fail with
    # "repository is already locked" until someone noticed. Best effort, and only
    # once the repository is known to exist — running it before the probe would
    # just print "repository does not exist" on every first run.
    #
    # NOTE: this is safe only because exactly one process backs up this repository
    # (one repository per tenant, one backup program per container). Do not copy
    # this into anything with concurrent writers.
    "$RESTIC" unlock >/dev/null 2>&1 || true

    log "backing up $VOLUME"
    "$RESTIC" backup "$VOLUME" \
        --tag squire \
        --host "${TENANT_ID:-squire-tenant}" \
        --exclude-caches \
        "${EXCLUDES[@]}"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        # Non-fatal: the loop must survive a bad night (B2 outage, transient
        # network) and try again tomorrow rather than dying and needing a human.
        log "ERROR: backup failed (rc=$rc)"
        return "$rc"
    fi

    # Record success BEFORE retention. The snapshot is already durable at this
    # point; a prune failure is an operational annoyance, not a lost backup, and
    # letting it suppress the timestamp would make the fleet view report a backup
    # gap that does not exist.
    if ! printf '%s\n' "$(date -u +%s)" > "$SUCCESS_FILE" 2>/dev/null; then
        log "warning: could not write $SUCCESS_FILE — the fleet view will under-report backups"
    fi

    # Retention runs after a SUCCESSFUL backup only. Forgetting snapshots on a
    # night when the new one failed is how you end up with no snapshots at all.
    #
    # stderr is captured rather than discarded: a prune that fails every night
    # (stale lock, B2 permissions, a repository slowly filling with data we are
    # paying to keep) is exactly the kind of thing that stays invisible when its
    # only output goes to /dev/null.
    local forget_err
    if ! forget_err="$("$RESTIC" forget \
            --tag squire \
            --keep-daily "$KEEP_DAILY" \
            --keep-weekly "$KEEP_WEEKLY" \
            --prune 2>&1 >/dev/null)"; then
        log "warning: retention/prune failed (the new snapshot is safe): ${forget_err:0:300}"
    fi

    log "backup complete in $(( $(date -u +%s) - started ))s"
    return 0
}

# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--once" ]; then
    run_backup
    exit $?
fi

# Loop mode (the supervisord program). See the scheduling note in the header for
# why this is not a Railway cron job.
seconds_until_window() {
    local now target jitter
    now="$(date -u +%s)"
    target="$(date -u -d "today ${BACKUP_HOUR}:00" +%s 2>/dev/null)" || target=""
    if [ -z "$target" ]; then
        # No GNU date (should not happen in this image). Fall back to a plain
        # 24h cadence rather than refusing to back up at all.
        echo $(( 86400 ))
        return
    fi
    [ "$target" -le "$now" ] && target=$(( target + 86400 ))
    # $RANDOM is 0..32767, so the effective jitter ceiling is 32767s (~9h) no
    # matter how large SQUIRE_BACKUP_JITTER_SECONDS is set. Fine at the 3600s
    # default; if a much wider spread is ever wanted, this needs two $RANDOMs
    # combined rather than a bigger modulus that silently does nothing.
    jitter=0
    [ "$JITTER_SECONDS" -gt 0 ] && jitter=$(( RANDOM % JITTER_SECONDS ))
    echo $(( target - now + jitter ))
}

trap 'log "SIGTERM — stopping"; exit 0' TERM INT

log "nightly backups armed (window ${BACKUP_HOUR}:00 UTC +up to ${JITTER_SECONDS}s jitter)"
while true; do
    delay="$(seconds_until_window)"
    log "next backup in ${delay}s"
    # `sleep & wait` rather than a bare `sleep`: bash does not run traps while a
    # foreground child runs, and a bare sleep here would defer SIGTERM for up to a
    # day, blowing through supervisord's stopwaitsecs on every redeploy.
    sleep "$delay" &
    wait $!
    run_backup
done
