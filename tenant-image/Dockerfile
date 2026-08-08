# syntax=docker/dockerfile:1
#
# Squire tenant image ("hermes-tenant") — Task 0.2, Phase 0.
# =========================================================
#
# ONE container per tenant. It bundles, under supervisord:
#
#   1. hermes-gateway      — the agent itself (upstream hermes-agent, pinned)
#   2. hindsight-api       — long-term memory daemon (127.0.0.1:9177)
#   3. pg0 (PostgreSQL 18) — embedded Postgres, started *by* hindsight-api
#   4. squire-webhook      — tiny shim that owns $PORT and forwards
#                            POST /webhook/telegram to the gateway's Telegram
#                            adapter (see bin/squire-webhook-shim.py)
#   5. squire-secrets-sync — re-encrypts credential files from tmpfs back onto
#                            the volume whenever the agent rewrites them
#   6. squire-hindsight-healthcheck — the wedge/OOM auto-recovery loop from
#                            docs/reference/hindsight-optimization-guide.md §4
#
# WHY WE BUILD *FROM* THE PUBLISHED UPSTREAM IMAGE
# ------------------------------------------------
# Upstream ships a production image (docker.io/nousresearch/hermes-agent:<tag>)
# built from the same Dockerfile we would otherwise copy. Building hermes from
# source takes 15-45 min (Playwright, npm workspaces, a from-source SQLite, a
# full `uv sync`), which would make the CI image build the slowest thing in the
# repo. Because upstream installs itself EDITABLE (`uv pip install -e .` over
# the source tree at /opt/hermes), our patch overlay can still be applied to
# real .py files at /opt/hermes and takes effect immediately — same result as a
# source build, minutes instead of an hour.
#
# PINNING / BUMPING
# -----------------
# HERMES_VERSION is the upstream *release tag*. v2026.8.3 is the current
# release as of 2026-08-07 (the same tag docs/reference/hermes-update-plan-
# v2026.8.3.md rehearsed and executed against, so the patch overlay and marker
# greps below are known-good on it).
#
# To bump: change HERMES_VERSION, clear HERMES_DIGEST (or replace it with the
# new amd64 digest from `docker buildx imagetools inspect`), rerun the patch
# overlay locally, and re-verify the markers. Never bump without re-running the
# canary drill in the update plan (§7 verification gates).
#
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent
ARG HERMES_VERSION=v2026.8.3

FROM ${HERMES_IMAGE}:${HERMES_VERSION}

# Re-declare after FROM so the values are readable inside build stages.
ARG HERMES_VERSION
# amd64 manifest digest for nousresearch/hermes-agent:v2026.8.3, recorded on
# 2026-08-07 for provenance. Not used as the FROM ref (that would break the
# multi-arch manifest); it is stamped into the image so `docker inspect` can
# prove which upstream build a tenant is running.
ARG HERMES_DIGEST=sha256:c0cab4e3711bcb27a312be1b3776254fc06fd50d5f7a6b8017915fc7171cb39e

# hindsight-all bundles hindsight-api-slim[all] (the memory server + local
# embeddings) plus pg0-embedded (the bundled PostgreSQL 18 that hindsight-api
# starts for itself). Pinned; bump deliberately.
ARG HINDSIGHT_VERSION=0.9.0

# Everything below runs as root (build time). Runtime drops to the `hermes`
# user (uid 10000) via upstream's main-wrapper.sh — see the CMD note at the
# bottom. This matters: pg0's initdb refuses to run as root, and so does
# hindsight's own embedded-mode guard.
USER root

LABEL org.opencontainers.image.title="squire-hermes-tenant" \
      org.opencontainers.image.description="Squire per-tenant agent runtime: hermes-agent + Hindsight + embedded Postgres" \
      ai.squire.hermes.version="${HERMES_VERSION}" \
      ai.squire.hermes.digest="${HERMES_DIGEST}"

