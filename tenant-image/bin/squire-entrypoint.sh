#!/usr/bin/env bash
#
# squire-entrypoint.sh — the tenant container's main program.
#
# Runs as the unprivileged `hermes` user (uid 10000), after upstream's s6
# stage2 hook has already bootstrapped and chowned the mounted volume. Its job,
# in order:
#
#   1. refuse to start without a usable DEK
#   2. put the plaintext credential files on tmpfs (/dev/shm), never the volume
#   3. first boot: seed ~/.hermes from the baked template and seal the secrets
#      later boots: unseal from the volume, and top up any newly-baked files
#   4. hand the process off to supervisord
#
# Everything here must be idempotent — a Railway redeploy re-runs it against a
# volume that already has state.

set -euo pipefail

log() { printf '[squire-entrypoint] %s\n' "$*"; }
die() { printf '[squire-entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# --- Resolved configuration -------------------------------------------------
HERMES_HOME="${HERMES_HOME:-/opt/data}"
STATE_DIR="${SQUIRE_STATE_DIR:-$HERMES_HOME/.squire}"
SECRETS_ENC_DIR="$STATE_DIR/secrets"
TMPFS_DIR="${SQUIRE_SECRETS_TMPFS:-/dev/shm/squire}"
TEMPLATE_DIR="${SQUIRE_HOME_TEMPLATE:-/opt/squire/home-template}"
SECRETS="${SQUIRE_SEALED_FILE_NAMES:-.env auth.json}"
INIT_MARKER="$STATE_DIR/initialized"

# The control venv's interpreter and our bin dir, in one place. Overridable so
# this boot path can be exercised outside a container (the CI job does exactly
# that) — everything else about the script is identical either way.
SQUIRE_PYTHON="${SQUIRE_PYTHON:-/opt/squire/venv/bin/python}"
SQUIRE_BIN="${SQUIRE_BIN:-/opt/squire/bin}"
# An ARRAY, not a string. An unquoted string expansion re-splits on IFS and
# would break the moment an install path contained a space — the classic
# shell footgun that only shows up on someone else's machine.
SECRETS_TOOL=("$SQUIRE_PYTHON" "$SQUIRE_BIN/squire_secrets.py")

# The final exec target, same override rationale. Making this substitutable is
# what lets the boot test distinguish "booted successfully" (exit 0) from
# "refused to boot" (non-zero) — without it every test boot ends in a failed
# exec and the exit status carries no signal at all, which is precisely how the
# fatality assertions in tests/test_boot_path.sh managed to pass against a
# non-fatal entrypoint.
SQUIRE_SUPERVISORD="${SQUIRE_SUPERVISORD:-/opt/squire/venv/bin/supervisord}"
SQUIRE_SUPERVISORD_CONF="${SQUIRE_SUPERVISORD_CONF:-/opt/squire/supervisord.conf}"

VOLUME_PATH="${SQUIRE_VOLUME:-/opt/data}"

# --- Preflight: are the helpers we shell out to actually runnable? ----------
# Cheap, and it converts a whole class of silent breakage into one clear line.
# The scripts below are invoked as executables, not via `bash <file>`, so a
# checkout that lost the exec bit (git records mode; a Windows/WSL working tree
# reports 777 for everything and hides the difference) turns every call into
# Permission denied. Downstream that looked like "sealing just didn't happen".
for _helper in squire-secrets-sync.sh squire-hindsight-env.sh; do
    if [ ! -x "$SQUIRE_BIN/$_helper" ]; then
        die "$SQUIRE_BIN/$_helper is not executable.
     The image sets 0755 via COPY --chmod, so this is a source checkout whose
     exec bit was never recorded in git. Fix with:
       git update-index --chmod=+x tenant-image/bin/$_helper"
    fi
done
unset _helper

log "tenant=${TENANT_ID:-<unset>} hermes_home=$HERMES_HOME home=${HOME:-<unset>} port=${PORT:-8080}"

# ---------------------------------------------------------------------------
# 0. Durability gate — is the Postgres cluster actually on the volume?
# ---------------------------------------------------------------------------
# The most expensive silent failure in this container: pg0 puts the embedded
# PostgreSQL data directory under $HOME/.pg0/instances/<name>/data. If $HOME is
# not the mounted volume, Hindsight works perfectly, the tenant's memory builds
# up normally — and the next redeploy throws all of it away with no error. The
# user just finds that their assistant has forgotten them.
#
# So assert it, loudly, before anything starts. This is cheap insurance against
# an upstream image bump quietly changing HOME.
PG0_DATA_ROOT="${HOME:-}/.pg0"

if [ -z "${HOME:-}" ]; then
    die "HOME is unset — pg0 would put the memory database somewhere unpredictable.
     The Dockerfile sets HOME=$VOLUME_PATH; something has unset it."
fi

# realpath both sides so a symlinked mount still compares equal.
home_real="$(realpath -m "$HOME")"
volume_real="$(realpath -m "$VOLUME_PATH")"
if [ "$home_real" != "$volume_real" ]; then
    die "HOME ($home_real) is not the mounted volume ($volume_real).
     pg0 would write the embedded PostgreSQL cluster to $PG0_DATA_ROOT, which
     is on the container filesystem — every redeploy would destroy the tenant's
     long-term memory. Set HOME=$VOLUME_PATH."
fi

# Second, weaker check: is the volume actually a mount, or just a directory in
# the image? /proc/self/mountinfo is the reliable source (a bind mount of a
# subdirectory still shows up there).
#
# This is only fatal when TENANT_ID is set. control-api always sets it when
# provisioning a real tenant, so its presence is a precise signal for "this is
# production and a missing volume means data loss". A bare `docker run` for
# local testing has no TENANT_ID and is allowed to run volume-less.
if ! grep -q " $volume_real " /proc/self/mountinfo 2>/dev/null; then
    if [ -n "${TENANT_ID:-}" ] && [ "${SQUIRE_ALLOW_EPHEMERAL_VOLUME:-}" != "1" ]; then
        die "$volume_real is not a mount point, but TENANT_ID=$TENANT_ID is set.
     This tenant would run entirely on ephemeral container storage: memory,
     credentials and conversation history would all be lost on redeploy.
     Attach the Railway volume at $volume_real.
     (Set SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 to override — testing only.)"
    fi
    log "WARNING: $volume_real is not a mount point — state is EPHEMERAL (dev mode)"
fi

log "durability ok: pg0 data root will be $PG0_DATA_ROOT"

# ---------------------------------------------------------------------------
# 1. DEK gate
# ---------------------------------------------------------------------------
# Fail fast and loudly. A tenant that boots without its DEK would either run
# with no credentials (looks like amnesia to the user) or, worse, re-initialize
# and overwrite sealed state it cannot read.
if ! "${SECRETS_TOOL[@]}" check; then
    die "SQUIRE_DEK missing or malformed — expected base64 of exactly 32 bytes.
     control-api sets this per tenant; without it the volume cannot be read
     and must not be re-initialized."
fi

# ---------------------------------------------------------------------------
# 2. tmpfs for plaintext
# ---------------------------------------------------------------------------
# /dev/shm is a tmpfs in every OCI runtime and needs no capabilities. Mounting
# our own tmpfs would require CAP_SYS_ADMIN, which Railway does not grant.
# 0700 so nothing else in the container's namespace can read the directory.
mkdir -p "$TMPFS_DIR"
chmod 0700 "$TMPFS_DIR"

mkdir -p "$STATE_DIR" "$SECRETS_ENC_DIR"
chmod 0700 "$STATE_DIR" "$SECRETS_ENC_DIR"

# ---------------------------------------------------------------------------
# 3. Template seeding
# ---------------------------------------------------------------------------
# FIRST BOOT (marker absent): install the whole baked template, overwriting the
# generic defaults upstream's stage2 hook just seeded (its SOUL.md and
# config.yaml are Hermes', not Squire's).
#
# LATER BOOTS: copy only files the tenant does not have. That is the image
# upgrade path — a new baked skill in image vN+1 lands on redeploy, while
# anything the tenant (or the agent, editing its own SOUL.md) has customised is
# left alone. Deliberately conservative: we would rather ship a stale template
# file than silently revert a user's personalisation.
first_boot=false
if [ ! -f "$INIT_MARKER" ]; then
    first_boot=true
fi

seed_template() {
    local overwrite="$1"
    local src rel dst
    # -print0/-d '' so paths with spaces cannot split. The template is ours, but
    # habits that only work on tidy input are how you get a 3am incident.
    while IFS= read -r -d '' src; do
        rel="${src#"$TEMPLATE_DIR"/}"
        dst="$HERMES_HOME/$rel"
        if [ -e "$dst" ] && [ "$overwrite" != "overwrite" ]; then
            continue
        fi
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
        chmod 0644 "$dst"
        log "seeded $rel"
    done < <(find "$TEMPLATE_DIR" -type f -print0)
}

if [ "$first_boot" = true ]; then
    log "first boot — initialising $HERMES_HOME from baked template"
    seed_template overwrite
else
    seed_template keep-existing
fi

# Directories the gateway expects to be able to write into. mkdir -p is cheap
# and makes a hand-restored volume behave like a fresh one.
mkdir -p "$HERMES_HOME/logs" "$HERMES_HOME/sessions" "$HERMES_HOME/skills" \
         "$HERMES_HOME/hindsight" "$HERMES_HOME/cron"

# ---------------------------------------------------------------------------
# 3b. Concierge onboarding state
# ---------------------------------------------------------------------------
# Seed POSITIVE state for the first conversation. The original design left this
# file absent and asked the agent to infer "the file is missing, therefore I
# have not onboarded anyone yet" — an absence is the single hardest signal for a
# model to act on, and on the first live tenant it duly did nothing at all.
#
# With the file present and saying `greet`, squire-concierge-hook.py has
# something concrete to read on every turn, and the agent is *told* which step
# it is on instead of being asked to deduce that it is on one.
#
# FIRST BOOT ONLY, and the -f guard is doubled deliberately:
#   * the first_boot test means an image upgrade never restarts onboarding for
#     an existing tenant, and
#   * the -f test means that even a lost or rolled-back init marker cannot reset
#     a tenant who has already finished — re-onboarding someone who has used the
#     product for a week is a worse bug than never onboarding them at all.
CONCIERGE_STATE="$STATE_DIR/concierge-state.json"
if [ "$first_boot" = true ] && [ ! -f "$CONCIERGE_STATE" ]; then
    # Written with printf rather than through the control venv: onboarding state
    # must not depend on a Python interpreter being importable this early.
    printf '{"state":"greet","name":null,"timezone":null,"llm":"trial","updated":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$CONCIERGE_STATE"
    chmod 0600 "$CONCIERGE_STATE"
    log "seeded concierge onboarding state (greet)"
fi

# ---------------------------------------------------------------------------
# 4. Secrets: volume (sealed) <-> tmpfs (plaintext)
# ---------------------------------------------------------------------------
# Each managed file lives on tmpfs and is symlinked into $HERMES_HOME, so every
# hermes read AND write goes to tmpfs. squire-secrets-sync re-seals changes back
# onto the volume (the agent rewrites .env / auth.json whenever the user
# connects or refreshes a provider).
#
# The symlink indirection is what makes "never plaintext on the volume" a
# structural property rather than a convention someone has to remember.
install_secret() {
    local name="$1"
    local plain="$TMPFS_DIR/$name"
    local enc="$SECRETS_ENC_DIR/$name.enc"
    local link="$HERMES_HOME/$name"

    if [ -f "$enc" ]; then
        # Restart path.
        if ! "${SECRETS_TOOL[@]}" unseal "$enc" "$plain" "$name"; then
            die "could not decrypt $name — refusing to start.
     This means SQUIRE_DEK does not match this volume. Re-check the tenant's
     DEK before doing anything else; re-initialising would destroy the
     tenant's memory and credentials."
        fi
        log "unsealed $name"
    elif [ -f "$link" ] && [ ! -L "$link" ]; then
        # Migration path: a real plaintext file is sitting on the volume (an
        # older image, a manual restore, or upstream's own seeding). Adopt it —
        # seal it, move it to tmpfs, replace it with the symlink — so the very
        # next boot is clean. Note the order: seal first, and only then remove
        # the plaintext.
        log "adopting pre-existing plaintext $name from the volume"
        # If the seal fails we must NOT delete the plaintext — that would
        # destroy the only copy of the tenant's credentials — and we must not
        # continue either, because the next secrets-sync pass would happily
        # re-seal an empty file over it. Stop, loudly.
        if ! "${SECRETS_TOOL[@]}" seal "$link" "$enc" "$name"; then
            die "failed to seal pre-existing $name — refusing to continue.
     The plaintext file has been left untouched at $link. Fix the DEK or the
     volume permissions and restart; do not delete it."
        fi
        cp "$link" "$plain"
        rm -f "$link"
    else
        # Nothing yet — start from an empty file so the symlink always resolves.
        # `hermes` treats a missing .env and an empty .env identically.
        : > "$plain"
    fi

    chmod 0600 "$plain"

    # Replace whatever is at $link with the symlink. -f handles a stale symlink
    # from a previous boot pointing at a now-empty tmpfs.
    rm -f "$link"
    ln -s "$plain" "$link"
}

for name in $SECRETS; do
    install_secret "$name"
done

# --- TELEGRAM_WEBHOOK_SECRET ------------------------------------------------
# This secret is the shared truth between three parties: Telegram (which stamps
# it on every update), the tenant's PTB adapter (which rejects updates without
# it), and ingress/control-api (which stores it). python-telegram-bot REFUSES to
# start in webhook mode without one (upstream cites GHSA-3vpc-7q5r-276h).
#
# WHO OWNS IT: control-api. Both TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET
# are part of the provisioning env contract. That resolves the setWebhook
# ownership question: because control-api and the tenant register the *same* URL
# with the *same* secret, the tenant's PTB setWebhook call and control-api's own
# are idempotent — last writer wins and both writers agree, so there is no fight
# and ingress's stored secret stays valid across tenant restarts.
#
# The generate-and-persist branch below is a STANDALONE-DEV FALLBACK only. It
# exists so `docker run` without control-api still boots; in that mode the tenant
# is the only writer, so self-generating is correct. It must never be the path a
# provisioned tenant takes — if it is, control-api forgot to set the variable and
# ingress will hold a secret the tenant does not know.
ENV_FILE="$TMPFS_DIR/.env"

env_get() {
    # Last assignment wins, matching how hermes' own loader reads the file.
    sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n 1
}

env_put() {
    # Idempotent upsert: drop any existing assignment, then append.
    local key="$1" value="$2" tmp
    tmp="$(mktemp "$TMPFS_DIR/.env.XXXXXX")"
    chmod 0600 "$tmp"
    grep -v "^$key=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
}

if [ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
    # Normal, provisioned path. Mirror the control-api value into .env so the
    # adapter's profile-scoped get_secret() read finds it too, and so a value
    # rotation propagates on redeploy rather than being masked by a stale one.
    if [ "$(env_get TELEGRAM_WEBHOOK_SECRET)" != "$TELEGRAM_WEBHOOK_SECRET" ]; then
        env_put TELEGRAM_WEBHOOK_SECRET "$TELEGRAM_WEBHOOK_SECRET"
        log "TELEGRAM_WEBHOOK_SECRET taken from the environment (control-api)"
    fi
elif [ -n "$(env_get TELEGRAM_WEBHOOK_SECRET)" ]; then
    log "TELEGRAM_WEBHOOK_SECRET reused from sealed .env (dev fallback)"
else
    # Dev fallback. Uses Python's `secrets` module rather than `openssl rand`:
    # openssl is not a declared dependency of the upstream image and a missing
    # binary here would abort boot under `set -e`, whereas the interpreter is
    # already a hard dependency of everything else in this script.
    env_put TELEGRAM_WEBHOOK_SECRET \
        "$("$SQUIRE_PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
    log "TELEGRAM_WEBHOOK_SECRET generated (dev fallback — control-api did not supply one)"
fi
chmod 0600 "$ENV_FILE"

# Export for the webhook shim, which stamps it on forwarded requests and does
# not read hermes' .env.
TELEGRAM_WEBHOOK_SECRET="$(env_get TELEGRAM_WEBHOOK_SECRET)"
export TELEGRAM_WEBHOOK_SECRET

# ---------------------------------------------------------------------------
# 5. Telegram webhook wiring
# ---------------------------------------------------------------------------
# The adapter derives its LOCAL listen path from TELEGRAM_WEBHOOK_URL's path
# component (adapter.py: `webhook_path = urlparse(webhook_url).path`), and
# separately hands the full URL to setWebhook so Telegram pushes updates at our
# ingress. Those are the same string upstream, so the shim reads the path back
# out of the URL rather than assuming.
#
# No TELEGRAM_WEBHOOK_URL -> the adapter stays in long-polling mode. That keeps
# a bare `docker run` (and any local smoke test) working with no ingress at all.
if [ -n "${TELEGRAM_WEBHOOK_URL:-}" ]; then
    SQUIRE_TELEGRAM_UPSTREAM_PATH="$(
        "$SQUIRE_PYTHON" - "$TELEGRAM_WEBHOOK_URL" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).path or "/telegram")
