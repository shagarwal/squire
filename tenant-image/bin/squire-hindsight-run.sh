#!/usr/bin/env bash
#
# squire-hindsight-run.sh — supervisord's exec target for the memory daemon.
#
# A one-line wrapper with a specific job: re-read the LLM credentials from
# squire-hindsight-env.sh's output at every (re)start.
#
# supervisord hands each program the environment IT was started with, frozen at
# supervisord's own launch. So `supervisorctl restart hindsight` would otherwise
# hand the daemon the same stale credentials it already had — which is useless
# precisely when it matters, i.e. after the user connects their own LLM and the
# trial key is revoked. Sourcing the generated file here is what turns a restart
# into an actual re-configuration.
#
# Deliberately NOT `exec`ing through a shell that keeps the secrets in its own
# environment longer than needed: we source, export, exec, and the wrapper shell
# is replaced by the daemon.

set -uo pipefail

TMPFS_DIR="${SQUIRE_SECRETS_TMPFS:-/dev/shm/squire}"
LLM_ENV="${SQUIRE_HINDSIGHT_ENV_FILE:-$TMPFS_DIR/hindsight-llm.env}"
HINDSIGHT_BIN="${SQUIRE_HINDSIGHT_BIN:-/opt/squire/hindsight-venv/bin/hindsight-api}"

if [ -f "$LLM_ENV" ]; then
    # `set -a` so every assignment in the file is exported to the daemon.
    set -a
    # shellcheck disable=SC1090
    . "$LLM_ENV"
    set +a
    printf '[squire-hindsight-run] loaded LLM config (provider=%s model=%s)\n' \
        "${HINDSIGHT_API_LLM_PROVIDER:-<none>}" "${HINDSIGHT_API_LLM_MODEL:-<none>}"
else
    printf '[squire-hindsight-run] no LLM config file at %s — starting without extraction credentials\n' \
        "$LLM_ENV"
fi

exec "$HINDSIGHT_BIN"