# ---------------------------------------------------------------------------
# 1. Patch overlay
# ---------------------------------------------------------------------------
# The three-tier fork discipline from docs/reference/git-strategy.md, moved into
# the image build: upstream is pinned and untouched; our deltas live as ordered
# .patch files; a marker grep proves both our patches AND the upstream fixes we
# depend on are present in the built tree.
#
# apply-patches.sh is a no-op when patches/ contains no .patch files, so the
# image builds cleanly before the founder's 001/002/003 patches are copied in
# from ~/.hermes/patches/. verify-markers.sh, however, ALWAYS runs and always
# checks the upstream markers, so a silent upstream regression (e.g. the
# _to_oauth_wire_name billing fix disappearing) fails the build rather than
# reaching a tenant.
COPY --chmod=0755 patches/ /opt/squire/patches/
RUN /opt/squire/patches/apply-patches.sh /opt/hermes \
 && /opt/squire/patches/verify-markers.sh /opt/hermes

# ---------------------------------------------------------------------------
# 2. Squire control venv (small): supervisord + cryptography
# ---------------------------------------------------------------------------
# Deliberately NOT installed into /opt/hermes/.venv. That venv is upstream's
# and is treated as sealed (the image sets HERMES_DISABLE_LAZY_INSTALLS=1 for
# exactly this reason); adding our own resolver pressure there is how you end
# up debugging a transitive pin at 2am on a tenant.
#
#   cryptography — AES-256-GCM for the DEK-encrypted secret files
#   supervisor   — process supervision inside the container
RUN uv venv --python 3.13 /opt/squire/venv \
 && uv pip install --python /opt/squire/venv/bin/python --no-cache \
        "supervisor==4.3.0" "cryptography>=45,<50"

# ---------------------------------------------------------------------------
# 3. Hindsight venv (large): memory daemon + embedded Postgres
# ---------------------------------------------------------------------------
# Also isolated from /opt/hermes/.venv — hindsight-api-slim pins sqlalchemy,
# litellm, anthropic, openai and tornado, all of which hermes also depends on.
# Two venvs, zero shared resolution, no cross-breakage. The gateway talks to
# the daemon over HTTP (local_external mode), never by import, so isolation
# costs us nothing.
#
# torch is installed FIRST from PyTorch's CPU-only index. The default PyPI
# torch wheel drags in the whole nvidia-cu* stack (multiple GB) that a tenant
# container can never use. Installing the CPU build first means the
# hindsight-all resolve below sees torch as already satisfied.
RUN uv venv --python 3.13 /opt/squire/hindsight-venv \
 && uv pip install --python /opt/squire/hindsight-venv/bin/python --no-cache \
        --index-url https://download.pytorch.org/whl/cpu "torch>=2.6.0" \
 && uv pip install --python /opt/squire/hindsight-venv/bin/python --no-cache \
        "hindsight-all==${HINDSIGHT_VERSION}" \
 && /opt/squire/hindsight-venv/bin/hindsight-api --help > /dev/null

# ---------------------------------------------------------------------------
# 4. Squire runtime files
# ---------------------------------------------------------------------------
COPY --chmod=0755 bin/ /opt/squire/bin/
COPY supervisord.conf /opt/squire/supervisord.conf
# The baked ~/.hermes template. Seeded onto the (empty) volume on first boot
# only — see bin/squire-entrypoint.sh. Never overwrites tenant-owned files.
COPY home-template/ /opt/squire/home-template/

# Byte-compile our Python so a syntax error fails the BUILD, not a tenant boot.
RUN /opt/squire/venv/bin/python -m py_compile \
        /opt/squire/bin/squire_secrets.py \
        /opt/squire/bin/squire-webhook-shim.py \
        /opt/squire/bin/squire-hindsight-healthcheck.py \
 && chmod -R a+rX /opt/squire \
 && mkdir -p /opt/squire/run \
 && chown hermes:hermes /opt/squire/run

# ---------------------------------------------------------------------------
# 5. Runtime environment
# ---------------------------------------------------------------------------

# --- Container contract (control-api + ingress depend on these) -------------
# PORT                  HTTP port the container listens on (webhook shim).
# /webhook/telegram     Path that receives raw Telegram update JSON.
# /opt/data             The single mounted volume. It is also HERMES_HOME, so
#                       upstream's own stage2 bootstrap (uid remap, dir seed,
#                       targeted chown) manages it unchanged.
ENV PORT=8080 \
    SQUIRE_WEBHOOK_PATH=/webhook/telegram \
    SQUIRE_VOLUME=/opt/data \
    SQUIRE_STATE_DIR=/opt/data/.squire \
    SQUIRE_HOME_TEMPLATE=/opt/squire/home-template