PY
    )"
    export SQUIRE_TELEGRAM_UPSTREAM_PATH
    # Log the PATH ONLY, never the full URL. The ingress URL embeds the bot id,
    # which is half of a Telegram bot token and a direct pointer at this
    # specific tenant — not something to scatter through a shared log
    # aggregator. The path is all anyone debugging routing actually needs.
    log "telegram webhook mode: local=127.0.0.1:${TELEGRAM_WEBHOOK_PORT:-8443}$SQUIRE_TELEGRAM_UPSTREAM_PATH (public URL redacted)"
else
    log "TELEGRAM_WEBHOOK_URL unset — adapter will use long polling (dev mode)"
fi

# ---------------------------------------------------------------------------
# 6. Hindsight LLM wiring
# ---------------------------------------------------------------------------
# Derived by squire-hindsight-env.sh into a file that squire-hindsight-run.sh
# sources at every daemon (re)start. Deliberately NOT exported into our own
# environment: doing that is what made the credentials a boot-time constant and
# left converted tenants extracting against a revoked trial key. See that
# script's header for the full failure mode.
#
# Exit 10 means "changed", which on first boot is always true and needs no
# action here — supervisord has not started the daemon yet.
"$SQUIRE_BIN/squire-hindsight-env.sh" || true

# ---------------------------------------------------------------------------
# 7. Done initialising
# ---------------------------------------------------------------------------
if [ "$first_boot" = true ]; then
    date -u +%Y-%m-%dT%H:%M:%SZ > "$INIT_MARKER"
    chmod 0600 "$INIT_MARKER"
    log "first-boot initialisation complete"
fi

# Re-seal everything now so the volume is consistent even if the container is
# killed before secrets-sync's first pass.
# NOT `|| log warning`. That is what hid the second CI failure: the helper was
# checked out non-executable, every invocation was Permission denied, nothing
# was ever sealed onto the volume, and boot carried on looking healthy. A tenant
# in that state accepts credentials, serves the user, and loses everything on
# the next redeploy — the worst possible failure shape. If the first seal cannot
# be completed, stop.
if ! "$SQUIRE_BIN/squire-secrets-sync.sh" --once; then
    die "initial credential seal FAILED — refusing to start.
     Nothing has been written to $SECRETS_ENC_DIR, so this tenant would run with
     credentials that exist only in tmpfs and vanish on the next redeploy.
     Check that $SQUIRE_BIN/squire-secrets-sync.sh is executable and that
     $STATE_DIR is writable."
fi

log "starting supervisord"
exec "$SQUIRE_SUPERVISORD" -n -c "$SQUIRE_SUPERVISORD_CONF"