# --- Secrets ---------------------------------------------------------------
# Plaintext credentials live ONLY here, and this is a tmpfs. /dev/shm is a
# tmpfs in every OCI runtime with no extra capabilities required (mounting our
# own tmpfs would need CAP_SYS_ADMIN, which Railway does not grant). The
# encrypted-at-rest copies live under SQUIRE_STATE_DIR on the volume.
ENV SQUIRE_SECRETS_TMPFS=/dev/shm/squire \
    SQUIRE_SECRET_FILES=".env auth.json"

# --- Telegram adapter, webhook mode ----------------------------------------
# The adapter (plugins/platforms/telegram/adapter.py) switches to webhook mode
# purely on TELEGRAM_WEBHOOK_URL being set, and derives its local listen path
# from that URL's path component. control-api supplies the real public URL at
# runtime; if it is absent the adapter falls back to long polling, which keeps
# `docker run` local testing working with no ingress at all.
#
# It binds 127.0.0.1 on purpose — the shim is the only thing that should own a
# public port, so the adapter's HTTP server is unreachable except through it.
ENV TELEGRAM_WEBHOOK_PORT=8443 \
    TELEGRAM_WEBHOOK_HOST=127.0.0.1

# --- Hindsight (docs/reference/hindsight-optimization-guide.md) -------------
# The guide's systemd-unit knobs, translated to this container. Cgroup limits
# from the guide are NOT set here: on Railway the whole service is one cgroup,
# so per-process MemoryMax has no equivalent. The healthcheck loop (guide §4)
# does carry over and is a supervisord program.
#
# DATABASE_URL: `pg0://<instance>:<port>` makes hindsight-api start its bundled
# PostgreSQL 18 on a FIXED port instead of auto-allocating one, which is what
# lets the healthcheck reach the DB to clear wedged tasks. pg0 keeps its data
# in $HOME/.pg0/instances/hindsight/data — and $HOME is /opt/data — so the
# memory database lives on the volume and survives redeploys.
ENV HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT=9177 \
    HINDSIGHT_API_LOG_LEVEL=info \
    HINDSIGHT_API_DATABASE_URL=pg0://hindsight:5432 \
    HINDSIGHT_API_RERANKER_PROVIDER=rrf \
    HINDSIGHT_API_WORKER_MAX_SLOTS=3 \
    HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS=1 \
    HINDSIGHT_API_DB_POOL_MIN_SIZE=3 \
    HINDSIGHT_API_DB_POOL_MAX_SIZE=15

# --- Gateway supervision opt-out -------------------------------------------
# CRITICAL, and non-obvious: upstream's `hermes gateway run` detects that it is
# inside an s6-overlay container and *redirects itself* into s6 supervision
# (hermes_cli/gateway.py::_maybe_redirect_run_to_s6_supervision), leaving the
# invoking process as a heartbeat stub. Under supervisord that would mean
# supervisord watching a stub while the real gateway runs somewhere it cannot
# see or restart. HERMES_GATEWAY_NO_SUPERVISE=1 is the documented opt-out; the
# supervisord program ALSO passes --no-supervise as a belt-and-braces guard.
ENV HERMES_GATEWAY_NO_SUPERVISE=1

EXPOSE 8080

# Health endpoint served by the shim. Kept cheap — it must answer while the
# gateway is still booting so a platform health probe doesn't kill a cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD /opt/squire/venv/bin/python -c \
        "import os,urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=4).read()" \
        || exit 1

# ENTRYPOINT stays upstream's (/opt/hermes/docker/entrypoint-dispatch.sh). That
# gives us, for free: s6 as PID 1 (zombie reaping for MCP stdio children), the
# stage2 volume bootstrap (uid remap + targeted chown of the mounted volume),
# and the privilege drop to the `hermes` user with the hermes venv activated.
# Upstream's main-wrapper.sh passes an absolute executable straight through, so
# our entrypoint becomes the container's main program and its exit code is the
# container's exit code.
CMD ["/opt/squire/bin/squire-entrypoint.sh"]
